"""v29 星图纯渲染层（T6 收敛：与开放模板 meow-starmap 的 build.py 同构）。

本模块是 v2.7→v2.9 模板的**唯一渲染真相**：吃规范化后的猫条目/坐标/图片字节
与文案参数，产出 (HTML, 分片分组)；不碰 FastAPI、不读写项目目录、不知道 zip。
工厂在线流程（main.py/injector.py）与开放模板构建器都只做数据装配后调这里。

模板 app/templates/starmap_v29.html 与规则 app/starmap_v29_rules.json 与
meow-starmap v1.2.0-rc 字节同源（入库时 sha256 核对）。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .config import PHOTO_CHUNK_BYTES

APP_DIR = Path(__file__).resolve().parent
V29_TEMPLATE = APP_DIR / "templates" / "starmap_v29.html"
V29_RULES = APP_DIR / "starmap_v29_rules.json"

# v29 CATS 条目规范字段（11 键契约，与 to_cat_entry 裁剪后一致）
CAT_FIELDS = ("id", "name", "rank", "title", "coat", "coatGroup", "features",
              "area", "bio", "photo", "photoCount", "brightness")


# ───────────────────────── 注入转义（与 injector._js_str 同规则） ─────────────────────────

def js_str(s) -> str:
    body = json.dumps(str(s if s is not None else ""), ensure_ascii=False)
    return (body[1:-1].replace("</", "<\\/")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def js(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028") \
        .replace("\u2029", "\\u2029")


def js_num(v) -> str:
    s = str(round(float(v), 2))
    return s[1:] if s.startswith("0") else s


def js_num3(v) -> str:
    s = f"{float(v):.3f}"
    return s[1:] if s.startswith("0") else s


# ───────────────────────── JS 数据块渲染 ─────────────────────────

def render_cats(cats: list[dict]) -> str:
    out = ["const CATS = ["]
    for i, c in enumerate(cats):
        tail = " " if i == len(cats) - 1 else ","
        out.append(
            f'  {{"id":{js_str(c["id"])},"name":{js_str(c["name"])},'
            f'"rank":{js_str(c["rank"])},"title":{js_str(c["title"])},'
            f'"coat":{js_str(c["coat"])},"coatGroup":{js_str(c["coatGroup"])},'
            f'"features":{js_str(c["features"])},"area":{js_str(c["area"])},'
            f'"bio":{js_str(c.get("bio",""))},\n'
            f'    "photo":{js_str(c["photo"])},"photoCount":{int(c["photoCount"])}, '
            f'"brightness":{js_num(c["brightness"])}}}{tail}')
    out.append("];")
    return "\n".join(out)


def render_calib(calib: dict) -> str:
    out = ["const CALIB={"]
    items = sorted(calib.items())
    for i, (cid, pts) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        out.append(
            f'  "{js_str(cid)}":{{"x":{js_num3(pts["x"])},"y":{js_num3(pts["y"])}}}{comma}')
    out.append("};")
    return "\n".join(out)


def render_areas(areas: list[dict]) -> str:
    return ("const AREAS=[\n" +
            "\n".join('  {"x":'+js_num(a["x"])+',"y":'+js_num(a["y"])+
                      ', "t":'+js_str(a["t"])+', "s":'+str(int(a["s"]))+'}'
                      for a in areas) + "\n];")


def render_area_keys(keys: list) -> str:
    return "const AREA_KEYS=" + js(keys) + ";"


def render_rel(rel: list) -> str:
    return "const REL=" + js(rel) + ";"


def render_const(consts: list[dict]) -> str:
    out = ["const CONST=["]
    for i, c in enumerate(consts):
        comma = "," if i < len(consts) - 1 else ""
        out.append(
            '  {"n":'+js_str(c["n"])+',"en":'+js_str(c["en"])+
            ',"s":'+js_str(c["s"])+comma)
    out.append("];")
    return "\n".join(out)


def render_poster_stars(cats: list[dict]) -> str:
    top = sorted(cats, key=lambda c: -int(c["photoCount"]) or 1)[:4]
    return ("const stars=" +
            js([[c["id"], f'{c["photoCount"] or 1}'] for c in top]) + ";")


# ───────────────────────── 默认推导（无人工 JSON 时与开放模板同算法） ─────────────────────────

def derive_areas(cats: list[dict]) -> list[dict]:
    uniq: list[str] = []
    counts: dict[str, int] = {}
    for c in cats:
        a = c["area"]
        if a and a not in uniq:
            uniq.append(a)
        counts[a] = counts.get(a, 0) + 1
    anchors = assign_anchors(uniq)
    return [{"x": anchors[a][0], "y": anchors[a][1], "t": a,
             "s": counts[a]} for a in uniq]


def assign_anchors(uniq: list[str]) -> dict[str, tuple[float, float]]:
    """与开放模板 starmap_layout.assign_anchors 同构（极坐标 12 锚环）。"""
    n = len(uniq)
    if not n:
        return {}
    if n == 1:
        return {uniq[0]: (0.5, 0.42)}
    rings = [(0.30, 0.30), (0.46, 0.40), (0.58, 0.46)]
    out: dict[str, tuple[float, float]] = {}
    for i, a in enumerate(uniq):
        ring = rings[min(i // 4, len(rings) - 1)]
        angle = (i % 4) * (math.pi / 2) + (math.pi / 4 if (i // 4) % 2 else 0)
        out[a] = (round(0.5 + ring[0] * math.cos(angle), 3),
                  round(0.52 + ring[1] * math.sin(angle), 3))
    return out


def derive_const(areas: list[dict]) -> list[dict]:
    out = []
    for i, a in enumerate(areas):
        t = (a["t"] or "").strip()
        out.append({
            "n": (t[:6] + "座") if len(t) > 6 else t + "座",
            "en": f"STAR {i + 1}",
            "s": f"{t}是{len(t)}只喵星的地盘",
        })
    return out


def derive_poster_stars(cats: list[dict]) -> list[list]:
    top = sorted(cats, key=lambda c: -int(c.get("photoCount") or 1))[:4]
    return [[c["id"], int(c.get("photoCount") or 1)] for c in top]


def milestones_for(n: int) -> list[int]:
    ms = [math.ceil(n / 4), math.ceil(n / 2), math.ceil(3 * n / 4), n]
    return sorted(set(max(1, int(x)) for x in ms))


def pass_titles_for(ms: list[int]) -> list[list]:
    names = ["初来乍到", "猫门常客", "校园通", "猫学长认证", "喵星传奇"]
    return [[0, names[0]]] + [[ms[i], names[i + 1]] for i in range(len(ms))]


# ───────────────────────── 照片分片（与 injector 同 chunk 规则） ─────────────────────────

def _photo_line(name: str, blob: bytes) -> str:
    import base64
    b64 = base64.b64encode(blob).decode("ascii")
    return f'__PHOTOS[{js(name)]="data:image/jpeg;base64,{b64}";'


def _photo_line_len(name: str, nbytes: int) -> int:
    return len(_photo_line(name, b"")) + 4 * ((nbytes + 2) // 3)


def chunk_plan(sizes: list[tuple[str, int]],
               chunk_bytes: int = PHOTO_CHUNK_BYTES) -> list[list[str]]:
    groups: list[list[str]] = [[]]
    size = 0
    for name, nbytes in sorted(sizes):
        n = _photo_line_len(name, nbytes)
        if size + n > chunk_bytes and groups[-1]:
            groups.append([])
            size = 0
        groups[-1].append(name)
        size += n
    return [g for g in groups if g]


def chunk_text(lines: list[str]) -> str:
    return "window.__PHOTOS=window.__PHOTOS||{};\n" + "\n".join(lines) + "\n"


# ───────────────────────── 入参 / 出参 ─────────────────────────

@dataclass
class V29RenderInput:
    school: str
    cats: list[dict]                     # 已规范化的 11 字段条目
    photos: dict[str, bytes]             # basename → 猫照片字节（已压缩）
    map_bytes: bytes
    map_key: str = "map.jpg"             # __PHOTOS 里的底图键
    map_src: str = "assets/map.jpg"      # 模板 MAP_SRC（PH 会取 basename）
    calib: dict = field(default_factory=dict)
    photo_loading: str = "lazy"          # lazy | eager | relative
    # ---- 文案（全部可选，缺省走与开放模板一致的推导） ----
    product: Optional[str] = None
    en: Optional[str] = None
    version: str = "v1.1.0"
    tagline: Optional[str] = None
    survey_date: Optional[str] = None
    photo_total: Optional[int] = None
    ls_prefix: Optional[str] = None
    repo_url: str = "https://github.com/TianWenzzzh/meow-starmap"
    docs_ref: str = "docs/快速上手.md"
    theme_css: str = ""
    logo_intro: str = ""
    logo_topbar: str = ""
    # ---- 可选富数据（工厂一般不用，给 05 全量再基线留口） ----
    areas: Optional[list[dict]] = None
    area_keys: Optional[list] = None
    rel: Optional[list] = None
    consts: Optional[list[dict]] = None
    poster_stars: Optional[list[list]] = None


@dataclass
class V29Bundle:
    html: str
    groups: list[list[str]]              # 落盘顺序对应的键分组
    first_index: int                     # 分片起始编号（lazy=0，eager=1）
    key_chunk: dict[str, int]

    def iter_chunks(self, read) -> list[tuple[str, str]]:
        """read(basename)->bytes；产出 (photo-data-NN.js 文件名, JS 文本)。"""
        out = []
        for i, group in enumerate(self.groups, start=self.first_index):
            out.append((f"photo-data-{i:02d}.js",
                        chunk_text([_photo_line(k, read(k)) for k in group])))
        return out


# ───────────────────────── 主渲染 ─────────────────────────

def render(inp: V29RenderInput) -> V29Bundle:
    n = len(inp.cats)
    if n == 0:
        raise ValueError("v29 渲染至少需要 1 只猫")
    school = inp.school
    short = school
    product = inp.product or (school + "喵星图")
    en = inp.en or "CAMPUS CAT GALAXY"
    survey_date = inp.survey_date or date.today().strftime("%Y-%m")
    p_total = inp.photo_total if inp.photo_total is not None \
        else sum(int(c.get("photoCount") or 1) for c in inp.cats)
    ls_prefix = re_safe(inp.ls_prefix) if inp.ls_prefix else re_safe(school)[:32]
    tagline = inp.tagline or f"{school}的喵星编制 · 每颗星都是一只真实生活的校园猫"
    repo_display = inp.repo_url.replace("https://", "")

    areas = inp.areas if inp.areas is not None else derive_areas(inp.cats)
    area_keys = inp.area_keys if inp.area_keys is not None else []
    rel = inp.rel if inp.rel is not None else []
    consts = inp.consts if inp.consts is not None else derive_const(areas)
    stars = (inp.poster_stars if inp.poster_stars is not None
             else derive_poster_stars(inp.cats))

    ms = milestones_for(n)
    pass_titles = pass_titles_for(ms)
    js_titles = "[" + ",".join(f'[{int(t)},"{js_str(nm)}"]'
                               for t, nm in pass_titles) + "]"
    committee = f"{school}猫咪编制委员会"
    king_name = short + "猫王"

    rules = json.loads(V29_RULES.read_text("utf-8"))["rules"]

    def block(name: str) -> str:
        return {
            "cats_block": render_cats(inp.cats),
            "calib_block": render_calib(inp.calib),
            "areas_block": render_areas(areas),
            "area_keys_block": render_area_keys(area_keys),
            "rel_block": render_rel(rel),
            "const_block": render_const(consts),
            "poster_stars": js_inline_stars(stars),
        }[name]

    values = {
        # F11（锚点必须带回，见 meow-starmap build.py 同构注释）
        "theme_css": "</style>" + inp.theme_css,
        "logo_intro": '<div id="intro">' + inp.logo_intro,
        "logo_topbar": '<div class="brand">' + inp.logo_topbar,
        "copy_line": f"  原创作品 © 2026 TianWenzzzh ｜ {n} 只猫的普查数据与 "
                     f"{p_total} 张照片均为实地采集",
        "meta_desc": f'<meta name="description" content="{product}：{n} 只'
                     f'{school}校园猫的实地普查星图。每只猫是一颗星——'
                     f'星色取毛色、星等看实拍数、星位即真实出没区。'
                     f'原创开源：{repo_display}">',
        "title_tag": f"<title>{product} · 校园猫咪星系</title>",
        "tagline": tagline,
        "stats_line": f"{short}校园实地普查 ｜ {n} 只在编基米 ｜ "
                      f"{p_total} 张学长学姐实拍",
        "guide_bound": f"档案数据仅覆盖{school}（{survey_date} 实地普查 {n} 只），"
                       f"别校的猫查不到；玩法谁都可用，想给母校复刻一份，"
                       f"完整流程在 {inp.docs_ref}。",
        "guide_reuse": f"档案数据不能（那是{short}的猫），方法论可以："
                       f"普查、归并、建星图到打包的完整流程都写在 {inp.docs_ref} 里。",
        "guide_accuracy": f"{p_total} 张照片逐张比对归并出 {n} 只，存疑的一律不收；"
                          f"每一条取舍都有记录，欢迎抽查。",
        "stats_foot": f"数据源：猫咪名册.csv · {survey_date} 实地普查 · 仅{short}校园<br>"
                      f"星色 = 毛色 ｜ 环绕光点 = 收录照片数",
        "banner_sub": f" * 底图: {inp.map_src} | 数据: CATS（{n}只真猫名册 · "
                      f"{survey_date}普查）",
        "cats_lead_comment":
            "// ---- 真实名册数据（build 自名册 CSV 注入 · "
            f"__CAT_COUNT__ 只在编 · {survey_date} 普查）----",
        "calib_lead_comment":
            "/* 人工固化校准坐标（语义聚簇落位 · 手动校准 localStorage 仍优先覆盖） */"
            if inp.calib else
            "/* 星位由页面内置算法按编号稳定推导（basePos）· "
            "可在页面上手拖校准后导出坐标 */",
        "map_src": f'const MAP_SRC = "{js_str(inp.map_src)}";',
        "ls_key": f'const LS_KEY  = "{ls_prefix}-cat-galaxy-positions";',
        "js_title": f'const TITLE="{js_str(product)}";',
        "ls_fx": f'"{ls_prefix}-cat-fx"',
        "ls_seen": f'"{ls_prefix}-cat-seen"',
        "ls_tip": f'"{ls_prefix}-cat-tip-dismiss"',
        "card_title_expr": f'c.id+" ｜ {committee} · 登记在册"',
        "card_meta_expr":
            f'"{survey_date} 实地普查 · 第 "+(+c.id.slice(4))+" 号星"',
        "export_map_name": f'map:"{js_str(school)}校园图"',
        "milestones": "[" + ",".join(str(x) for x in ms) + "]",
        "pass_titles": js_titles,
        "milestone_toast":
            f'm==={n}?"🏆 喵图鉴全收集！你就是{king_name}！"',
        "clear_filter": f'cb.textContent="清筛选 · 看全部{n}只"',
        "poster_title": f'g.fillText("{js_str(short)}寻猫地图",pw/2,118)',
        "poster_sub":
            f'"跟着星图走遍它们的地盘 · "+CATS.length+" 只在编基米 · '
            f'{survey_date} 实地普查"',
        "poster_open_hint":
            f'打开「{product}」点击任意星星，即可查看它的档案与出没星域',
        "skill_foot_line":
            f"{school}—校园猫咪档案 · {tagline}",
        "poster_file": f'a.download="{js_str(short)}寻猫海报.png"',
        "rec_badge":
            f'" ｜ 图鉴 "+SEEN.size+"/"+CATS.length+" ｜ {product}"',
        "soul_line": f"和它一样：真实、在编、被记录在册的{short}猫",
        "soul_meta":
            f'"{n}只在编基米 · {survey_date}实地普查 · 图鉴 "'
            f'+SEEN.size+"/"+CATS.length',
        "meme_brand":
            f'"{product}原创 · "+(MEME.cat?MEME.cat.name:"")',
        "passport_hint":
            f"打开「{product}」· 按十二星宿寻访 · 遇见即可盖章",
        "console_line1": f"%c🐱 {product} · {en} {inp.version}",
        "console_line2":
            f"%c原创作品 © 2026 TianWenzzzh · {n} 只猫的实地普查档案\\n"
            f"开源仓库：{inp.repo_url}\\n"
            f"转载请署名并附仓库链接 · 一起给更多学校点亮喵星系 ✦",
        "chip_all": f">全部<b class=\"n\">{n}</b>",
        "seen_ring": f"已遇见 0 / {n}",
    }

    # 分片分组与引导串
    blobs = dict(inp.photos)
    blobs[inp.map_key] = inp.map_bytes
    loading = inp.photo_loading
    if loading == "lazy":
        groups = [[inp.map_key]] + chunk_plan(
            [(k, len(v)) for k, v in blobs.items() if k != inp.map_key])
        key_chunk = {k: i for i, g in enumerate(groups) for k in g}
        bootstrap = ('<script src="assets/photo-data-00.js"></script>'
                     '<script>const __PM='
                     + js({"n": len(groups), "m": key_chunk}) + ";</script>")
        first_index = 0
    elif loading == "eager":
        groups = chunk_plan([(k, len(v)) for k, v in blobs.items()])
        key_chunk = {k: i + 1 for i, g in enumerate(groups) for k in g}
        bootstrap = "".join(
            f'<script src="assets/photo-data-{i:02d}.js"></script>'
            for i in range(1, len(groups) + 1))
        first_index = 1
    elif loading == "relative":
        groups = []
        key_chunk = {}
        bootstrap = ""
        first_index = 0
    else:
        raise ValueError(f"非法 photo_loading：{loading!r}")

    html = V29_TEMPLATE.read_text("utf-8")
    for r in rules:
        name, token = r["name"], r["new"]
        if html.count(token) != int(r["count"]):
            raise RuntimeError(
                f"token {name} 出现 {html.count(token)} 次，"
                f"与 v29 规则凭据 {r['count']} 不一致——模板/规则版本漂移")
        value = bootstrap if name == "photo_scripts" else values[name]
        html = html.replace(token, value)

    for token, value in (
        ("__CAT_COUNT__", str(n)),
        ("__PHOTO_COUNT__", str(p_total)),
        ("__SURVEY_DATE__", survey_date),
        ("__LS_PREFIX__", ls_prefix),
        ("__MAP_FILE__", inp.map_src),
        ("__KING_NAME__", king_name),
        ("__COMMITTEE__", committee),
    ):
        html = html.replace(token, value)

    leftover = re_find_tokens(html)
    if leftover:
        raise RuntimeError("v29 产物残留未替换 token：" + ", ".join(sorted(set(leftover))))

    return V29Bundle(html=html, groups=groups, first_index=first_index,
                     key_chunk=key_chunk)


def js_inline_stars(stars: list[list]) -> str:
    return "const stars=" + js(stars) + ";"


def re_safe(s: str) -> str:
    import re
    return re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]", "-", str(s))[:32] or "school"


def re_find_tokens(s: str) -> list[str]:
    import re
    return re.findall(r"__[A-Z_]+__", s)
