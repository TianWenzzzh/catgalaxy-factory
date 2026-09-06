"""F3 · 模板注入：把校验通过的猫写进星图模板（CATS / CALIB / 校名 / __PHOTOS）。"""
from __future__ import annotations

import base64
import json
import re
from datetime import date
from html import escape
from pathlib import Path
from typing import Iterable, Optional

from .config import DEFAULT_MAP_H, DEFAULT_MAP_W, PHOTO_CHUNK_BYTES, STARMAP_TEMPLATE
from .csv_loader import normalize_id
from .models import CatRow
from .star_mapper import coat_stats, layout_positions, to_cat_entry, zone_of, zone_stats
from .theme import Theme, css_block

CATS_TOKEN = "/*__CATS__*/[]"
CALIB_TOKEN = "/*__CALIB__*/{}"
ZONES_TOKEN = "/*__ZONES__*/[]"
PHOTO_SCRIPTS_TOKEN = "<!--__PHOTO_SCRIPTS__-->"
THEME_TOKEN = "<!--__THEME_CSS__-->"       # 连注释符一起替换：没有主题时整段消失
LOGO_TOKEN = "__LOGO_HTML__"               # 替换成完整的 <img> 或空串

_KEY_BAD = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff-]+")

# 校徽 src 白名单：相对形态只认打包出来的那个文件，inline 形态只认 PNG 的
# base64 data URI。src 是拼进 HTML 属性的，白名单比转义更省事也更硬。
_LOGO_SRC = re.compile(r"^(?:assets/logo\.png|data:image/png;base64,[A-Za-z0-9+/=]+)$")


def _html(s) -> str:
    """拼进 HTML 文本节点 / 属性值的东西一律走这里。

    校名、副标题、页脚署名都是用户填的。产物要挂到学校公众号上，一个
    `<img onerror=…>` 混进页脚就是别人家网站上的脚本执行。
    """
    return escape(str(s if s is not None else ""), quote=True)


