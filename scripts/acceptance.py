"""验收脚本 · 对应 SPEC §7 的两个验收场景。

    python scripts/acceptance.py --case demo2      # 空数据包骨架 + 2 行示例 → F1-F5 全流程
    python scripts/acceptance.py --case full76     # 真实名册 76 只全量回归
    python scripts/acceptance.py --case all

走真实 HTTP 接口（进程内 TestClient），产物落在 workspace/，证据写入 docs/。E 盘全程只读。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import config, csv_loader  # noqa: E402
from app.main import app  # noqa: E402

E_ROOT = Path(r"E:\猫咪星图_总库")
SKELETON = E_ROOT / "12_跨校复制包" / "示例校_空数据包骨架"
REAL_ROSTER = E_ROOT / "07_普查原始数据" / "猫咪名册.csv"
PHOTO_DIRS = [E_ROOT / "08_原始照片视频" / "照片视频",
              E_ROOT / "08_原始照片视频" / "补充视频照片"]
DOCS = ROOT / "docs"


class Evidence:
    """把每一步的实测结果攒成 Markdown，作为交付报告的验收证据。"""

    def __init__(self, title: str):
        self.title = title
        self.rows: list[tuple[str, str]] = []
        self.failed: list[str] = []

    def check(self, step: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((step, ("✅ " if ok else "❌ ") + detail))
        if not ok:
            self.failed.append(f"{step}: {detail}")
        print(f"[{'PASS' if ok else 'FAIL'}] {step} — {detail}")
        return ok

    def note(self, step: str, detail: str) -> None:
        self.rows.append((step, detail))
        print(f"[ .. ] {step} — {detail}")

    def markdown(self) -> str:
        lines = [f"# {self.title}", "",
                 f"- 执行日期：{date.today().isoformat()}",
                 f"- 结论：{'全部通过' if not self.failed else f'{len(self.failed)} 项未通过'}",
                 "", "| 步骤 | 实测结果 |", "|---|---|"]
        lines += [f"| {s} | {d} |" for s, d in self.rows]
        if self.failed:
            lines += ["", "## 未通过项", ""] + [f"- {f}" for f in self.failed]
        return "\n".join(lines) + "\n"

    def save(self, name: str) -> Path:
        DOCS.mkdir(parents=True, exist_ok=True)
        path = DOCS / name
        path.write_text(self.markdown(), encoding="utf-8")
        return path


def cats_from_html(html: str) -> list[dict]:
    """从生成的星图 HTML 里反解 CATS 数组——验证产物本身，而不是验证代码。"""
    m = re.search(r"const CATS = (\[.*?\]);\s*\nconst CALIB", html, re.S)
    if not m:
        raise AssertionError("生成的 HTML 里找不到 CATS 数组")
    return json.loads(m.group(1))


def make_photo(width: int, height: int, seed: int) -> bytes:
    img = Image.new("RGB", (width, height), (18, 24, 48))
    px = img.load()
    for y in range(height):
        for x in range(0, width, 3):
            px[x, y] = ((x * 7 + seed * 31) % 255, (y * 5 + seed * 17) % 255,
                        ((x + y) * 3 + seed) % 255)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def build_skeleton_csv() -> tuple[str, list[dict], str]:
    """空数据包骨架的表头 + 2 行示例数据，顺带证明骨架的 12 列与本工具完全对齐。"""
    sample_rows = list(csv.DictReader(io.StringIO(csv_loader.empty_template())))
    tpl = SKELETON / "data" / "猫咪名册_模板.csv"
    if tpl.exists():
        header = next(r for r in csv.reader(io.StringIO(read_text(tpl))) if r)
        source = f"表头取自 {tpl.name}（{len(header)} 列）"
    else:
        header = list(config.ROSTER_COLUMNS)
        source = "E 盘不可读，表头回退到内置 12 列"
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=header, extrasaction="ignore")
    w.writeheader()
    for r in sample_rows:
        w.writerow({k: r.get(k, "") for k in header})
    return out.getvalue(), sample_rows, source


def find_real_photos(names: list[str]) -> tuple[dict[str, Path], list[str]]:
    """按 basename 在 E 盘照片库里定位真实照片（名册里有的带 `照片视频/` 前缀）。"""
    index: dict[str, Path] = {}
    for d in PHOTO_DIRS:
        if d.exists():
            for p in d.iterdir():
                if p.is_file():
                    index.setdefault(p.name.lower(), p)
    found: dict[str, Path] = {}
    missing: list[str] = []
    for n in names:
        base = n.replace("\\", "/").split("/")[-1]
        hit = index.get(base.lower())
        if hit:
            found[base] = hit
        else:
            missing.append(base)
    return found, missing


def photo_sizes_on_disk(pid: str) -> tuple[int, int]:
    """返回 (张数, 最大单张字节)，直接量工作区里的成品，验证 200KB 红线。"""
    d = config.ensure_dirs(pid)["photos"]
    sizes = [p.stat().st_size for p in d.iterdir() if p.is_file()]
    return len(sizes), max(sizes, default=0)


def verify_zip_contents(ev: Evidence, zf: zipfile.ZipFile, cats: list[dict],
                        form: str) -> None:
    names = set(zf.namelist())
    htmls = [n for n in names if n.endswith(".html")]
    ev.check("zip 内含星图 HTML", len(htmls) == 1, str(htmls))
    photos = sorted(n for n in names if n.startswith("assets/photos/"))

    if form == "relative":
        missing = [c["photo"] for c in cats if c["photo"] not in names]
        ev.check("每只猫的照片都在 zip 里（相对路径可解析）", not missing,
                 f"照片 {len(photos)} 张 / CATS {len(cats)} 条，缺失 {len(missing)}"
                 + (f"：{missing[:3]}" if missing else ""))
        ev.check("zip 内含底图", "assets/map.jpg" in names, "assets/map.jpg")
    else:
        chunks = sorted(n for n in names if re.fullmatch(r"assets/photo-data-\d+\.js", n))
        blob = "".join(zf.read(c).decode("utf-8") for c in chunks)
        missing = [Path(c["photo"]).name for c in cats
                   if f'"{Path(c["photo"]).name}"' not in blob]
        ev.check("每只猫的照片都已 base64 内嵌", not missing,
                 f"分片 {len(chunks)} 个 / CATS {len(cats)} 条，未内嵌 {len(missing)}"
                 + (f"：{missing[:3]}" if missing else ""))
        ev.check("底图也已内嵌", '"map.jpg"' in blob, "map.jpg")
        ev.check("内嵌形态不含照片目录", not photos, f"assets/photos 条目 {len(photos)} 个")
        html = zf.read(htmls[0]).decode("utf-8")
        refs = re.findall(r'<script src="([^"]+)"', html)
        unresolved = [r for r in refs if r not in names]
        # 内嵌形态下 CATS.photo 仍是 assets/photos/xxx.jpg，但它只是 PH() 查 base64 的键，
        # 不构成文件依赖；真正会断链的是 <img src> 与 CSS url()。
        img_refs = re.findall(r'<img[^>]+src="([^"]+)"', html)
        img_refs += re.findall(r'url\((?!["\']?data:)([^)"\']+)\)', html)
        ev.check("解压后双击即开（不依赖任何外部图片文件）",
                 bool(refs) and not unresolved and not img_refs,
                 f"HTML 仅引用同目录 JS {len(refs)} 个且全部在包内；"
                 f"图片文件引用 {len(img_refs)} 个，照片与底图均为 base64")

    for extra in ("data/猫咪名册.csv", "校验报告.md", "归并决策摘要.md"):
        ev.check(f"zip 内含 {extra}", extra in names, extra)


CALIB_XY = {"x": 0.147, "y": 0.258}


def verify_calib(client: TestClient, ev: Evidence, pid: str, n_cats: int) -> None:
    """F8 · 底图标定：人工星位要能存下来、烘焙进产物，并能一键退回算法推导。"""
    r = client.get(f"/api/projects/{pid}/calib")
    if not ev.check("F8 读取星位标定", r.status_code == 200,
                    "" if r.status_code == 200 else r.text[:200]):
        return
    j = r.json()
    ids = j["ids"][:2]
    s = j["stats"]
    ev.check("F8 初始状态为算法推导",
             j["source"] == "derived" and s["manual"] == 0 and s["total"] == n_cats,
             f"来源 {j['source']}，{s['manual']}/{s['total']} 人工标定，"
             f"底图尺寸 {j['map_size'] or '未上传（用兜底 1920×1239）'}")
    if not ids:
        ev.check("F8 名册里有可标定的编号", False, "ids 为空")
        return

    manual = {ids[0]: CALIB_XY}
    if len(ids) > 1:
        manual[ids[1]] = {"x": 0.803, "y": 0.611}
    r = client.put(f"/api/projects/{pid}/calib",
                   json={"positions": manual, "note": "验收：人工标定 2 颗星"})
    j = r.json() if r.status_code == 200 else {}
    ev.check("F8 保存人工标定", r.status_code == 200 and j.get("saved") == len(manual),
             f"本次 {j.get('saved')} 颗，累计 {j.get('total')} 颗，"
             f"忽略未知编号 {j.get('ignored_unknown_ids') or '无'}")

    r = client.get(f"/api/projects/{pid}/calib")
    j = r.json()
    ev.check("F8 复读来源变为 manual",
             j["source"] == "manual" and j["stats"]["manual"] == len(manual)
             and j["note"] == "验收：人工标定 2 颗星",
             f"来源 {j['source']}，人工 {j['stats']['manual']} / 推导 {j['stats']['derived']}"
             f"，备注「{j['note']}」")

    r = client.put(f"/api/projects/{pid}/calib",
                   json={"positions": {ids[0]: {"x": 1.7, "y": -0.2}}})
    ok = r.status_code == 400
    ev.check("F8 越界坐标被拒（必须是 0~1 归一化值）", ok,
             f"HTTP {r.status_code}" + (f"：{r.json().get('detail', '')[:60]}" if ok else ""))
    r2 = client.get(f"/api/projects/{pid}/calib")
    ev.check("F8 被拒的写入没有污染已存标定",
             r2.json()["stats"]["manual"] == len(manual),
             f"仍是人工 {r2.json()['stats']['manual']} 颗")

    r = client.post(f"/api/projects/{pid}/generate", json={"form": "relative"})
    d = r.json()
    ev.check("F8 标定后重新生成", r.status_code == 200 and d["calib"]["manual"] == len(manual),
             f"烘焙人工星位 {d['calib']['manual']}/{d['calib']['total']} 颗")
    html = client.get(d["preview_url"]).text
    baked = f'"{ids[0]}":{{"x":{CALIB_XY["x"]},"y":{CALIB_XY["y"]}}}'
    ev.check("F8 人工坐标已烘焙进产物 CALIB", baked in html,
             f"产物里找到 {baked}" if baked in html else f"产物里找不到 {baked}")
    ev.check("F8 产物页脚注明标定比例",
             f"星位人工标定 {len(manual)}/{d['calib']['total']}" in html,
             f"星位人工标定 {len(manual)}/{d['calib']['total']}")

    r = client.delete(f"/api/projects/{pid}/calib")
    back = client.get(f"/api/projects/{pid}/calib").json()
    ev.check("F8 清除标定后回到算法推导",
             r.status_code == 200 and r.json()["cleared"] is True
             and back["source"] == "derived" and back["stats"]["manual"] == 0,
             f"cleared={r.json().get('cleared')}，来源 {back['source']}，"
             f"人工 {back['stats']['manual']} 颗")


def run_flow(client: TestClient, ev: Evidence, *, school: str, subtitle: str,
             roster_text: str, photos: list[tuple[str, bytes]], expect_cats: int,
             forms: tuple[str, ...] = ("relative", "inline")) -> str:
    r = client.post("/api/projects", json={"school": school, "subtitle": subtitle})
    ev.check("F1 建项目", r.status_code == 200, f"{school} → {r.json().get('id')}")
    pid = r.json()["id"]

    r = client.post(f"/api/projects/{pid}/roster",
                    files={"file": ("猫咪名册.csv", roster_text.encode("utf-8-sig"),
                                    "text/csv")})
    ok = r.status_code == 200
    if ok:
        j = r.json()
        detail = (f"编码 {j['encoding']}，解析出 {j['rows']} 行，"
                  f"缺列 {j['missing_columns'] or '无'}，未识别列 {j['unknown_columns'] or '无'}")
    else:
        detail = r.text[:200]
    ev.check("F1 上传名册 CSV", ok, detail)

    files = [("files", (n, b, "image/jpeg")) for n, b in photos]
    r = client.post(f"/api/projects/{pid}/photos", files=files)
    j = r.json()
    count, biggest = photo_sizes_on_disk(pid)
    ev.check("F1 上传照片", r.status_code == 200 and j["saved"] == len(photos),
             f"入库 {j['saved']} 张，跳过 {j['skipped'] or '无'}")
    ev.check("照片压缩达标（长边≤1200px、单张≤200KB）",
             j["max_side"] <= config.MAX_PHOTO_SIDE
             and biggest <= config.MAX_PHOTO_BYTES,
             f"最大长边 {j['max_side']}px，最大单张 {biggest // 1024}KB；"
             f"体积 {j['src_bytes'] // 1024}KB → {j['out_bytes'] // 1024}KB，工作区 {count} 张")

    r = client.post(f"/api/projects/{pid}/validate")
    rep = r.json()
    s = rep["summary"]
    ev.check("F2 校验通过", s["ok"] is True,
             f"{s['total_rows']} 行 / 可入图 {s['valid_rows']} 行 / "
             f"错误 {s['error_count']} 警告 {s['warning_count']} 提示 {s['info_count']}")
    by_code: dict[str, int] = {}
    for i in rep["issues"]:
        by_code[i["code"]] = by_code.get(i["code"], 0) + 1
    ev.note("F2 问题分布", ", ".join(f"{k}×{v}" for k, v in sorted(by_code.items())) or "无")
    ev.note("F2 编号范围", f"{s['id_min']} ~ {s['id_max']}，空缺 {s['id_gaps'] or '无'}，"
                          f"低置信度 {s['low_confidence'] or '无'}")
    r = client.get(f"/api/projects/{pid}/report.md")
    ev.check("F2 校验报告可下载", r.status_code == 200 and "校验报告" in r.text,
             f"{len(r.text)} 字符 Markdown")

    for form in forms:
        r = client.post(f"/api/projects/{pid}/generate", json={"form": form})
        if not ev.check(f"F3+F5 生成并打包（{form}）", r.status_code == 200,
                        "" if r.status_code == 200 else r.text[:200]):
            continue
        d = r.json()
        ev.check(f"F3 星图数据完整（{form}）",
                 d["cats"] == expect_cats and not d["missing_photos"],
                 f"CATS {d['cats']} 条（期望 {expect_cats}），照片 {d['photos_embedded']} 张，"
                 f"缺失 {len(d['missing_photos'])}")
        ev.check(f"F5 zip 命名规范（{form}）",
                 re.fullmatch(r".+-校园猫咪星图-\d{4}-\d{2}-\d{2}\.zip", d["zip_name"])
                 is not None, f"{d['zip_name']}（{d['zip_bytes'] // 1024}KB）")

        pv = client.get(f"/api/projects/{pid}/preview?form={form}")
        page = client.get(d["preview_url"])
        ev.check(f"F4 在线预览（{form}）",
                 pv.status_code == 200 and page.status_code == 200 and "CATS" in page.text,
                 f"{d['preview_url']} → HTTP {page.status_code}，{len(page.text)} 字符")

        dl = client.get(f"/api/projects/{pid}/download?form={form}")
        zf = zipfile.ZipFile(io.BytesIO(dl.content))
        cats = cats_from_html(zf.read(d["html_name"]).decode("utf-8"))
        ev.check(f"F3 注入结果可从产物反解（{form}）", len(cats) == expect_cats,
                 f"HTML 里解出 {len(cats)} 条 CATS，首条键：{','.join(list(cats[0])[:8])}…")
        verify_zip_contents(ev, zf, cats, form)

    verify_calib(client, ev, pid, expect_cats)

    r = client.post(f"/api/projects/{pid}/summary")
    md = r.json().get("markdown", "")
    ev.check("F7 归并决策摘要生成", r.status_code == 200 and "数据总览" in md,
             f"{len(md)} 字符")
    return pid


def case_demo2(client: TestClient) -> Evidence:
    ev = Evidence("验收场景 1 · 空数据包骨架 + 2 行示例数据走通 F1-F5")
    roster_text, rows, source = build_skeleton_csv()
    ev.note("数据来源", source)
    ev.note("示例行", "; ".join(f"{r['编号']} {r['昵称']}（{r['毛色']}，{r['代表照片文件']}）"
                                for r in rows))
    photos = [(r["代表照片文件"], make_photo(1600, 1200, i + 1)) for i, r in enumerate(rows)]
    pid = run_flow(client, ev, school="示例校", subtitle="跨校复制包 · 空骨架自检",
                   roster_text=roster_text, photos=photos, expect_cats=len(rows))

    def parse_census(path: Path) -> dict:
        r = client.post("/api/census/parse",
                        files={"file": (path.name, path.read_bytes(), "text/plain")})
        return r.json() if r.status_code == 200 else {"_status": r.status_code,
                                                      "_text": r.text[:200]}

    batch = SKELETON / "普查" / "普查-batch01_模板.txt"
    if batch.exists():
        j = parse_census(batch)
        ev.check("F6 普查解析接受骨架模板", "_status" not in j,
                 f"{batch.name}：解析 {j.get('count', 0)} 条（空模板，0 条属预期），"
                 f"警告 {len(j.get('warnings', []))} 条")
    real_batch = E_ROOT / "07_普查原始数据" / "普查-batch1.txt"
    if real_batch.exists():
        j = parse_census(real_batch)
        recs = j.get("records", [])
        draft = j.get("draft_csv") or ""
        ev.check("F6 普查 batch → 名册草稿（真实 batch1）",
                 "_status" not in j and j.get("count", 0) > 0 and recs
                 and draft.startswith("编号,") and len(draft.splitlines()) - 1 == j["count"],
                 f"解析 {j.get('count', 0)} 条，警告 {len(j.get('warnings', []))} 条，"
                 f"归并建议 {len(j.get('suggestions', []))} 组，"
                 f"草稿 CSV {len(draft)} 字符 / {len(draft.splitlines()) - 1} 行；"
                 f"首条 {recs[0]['filename']} → {recs[0]['coat_main']}/{recs[0]['area']}"
                 if recs else "无记录")
    ev.note("工作区产物", json.dumps(client.get(f"/api/projects/{pid}/artifacts").json(),
                                    ensure_ascii=False)[:300])
    return ev


def case_full76(client: TestClient) -> Evidence:
    ev = Evidence("验收场景 2 · 真实名册 76 只全量回归")
    if not REAL_ROSTER.exists():
        ev.check("读取 E 盘真实名册", False, f"{REAL_ROSTER} 不存在（U 盘未挂载）")
        return ev
    roster_text = read_text(REAL_ROSTER)
    rows, missing_cols, unknown_cols, _header = csv_loader.parse_roster(roster_text)
    ev.note("名册来源", f"{REAL_ROSTER.name}，{len(rows)} 行，"
                       f"缺列 {missing_cols or '无'}，未识别列 {unknown_cols or '无'}")

    raw_names = sorted({r.photo_file.strip() for r in rows if r.photo_file.strip()})
    prefixed = [n for n in raw_names if "/" in n or "\\" in n]
    ev.note("代表照片写法", f"{len(raw_names)} 个唯一值，其中 {len(prefixed)} 个带目录前缀"
                          f"（如 {prefixed[0] if prefixed else '—'}）")
    found, missing = find_real_photos(raw_names)
    if not ev.check("在 E 盘照片库定位真实照片", not missing,
                    f"命中 {len(found)} / {len(raw_names)}"
                    + (f"，缺失 {missing[:3]}" if missing else "")):
        return ev

    photos = [(name, p.read_bytes()) for name, p in sorted(found.items())]
    ev.note("上传体积", f"{len(photos)} 张原图共 {sum(len(b) for _, b in photos) / 1048576:.1f}MB")
    run_flow(client, ev, school="中北大学", subtitle="76 只校园猫 · 全量回归",
             roster_text=roster_text, photos=photos, expect_cats=len(rows))
    return ev


def main() -> int:
    ap = argparse.ArgumentParser(description="喵星图工厂验收脚本")
    ap.add_argument("--case", choices=("demo2", "full76", "all"), default="all")
    args = ap.parse_args()

    client = TestClient(app)
    cases = [("demo2", case_demo2, "验收证据-场景1-空骨架2行.md"),
             ("full76", case_full76, "验收证据-场景2-全量76只.md")]
    picked = cases if args.case == "all" else [c for c in cases if c[0] == args.case]

    exit_code = 0
    for _key, fn, name in picked:
        ev = fn(client)
        path = ev.save(name)
        print(f"\n证据已写入 {path}\n")
        exit_code |= 1 if ev.failed else 0
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
