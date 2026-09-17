#!/usr/bin/env python3
"""T6 F5 满血验收：full76 走 v29 引擎，与 05 仓 v2.7 基线逐字段比对。

流程：解析 05 仓 v2.7 正式版的 CATS/CALIB/照片键清单 → 用 E 盘真实名册
76 只 + 真实照片 + v2.7 人工标定坐标（calib override）驱动 API 生成 v29
inline 产物 → 逐字段比对：
  硬门禁（不一致即退出码 1）：CATS 12 字段逐条、CALIB 逐键坐标（≤0.002 容差）、
  照片键集合；
  记录项（算法推导 vs 人工布置，F6 用 schools/nuc 数据包覆盖收口）：
  AREAS / AREA_KEYS / CONST / REL / stars。

证据写入 docs/验收证据-F5-full76-v29-对v2.7逐字段比对.md。
用法：STARMAP_HOME=总库根 python scripts/compare_full76_v29.py
"""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import acceptance as A  # noqa: E402
from app import csv_loader  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

V27_HTML = A.E_ROOT / "05_git仓库_最新v2.6" / "中北喵星图.html"
V27_ASSETS = A.E_ROOT / "05_git仓库_最新v2.6" / "assets"
MAP_PNG = A.E_ROOT / "09_地图底图" / "学校地图.png"
TOL = 0.002  # CALIB 三位小数舍入容差


def _cats_of(html: str) -> list[dict]:
    m = re.search(r"const CATS = (\[.*?\]);", html, re.S)
    assert m, "CATS 块没找到"
    body = m.group(1).strip()
    if body.startswith("["):
        body = body[1:-1]
    body = re.sub(r"([{,]\s*)([A-Za-z_]\w*)(\s*:)", r'\1"\2"\3', body)
    body = re.sub(r":\s*\.(\d)", r": 0.\1", body)
    return json.loads("[" + body + "]")


def _calib_of(html: str) -> dict[str, tuple[float, float]]:
    m = re.search(r"const CALIB=\{(.*?)\};", html, re.S)
    out = {}
    if not m:
        return out
    for cid, x, y in re.findall(
            r'"(CAT-\d+)":\{x:([\d.]+),y:([\d.]+)\}', m.group(1)):
        out[cid] = (float(x), float(y))
    return out


def _areas_of(html: str) -> list[dict]:
    m = re.search(r"const AREAS=\[(.*?)\];", html, re.S)
    if not m:
        return []
    return [{"x": float(x), "y": float(y), "t": t}
            for x, y, t in re.findall(
                r"\{x:([\d.]+),y:([\d.]+),t:\"([^\"]*)\"\}", m.group(1))]


def _consts_of(html: str) -> list[str]:
    m = re.search(r"const CONST=\[(.*?)\];", html, re.S)
    return re.findall(r'n:"([^"]*)"', m.group(1)) if m else []


def _photo_keys_of(paths: list[Path]) -> set[str]:
    keys: set[str] = set()
    for p in paths:
        keys |= set(re.findall(r'__PHOTOS\["([^"]+)"\]', read(p)))
    return keys


def read(p: Path) -> str:
    return p.read_text("utf-8", errors="replace")


