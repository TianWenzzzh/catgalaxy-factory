"""v29 星图纯渲染层（T6 收敛：与开放模板 meow-starmap 的 build.py 同构）。

本模块是 v2.9 模板的**唯一渲染真相**：吃规范化后的猫条目/坐标/图片字节
与文案参数，产出 (HTML, 分片分组)；不碰 FastAPI、不读写项目目录、不知道 zip。
工厂在线流程（main.py/injector.py）与开放模板构建器都只做数据装配后调这里。

与 16 仓库 tools/build.py 的分工：那边是本地 CLI（输入过 check_safe 白名单，
可信任，值直接内插）；这边是在线服务（输入不可信任），所有进 HTML/JS 上下文
的自由文本按**落地上下文**转义（html_esc / js_str）。两个转义函数对白名单内
的合法取值都是恒等变换——因此两条路径对同一数据包产出逐字节一致的产物
（T6 F6 的验收依据），恶意输入则被就地拆解。

模板 app/templates/starmap_v29.html 与规则 app/starmap_v29_rules.json 与
meow-starmap v1.2.0-rc 字节同源（入库时 sha256 核对）。
"""
from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from html import escape
from pathlib import Path
from typing import Optional

from .config import PHOTO_CHUNK_BYTES
from .star_mapper import assign_anchors, normalize_area

APP_DIR = Path(__file__).resolve().parent
V29_TEMPLATE = APP_DIR / "templates" / "starmap_v29.html"
V29_RULES = APP_DIR / "starmap_v29_rules.json"

# 与 16 仓库 build.py 相同的底图文件名白名单 / 仓库 URL 白名单
MAP_NAME_RE = re.compile(r"^[0-9A-Za-z\u4e00-\u9fff_.\- ]+\.(jpe?g)$",
                         re.IGNORECASE)
URL_RE = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")

# v29 CATS 条目规范字段（与 to_cat_entry 裁剪后一致；photo 已含 assets/photos/ 前缀）
CAT_FIELDS = ("id", "name", "rank", "title", "coat", "coatGroup", "features",
              "area", "bio", "photo", "photoCount", "brightness")


# ───────────────────────── 注入转义 ─────────────────────────

def html_esc(s) -> str:
    """拼进 HTML 文本节点/属性值的自由文本（与 injector._html 同规则）。"""
    return escape(str(s if s is not None else ""), quote=True)


