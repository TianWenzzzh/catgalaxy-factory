#!/usr/bin/env python3
"""T6 F5/F6 终极字节对齐验证：schools/nuc 数据包直灌工厂 v29 渲染器，
与 meow-starmap 托管产物（nuc/index.html + assets/*.js）逐字节比对。

照片走 prepare_package_jpeg（保真直通策略，与 16 仓 build.py prepare_jpeg
同口径），人工数据（override/calib/areas/consts/stars/build_meta）全部按包
注入。比对：HTML sha256 + 各分片 sha256 + __PM 清单。

退出码 0 = 工厂渲染器与 16 仓构建器对同一数据包输出逐字节一致。
证据写入 docs/验收证据-F5-数据包字节对齐.md。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import acceptance as A  # noqa: E402
from app import image_proc  # noqa: E402
from app import starmap_render as sr  # noqa: E402

PKG = A.E_ROOT / "16_开源模板_meow-starmap" / "schools" / "nuc"
HOSTED = A.E_ROOT / "16_开源模板_meow-starmap" / "nuc"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    meta = json.loads((PKG / "data" / "build_meta.json").read_text("utf-8"))
    overrides = json.loads(
        (PKG / "data" / "cats_override.json").read_text("utf-8"))
    calib = json.loads((PKG / "data" / "calib.json").read_text("utf-8"))
    areas = json.loads((PKG / "data" / "areas.json").read_text("utf-8"))
    area_keys = json.loads((PKG / "data" / "area_keys.json").read_text("utf-8"))
    rel = json.loads((PKG / "data" / "relations.json").read_text("utf-8"))
    consts = json.loads((PKG / "data" / "constellations.json").read_text("utf-8"))
    stars = json.loads((PKG / "data" / "poster_stars.json").read_text("utf-8"))

    # 照片：保真直通压缩（与 16 仓 build 同口径）
    blobs: dict[str, bytes] = {}
    recompressed = 0
    for entry in overrides:
        name = entry["photo"]
        if name in blobs:
            continue
        raw = (PKG / "photos" / name).read_bytes()
        out, changed = image_proc.prepare_package_jpeg(
            raw, max_side=1200, max_bytes=200 * 1024, is_jpeg=True)
        blobs[name] = out
        recompressed += int(changed)
    map_path = next((PKG / "map").glob("*.jp*g"))
    map_key = map_path.name
    map_bytes, map_changed = image_proc.prepare_package_jpeg(
        map_path.read_bytes(), max_side=1920, max_bytes=None, is_jpeg=True)

    cats = [{**e, "photo": "assets/photos/" + e["photo"]} for e in overrides]

    inp = sr.V29RenderInput(
        school=meta["school"], cats=cats, photo_sizes={k: len(b) for k, b in blobs.items()},
        map_bytes=map_bytes, map_key=map_key, calib=calib,
        photo_loading="lazy",
        product=meta["product"], en=meta["en"], version=meta["version"],
        tagline=meta["tagline"], survey_date=meta["survey_date"],
        photo_total=meta["survey_photo_total"], ls_prefix=meta["ls_prefix"],
        repo_url=meta["repo_url"], docs_ref=meta["docs_ref"],
        milestones=meta["milestones"], pass_titles=meta["pass_titles"],
        king_name=meta["king_name"], poster_title=meta["poster_title"],
        poster_file=meta["poster_file"], export_map_name=meta["export_map_name"],
        skill_foot_line=meta["skill_foot_line"], soul_line=meta["soul_line"],
        calib_note=meta["calib_note"],
        areas=areas, area_keys=area_keys, rel=rel, consts=consts,
        poster_stars=stars)
    bundle = sr.render(inp)

    failures: list[str] = []
    lines = ["# T6 F5/F6 · schools/nuc 数据包 × 工厂 v29 渲染器 字节对齐", "",
             f"- 数据包：`{PKG}`（{len(overrides)} 猫 / {len(blobs)} 图，"
             f"重编码 {recompressed} 张 / 底图{'重编码' if map_changed else '直通'}）",
             f"- 对照物：meow-starmap 托管 `nuc/index.html` + `nuc/assets/photo-data-*.js`",
             ""]

    hosted_html = sha(HOSTED / "index.html")
    built_html = hashlib.sha256(bundle.html.encode("utf-8")).hexdigest()
    html_ok = built_html == hosted_html
    lines.append(f"- HTML：{'✅ 逐字节一致' if html_ok else '❌ 不一致'}　"
                 f"工厂 {built_html[:16]}… / 托管 {hosted_html[:16]}…")
    if not html_ok:
        failures.append("HTML 字节不一致")
        # 找第一个差异位置帮助定位
        hosted_bytes = (HOSTED / "index.html").read_bytes()
        built = bundle.html.encode("utf-8")
        for i, (a, b) in enumerate(zip(hosted_bytes, built)):
            if a != b:
                lo = max(0, i - 80)
                lines.append(f"  - 首个差异 @ {i}：托管 "
                             f"`{hosted_bytes[lo:i+80].decode('utf-8', 'replace')}` vs "
                             f"工厂 `{built[lo:i+80].decode('utf-8', 'replace')}`")
                break
        lines.append(f"  - 长度：托管 {len(hosted_bytes)} / 工厂 {len(built)}")

    chunks = bundle.iter_chunks(lambda k: map_bytes if k == map_key else blobs[k])
    hosted_js = {p.name: p for p in (HOSTED / "assets").glob("photo-data-*.js")}
    built_js = {n: hashlib.sha256(t.encode("utf-8")).hexdigest()
                for n, t in chunks}
    lines.append(f"- 分片清单：托管 {sorted(hosted_js)} / 工厂 {sorted(built_js)}")
    if set(built_js) != set(hosted_js):
        failures.append("分片文件清单不一致")
    for n in sorted(set(built_js) & set(hosted_js)):
        same = built_js[n] == sha(hosted_js[n])
        lines.append(f"- {n}：{'✅ 一致' if same else '❌ 不一致'}")
        if not same:
            failures.append(f"{n} 字节不一致")
    lines.append("")

    verdict = "通过" if not failures else f"{len(failures)} 项失败"
    lines.insert(2, f"- 结论：**{verdict}**")
    if failures:
        lines += ["## 失败清单"] + [f"- {x}" for x in failures]

    evidence = ROOT / "docs" / "验收证据-F5-数据包字节对齐.md"
    evidence.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": not failures, "failures": failures,
                      "evidence": str(evidence)}, ensure_ascii=False, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