def main() -> int:
    for probe in (V27_HTML, A.REAL_ROSTER):
        if not probe.exists():
            print(f"[早退] E 盘基线缺失：{probe}")
            return 1

    v27 = read(V27_HTML)
    v27_cats = _cats_of(v27)
    v27_calib = _calib_of(v27)
    v27_areas = _areas_of(v27)
    v27_consts = _consts_of(v27)
    v27_photos = {k for k in _photo_keys_of(sorted(V27_ASSETS.glob("photo-data-*.js")))
                  if k.lower().endswith(".jpg") and "地图" not in k and "map" not in k.lower()}

    # ── 驱动 API：真实名册 + 真实照片 + v2.7 人工标定 → v29 inline ──
    roster_text = A.read_text(A.REAL_ROSTER)
    rows, _, _, _ = csv_loader.parse_roster(roster_text)
    raw_names = sorted({r.photo_file.strip() for r in rows if r.photo_file.strip()})
    found, missing = A.find_real_photos(raw_names)
    assert not missing, f"照片缺失：{missing[:5]}"
    photos = [(n, p.read_bytes()) for n, p in sorted(found.items())]

    ws = Path(tempfile.mkdtemp(prefix="cmp_v29_"))
    from app import config, store
    from app.main import app
    config.WORKSPACE = ws
    store.WORKSPACE = ws
    client = TestClient(app)

    pid = client.post("/api/projects",
                      json={"school": "中北大学", "subtitle": "F5 满血回归"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/roster",
                    files={"file": ("猫咪名册.csv", roster_text.encode("utf-8-sig"),
                                    "text/csv")})
    assert r.status_code == 200, r.text
    files = [("files", (n, b, "image/jpeg")) for n, b in photos]
    r = client.post(f"/api/projects/{pid}/photos", files=files)
    assert r.status_code == 200 and r.json()["saved"] == len(photos), r.text[:300]
    if MAP_PNG.exists():
        img = Image.open(MAP_PNG).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        r = client.post(f"/api/projects/{pid}/map",
                        files={"file": ("校园地图-夜空版-web.jpg", buf.getvalue(),
                                        "image/jpeg")})
        note_map = "真实底图（PNG→JPEG）" if r.status_code == 200 else \
            f"底图上传失败（{r.status_code}），走默认底图"
    else:
        note_map = "未找到 09_地图底图，走默认底图"

    r = client.put(f"/api/projects/{pid}/calib", json={
        "positions": {cid: {"x": x, "y": y} for cid, (x, y) in v27_calib.items()},
        "note": "v2.7 人工标定坐标全量回灌", "merge": False})
    assert r.status_code == 200, r.text

    g = client.post(f"/api/projects/{pid}/generate",
                    json={"form": "inline", "engine": "v29"})
    assert g.status_code == 200, g.text
    d = g.json()
    zr = client.get(f"/api/projects/{pid}/download?form=inline")
    zf = zipfile.ZipFile(io.BytesIO(zr.content))
    html_name = d["html_name"]
    v29 = zf.read(html_name).decode("utf-8")
    v29_cats = _cats_of(v29)
    v29_calib = _calib_of(v29)
    v29_areas = _areas_of(v29)
    v29_consts = _consts_of(v29)
    pm = re.search(r'const __PM=(\{.*?\});</script>', v29, re.S)
    v29_photo_keys = set(json.loads(pm.group(1))["m"]) if pm else set()
    v29_photo_keys = {k for k in v29_photo_keys
                      if k.lower().endswith(".jpg") and "map" not in k.lower()}

    # ── 逐字段比对 ──
    failures: list[str] = []
    lines = ["# T6 F5 · full76 v29 引擎对 v2.7 基线逐字段比对", "",
             f"- v2.7 基线：`{V27_HTML}`",
             f"- v29 产物：API 生成（{d['cats']} 猫 / {d['photos_embedded']} 图，"
             f"inline lazy）；{note_map}",
             f"- 人工标定：v2.7 的 {len(v27_calib)} 键全量回灌后生成", ""]

    lines += ["## CATS（硬门禁）", ""]
    lines.append(f"- 条数：v2.7={len(v27_cats)} / v29={len(v29_cats)}")
    if len(v27_cats) != len(v29_cats):
        failures.append(f"CATS 条数不一致：{len(v27_cats)} vs {len(v29_cats)}")
    fields = ("id", "name", "rank", "title", "coat", "coatGroup", "features",
              "area", "bio", "photo", "photoCount", "brightness")
    v27_by_id = {c["id"]: c for c in v27_cats}
    diffs: dict[str, list] = {}
    for c in v29_cats:
        o = v27_by_id.get(c["id"])
        if o is None:
            failures.append(f"CATS 多出 {c['id']}")
            continue
        for k in fields:
            if k == "brightness":
                if abs(float(o[k]) - float(c[k])) > 1e-6:
                    diffs.setdefault(k, []).append((c["id"], o[k], c[k]))
            elif str(o.get(k, "")) != str(c.get(k, "")):
                diffs.setdefault(k, []).append((c["id"], o.get(k), c.get(k)))
    for k, lst in diffs.items():
        failures.append(f"CATS 字段 {k} 有 {len(lst)} 处不一致，例：{lst[:3]}")
    if not diffs:
        lines.append(f"- ✅ {len(v29_cats)} 条 × {len(fields)} 字段全部一致"
                     "（含小传/照片路径/星等）")
    else:
        lines += [f"- ❌ {k}: {len(lst)} 处，例 {lst[:2]}" for k, lst in diffs.items()]
    lines.append("")

    lines += ["## CALIB（硬门禁，容差 0.002）", ""]
    lines.append(f"- 键数：v2.7={len(v27_calib)} / v29={len(v29_calib)}")
    bad = [(cid, v27_calib[cid], v29_calib[cid]) for cid in v27_calib
           if cid in v29_calib
           and (abs(v27_calib[cid][0] - v29_calib[cid][0]) > TOL
                or abs(v27_calib[cid][1] - v29_calib[cid][1]) > TOL)]
    missing_keys = sorted(set(v27_calib) - set(v29_calib))
    if bad:
        failures.append(f"CALIB 坐标超差 {len(bad)} 键，例：{bad[:3]}")
    if missing_keys:
        failures.append(f"CALIB 丢失键：{missing_keys[:5]}")
    lines.append(f"- ✅ {len(v27_calib) - len(bad) - len(missing_keys)} 键一致；"
                 f"超差 {len(bad)}，丢失 {len(missing_keys)}" if not bad and not missing_keys
                 else f"- ❌ 超差 {len(bad)}，丢失 {len(missing_keys)}：{missing_keys[:5]}")
    lines.append("")

    lines += ["## 照片键集合（硬门禁）", ""]
    only27 = sorted(v27_photos - v29_photo_keys)
    only29 = sorted(v29_photo_keys - v27_photos)
    lines.append(f"- v2.7={len(v27_photos)} / v29={len(v29_photo_keys)}；"
                 f"仅 v2.7 有：{only27[:3]}；仅 v29 有：{only29[:3]}")
    if only27 or only29:
        failures.append(f"照片键集合不一致：仅v2.7 {len(only27)}，仅v29 {len(only29)}")
    else:
        lines.append(f"- ✅ {len(v29_photo_keys)} 张代表照键完全一致")
    lines.append("")

    lines += ["## 星域/星宿/关系/海报星（记录项：算法推导 vs 人工布置）", ""]
    t27 = {a["t"] for a in v27_areas}
    t29 = {a["t"] for a in v29_areas}
    lines.append(f"- AREAS：v2.7={len(v27_areas)} 域 {sorted(t27)}")
    lines.append(f"- AREAS：v29={len(v29_areas)} 域 {sorted(t29)}；"
                 f"域集合{'一致' if t27 == t29 else '不一致（坐标为算法推导）'}")
    if t27 != t29:
        lines.append(f"  - 仅 v2.7：{sorted(t27 - t29)}；仅 v29：{sorted(t29 - t27)}")
    lines.append(f"- CONST：v2.7={len(v27_consts)} / v29={len(v29_consts)}；"
                 f"星宿名{'一致' if v27_consts == v29_consts else '存在差异：'
                          + str([x for x in v27_consts if x not in v29_consts][:3])}")
    lines.append("- 说明：v2.7 的星域坐标/星宿文案为人工布置；v29 引擎默认用"
                 "名册推导（star_mapper 同款算法）。F6 重建 05 时将消费 "
                 "schools/nuc 数据包的 areas.json/constellations.json 等"
                 "人工数据，实现字节级对齐。")
    lines.append("")

    lines += ["## 署名（硬门禁：只许追加不许抹除）", ""]
    for needle in ("原创作品 © 2026 TianWenzzzh", "TianWenzzzh/meow-starmap"):
        ok = needle in v29
        lines.append(f"- {'✅' if ok else '❌'} 「{needle}」")
        if not ok:
            failures.append(f"署名缺失：{needle}")
    lines.append("")

    verdict = "通过" if not failures else f"{len(failures)} 项失败"
    lines.insert(2, f"- 结论：**{verdict}**")
    if failures:
        lines += ["## 失败清单"] + [f"- {x}" for x in failures]

    evidence = ROOT / "docs" / "验收证据-F5-full76-v29-对v2.7逐字段比对.md"
    evidence.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": not failures, "failures": failures,
                      "evidence": str(evidence)}, ensure_ascii=False, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
