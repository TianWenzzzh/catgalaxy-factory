"""喵星图工厂 · FastAPI 入口。"""
from __future__ import annotations

import csv
import io
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import (census_parser, image_proc, injector, locking, merge, packager, store,
               summary_writer)
from .config import (IMAGE_EXTS, MAX_PHOTOS_PER_PROJECT, MAX_PHOTOS_PER_UPLOAD,
                     MAX_UPLOAD_BYTES, ROSTER_COLUMNS, WORKSPACE, atomic_write_bytes,
                     atomic_write_text, ensure_dirs, retry_read_bytes, retry_read_text)
from .csv_loader import build_column_map, decode_bytes, empty_template, normalize_id, parse_roster
from .image_proc import generate_default_map
from .injector import build_photo_chunks, render_starmap
from .models import (CalibData, CalibPoint, CalibUpdateRequest, CreateProjectRequest,
                     GenerateRequest, MergeDecision, MergeDecisionRequest, ProjectMeta,
                     RosterPatch, ValidationReport)
from .validate import passing_rows, report_markdown, validate

app = FastAPI(title="喵星图工厂 CatGalaxy Factory", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

WORKSPACE.mkdir(parents=True, exist_ok=True)


# ---------- 内部工具 ----------

# 所有写路由都挂这把锁：同一项目的并发写被串行化，不同项目互不影响。
# 读路由不挂——状态文件已改成原子替换，读者只会看到完整的旧版或完整的新版，
# 没必要跟正在跑的生成/上传抢锁。
_GUARD = Depends(locking.project_guard)


def _meta_or_404(pid: str) -> ProjectMeta:
    meta = store.load_meta(pid)
    if not meta:
        raise HTTPException(404, f"项目 {pid} 不存在")
    return meta


def _report_path(pid: str) -> Path:
    return store.project_dir(pid) / "report.json"


def _load_report(pid: str) -> Optional[ValidationReport]:
    try:
        text = retry_read_text(_report_path(pid))
    except FileNotFoundError:
        return None
    try:
        return ValidationReport.model_validate(json.loads(text))
    except Exception:
        return None


def _save_report(pid: str, report: ValidationReport) -> None:
    atomic_write_text(
        _report_path(pid), json.dumps(report.model_dump(), ensure_ascii=False, indent=2))


def _run_validation(meta: ProjectMeta) -> ValidationReport:
    roster = store.project_dir(meta.id) / "roster.csv"
    if not roster.exists():
        raise HTTPException(400, "尚未上传名册 CSV")
    text, _enc = decode_bytes(retry_read_bytes(roster))
    rows, missing, unknown, _header = parse_roster(text)
    report = validate(rows, store.photo_names(meta.id), missing_columns=missing,
                      unknown_columns=unknown, school=meta.school, project_id=meta.id)
    _save_report(meta.id, report)
    return report


def _ensure_map(pid: str) -> Path:
    mp = store.map_path(pid)
    if not mp.exists():
        generate_default_map(mp)
        meta = store.load_meta(pid)
        if meta:
            meta.has_map = True
            store.save_meta(meta)
    return mp


def _map_size(pid: str) -> Optional[tuple[int, int]]:
    """底图真实像素尺寸，供模板按真实长宽比绘制（避免拉伸变形）。"""
    mp = store.map_path(pid)
    if not mp.exists():
        return None
    try:
        from PIL import Image
        with Image.open(mp) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None


def _calib_positions(pid: str) -> Optional[dict]:
    """项目已保存的人工标定 → {id: {"x":…, "y":…}}；未标定返回 None。"""
    data = store.load_calib(pid)
    if not data or not data.positions:
        return None
    return {k: v.model_dump() for k, v in data.positions.items()}


def _photo_url(pid: str, name: str) -> str:
    """已入库照片 → 浏览器可访问的 URL；文件不在就返回空串。"""
    base = (name or "").strip().replace("\\", "/").split("/")[-1]
    if not base:
        return ""
    d = store.project_dir(pid) / "assets" / "photos"
    for p in (d.iterdir() if d.exists() else []):
        if p.is_file() and p.name.lower() == base.lower():
            return f"/bundle/{pid}/assets/photos/{quote(p.name)}"
    return ""


def _photo_blobs(pid: str, names: Optional[set[str]] = None) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in store.photo_files(pid):
        if names is None or p.name in names or p.name.lower() in {n.lower() for n in names}:
            out[p.name] = retry_read_bytes(p)
    return out


def _bundle_dir(pid: str, form: str) -> Path:
    return store.project_dir(pid) / "out" / form


def generate_bundle(pid: str, form: str = "relative", *,
                    exclude_low_confidence: bool = False,
                    school: Optional[str] = None) -> dict:
    """F3+F4+F5 的核心编排：渲染 HTML → 落盘 bundle → 打 zip。"""
    if form not in ("relative", "inline"):
        raise HTTPException(400, "form 只能是 relative 或 inline")

    meta = _meta_or_404(pid)
    if school:
        meta.school = school
    report = _load_report(pid) or _run_validation(meta)
    if not report.summary.ok:
        raise HTTPException(409, "校验未通过，请先修复名册错误")

    rows = passing_rows(report, exclude_low_confidence=exclude_low_confidence)
    if not rows:
        raise HTTPException(409, "没有可入图的行（可能全部被排除）")

    dirs = ensure_dirs(pid)
    _ensure_map(pid)
    map_bytes = retry_read_bytes(store.map_path(pid))
    map_size = _map_size(pid)
    calib_override = _calib_positions(pid)
    html_name = packager.html_basename(meta.school)
    dest = _bundle_dir(pid, form)
    dest.parent.mkdir(parents=True, exist_ok=True)

    referenced = {r.photo_file.strip().replace("\\", "/").split("/")[-1]
                  for r in rows if r.photo_file.strip()}
    photos = _photo_blobs(pid, referenced)
    roster_csv = retry_read_bytes(
        store.project_dir(pid) / "roster.csv").decode("utf-8", errors="replace")
    md = report_markdown(report)
    atomic_write_text(dirs["root"] / "校验报告.md", md)
    stats = injector.generation_stats(rows)
    summary_md = summary_writer.build_summary(meta, report, stats,
                                             merge=store.load_merge(pid))
    atomic_write_text(dirs["root"] / "归并决策摘要.md", summary_md)

    if form == "inline":
        payload = dict(photos)
        payload["map.jpg"] = map_bytes
        chunks = build_photo_chunks(payload)
        html = render_starmap(school=meta.school, subtitle=meta.subtitle, rows=rows,
                              form=form, map_filename="assets/map.jpg",
                              photo_script_names=[n for n, _ in chunks],
                              calib=calib_override, map_size=map_size)
        written = packager.build_inline_bundle(dest, html=html, html_name=html_name,
                                               chunks=chunks, roster_csv=roster_csv,
                                               report_md=md, summary_md=summary_md)
    else:
        html = render_starmap(school=meta.school, subtitle=meta.subtitle, rows=rows,
                              form=form, map_filename="assets/map.jpg",
                              photo_script_names=[],
                              calib=calib_override, map_size=map_size)
        written = packager.build_relative_bundle(dest, html=html, html_name=html_name,
                                                 photos=photos, map_bytes=map_bytes,
                                                 roster_csv=roster_csv, report_md=md,
                                                 summary_md=summary_md)

    zip_name = packager.zip_basename(meta.school)
    zip_path = packager.make_zip(dest, dirs["dist"] / f"{zip_name}.zip")

    calib_stat = injector.calib_stats(rows, calib_override)
    meta.generated_form = form
    meta.has_roster = True
    meta.photo_count = len(store.photo_files(pid))
    meta.has_map = True
    store.log(meta, "生成星图",
              f"form={form} cats={len(rows)} photos={len(photos)} "
              f"exclude_low={exclude_low_confidence} "
              f"标定={calib_stat['manual']}/{calib_stat['total']} zip={zip_path.name}")

    missing_photos = sorted(referenced - set(photos))
    return {
        "project_id": pid,
        "school": meta.school,
        "form": form,
        "cats": len(rows),
        "photos_embedded": len(photos),
        "missing_photos": missing_photos,
        "files": written,
        "stats": stats,
        "calib": calib_stat,
        "map_size": list(map_size) if map_size else None,
        "html_name": html_name,
        "preview_url": f"/bundle/{pid}/out/{form}/{quote(html_name)}",
        "zip_name": zip_path.name,
        "zip_bytes": zip_path.stat().st_size,
        "bundle_bytes": packager.bundle_size(dest),
        "generated_at": date.today().isoformat(),
    }


# ---------- 路由：项目 ----------

@app.post("/api/projects")
def create_project(req: CreateProjectRequest) -> dict:
    meta = store.create_project(req.school, req.subtitle, req.motto)
    return meta.model_dump()


@app.get("/api/projects")
def list_projects(limit: int = 50, offset: int = 0, q: str = "") -> dict:
    """分页列出项目（按最近更新倒序）。q 按校名 / 项目 id 模糊匹配。"""
    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit 必须在 1~500 之间")
    if offset < 0:
        raise HTTPException(400, "offset 不能为负")

    metas = store.list_projects()
    if q.strip():
        kw = q.strip().lower()
        metas = [m for m in metas if kw in m.school.lower() or kw in m.id.lower()]
    total = len(metas)
    page = metas[offset:offset + limit]
    return {"items": [m.model_dump() for m in page],
            "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(page) < total}


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    meta = _meta_or_404(pid)
    report = _load_report(pid)
    return {
        "meta": meta.model_dump(),
        "photos": [p.name for p in store.photo_files(pid)],
        "report": report.model_dump() if report else None,
        "has_map": store.map_path(pid).exists(),
    }


@app.delete("/api/projects/{pid}", dependencies=[_GUARD])
def delete_project(pid: str) -> dict:
    _meta_or_404(pid)
    shutil.rmtree(store.project_dir(pid), ignore_errors=True)
    return {"deleted": pid}


# ---------- 路由：F1 数据导入 ----------

@app.post("/api/projects/{pid}/roster", dependencies=[_GUARD])
async def upload_roster(pid: str, file: UploadFile = File(...)) -> dict:
    meta = _meta_or_404(pid)
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传的 CSV 为空")
    text, enc = decode_bytes(data)
    rows, missing, unknown, header = parse_roster(text)
    dirs = ensure_dirs(pid)
    atomic_write_bytes(dirs["root"] / "roster.csv", data)
    atomic_write_bytes(dirs["data"] / "猫咪名册.csv", data)
    meta.has_roster = True
    store.log(meta, "上传名册", f"编码={enc} 行数={len(rows)} 缺列={missing or '无'}")

    report = validate(rows, store.photo_names(pid), missing_columns=missing,
                      unknown_columns=unknown, school=meta.school, project_id=pid)
    _save_report(pid, report)
    return {"encoding": enc, "rows": len(rows), "missing_columns": missing,
            "unknown_columns": unknown, "report": report.model_dump()}


@app.post("/api/projects/{pid}/photos", dependencies=[_GUARD])
async def upload_photos(pid: str, files: list[UploadFile] = File(...)) -> dict:
    meta = _meta_or_404(pid)
    dirs = ensure_dirs(pid)
    saved: list[dict] = []
    written: list[Path] = []
    skipped: list[str] = []
    warnings: list[str] = []
    compressed_bytes = 0
    src_bytes = 0

    existing = len(store.photo_files(pid))
    room = MAX_PHOTOS_PER_PROJECT - existing
    if room <= 0:
        raise HTTPException(413, f"项目已有 {existing} 张照片，达到累计上限 "
                                 f"{MAX_PHOTOS_PER_PROJECT} 张；请新建项目或先清理旧照片")

    budget = image_proc.ZipBudget(max_files=min(MAX_PHOTOS_PER_UPLOAD, room))

    def rollback_and_413(why: str):
        """超限就把本次写入的照片全删掉——不留半截入库的项目。"""
        for p in written:
            try:
                p.unlink()
            except OSError:
                pass
        raise HTTPException(413, why)

    for f in files:
        name = f.filename or ""
        declared = getattr(f, "size", None)
        if declared and declared > MAX_UPLOAD_BYTES:
            rollback_and_413(f"「{name or '(无名)'}」体积 {declared // 1024 // 1024}MB，"
                             f"超过单文件上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
        if budget.stopped:
            skipped.append(name or "(未处理：已超限额)")
            continue

        data = await f.read()
        if not data:
            skipped.append(name or "(空文件)")
            continue
        if len(data) > MAX_UPLOAD_BYTES:
            rollback_and_413(f"「{name or '(无名)'}」体积 {len(data) // 1024 // 1024}MB，"
                             f"超过单文件上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")

        if image_proc.is_zip(data):
            got = image_proc.extract_photo_zip(data, dirs["photos"], budget=budget,
                                               label=name or "zip")
            for path, info in got:
                written.append(path)
                saved.append(info)
                src_bytes += info["src_bytes"]
                compressed_bytes += info["out_bytes"]
            continue

        if Path(name).suffix.lower() not in IMAGE_EXTS:
            skipped.append(name or "(无扩展名)")
            continue
        if not budget.charge_file():
            break
        try:
            path, info = image_proc.process_and_save(data, dirs["photos"], name)
        except Exception:
            skipped.append(f"{name}（不是有效的图片文件，已跳过）")
            budget.release_file()
            continue
        written.append(path)
        saved.append(info)
        src_bytes += info["src_bytes"]
        compressed_bytes += info["out_bytes"]

    if budget.stopped:
        rollback_and_413(budget.stop_reason)

    if budget.corrupt:
        head = ", ".join(Path(c).name for c in budget.corrupt[:6])
        warnings.append(f"这些文件扩展名是图片但解不开，已跳过：{head}"
                        + ("…" if len(budget.corrupt) > 6 else ""))
    if budget.too_deep:
        head = ", ".join(Path(z).name for z in budget.too_deep[:6])
        warnings.append(f"这些嵌套 zip 超过 {budget.max_depth} 层递归上限，未展开：{head}"
                        + ("…" if len(budget.too_deep) > 6 else ""))

    meta.photo_count = len(store.photo_files(pid))
    store.log(meta, "上传照片",
              f"张数={len(saved)} 跳过={len(skipped)} "
              f"原始={src_bytes/1024:.0f}KB 压缩后={compressed_bytes/1024:.0f}KB "
              f"解压={budget.inflated/1024/1024:.1f}MB 嵌套zip={budget.nested_zips}")

    report = None
    if meta.has_roster:
        report = _run_validation(meta)

    return {"saved": len(saved), "skipped": skipped, "photos": sorted(store.photo_names(pid)),
            "src_bytes": src_bytes, "out_bytes": compressed_bytes,
            "max_side": max((s["width"] for s in saved), default=0) and
                        max(max(s["width"], s["height"]) for s in saved) if saved else 0,
            "limits": {"max_files_this_request": budget.max_files,
                       "max_files_per_project": MAX_PHOTOS_PER_PROJECT,
                       "max_upload_bytes": MAX_UPLOAD_BYTES,
                       "max_inflated_bytes": budget.max_inflated,
                       "max_zip_depth": budget.max_depth,
                       "files_seen": budget.files,
                       "inflated_bytes": budget.inflated,
                       "zip_entries": budget.entries,
                       "project_total": meta.photo_count,
                       "project_room_left": MAX_PHOTOS_PER_PROJECT - meta.photo_count},
            "warnings": warnings,
            "report": report.model_dump() if report else None}


@app.post("/api/projects/{pid}/map", dependencies=[_GUARD])
async def upload_map(pid: str, file: UploadFile = File(...)) -> dict:
    meta = _meta_or_404(pid)
    data = await file.read()
    if not image_proc.looks_like_image(data):
        raise HTTPException(400, "底图不是有效图片")
    mp = store.map_path(pid)
    mp.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(data)).convert("RGB")
    from .config import MAX_MAP_SIDE
    if max(img.size) > MAX_MAP_SIDE:
        s = MAX_MAP_SIDE / max(img.size)
        img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
    img.save(mp, "JPEG", quality=88, optimize=True)
    meta.has_map = True
    store.log(meta, "上传底图", f"{mp.stat().st_size/1024:.0f}KB {img.size}")
    return {"ok": True, "size": list(img.size), "bytes": mp.stat().st_size}


# ---------- 路由：F2 校验 ----------

@app.post("/api/projects/{pid}/validate", dependencies=[_GUARD])
def run_validate(pid: str) -> dict:
    meta = _meta_or_404(pid)
    report = _run_validation(meta)
    store.log(meta, "运行校验",
              f"行={report.summary.total_rows} 错={report.summary.error_count} "
              f"警={report.summary.warning_count}")
    return report.model_dump()


@app.get("/api/projects/{pid}/report.md")
def report_md(pid: str) -> PlainTextResponse:
    _meta_or_404(pid)
    report = _load_report(pid)
    if not report:
        raise HTTPException(404, "尚未生成校验报告")
    return PlainTextResponse(report_markdown(report), media_type="text/markdown; charset=utf-8")


# ---------- 路由：F3/F4/F5 生成 · 预览 · 打包 ----------

@app.post("/api/projects/{pid}/generate", dependencies=[_GUARD])
def generate(pid: str, req: GenerateRequest) -> dict:
    return generate_bundle(pid, req.form, exclude_low_confidence=req.exclude_low_confidence,
                           school=req.school)


@app.get("/api/projects/{pid}/preview")
def preview(pid: str, form: str = "relative") -> dict:
    """返回预览地址（bundle 已按 StaticFiles 挂载，相对路径可正确解析）。"""
    _meta_or_404(pid)
    dest = _bundle_dir(pid, form)
    htmls = list(dest.glob("*.html")) if dest.exists() else []
    if not htmls:
        raise HTTPException(404, "尚未生成，请先调用 POST /generate")
    name = htmls[0].name
    return {"form": form, "url": f"/bundle/{pid}/out/{form}/{quote(name)}",
            "html_name": name}


@app.get("/api/projects/{pid}/download")
def download(pid: str, form: str = "relative") -> FileResponse:
    meta = _meta_or_404(pid)
    zp = store.project_dir(pid) / "dist" / f"{packager.zip_basename(meta.school)}.zip"
    if not zp.exists():
        # 未打包则即时生成
        generate_bundle(pid, form)
    if not zp.exists():
        raise HTTPException(404, "zip 不存在")
    return FileResponse(zp, filename=zp.name, media_type="application/zip")


@app.get("/api/projects/{pid}/artifacts")
def artifacts(pid: str) -> dict:
    _meta_or_404(pid)
    root = store.project_dir(pid)
    dist = sorted((p.name, p.stat().st_size) for p in (root / "dist").glob("*.zip")) \
        if (root / "dist").exists() else []
    out = {}
    for form in ("relative", "inline"):
        d = root / "out" / form
        if d.exists():
            out[form] = [str(p.relative_to(d).as_posix()) for p in sorted(d.rglob("*"))
                         if p.is_file()]
    return {"zips": [{"name": n, "bytes": b} for n, b in dist], "bundles": out}


# ---------- 路由：F8 底图标定 ----------

@app.get("/api/projects/{pid}/calib")
def get_calib(pid: str) -> dict:
    """当前星位标定：人工标定优先，缺失的是算法推导值。"""
    _meta_or_404(pid)
    data = store.load_calib(pid)
    report = _load_report(pid)
    rows = report.rows if report else []
    positions = {k: v.model_dump() for k, v in data.positions.items()} if data else {}
    return {
        "project_id": pid,
        "source": data.source if data else "derived",
        "updated_at": data.updated_at if data else "",
        "note": data.note if data else "",
        "positions": positions,
        "stats": injector.calib_stats(rows, positions),
        "map_size": list(_map_size(pid) or []) or None,
        "ids": [normalize_id(r.id) or r.id for r in rows],
    }


@app.put("/api/projects/{pid}/calib", dependencies=[_GUARD])
def put_calib(pid: str, req: CalibUpdateRequest) -> dict:
    """保存人工标定。坐标为相对底图左上角的归一化值（0~1）。"""
    meta = _meta_or_404(pid)
    report = _load_report(pid) or _run_validation(meta)
    known = {(normalize_id(r.id) or r.id) for r in report.rows}

    bad = sorted(cid for cid, p in req.positions.items()
                 if not (0.0 <= p.x <= 1.0 and 0.0 <= p.y <= 1.0))
    if bad:
        raise HTTPException(400, f"坐标必须是 0~1 的归一化值，越界：{', '.join(bad[:8])}")

    existing = store.load_calib(pid)
    merged = ({k: v.model_dump() for k, v in existing.positions.items()}
              if (existing and req.merge) else {})
    incoming = {k: v.model_dump() for k, v in req.positions.items()}
    ignored = sorted(k for k in incoming if k not in known)
    for k in ignored:
        incoming.pop(k)
    merged.update(incoming)

    data = CalibData(positions={k: CalibPoint(**v) for k, v in merged.items()},
                     source="manual", note=req.note)
    store.save_calib(pid, data)
    store.log(meta, "保存星位标定",
              f"本次={len(incoming)} 累计={len(merged)} 忽略未知编号={len(ignored)}")
    return {"saved": len(incoming), "total": len(merged),
            "ignored_unknown_ids": ignored, "updated_at": data.updated_at,
            "stats": injector.calib_stats(report.rows, merged)}


@app.delete("/api/projects/{pid}/calib", dependencies=[_GUARD])
def delete_calib(pid: str) -> dict:
    meta = _meta_or_404(pid)
    removed = store.clear_calib(pid)
    if removed:
        store.log(meta, "清除星位标定", "回到算法推导坐标")
    return {"cleared": removed}


# ---------- 路由：F9 归并工作台 ----------

@app.get("/api/projects/{pid}/merge")
def get_merge(pid: str) -> dict:
    """疑似重复建档的候选组 + 已有判定，供工作台左右并排看图。"""
    meta = _meta_or_404(pid)
    report = _load_report(pid) or _run_validation(meta)
    groups = merge.candidate_groups(report.rows)
    book = store.load_merge(pid)
    for g in groups:
        d = book.decisions.get(g["gid"])
        g["decision"] = d.model_dump() if d else None
        for m in g["members"]:
            m["photo_url"] = _photo_url(pid, m["photo"])
    verdicts = [book.decisions[g["gid"]].verdict for g in groups if g["gid"] in book.decisions]
    return {
        "project_id": pid,
        "groups": groups,
        "stats": {
            "groups": len(groups),
            "members": sum(len(g["members"]) for g in groups),
            "decided": len(verdicts),
            "pending": len(groups) - len(verdicts),
            "same": verdicts.count("same"),
            "different": verdicts.count("different"),
            "unsure": verdicts.count("unsure"),
            "stale_gids": sorted(set(book.decisions) - {g["gid"] for g in groups}),
        },
    }


@app.put("/api/projects/{pid}/merge", dependencies=[_GUARD])
def put_merge(pid: str, req: MergeDecisionRequest) -> dict:
    """落一条人工判定。理由会进操作日志，最终出现在 F7 归并决策摘要里。"""
    meta = _meta_or_404(pid)
    report = _load_report(pid) or _run_validation(meta)
    groups = {g["gid"]: g for g in merge.candidate_groups(report.rows)}
    group = groups.get(req.gid)
    if not group:
        raise HTTPException(404, f"候选组 {req.gid} 不存在（名册可能已改动，请刷新工作台）")

    errs = merge.check_decision(group, req.verdict, req.keep, req.drop, req.reason)
    if errs:
        raise HTTPException(400, "；".join(errs))

    book = store.load_merge(pid)
    decision = MergeDecision(gid=req.gid, verdict=req.verdict, keep=req.keep,
                             drop=sorted(set(req.drop)), reason=req.reason.strip(),
                             members=[m["id"] for m in group["members"]],
                             kind=group["kind"], updated_at=store.now())
    book.decisions[req.gid] = decision
    store.save_merge(pid, book)
    store.log(meta, "归并判定",
              f"组={req.gid}（{group['kind']}）判定={merge.VERDICT_LABEL[req.verdict]} "
              f"保留={req.keep or '—'} 弃用={','.join(decision.drop) or '—'} "
              f"理由={req.reason.strip()[:60]}")
    return {"saved": decision.model_dump(), "decided": len(book.decisions),
            "stats": {"groups": len(groups), "decided": len(book.decisions)}}


@app.delete("/api/projects/{pid}/merge/{gid}", dependencies=[_GUARD])
def delete_merge(pid: str, gid: str) -> dict:
    meta = _meta_or_404(pid)
    removed = store.drop_merge(pid, gid)
    if removed:
        store.log(meta, "撤销归并判定", f"组={gid}")
    return {"cleared": removed}


# ---------- 路由：F10 名册在线编辑 ----------

@app.get("/api/projects/{pid}/roster")
def get_roster(pid: str) -> dict:
    """当前名册的原始行与列映射，前端据此做行内编辑（不用再下载-改-上传）。"""
    _meta_or_404(pid)
    p = store.project_dir(pid) / "roster.csv"
    if not p.exists():
        raise HTTPException(404, "尚未上传名册 CSV")
    text, enc = decode_bytes(retry_read_bytes(p))
    raw = [r for r in csv.reader(io.StringIO(text))]
    header = raw[0] if raw else []
    mapping, missing, unknown = build_column_map(header)
    report = _load_report(pid)
    return {"encoding": enc, "header": header, "rows": raw[1:],
            "line_offset": 2, "mapping": mapping,
            "editable_columns": list(ROSTER_COLUMNS),
            "missing_columns": missing, "unknown_columns": unknown,
            "report": report.model_dump() if report else None}


@app.patch("/api/projects/{pid}/roster", dependencies=[_GUARD])
def patch_roster(pid: str, req: RosterPatch) -> dict:
    """按「物理行号 + 标准列名」改单元格，回写 CSV 并自动重跑校验。"""
    meta = _meta_or_404(pid)
    p = store.project_dir(pid) / "roster.csv"
    if not p.exists():
        raise HTTPException(404, "尚未上传名册 CSV")
    if not req.edits:
        raise HTTPException(400, "没有要应用的改动")

    text, _enc = decode_bytes(retry_read_bytes(p))
    raw = [r for r in csv.reader(io.StringIO(text))]
    if not raw:
        raise HTTPException(400, "名册为空，无从修改")
    mapping, _missing, _unknown = build_column_map(raw[0])

    applied: list[dict] = []
    rejected: list[dict] = []
    for e in req.edits:
        if e.field not in mapping:
            rejected.append({"line": e.line, "field": e.field,
                             "why": f"名册没有「{e.field}」这一列"})
            continue
        if e.line < 2 or e.line - 1 >= len(raw):
            rejected.append({"line": e.line, "field": e.field,
                             "why": f"行号越界（名册只有 {len(raw)} 行，表头是第 1 行）"})
            continue
        idx = mapping[e.field]
        row = raw[e.line - 1]
        while len(row) <= idx:
            row.append("")
        old = row[idx]
        row[idx] = (e.value or "").strip()
        applied.append({"line": e.line, "field": e.field, "old": old, "new": row[idx]})

    report = None
    if applied:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(raw)
        data = buf.getvalue().encode("utf-8-sig")
        atomic_write_bytes(p, data)
        atomic_write_bytes(store.project_dir(pid) / "data" / "猫咪名册.csv", data)
        store.log(meta, "在线编辑名册",
                  f"改动 {len(applied)} 处，拒绝 {len(rejected)} 处："
                  + "；".join(f"行{a['line']}·{a['field']}" for a in applied[:6]))
        if req.revalidate:
            report = _run_validation(meta)

    return {"applied": applied, "rejected": rejected,
            "report": report.model_dump() if report else None}


# ---------- 路由：F6 / F7 ----------

@app.post("/api/census/parse")
async def census_parse(file: UploadFile = File(...), as_csv: bool = Form(False)) -> JSONResponse:
    data = await file.read()
    text, enc = decode_bytes(data)
    records, warnings = census_parser.parse_batch(text)
    label = census_parser.re.search(r"batch\s*(\d+)", text[:200], census_parser.re.IGNORECASE)
    batch_label = f"batch{label.group(1)}" if label else "batch"
    draft = census_parser.records_to_draft_csv(records, batch_label)
    if as_csv:
        return JSONResponse({"encoding": enc, "count": len(records), "draft_csv": draft,
                             "warnings": warnings,
                             "suggestions": census_parser.merge_suggestions(records)})
    return JSONResponse({
        "encoding": enc, "count": len(records), "warnings": warnings,
        "records": [r.as_dict() for r in records][:500],
        "suggestions": census_parser.merge_suggestions(records),
        "draft_csv": draft,
    })


@app.post("/api/projects/{pid}/summary", dependencies=[_GUARD])
def summary(pid: str) -> dict:
    meta = _meta_or_404(pid)
    report = _load_report(pid)
    rows = passing_rows(report) if report else []
    stats = injector.generation_stats(rows) if rows else None
    md = summary_writer.build_summary(meta, report, stats, merge=store.load_merge(pid))
    out = store.project_dir(pid) / "归并决策摘要.md"
    atomic_write_text(out, md)
    store.log(meta, "生成归并决策摘要", f"{len(md)} 字符")
    return {"markdown": md, "path": str(out)}


# ---------- 路由：模板与首页 ----------

@app.get("/api/template")
def template_csv() -> PlainTextResponse:
    return PlainTextResponse(empty_template(),
                             media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition":
                                      'attachment; filename="cat-roster-template.csv"'})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (Path(__file__).resolve().parent.parent / "static" / "index.html") \
        .read_text(encoding="utf-8")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "catgalaxy-factory", "version": "1.0.0"}


# bundle 静态托管：让生成的 HTML 里的相对路径（assets/…）可被浏览器解析
app.mount("/bundle", StaticFiles(directory=str(WORKSPACE)), name="bundle")
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "static")),
          name="static")