def _js_str(s) -> str:
    """拼进 JS 双引号字符串字面量的值。

    json.dumps 负责 `"` `\\` 和控制字符；再补三件它不管的事：`</` 会让
    `</script>` 提前收掉整个脚本块，U+2028/2029 在 JS 里是行终止符。
    """
    body = json.dumps(str(s if s is not None else ""), ensure_ascii=False)
    return (body[1:-1].replace("</", "<\\/")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _js_comment(s) -> str:
    """拼进 /* … */ 注释的值。校名里出现 `*/` 会把注释提前关掉，后半截横幅
    就成了代码。压掉换行（注释里的换行会打乱排版）并把 `*/` 拆开。"""
    return " ".join(str(s if s is not None else "").split()).replace("*/", "* /")


def logo_tag(src: str) -> str:
    """有校徽就吐一个完整的 <img>，没有就吐空串。

    整个标签在这里生成、不给调用方拼：没有校徽时模板里连 <img> 都不该有，
    否则浏览器会在顶栏画一个碎图标。
    """
    s = str(src or "").strip()
    if not s or not _LOGO_SRC.match(s):
        return ""
    return f'<img class="logo" src="{s}" alt="校徽">'


def _safe_key_part(s: str) -> str:
    """校名 → localStorage 键安全片段（剔除会破坏 JS 字符串字面量的字符）。"""
    return _KEY_BAD.sub("-", str(s or "")).strip("-")[:32] or "school"


def _js(obj) -> str:
    """JSON → 可安全嵌入 <script> 的字符串。"""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _fingerprint(calib: dict) -> str:
    """CALIB 内容指纹（FNV-1a，与模板端 hashStr 同构）。

    用作 localStorage 键后缀：坐标一变键就变，上一版产物里用户手拖的本机标定
    不会遮蔽新产物烘焙进去的 CALIB。取内容而非时钟，产物仍可字节级复现。
    """
    payload = json.dumps(calib, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    h = 2166136261
    for ch in payload:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return f"{h:08x}"


def _js_array_pretty(items: list[dict]) -> str:
    """每个对象一行，便于人工核对生成的产物。"""
    if not items:
        return "[]"
    body = ",\n  ".join(_js(it) for it in items)
    return "[\n  " + body + "\n]"


def _xy(v) -> Optional[tuple[float, float]]:
    """把多种坐标写法（dict / 二元组 / 带 .x.y 的对象）统一成 (x, y)。"""
    if v is None:
        return None
    if isinstance(v, dict):
        x, y = v.get("x"), v.get("y")
    elif isinstance(v, (tuple, list)) and len(v) == 2:
        x, y = v
    else:
        x, y = getattr(v, "x", None), getattr(v, "y", None)
    try:
        return float(x), float(y)
    except (TypeError, ValueError):
        return None


def normalize_calib(calib) -> dict[str, dict[str, float]]:
    """任意标定输入 → {id: {"x":…, "y":…}}，丢弃无法解析的条目。"""
    out: dict[str, dict[str, float]] = {}
    for cid, v in (calib or {}).items():
        xy = _xy(v)
        if xy:
            out[str(cid)] = {"x": round(xy[0], 3), "y": round(xy[1], 3)}
    return out


def build_cat_entries(rows: list[CatRow], positions: Optional[dict] = None,
                      calib_override: Optional[dict] = None) -> tuple[list[dict], dict]:
    """rows → (CATS 条目列表, CALIB 坐标表)。

    calib_override 为人工标定坐标，命中则覆盖算法推导值（F8）。
    """
    positions = positions if positions is not None else layout_positions(rows)
    override = normalize_calib(calib_override)
    entries: list[dict] = []
    calib: dict[str, dict[str, float]] = {}
    for row in rows:
        e = to_cat_entry(row)
        e["zone"] = zone_of(row.area) or "未归类区"
        p = override.get(e["id"]) or positions.get(e["id"])
        if p:
            calib[e["id"]] = {"x": round(float(p["x"]), 3), "y": round(float(p["y"]), 3)}
        entries.append(e)
    return entries, calib


def calib_stats(rows: list[CatRow], calib_override: Optional[dict]) -> dict:
    """人工标定覆盖情况：{manual, derived, total, missing}。"""
    ids = [normalize_id(r.id) or r.id for r in rows]
    override = normalize_calib(calib_override)
    manual = sum(1 for i in ids if i in override)
    return {"manual": manual, "derived": len(ids) - manual,
            "total": len(ids), "missing": sorted(set(override) - set(ids))}


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
                   template_path: Optional[Path] = None,
                   calib: Optional[dict] = None,
                   map_size: Optional[tuple[int, int]] = None,
                   theme: Optional[Theme] = None,
                   logo_src: str = "") -> str:
    """渲染最终 HTML 字符串。

    每个 token 都按它落地的上下文转义——HTML 文本、JS 字符串、JS 注释是三套
    不同的规则，混用就会漏（校名里的 `</script>` 在 HTML 里无害，在 JS 里能
    把整个脚本块收掉）。
    """
    tpl = (template_path or STARMAP_TEMPLATE).read_text(encoding="utf-8")
    entries, derived = build_cat_entries(rows, calib_override=calib)
    generated_on = generated_on or date.today().isoformat()
    theme = (theme or Theme()).normalized()

    ids = {e["id"] for e in entries}
    manual = {k: v for k, v in normalize_calib(calib).items() if k in ids}

    title = f"{school}喵星图 · 校园猫咪星系"
    stats = stats or (f"{school}实地普查 ｜ {len(entries)} 只在编基米 ｜ "
                      f"{sum(e['photoCount'] for e in entries)} 张实拍照片 ｜ "
                      f"{len(zone_stats(rows))} 个出没分区")
    subtitle = subtitle or f"{school}的喵星编制 · 每颗星都是一只真实生活的校园猫"
    calib_note = (f" ｜ 星位人工标定 {len(manual)}/{len(entries)}"
                  if manual else " ｜ 星位为算法推导（未人工标定）")
    footer = (f"数据源：data/猫咪名册.csv ｜ 生成日期 {generated_on}{calib_note} ｜ "
              f"由「喵星图工厂 CatGalaxy Factory」自动生成")
    # 署名是**追加**，不是替换：数据源、生成日期、标定方式这三样是留痕要求，
    # 学校想加自己的落款可以，想把出处抹掉不行。
    if theme.footer_signature:
        footer += f" ｜ {theme.footer_signature}"

    ls_key = f"catgalaxy-{_safe_key_part(school)}-{len(entries)}-{_fingerprint(derived)}"
    mw, mh = map_size or (DEFAULT_MAP_W, DEFAULT_MAP_H)

    scripts = photo_script_tags(photo_script_names or [])

    html = tpl
    for token, value in (
        (CATS_TOKEN, _js_array_pretty(entries)),
        (CALIB_TOKEN, _js(derived)),
        (ZONES_TOKEN, _js(build_zone_list(rows))),
        (PHOTO_SCRIPTS_TOKEN, scripts),
        (THEME_TOKEN, css_block(theme)),
        (LOGO_TOKEN, logo_tag(logo_src)),
        ("__MAP_SRC__", _js_str(map_filename)),
        ("__MAP_W__", str(int(mw))),
        ("__MAP_H__", str(int(mh))),
        ("__CALIB_MODE__", "1" if manual else "0"),
        ("__LS_KEY__", _js_str(ls_key)),
        ("__TITLE_COMMENT__", _js_comment(title)),
        ("__TITLE__", _html(title)),
        ("__SUBTITLE__", _html(subtitle)),
        ("__STATS__", _html(stats)),
        ("__FOOTER__", _html(footer)),
        ("__SCHOOL__", _js_str(school)),
    ):
        html = html.replace(token, value)

    # 兜底：未替换的 token 一律清空，避免产物里出现占位符字面量
    for leftover in ("__TITLE_COMMENT__", "__TITLE__", "__SUBTITLE__", "__STATS__",
                     "__FOOTER__", "__SCHOOL__", "__MAP_SRC__", "__LS_KEY__",
                     LOGO_TOKEN, THEME_TOKEN):
        html = html.replace(leftover, "")
    return html


def generation_stats(rows: list[CatRow]) -> dict:
    return {
        "cats": len(rows),
        "zones": zone_stats(rows),
        "coats": coat_stats(rows),
        "photos": sum(max(1, r.photo_count) for r in rows),
    }
