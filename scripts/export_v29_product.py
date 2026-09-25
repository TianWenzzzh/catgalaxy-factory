#!/usr/bin/env python3
"""T6 F6：用工厂 v29 渲染器 + schools/nuc 数据包产出正式发布产物。

与 parity_nuc_package.py 同一套构建（输出与 meow-starmap 托管 nuc/ 逐字节
一致，见 docs/验收证据-F5-数据包字节对齐.md），只是把产物**写到磁盘**：
  <out>/中北喵星图.html + <out>/assets/photo-data-*.js
用法：python scripts/export_v29_product.py [--out 目录]（缺省打印到 %TEMP%）
"""
from __future__ import annotations

import argparse
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


def build_bundle():
    meta = json.loads((PKG / "data" / "build_meta.json").read_text("utf-8"))
    overrides = json.loads(
        (PKG / "data" / "cats_override.json").read_text("utf-8"))
    blobs: dict[str, bytes] = {}
    for entry in overrides:
        name = entry["photo"]
        if name in blobs:
            continue
        out, _ = image_proc.prepare_package_jpeg(
            (PKG / "photos" / name).read_bytes(),
            max_side=1200, max_bytes=200 * 1024, is_jpeg=True)
        blobs[name] = out
    map_path = next((PKG / "map").glob("*.jp*g"))
    map_bytes, _ = image_proc.prepare_package_jpeg(
        map_path.read_bytes(), max_side=1920, max_bytes=None, is_jpeg=True)

    inp = sr.V29RenderInput(
        school=meta["school"],
        cats=[{**e, "photo": "assets/photos/" + e["photo"]} for e in overrides],
        photo_sizes={k: len(b) for k, b in blobs.items()},
        map_bytes=map_bytes, map_key=map_path.name,
        calib=json.loads((PKG / "data" / "calib.json").read_text("utf-8")),
        photo_loading="lazy",
        product=meta["product"], en=meta["en"], version=meta["version"],
        tagline=meta["tagline"], survey_date=meta["survey_date"],
        photo_total=meta["survey_photo_total"], ls_prefix=meta["ls_prefix"],
        repo_url=meta["repo_url"], docs_ref=meta["docs_ref"],
        milestones=meta["milestones"], pass_titles=meta["pass_titles"],
        king_name=meta["king_name"], poster_title=meta["poster_title"],
        poster_file=meta["poster_file"], export_map_name=meta["export_map_name"],
        skill_foot_line=meta["skill_foot_line"], soul_line=meta["soul_line"],
        calib_note=meta.get("calib_note"),        # v34 起 nuc 不带该键
        star_box=meta.get("star_box"),            # v34：建成区框随 build_meta 下发
        star_zone=meta.get("star_zone"),          # v35：多边形撒点区随 build_meta 下发
        areas=json.loads((PKG / "data" / "areas.json").read_text("utf-8")),
        area_keys=json.loads((PKG / "data" / "area_keys.json").read_text("utf-8")),
        rel=json.loads((PKG / "data" / "relations.json").read_text("utf-8")),
        consts=json.loads((PKG / "data" / "constellations.json").read_text("utf-8")),
        poster_stars=json.loads((PKG / "data" / "poster_stars.json").read_text("utf-8")))
    bundle = sr.render(inp)
    read = lambda k: map_bytes if k == map_path.name else blobs[k]  # noqa: E731
    return bundle, read


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(A.E_ROOT) / "05_git仓库_最新v2.6"))
    args = ap.parse_args()
    out = Path(args.out)

    bundle, read = build_bundle()
    (out / "中北喵星图.html").write_text(bundle.html, encoding="utf-8",
                                         newline="\n")
    assets = out / "assets"
    assets.mkdir(exist_ok=True)
    for old in assets.glob("photo-data-*.js"):
        old.unlink()                      # 旧 eager 分片清干净防残留
    for name, text in bundle.iter_chunks(read):
        (assets / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"✓ v2.8 产物已导出：{out/'中北喵星图.html'} + "
          f"{len(bundle.groups)} 个分片 → {assets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
