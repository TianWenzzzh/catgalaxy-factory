"""F3 · 模板注入：把校验通过的猫写进星图模板（CATS / CALIB / 校名 / __PHOTOS）。"""
from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from .config import PHOTO_CHUNK_BYTES, STARMAP_TEMPLATE
from .models import CatRow
from .star_mapper import coat_stats, layout_positions, to_cat_entry, zone_of, zone_stats

CATS_TOKEN = "/*__CATS__*/[]"
CALIB_TOKEN = "/*__CALIB__*/{}"
ZONES_TOKEN = "/*__ZONES__*/[]"
PHOTO_SCRIPTS_TOKEN = "<!--__PHOTO_SCRIPTS__-->"


def _js(obj) -> str:
    """JSON → 可安全嵌入 <script> 的字符串。"""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _js_array_pretty(items: list[dict]) -> str:
    """每个对象一行，便于人工核对生成的产物。"""
    if not items:
        return "[]"
    body = ",\n  ".join(_js(it) for it in items)
    return "[\n  " + body + "\n]"


def build_cat_entries(rows: list[CatRow],
                      positions: Optional[dict] = None) -> tuple[list[dict], dict]:
    """rows → (CATS 条目列表, CALIB 坐标表)。"""
    positions = positions if positions is not None else layout_positions(rows)
    entries: list[dict] = []
    calib: dict[str, dict[str, float]] = {}
    for row in rows:
        e = to_cat_entry(row)
        e["zone"] = zone_of(row.area) or "未归类区"
        p = positions.get(e["id"])
        if p:
            calib[e["id"]] = {"x": round(float(p["x"]), 3), "y": round(float(p["y"]), 3)}
        entries.append(e)
    return entries, calib


def build_zone_list(rows: list[CatRow]) -> list[dict]:
    zs = zone_stats(rows)
    return [{"name": k, "count": v} for k, v in zs.items()]


def build_photo_chunks(photos: dict[str, bytes],
                       chunk_bytes: int = PHOTO_CHUNK_BYTES) -> list[tuple[str, str]]:
    """照片字节 → base64 分片 JS 文件内容。返回 [(文件名, JS 文本)]。"""
    chunks: list[list[str]] = [[]]
    sizes = [0]
    for name in sorted(photos):
        b64 = base64.b64encode(photos[name]).decode("ascii")
        line = f'__PHOTOS[{_js(name)}]="data:image/jpeg;base64,{b64}";'
        if sizes[-1] + len(line) > chunk_bytes and chunks[-1]:
            chunks.append([])
            sizes.append(0)
        chunks[-1].append(line)
        sizes[-1] += len(line)

    out: list[tuple[str, str]] = []
    for i, lines in enumerate(chunks, start=1):
        if not lines:
            continue
        content = "window.__PHOTOS=window.__PHOTOS||{};\n" + "\n".join(lines) + "\n"
        out.append((f"photo-data-{i:02d}.js", content))
    return out


def photo_script_tags(names: Iterable[str]) -> str:
    return "".join(f'<script src="assets/{n}"></script>' for n in names)


def render_starmap(*, school: str, subtitle: str = "", rows: list[CatRow],
                   form: str = "relative", map_filename: str = "assets/map.jpg",
                   photo_script_names: Optional[list[str]] = None,
                   stats: Optional[str] = None, generated_on: Optional[str] = None,
                   template_path: Optional[Path] = None) -> str:
    """渲染最终 HTML 字符串。"""
    tpl = (template_path or STARMAP_TEMPLATE).read_text(encoding="utf-8")
    entries, calib = build_cat_entries(rows)
    generated_on = generated_on or date.today().isoformat()

    title = f"{school}喵星图 · 校园猫咪星系"
    stats = stats or (f"{school}实地普查 ｜ {len(entries)} 只在编基米 ｜ "
                      f"{sum(e['photoCount'] for e in entries)} 张实拍照片 ｜ "
                      f"{len(zone_stats(rows))} 个出没分区")
    subtitle = subtitle or f"{school}的喵星编制 · 每颗星都是一只真实生活的校园猫"
    footer = (f"数据源：data/猫咪名册.csv ｜ 生成日期 {generated_on} ｜ "
              f"由「喵星图工厂 CatGalaxy Factory」自动生成")

    ls_key = f"catgalaxy-{school}-positions"

    scripts = photo_script_tags(photo_script_names or [])

    html = tpl
    for token, value in (
        (CATS_TOKEN, _js_array_pretty(entries)),
        (CALIB_TOKEN, _js(calib)),
        (ZONES_TOKEN, _js(build_zone_list(rows))),
        (PHOTO_SCRIPTS_TOKEN, scripts),
        ("__MAP_SRC__", map_filename),
        ("__LS_KEY__", ls_key),
        ("__TITLE__", title),
        ("__SUBTITLE__", subtitle),
        ("__STATS__", stats),
        ("__FOOTER__", footer),
        ("__SCHOOL__", school),
    ):
        html = html.replace(token, value)

    # 兜底：未替换的 token 一律清空，避免产物里出现占位符字面量
    for leftover in ("__TITLE__", "__SUBTITLE__", "__STATS__", "__FOOTER__",
                     "__SCHOOL__", "__MAP_SRC__", "__LS_KEY__"):
        html = html.replace(leftover, "")
    return html


def generation_stats(rows: list[CatRow]) -> dict:
    return {
        "cats": len(rows),
        "zones": zone_stats(rows),
        "coats": coat_stats(rows),
        "photos": sum(max(1, r.photo_count) for r in rows),
    }