def js_str(s) -> str:
    """拼进 JS 双引号字符串字面量的**值**（剥引号版，外层引号由调用方提供）。

    json.dumps 负责 `"` `\\` 和控制字符；再补三件它不管的事：`</` 会让
    `</script>` 提前收掉整个脚本块，U+2028/2029 在 JS 里是行终止符。
    """
    body = json.dumps(str(s if s is not None else ""), ensure_ascii=False)
    return (body[1:-1].replace("</", "<\\/")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def js(obj) -> str:
    """JSON → 可安全嵌入 <script> 的字符串。"""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028") \
        .replace("\u2029", "\\u2029")


def js_num(v) -> str:
    """0.5→.5、0.61→.61、1.05→1.05（16 仓库同款去前导零口径）。"""
    s = str(round(float(v), 2))
    return s[1:] if s.startswith("0.") else s


def js_num3(v) -> str:
    """坐标三位小数、去尾零、去前导零（与 16 仓库 build.py 逐字节同口径）。"""
    s = f"{float(v):.3f}".rstrip("0").rstrip(".")
    return s[1:] if s.startswith("0.") else s


def re_safe(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]", "-", str(s))[:32] or "school"


def re_find_tokens(s: str) -> list[str]:
    return re.findall(r"__[A-Z_]+__", s)


# ───────────────────────── JS 数据块渲染（16 仓库 build.py 同构） ─────────────────────────

def render_cats(cats: list[dict]) -> str:
    """CATS 块，v2.7 风格（裸键 + 双引号值）。

    16 仓库在渲染时给 photo 拼 assets/photos/ 前缀；工厂的规范条目
    （to_cat_entry）产出时已带该前缀，故直接用现值。
    """
    out = ["const CATS = ["]
    for i, c in enumerate(cats):
        tail = " " if i == len(cats) - 1 else ","
        out.append(
            f'  {{ id:"{js_str(c["id"])}", name:"{js_str(c["name"])}", '
            f'rank:"{js_str(c["rank"])}", title:"{js_str(c["title"])}", '
            f'coat:"{js_str(c["coat"])}", coatGroup:"{js_str(c["coatGroup"])}",\n'
            f'    features:"{js_str(c["features"])}", area:"{js_str(c["area"])}", '
            f'bio:"{js_str(c["bio"])}",\n'
            f'    photo:"{js_str(c["photo"])}", '
            f'photoCount:{int(c["photoCount"])}, '
            f'brightness:{js_num(c["brightness"])}}}' + tail)
    out.append("];")
    return "\n".join(out)


def render_calib(calib: dict) -> str:
    if not calib:
        return "const CALIB={};"
    items = list(calib.items())  # 保持调用方顺序（与 calib.json/名册序一致）
    lines = ["const CALIB={"]
    for i in range(0, len(items), 4):
        row = ",".join(
            f'"{js_str(cid)}":{{x:{js_num3(p["x"])},y:{js_num3(p["y"])}}}'
            for cid, p in items[i:i + 4])
        lines.append("  " + row + ("," if i + 4 < len(items) else ""))
    lines.append("};")
    return "\n".join(lines)


def render_areas(areas: list[dict]) -> str:
    if not areas:
        return "const AREAS=[];"
    lines = ["const AREAS=["]
    for i in range(0, len(areas), 3):
        row = "".join(
            f'{{x:{js_num3(a["x"])},y:{js_num3(a["y"])},'
            f't:"{js_str(a["t"])}"}}' + ("," if not (
                i + 3 >= len(areas) and j == len(areas[i:i + 3]) - 1) else "")
            for j, a in enumerate(areas[i:i + 3]))
        lines.append("  " + row)
    lines.append("];")
    return "\n".join(lines)


def render_area_keys(keys: list) -> str:
    if not keys:
        return "const AREA_KEYS=[];"
    lines = ["const AREA_KEYS=["]
    for i in range(0, len(keys), 6):
        row = ",".join(f'["{js_str(k)}",{int(idx)}]' for k, idx in keys[i:i + 6])
        lines.append("  " + row + ("," if i + 6 < len(keys) else ""))
    lines.append("];")
    return "\n".join(lines)


def render_rel(rel: list) -> str:
    if not rel:
        return "const REL=[];"
    lines = ["const REL=["]
    for i, r in enumerate(rel):
        lines.append(
            f'  {{ a:"{js_str(r["a"])}", b:"{js_str(r["b"])}", '
            f'label:"{js_str(r["label"])}" }}'
            + ("," if i < len(rel) - 1 else ""))
    lines.append("];")
    return "\n".join(lines)


def render_const(consts: list[dict]) -> str:
    if not consts:
        return "const CONST=[];"
    lines = ["const CONST=["]
    for i, c in enumerate(consts):
        lines.append(
            f' {{n:"{js_str(c["n"])}",en:"{js_str(c["en"])}",'
            f's:"{js_str(c["s"])}"}}' + ("," if i < len(consts) - 1 else ""))
    lines.append("];")
    return "\n".join(lines)


def render_poster_stars(stars: list) -> str:
    return "const stars=" + js(stars) + ";"


# ───────────────────────── 默认推导（无人工 JSON 时与 16 仓库同算法） ─────────────────────────

def derive_areas(cats: list[dict]) -> list[dict]:
    uniq: list[str] = []
    for c in cats:
        a = normalize_area(c["area"])
        if a and a not in uniq:
            uniq.append(a)
    anchors = assign_anchors(uniq)
    return [{"x": anchors[a][0], "y": anchors[a][1], "t": a} for a in uniq]


def derive_const(areas: list[dict], counts: dict[str, int]) -> list[dict]:
    out = []
    for i, a in enumerate(areas):
        t = a["t"]
        out.append({
            "n": (t[:6] + "座") if len(t) > 6 else t + "座",
            "en": f"STAR {i + 1}",
            "s": f"{t}是它们常来常往的地盘，{counts.get(t, 0)} 颗星在此落脚。"
                 f"路过时放慢脚步，也许就能撞见一颗会打呼的星。"})
    return out


def derive_poster_stars(cats: list[dict]) -> list[list]:
    top = sorted(cats, key=lambda c: -int(c["photoCount"]))[:4]
    return [[c["id"], f'{c["name"]} · {c["area"]}'] for c in top]


def milestones_for(n: int) -> list[int]:
    if n <= 3:
        return [n]
    ms = [math.ceil(n / 4), math.ceil(n / 2), math.ceil(3 * n / 4), n]
    return sorted(set(ms))


def pass_titles_for(ms: list[int]) -> list[list]:
    names = ["猫门常客", "校园通", "猫学长认证"]
    titles = [[0, "初来乍到"]]
    for i, m in enumerate(ms[:-1]):
        titles.append([m, names[min(i, len(names) - 1)]])
    titles.append([ms[-1], "喵星传奇"])
    return titles


# ───────────────────────── 照片分片（与 injector 同 chunk 规则） ─────────────────────────

def _photo_line(name: str, blob: bytes) -> str:
    b64 = base64.b64encode(blob).decode("ascii")
    return f'__PHOTOS[{js(name)}]="data:image/jpeg;base64,{b64}";'


def _photo_line_len(name: str, nbytes: int) -> int:
    """不拿到字节也算得准的行长：base64 长度恒为 4*ceil(n/3)。"""
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
    cats: list[dict]                     # 已规范化的 12 字段条目（photo 含前缀）
    photos: dict[str, bytes] = field(default_factory=dict)   # basename → 照片字节
    photo_sizes: Optional[dict[str, int]] = None             # 给了就免读字节做分片规划
    map_bytes: bytes = b""
    map_key: str = "map.jpg"             # 底图在 __PHOTOS 里的键 = MAP_SRC
    calib: dict = field(default_factory=dict)
    photo_loading: str = "lazy"          # lazy | eager | relative
    # ---- 文案（全部可选，缺省走与 16 仓库一致的推导） ----
    product: Optional[str] = None
    en: Optional[str] = None
    version: str = "v1.1.0"
    tagline: Optional[str] = None
    survey_date: Optional[str] = None
    photo_total: Optional[int] = None
    ls_prefix: Optional[str] = None      # 缺省 "starmap"（与 16 仓库一致）
    repo_url: str = "https://github.com/TianWenzzzh/meow-starmap"
    docs_ref: str = "docs/快速上手.md"
    theme_css: str = ""
    logo_intro: str = ""
    logo_topbar: str = ""
    # ---- 人工文案/节奏覆盖（schools/nuc 的 build_meta 逐项对应；缺省走推导） ----
    milestones: Optional[list] = None
    pass_titles: Optional[list] = None
    king_name: Optional[str] = None
    poster_title: Optional[str] = None
    poster_file: Optional[str] = None
    export_map_name: Optional[str] = None
    skill_foot_line: Optional[str] = None
    soul_line: Optional[str] = None
    calib_note: Optional[str] = None
    footer_signature: str = ""           # 工厂主题落款（追加在页脚，转义后输出）
    # ---- 可选富数据（工厂一般不用，给 05 全量再基线留口） ----
    areas: Optional[list[dict]] = None
    area_keys: Optional[list] = None
    rel: Optional[list] = None
    consts: Optional[list[dict]] = None
    poster_stars: Optional[list] = None


@dataclass
class V29Bundle:
    html: str
    groups: list[list[str]]              # 落盘顺序对应的键分组
    first_index: int                     # 分片起始编号（lazy=0，eager=1）
    key_chunk: dict[str, int]

    def iter_chunks(self, read):
        """read(basename)->bytes；逐片**产出** (photo-data-NN.js 文件名, JS 文本)。

        生成器：与 build_inline_bundle 的流式契约对齐——用到哪片才读哪片，
        峰值内存 = 1 片 + 1 图。调用方要列表就自己 list() 包一层。
        """
        for i, group in enumerate(self.groups, start=self.first_index):
            yield (f"photo-data-{i:02d}.js",
                   chunk_text([_photo_line(k, read(k)) for k in group]))


# ───────────────────────── 主渲染 ─────────────────────────

def render(inp: V29RenderInput) -> V29Bundle:
    n = len(inp.cats)
    if n == 0:
        raise ValueError("v29 渲染至少需要 1 只猫")
    if not MAP_NAME_RE.match(inp.map_key or ""):
        raise ValueError(f"底图键名不安全：{inp.map_key!r}")
    if not URL_RE.match(inp.repo_url or ""):
        raise ValueError(f"repo_url 非法：{inp.repo_url!r}")
    school = inp.school
    short = re.sub(r"(大学|学院|学校)$", "", school) or school
    product = inp.product or (school + "喵星图")
    en = inp.en or "CAMPUS CAT GALAXY"
    survey_date = inp.survey_date or date.today().strftime("%Y-%m")
    p_total = inp.photo_total if inp.photo_total is not None \
        else sum(int(c.get("photoCount") or 1) for c in inp.cats)
    ls_prefix = re_safe(inp.ls_prefix) if inp.ls_prefix else "starmap"
    tagline = inp.tagline or f"{school}的喵星编制 · 每颗星都是一只真实生活的校园猫"
    repo_display = inp.repo_url.replace("https://", "")

    areas = inp.areas if inp.areas is not None else derive_areas(inp.cats)
    area_keys = inp.area_keys if inp.area_keys is not None else []
    rel = inp.rel if inp.rel is not None else []
    if inp.consts is not None:
        consts = inp.consts
    else:
        counts: dict[str, int] = {}
        for c in inp.cats:
            a = normalize_area(c["area"])
            counts[a] = counts.get(a, 0) + 1
        consts = derive_const(areas, counts)
    stars = (inp.poster_stars if inp.poster_stars is not None
             else derive_poster_stars(inp.cats))

    ms = list(inp.milestones) if inp.milestones else milestones_for(n)
    pass_titles = (list(inp.pass_titles) if inp.pass_titles
                   else pass_titles_for(ms))
    js_titles = "[" + ",".join(f'[{int(t)},"{js_str(nm)}"]'
                               for t, nm in pass_titles) + "]"
    committee = f"{school}猫咪编制委员会"
    king_name = inp.king_name or (short + "猫王")
    poster_title_v = inp.poster_title or f"{short}寻猫地图"
    poster_file_v = inp.poster_file or f"{short}寻猫海报.png"
    export_map_name_v = inp.export_map_name or f"{school}校园图"
    skill_foot_v = inp.skill_foot_line or f"{school}—校园猫咪档案 · {tagline}"
    soul_line_v = inp.soul_line or f"和它一样：真实、在编、被记录在册的{short}猫"

    loading = inp.photo_loading
    if loading not in ("lazy", "eager", "relative"):
        raise ValueError(f"非法 photo_loading：{loading!r}")
    # MAP_SRC 口径：lazy/eager 下底图走 __PHOTOS[键]（分片 00/首片）；relative
    # 下无分片，PH(p) 回退返回路径本身，故直接给 packager 的固定落点
    # assets/map.jpg（build_relative_bundle 的写入位置）。
    map_ref = inp.map_key if loading != "relative" else "assets/map.jpg"

    rules = json.loads(V29_RULES.read_text("utf-8"))["rules"]

    # 52+3 条规则的最终值（顺序/措辞与 16 仓库 build.py 的 rule_values 一致；
    # 用户自由文本按落地上下文转义，合法取值下与 16 的裸内插逐字节相同）
    values = {
        "theme_css": "</style>" + inp.theme_css,
        "logo_intro": '<div id="intro">' + inp.logo_intro,
        "logo_topbar": '<div class="brand">' + inp.logo_topbar,
        "copy_line": f"  原创作品 © 2026 TianWenzzzh ｜ {n} 只猫的普查数据与 "
                     f"{p_total} 张照片均为实地采集",
        "meta_desc": f'<meta name="description" content="{html_esc(product)}：'
                     f'{n} 只{html_esc(school)}校园猫的实地普查星图。'
                     f'每只猫是一颗星——星色取毛色、星等看实拍数、'
                     f'星位即真实出没区。原创开源：{repo_display}">',
        "title_tag": f"<title>{html_esc(product)} · 校园猫咪星系</title>",
        "tagline": html_esc(tagline),
        "stats_line": f"{html_esc(short)}校园实地普查 ｜ {n} 只在编基米 ｜ "
                      f"{p_total} 张学长学姐实拍",
        "guide_bound": f"档案数据仅覆盖{html_esc(school)}"
                       f"（{html_esc(survey_date)} 实地普查 {n} 只），"
                       f"别校的猫查不到；玩法谁都可用，想给母校复刻一份，"
                       f"完整流程在 {inp.docs_ref}。",
        "guide_reuse": f"档案数据不能（那是{html_esc(short)}的猫），方法论可以："
                       f"普查、归并、建星图到打包的完整流程都写在 "
                       f"{inp.docs_ref} 里。",
        "guide_accuracy": f"{p_total} 张照片逐张比对归并出 {n} 只，"
                          f"存疑的一律不收；每一条取舍都记在"
                          f"《归并决策摘要.md》，欢迎抽查。",
        "stats_foot": f"数据源：猫咪名册.csv · {html_esc(survey_date)} 实地普查 · "
                      f"仅{html_esc(short)}校园<br>"
                      f"星色 = 毛色 ｜ 环绕光点 = 收录照片数"
                      + (f"<br>✍ {html_esc(inp.footer_signature)}"
                         if inp.footer_signature else ""),
        "banner_sub": f" * 底图: {map_ref} | 数据: CATS（{n}只真猫名册 · "
                      f"{js_str(survey_date)}普查）",
        "cats_lead_comment":
            f"// ---- 真实名册数据（build.py 自名册 CSV 注入 · "
            f"{n} 只在编 · {survey_date} 普查）----",
        "calib_lead_comment":
            f"/* {inp.calib_note} */" if inp.calib_note else
            ("/* 人工固化校准坐标 · 手动校准 localStorage 仍优先覆盖 */"
             if inp.calib else
             "/* 星位由页面内置算法按编号稳定推导（basePos）· "
             "可在页面上手拖校准后导出坐标 */"),
        "map_src": f'const MAP_SRC = "{js_str(map_ref)}";',
        "ls_key": f'const LS_KEY  = "{js_str(ls_prefix)}-cat-galaxy-positions";',
        "js_title": f'const TITLE="{js_str(product)}";',
        "ls_fx": f'"{js_str(ls_prefix)}-cat-fx"',
        "ls_seen": f'"{js_str(ls_prefix)}-cat-seen"',
        "ls_tip": f'"{js_str(ls_prefix)}-cat-tip-dismiss"',
        "card_title_expr": f'c.id+" ｜ {js_str(committee)} · 登记在册"',
        "card_meta_expr":
            f'"{js_str(survey_date)} 实地普查 · 第 "+(+c.id.slice(4))+" 号星"',
        "export_map_name": f'map:"{js_str(export_map_name_v)}"',
        "milestones": "[" + ",".join(str(int(x)) for x in ms) + "]",
        "pass_titles": js_titles,
        "milestone_toast":
            f'm==={n}?"🏆 喵图鉴全收集！你就是{js_str(king_name)}！"',
        "clear_filter": f'cb.textContent="清筛选 · 看全部{n}只"',
        "poster_title": f'g.fillText("{js_str(poster_title_v)}",pw/2,118)',
        "poster_sub":
            f'"跟着星图走遍它们的地盘 · "+CATS.length+" 只在编基米 · '
            f'{js_str(survey_date)} 实地普查"',
        "poster_open_hint":
            f"打开「{html_esc(product)}」点击任意星星，"
            f"即可查看它的档案与出没星域",
        "skill_foot_line": html_esc(skill_foot_v),
        "poster_file": f'a.download="{js_str(poster_file_v)}"',
        "rec_badge":
            f'" ｜ 图鉴 "+SEEN.size+"/"+CATS.length+" ｜ {js_str(product)}"',
        "soul_line": html_esc(soul_line_v),
        "soul_meta":
            f'"{n}只在编基米 · {js_str(survey_date)}实地普查 · 图鉴 "'
            f'+SEEN.size+"/"+CATS.length',
        "meme_brand":
            f'"{js_str(product)}原创 · "+(MEME.cat?MEME.cat.name:"")',
        "passport_hint":
            f"打开「{html_esc(product)}」· 按十二星宿寻访 · 遇见即可盖章",
        "console_line1": f"%c🐱 {js_str(product)} · {js_str(en)} "
                         f"{js_str(inp.version)}",
        "console_line2":
            f"%c原创作品 © 2026 TianWenzzzh · {n} 只猫的实地普查档案\\n"
            f"开源仓库：{inp.repo_url}\\n"
            f"转载请署名并附仓库链接 · 一起给更多学校点亮喵星系 ✦",
        "chip_all": f'>全部<b class="n">{n}</b>',
        "seen_ring": f"已遇见 0 / {n}",
        "repo_url": inp.repo_url,
        "repo_display": repo_display,
        # __PRODUCT__/__EN__/__VERSION__ 直通点位混合 HTML/JS 两种上下文，
        # 统一走 html_esc：合法取值恒等（字节对齐不受影响），恶意值两头拆解
        "product": html_esc(product),
        "en": html_esc(en),
        "version": html_esc(inp.version),
        # ---- JS 数据块（规则表前 7 条）----
        "cats_block": render_cats(inp.cats),
        "calib_block": render_calib(inp.calib),
        "areas_block": render_areas(areas),
        "area_keys_block": render_area_keys(area_keys),
        "rel_block": render_rel(rel),
        "const_block": render_const(consts),
        "poster_stars": render_poster_stars(stars),
    }

    # 分片分组与引导串（照片尺寸可由调用方直给，免整册读进内存）
    photo_sizes = (dict(inp.photo_sizes) if inp.photo_sizes is not None
                   else {k: len(v) for k, v in inp.photos.items()})
    if loading == "lazy":
        groups = [[inp.map_key]] + chunk_plan(
            [(k, n) for k, n in photo_sizes.items() if k != inp.map_key])
        key_chunk = {k: i for i, g in enumerate(groups) for k in g}
        bootstrap = ('<script src="assets/photo-data-00.js"></script>'
                     '<script>const __PM='
                     + js({"n": len(groups), "m": key_chunk}) + ";</script>")
        first_index = 0
    elif loading == "eager":
        all_sizes = dict(photo_sizes)
        all_sizes.setdefault(inp.map_key, len(inp.map_bytes))
        groups = chunk_plan(list(all_sizes.items()))
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
        ("__MAP_FILE__", inp.map_key),
        ("__KING_NAME__", king_name),
        ("__COMMITTEE__", committee),
    ):
        html = html.replace(token, value)

    leftover = re_find_tokens(html)
    if leftover:
        raise RuntimeError("v29 产物残留未替换 token：" + ", ".join(sorted(set(leftover))))

    return V29Bundle(html=html, groups=groups, first_index=first_index,
                     key_chunk=key_chunk)
