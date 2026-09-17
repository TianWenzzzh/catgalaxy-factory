"""喵星图工厂 · FastAPI 入口。"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import (census_parser, image_proc, injector, locking, merge, packager,
               phash, progress, store, summary_writer)
from .config import (ACTIVE_ENGINE, IMAGE_EXTS, LOGO_EXTS, MAX_LOGO_UPLOAD_BYTES,
                     MAX_PHOTOS_PER_PROJECT, MAX_PHOTOS_PER_UPLOAD, MAX_UPLOAD_BYTES,
                     ROSTER_COLUMNS, WORKSPACE, atomic_write_bytes, atomic_write_text,
                     ensure_dirs, retry_read_bytes, retry_read_text)
from .csv_loader import build_column_map, decode_bytes, empty_template, normalize_id, parse_roster
from .image_proc import generate_default_map
from .injector import render_starmap, render_starmap_v29
from .models import (CalibData, CalibPoint, CalibUpdateRequest, CreateProjectRequest,
                     GenerateRequest, MergeDecision, MergeDecisionRequest, ProjectMeta,
                     RosterPatch, RosterRowInsert, ThemeUpdateRequest, ValidationReport)
# 按名字导入而不是 `from . import theme`：generate_bundle 里有个局部变量就叫
# theme，模块名被它遮住之后想在同一个函数里调 theme.css_block 就会莫名其妙地炸。
from .theme import (DEFAULT_PRESET, MAX_SIGNATURE, Theme, color_menu, font_menu,
                    preset_menu)
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


def _photo_dir(pid: str) -> Path:
    return store.project_dir(pid) / "assets" / "photos"


def _photo_names(pid: str, names: Optional[set[str]] = None) -> set[str]:
    """名册引用的照片里，盘上真实存在的那些（返回盘上的写法）。

    只扫名字、不读字节：inline 形态靠它把「整册照片进内存」推迟到写分片那一刻。
    命中规则和原来读字节的版本一致——名册里大小写写错也认得出来，但返回的是盘上
    的名字，所以 missing_photos 里仍会留下那个「只差大小写」的引用名（旧行为如此）。
    """
    lowered = {n.lower() for n in names} if names is not None else None
    return {p.name for p in store.photo_files(pid)
            if lowered is None or p.name in names or p.name.lower() in lowered}


def _photo_reader(pid: str):
    """按名字逐张读盘的回调。产物落盘时用它，整册照片就不必同时在内存里。"""
    d = _photo_dir(pid)

    def read(name: str) -> bytes:
        return retry_read_bytes(d / name)

    return read


def _photo_hasher(pid: str):
    """「代表照片名 → 感知哈希」的回调，给 F9 视觉预排序用。

    目录只列一次、同名只解码一次：same-photo 组里两行共用一张照片正是最常见的
    重复建档，不该为它把同一个文件解两遍。名字匹配规则和 `_photo_url` 一致
    （取 basename、忽略大小写）；文件不在或解不开就返回 None，排序时自然沉底。
    """
    by_lower = {p.name.lower(): p for p in store.photo_files(pid)}
    cache: dict[str, int | None] = {}

    def hash_of(name: str) -> int | None:
        base = (name or "").strip().replace("\\", "/").split("/")[-1]
        if not base:
            return None
        key = base.lower()
        if key not in cache:
            p = by_lower.get(key)
            cache[key] = phash.dhash_file(p) if p else None
        return cache[key]

    return hash_of


def _inline_reader(pid: str, map_bytes: bytes):
    """iter_photo_chunks 的取字节回调：底图来自内存，照片用到哪张读哪张。"""
    read_photo = _photo_reader(pid)

    def read(name: str) -> bytes:
        return map_bytes if name == "map.jpg" else read_photo(name)

    return read


def _bundle_dir(pid: str, form: str) -> Path:
    return store.project_dir(pid) / "out" / form


# generate_bundle 里 stage() 的调用次数。路由用它当进度条的分母，
# 所以对不上就会「条子走到 83% 就停了」。tests/test_progress.py 会盯住这个数。
GENERATE_STAGES = 6


def generate_bundle(pid: str, form: str = "relative", *,
                    exclude_low_confidence: bool = False,
                    school: Optional[str] = None,
                    engine: Optional[str] = None,
                    survey_date: Optional[str] = None,
                    rep: Optional[progress.Reporter] = None) -> dict:
    """F3+F4+F5 的核心编排：渲染 HTML → 落盘 bundle → 打 zip。

    ``rep`` 可选：预览路由的「产物不在就顺手生成一次」也调这个函数，那条路径
    没人轮询进度，传 None 即可。
    """
    def stage(note: str) -> None:
        if rep is not None:
            rep.stage(note)

    if form not in ("relative", "inline"):
        raise HTTPException(400, "form 只能是 relative 或 inline")

    stage("读校验报告")
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
    stage("准备底图")
    _ensure_map(pid)
    map_bytes = retry_read_bytes(store.map_path(pid))
    map_size = _map_size(pid)
    calib_override = _calib_positions(pid)
    theme = store.load_theme(pid)
    logo_bytes = store.load_logo(pid)
    # inline 形态必须自包含，校徽只能内嵌；相对形态写文件、HTML 里给相对路径。
    logo_src = ""
    if logo_bytes:
        logo_src = (f"data:image/png;base64,{base64.b64encode(logo_bytes).decode('ascii')}"
                    if form == "inline" else "assets/logo.png")
    html_name = packager.html_basename(meta.school)
    dest = _bundle_dir(pid, form)
    dest.parent.mkdir(parents=True, exist_ok=True)

    referenced = {r.photo_file.strip().replace("\\", "/").split("/")[-1]
                  for r in rows if r.photo_file.strip()}
    stage(f"读 {len(referenced)} 张照片")
    found = _photo_names(pid, referenced)
    stage("写名册与两份报告")
    roster_csv = retry_read_bytes(
        store.project_dir(pid) / "roster.csv").decode("utf-8", errors="replace")
    md = report_markdown(report)
    atomic_write_text(dirs["root"] / "校验报告.md", md)
    stats = injector.generation_stats(rows)
    summary_md = summary_writer.build_summary(meta, report, stats,
                                             merge=store.load_merge(pid))
    atomic_write_text(dirs["root"] / "归并决策摘要.md", summary_md)

    stage("渲染星图 HTML 并落盘产物")
    engine = (engine or ACTIVE_ENGINE).lower()
    if engine not in ("v1", "v29"):
        raise HTTPException(422, f"engine 非法：{engine!r}（v1|v29）")
    if engine == "v29" and form != "inline":
        # v29 的 relative 形态需要路径引导分片，F3 落地后开放
        raise HTTPException(409, "v29 引擎暂只支持 inline 形态（relative 将随 F3 开放）")
    if form == "inline":
        # 底图也进分片（模板从 __PHOTOS["map.jpg"] 取它），所以一起参与排序和切片；
        # 真有照片叫 map.jpg 时由底图覆盖——与旧写法 payload["map.jpg"]=map_bytes 同义。
        sizes = {n: (_photo_dir(pid) / n).stat().st_size for n in found}
        sizes["map.jpg"] = len(map_bytes)
        pairs = [(n, sizes[n]) for n in sorted(sizes)]
        if engine == "v29":
            bundle = render_starmap_v29(
                school=meta.school, rows=rows,
                photo_sizes={n: s for n, s in sizes.items() if n != "map.jpg"},
                map_bytes=map_bytes, map_key="map.jpg",
                calib=calib_override, photo_loading="lazy",
                theme=theme, logo_tag_html=injector.logo_tag(logo_src),
                generated_on=survey_date)
            html, chunks = bundle.html, bundle.iter_chunks(
                _inline_reader(pid, map_bytes))
        else:
            html = render_starmap(school=meta.school, subtitle=meta.subtitle, rows=rows,
                                  form=form, map_filename="assets/map.jpg",
                                  photo_script_names=injector.plan_photo_chunks(pairs),
                                  calib=calib_override, map_size=map_size,
                                  theme=theme, logo_src=logo_src)
            chunks = injector.iter_photo_chunks(pairs, _inline_reader(pid, map_bytes))
        written = packager.build_inline_bundle(
            dest, html=html, html_name=html_name,
            chunks=chunks,
            roster_csv=roster_csv, report_md=md, summary_md=summary_md)
    else:
        read_photo = _photo_reader(pid)
        names = sorted(found)          # build_relative_bundle 不再排序，顺序在这里定
        html = render_starmap(school=meta.school, subtitle=meta.subtitle, rows=rows,
                              form=form, map_filename="assets/map.jpg",
                              photo_script_names=[],
                              calib=calib_override, map_size=map_size,
                              theme=theme, logo_src=logo_src)
        written = packager.build_relative_bundle(dest, html=html, html_name=html_name,
                                                 photos=((n, read_photo(n)) for n in names),
                                                 map_bytes=map_bytes,
                                                 logo_bytes=logo_bytes,
                                                 roster_csv=roster_csv, report_md=md,
                                                 summary_md=summary_md)

    stage("打包 zip")
    zip_name = packager.zip_basename(meta.school)
    zip_path = packager.make_zip(dest, dirs["dist"] / f"{zip_name}.zip")

    calib_stat = injector.calib_stats(rows, calib_override)
    meta.generated_form = form
    meta.has_roster = True
    meta.photo_count = len(store.photo_files(pid))
    meta.has_map = True
    store.log(meta, "生成星图",
              f"form={form} engine={engine} cats={len(rows)} photos={len(found)} "
              f"exclude_low={exclude_low_confidence} "
              f"标定={calib_stat['manual']}/{calib_stat['total']} "
              f"主题={theme.preset} 校徽={'有' if logo_bytes else '无'} zip={zip_path.name}")

    missing_photos = sorted(referenced - found)
    return {
        "project_id": pid,
        "school": meta.school,
        "form": form,
        "engine": engine,
        "cats": len(rows),
        "photos_embedded": len(found),
        "missing_photos": missing_photos,
        "files": written,
        "stats": stats,
        "calib": calib_stat,
        "theme": {"preset": theme.preset,
                  "custom_colors": len(theme.colors),
                  "signature": theme.footer_signature,
                  "logo": bool(logo_bytes),
                  "logo_bytes": len(logo_bytes or b"")},
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
    """收照片。干活的是 _ingest_photos，这一层只负责把进度记下来。

    先 _meta_or_404 再进 Reporter：Reporter 一进去就写 progress.json，而
    atomic_write_text 会 mkdir 父目录——顺序反了的话，随便探一个不存在的 pid
    就能在 workspace/ 里留下一个空目录。

    with 是必要的：中途 413 回滚或 Pillow 抛错时 __exit__ 会把进度标成 failed。
    少了这层，progress.json 会永远停在 running，下一个打开页面的人会看到一条
    走不完的进度条。

    另外：_ingest_photos 里的重活必须 await run_in_threadpool，别图省事改成
    直接调用。这条路由是 async 的，同步的解压/压缩会把事件循环整个占住，
    同一时间 /progress 一个请求都答不上来（实测 60 张的 zip 哑了 6.6 秒），
    进度条就白做了。
    """
    _meta_or_404(pid)
    dirs = ensure_dirs(pid)
    with progress.Reporter(pid, "上传并压缩照片", total=len(files)) as rep:
        # 压缩按批并行（见 image_proc.PhotoBatcher），池是懒创建、整个请求共用一个。
        # 生命周期放在这一层，和 Reporter 同一个道理：中途 413 回滚、Pillow 抛错，
        # 都得有人把工人进程关掉，不能让它活过这次上传。
        # zip 里的照片和散着传的照片分开记账：前者的失败要进 budget.corrupt（报成
        # 「扩展名是图片但解不开」），后者的失败要进 skipped（报成「不是有效的图片
        # 文件」）——两条文案是给用户看的，混在一起就分不清是哪个文件出的问题。
        packed = image_proc.PhotoBatcher(dirs["photos"], on_item=rep.as_callback())
        loose = image_proc.PhotoBatcher(dirs["photos"], on_item=rep.as_callback())
        with packed, loose:
            return await _ingest_photos(pid, files, rep, packed, loose)


async def _ingest_photos(pid: str, files: list[UploadFile],
                         rep: progress.Reporter,
                         packed: image_proc.PhotoBatcher,
                         loose: image_proc.PhotoBatcher) -> dict:
    meta = _meta_or_404(pid)
    dirs = ensure_dirs(pid)
    saved: list[dict] = []
    written: list[Path] = []
    skipped: list[str] = []
    warnings: list[str] = []

    existing = len(store.photo_files(pid))
    room = MAX_PHOTOS_PER_PROJECT - existing
    if room <= 0:
        raise HTTPException(413, f"项目已有 {existing} 张照片，达到累计上限 "
                                 f"{MAX_PHOTOS_PER_PROJECT} 张；请新建项目或先清理旧照片")

    budget = image_proc.ZipBudget(max_files=min(MAX_PHOTOS_PER_UPLOAD, room),
                                  on_item=rep.as_callback(), on_total=rep.grow_total)

    def collect(got: list[tuple[Path, dict]]) -> None:
        for path, info in got:
            written.append(path)
            saved.append(info)

    def rollback_and_413(why: str):
        """超限就把本次写入的照片全删掉——不留半截入库的项目。

        先排空 batcher：并行压缩是按批落盘的，它手里可能已经写好了一批，也可能
        还攥着一批原图没压。后者直接丢掉，别为一个注定要回滚的请求再花几秒压缩。
        """
        packed.drop_pending()
        loose.drop_pending()
        collect(packed.take()[0])
        collect(loose.take()[0])
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
            rep.drop_total()      # zip 自己不是一个照片名额，展开后才知道有多少张
            # 解压 + 逐张压缩 + 落盘全在工作线程里跑。留在事件循环里的话，
            # 这几秒到几十秒内 /progress 一个请求都答不上来——进度条恰恰
            # 在它唯一有意义的那段时间里是哑的（实测 60 张的包哑了 6.6 秒）。
            got = await run_in_threadpool(
                image_proc.extract_photo_zip, data, dirs["photos"],
                budget=budget, label=name or "zip", batcher=packed)
            collect(got)
            continue

        if Path(name).suffix.lower() not in IMAGE_EXTS:
            skipped.append(name or "(无扩展名)")
            continue
        if not budget.charge_file():
            break
        # 同样必须在工作线程里：攒批、压缩、落盘都是同步活。
        await run_in_threadpool(loose.add, name, data)

    got, bad = await run_in_threadpool(loose.take)
    collect(got)
    for name in bad:
        budget.release_file()
        skipped.append(f"{name}（不是有效的图片文件，已跳过）")
    # zip 那一路的结果在 extract_photo_zip 顶层就排空了；这里兜底扫一次，
    # 免得哪天 zip 打不开提前 return 时把照片留在 batcher 里没人收。
    got, bad = await run_in_threadpool(packed.take)
    collect(got)
    for name in bad:
        budget.release_file()
        budget.corrupt.append(name)

    if budget.stopped:
        rollback_and_413(budget.stop_reason)

    src_bytes = sum(s["src_bytes"] for s in saved)
    compressed_bytes = sum(s["out_bytes"] for s in saved)

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
              f"解压={budget.inflated/1024/1024:.1f}MB 嵌套zip={budget.nested_zips} "
              f"并行批={packed.parallel_batches + loose.parallel_batches}")

    report = None
    if meta.has_roster:
        rep.stage("重跑校验")
        report = await run_in_threadpool(_run_validation, meta)

    rep.finish(f"{len(saved)} 张已入库" + (f"，跳过 {len(skipped)} 个" if skipped else ""))

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
    _meta_or_404(pid)      # 同 upload_photos：别为一个不存在的 pid 建目录
    with progress.Reporter(pid, "生成星图", total=GENERATE_STAGES) as rep:
        out = generate_bundle(pid, req.form,
                              exclude_low_confidence=req.exclude_low_confidence,
                              school=req.school, engine=req.engine,
                              survey_date=req.survey_date, rep=rep)
        rep.finish(f"{out['cats']} 颗星 · 内嵌 {out['photos_embedded']} 张照片 · "
                   f"zip {out['zip_bytes'] / 1024 / 1024:.1f}MB")
        return out


@app.get("/api/projects/{pid}/progress")
def get_progress(pid: str) -> dict:
    """轮询长任务进度（上传压缩 / 生成打包）。

    故意不挂项目锁：progress.json 是原子替换写的，读到的一定是完整记录；
    更要紧的是，挂了锁它就会堵在自己要汇报的那个操作后面——上传正拿着锁，
    进度请求排在锁外面，前端于是什么也看不到，进度条彻底失去意义。
    """
    _meta_or_404(pid)
    return progress.snapshot_for_client(pid)


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


# ---------- 路由：F11 星图主题化 ----------

def _logo_url(pid: str) -> str:
    """带内容版本号：路径固定，不加版本浏览拿到的会是换徽章之前的那张。

    版本取内容哈希而非 mtime——Windows 粗时钟刻度内连传两张新徽章，
    mtime_ns 会一毫不差，版本号不变浏览器就永远吃旧缓存（本机实测踩过；
    Linux 的 ns 级时钟撞不出来，所以 CI 一直绿）。内容寻址还有个附带好处：
    重复传同一张图版本不变，缓存照样命中。
    """
    p = store.logo_path(pid)
    if not p.exists():
        return ""
    try:
        v = hashlib.md5(p.read_bytes()).hexdigest()[:12]
    except OSError:
        v = ""
    return f"/api/projects/{pid}/logo?v={v}"


def _theme_payload(pid: str, rejected: Optional[list[str]] = None) -> dict:
    th = store.load_theme(pid)
    return {
        "theme": asdict(th),
        # 生效色由服务端算好再给。前端要是自己按预设调色板拼，就等于把配色白名单
        # 在 JS 里又实现了一遍——两处规则迟早对不上，而这里给的正是产物里会用的值。
        "effective_colors": th.effective_colors(),
        "has_logo": store.logo_path(pid).exists(),
        "logo_url": _logo_url(pid),
        "rejected": rejected or [],
        "note": "主题改动只影响之后生成的产物——改完要重新点「生成星图」。",
    }


@app.get("/api/theme/options")
def theme_options() -> dict:
    """预设 / 字体 / 可改颜色项的清单。与项目无关，做成全局端点。"""
    return {
        "presets": preset_menu(),
        "fonts": font_menu(),
        "colors": color_menu(),
        "max_signature": MAX_SIGNATURE,
        "default_preset": DEFAULT_PRESET,
    }


@app.get("/api/projects/{pid}/theme")
def get_theme(pid: str) -> dict:
    _meta_or_404(pid)
    return _theme_payload(pid)


@app.put("/api/projects/{pid}/theme", dependencies=[_GUARD])
def put_theme(pid: str, req: ThemeUpdateRequest) -> dict:
    """改主题。字段留 None 表示「这项不动」，前端只发用户碰过的。

    非法值不报错，而是丢掉并在 rejected 里说明——一个手改坏的 theme.json
    不该让整个项目生成不了。
    """
    meta = _meta_or_404(pid)

    if req.reset:
        store.clear_theme(pid)
        store.log(meta, "重置星图主题", "回到默认预设（校徽保留，要删请单独删）")
        return _theme_payload(pid)

    cur = store.load_theme(pid)
    draft = Theme(
        preset=cur.preset if req.preset is None else req.preset,
        colors=dict(cur.colors) if req.colors is None else dict(req.colors),
        title_font=cur.title_font if req.title_font is None else req.title_font,
        body_font=cur.body_font if req.body_font is None else req.body_font,
        footer_signature=(cur.footer_signature if req.footer_signature is None
                          else req.footer_signature),
    )
    rejected = draft.rejected()          # 归一化之前问，不然就查不出丢了什么
    saved = store.save_theme(pid, draft)
    store.log(meta, "修改星图主题",
              f"预设={saved.preset} 自定义色={len(saved.colors)} "
              f"标题字体={saved.title_font} 正文字体={saved.body_font} "
              f"署名={len(saved.footer_signature)}字 丢弃={len(rejected)}")
    return _theme_payload(pid, rejected)


@app.delete("/api/projects/{pid}/theme", dependencies=[_GUARD])
def delete_theme(pid: str) -> dict:
    meta = _meta_or_404(pid)
    cleared = store.clear_theme(pid)
    if cleared:
        store.log(meta, "重置星图主题", "回到默认预设")
    return _theme_payload(pid)


@app.post("/api/projects/{pid}/logo", dependencies=[_GUARD])
async def upload_logo(pid: str, file: UploadFile = File(...)) -> dict:
    """上传校徽。一律重编码成 256px 以内的 PNG。"""
    meta = _meta_or_404(pid)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in LOGO_EXTS:
        raise HTTPException(400, f"校徽只收 {'、'.join(sorted(LOGO_EXTS))}。"
                                 f"SVG 能带 <script>，产物要挂到学校公众号上，不收。")

    data = await file.read()
    if not data:
        raise HTTPException(400, "上传的校徽是空文件")
    if len(data) > MAX_LOGO_UPLOAD_BYTES:
        raise HTTPException(413, f"校徽原图 {len(data) / 1024 / 1024:.1f}MB 超过 "
                                 f"{MAX_LOGO_UPLOAD_BYTES // 1024 // 1024}MB 上限，"
                                 f"请先导出成小一点的位图")
    if not image_proc.looks_like_image(data):
        raise HTTPException(400, "校徽不是有效图片")

    # Pillow 解码挪到线程池：一张 4MB 的 PNG 解起来能在事件循环上坐几百毫秒，
    # 那段时间进度接口一个请求都答不上来（照片入库踩过同一个坑）。
    try:
        blob, info = await run_in_threadpool(image_proc.process_logo, data)
    except Exception:
        raise HTTPException(400, "校徽解不开，换一张试试") from None

    store.save_logo(pid, blob)
    store.log(meta, "上传校徽",
              f"{info['src_width']}×{info['src_height']} → "
              f"{info['width']}×{info['height']} {info['out_bytes'] / 1024:.0f}KB")
    return {"ok": True, "width": info["width"], "height": info["height"],
            "bytes": info["out_bytes"], "resized": info["resized"],
            "url": _logo_url(pid), **_theme_payload(pid)}


@app.get("/api/projects/{pid}/logo")
def get_logo(pid: str) -> Response:
    """校徽原字节，给控制台预览用。读路由不挂锁。"""
    _meta_or_404(pid)
    blob = store.load_logo(pid)
    if blob is None:
        raise HTTPException(404, "还没有上传校徽")
    return Response(content=blob, media_type="image/png")


@app.delete("/api/projects/{pid}/logo", dependencies=[_GUARD])
def delete_logo(pid: str) -> dict:
    meta = _meta_or_404(pid)
    removed = store.clear_logo(pid)
    if removed:
        store.log(meta, "删除校徽", "顶栏不再显示徽章")
    return _theme_payload(pid)


# ---------- 路由：F9 归并工作台 ----------

@app.get("/api/projects/{pid}/merge")
def get_merge(pid: str) -> dict:
    """疑似重复建档的候选组 + 已有判定，供工作台左右并排看图。

    组内成员按「长得像不像」排过序，每张卡片还标着组里最像它的那一位——人从左边
    第一对看起就行，不用把组内两两都比一遍。
    """
    meta = _meta_or_404(pid)
    report = _load_report(pid) or _run_validation(meta)
    groups = merge.candidate_groups(report.rows)
    book = store.load_merge(pid)
    hash_of = _photo_hasher(pid)
    for g in groups:
        merge.annotate_visual(g, hash_of)
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
            "hashed_groups": sum(1 for g in groups if g["visual_hashed"] >= 2),
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


def _read_roster_rows(p: Path) -> list[list[str]]:
    text, _enc = decode_bytes(retry_read_bytes(p))
    return [r for r in csv.reader(io.StringIO(text))]


def _save_roster(pid: str, meta: ProjectMeta, raw: list[list[str]],
                 action: str, detail: str) -> None:
    """名册的唯一落盘出口：两份拷贝一起原子换，BOM 与 LF 都由这里保证。

    改单元格、增行、删行走同一个出口，才不会出现「roster.csv 改了、随包的
    data/猫咪名册.csv 还是旧的」这种半新半旧的交付物。
    """
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(raw)
    data = buf.getvalue().encode("utf-8-sig")
    atomic_write_bytes(store.project_dir(pid) / "roster.csv", data)
    atomic_write_bytes(store.project_dir(pid) / "data" / "猫咪名册.csv", data)
    store.log(meta, action, detail)


@app.patch("/api/projects/{pid}/roster", dependencies=[_GUARD])
def patch_roster(pid: str, req: RosterPatch) -> dict:
    """按「物理行号 + 标准列名」改单元格，回写 CSV 并自动重跑校验。"""
    meta = _meta_or_404(pid)
    p = store.project_dir(pid) / "roster.csv"
    if not p.exists():
        raise HTTPException(404, "尚未上传名册 CSV")
    if not req.edits:
        raise HTTPException(400, "没有要应用的改动")

    raw = _read_roster_rows(p)
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
        _save_roster(pid, meta, raw, "在线编辑名册",
                     f"改动 {len(applied)} 处，拒绝 {len(rejected)} 处："
                     + "；".join(f"行{a['line']}·{a['field']}" for a in applied[:6]))
        if req.revalidate:
            report = _run_validation(meta)

    return {"applied": applied, "rejected": rejected,
            "report": report.model_dump() if report else None}


@app.post("/api/projects/{pid}/roster/rows", dependencies=[_GUARD])
def insert_roster_row(pid: str, req: RosterRowInsert) -> dict:
    """在第 `after` 行之后插一行（表头是第 1 行，所以 after=1 就是插到最前面）。

    没给的列留空；名册里没有的列进 `ignored`，但行照样插——用户想加的猫
    不该因为一个手滑的列名就加不进来。
    """
    meta = _meta_or_404(pid)
    p = store.project_dir(pid) / "roster.csv"
    if not p.exists():
        raise HTTPException(404, "尚未上传名册 CSV")
    raw = _read_roster_rows(p)
    if not raw:
        raise HTTPException(400, "名册为空，无从插入")
    if req.after < 1 or req.after > len(raw):
        raise HTTPException(400, f"行号越界（名册只有 {len(raw)} 行，表头是第 1 行，"
                                 f"after 要在 1~{len(raw)} 之间）")
    mapping, _missing, _unknown = build_column_map(raw[0])

    width = max([len(raw[0])] + [i + 1 for i in mapping.values()])
    row = [""] * width
    ignored: list[dict] = []
    for field, value in req.values.items():
        if field not in mapping:
            ignored.append({"field": field, "why": f"名册没有「{field}」这一列"})
            continue
        row[mapping[field]] = (value or "").strip()
    raw.insert(req.after, row)

    id_idx = mapping.get("编号")
    label = row[id_idx].strip() if id_idx is not None else ""
    _save_roster(pid, meta, raw, "名册增行",
                 f"在第 {req.after} 行后插入新行（编号 {label or '未填'}，"
                 f"填了 {len(req.values) - len(ignored)} 列，忽略 {len(ignored)} 列）")
    report = _run_validation(meta) if req.revalidate else None
    return {"line": req.after + 1, "row": row, "ignored": ignored,
            "report": report.model_dump() if report else None}


@app.delete("/api/projects/{pid}/roster/rows/{line}", dependencies=[_GUARD])
def delete_roster_row(pid: str, line: int, revalidate: bool = True) -> dict:
    """删掉一个物理行。表头（第 1 行）不许删——删了名册就不是名册了。

    只删名册里这一行，**不动已入库的照片**：那只猫的代表照片会变成
    「未使用照片」提示，这是事实，不该由删除动作顺手抹掉。
    """
    meta = _meta_or_404(pid)
    p = store.project_dir(pid) / "roster.csv"
    if not p.exists():
        raise HTTPException(404, "尚未上传名册 CSV")
    raw = _read_roster_rows(p)
    if not raw:
        raise HTTPException(400, "名册为空，无从删除")
    if line < 2 or line > len(raw):
        raise HTTPException(400, f"行号越界（名册只有 {len(raw)} 行，表头是第 1 行，"
                                 f"可删范围是 2~{len(raw)}）")
    mapping, _missing, _unknown = build_column_map(raw[0])
    removed = raw.pop(line - 1)

    id_idx = mapping.get("编号")
    label = removed[id_idx].strip() if id_idx is not None and len(removed) > id_idx else ""
    _save_roster(pid, meta, raw, "名册删行",
                 f"删除第 {line} 行（编号 {label or '空缺'}），名册余 {len(raw) - 1} 行数据")
    report = _run_validation(meta) if revalidate else None
    return {"deleted": {"line": line, "row": removed},
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
