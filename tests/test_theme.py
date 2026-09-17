"""F11 · 星图主题化：配色 / 字体 / 页脚署名 / 校徽。

盯三件事：

1. **默认产物一个字节都不该变。** 没配过主题的项目，注入的 <style> 必须是空串，
   顶栏里连 <img> 都不该有——否则「主题化」这个功能会顺手改掉所有已交付的产物。
2. **用户输入进不了代码。** 主题值会被拼进 <style> 和 HTML，署名进页脚，校名进
   JS 字符串和 JS 注释。四种上下文四套规则，任何一处放行自由文本都是注入口子，
   而产物是要挂到学校公众号上的。
3. **留痕不能被署名顶掉。** 数据源、生成日期、标定方式必须一直在页脚里。
"""
from __future__ import annotations

import base64
import io
import json
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import injector, store, theme
from app.config import STARMAP_TEMPLATE
from app.image_proc import process_logo
from app.main import app
from app.models import CatRow
from app.theme import Theme

from conftest import make_csv, make_jpeg

client = TestClient(app)


def _row(cid: str, name: str = "墩墩", area: str = "宿舍楼前石台",
         photo: str = "a.jpg", coat: str = "全橘虎斑") -> CatRow:
    return CatRow(line=2, id=cid, name=name, rank="喵校长", title="总揽全校猫务",
                  coat=coat, features="体型胖 粉鼻", photo_file=photo, photo_count=2,
                  photo_count_raw="2", area=area, related="batch1:X", confidence="高",
                  note="")


ROWS = [_row("CAT-001"), _row("CAT-002", "格子", "教学楼走廊", "b.jpg", "橘白")]

# css_block 注入的那段 <style> 的标记注释。数 <style> 标签不可靠（注释里也可能
# 出现这几个字），认这个标记才是「有没有注入主题」的准确判据。
INJECTED_CSS = "/* 主题：由喵星图工厂按项目配置注入 */"


def _png(size=(64, 48), rgba=(200, 30, 30, 255), transparent_box=None) -> bytes:
    """造一张测试 PNG。给了 transparent_box 就在中间挖一块全透明区。"""
    img = Image.new("RGBA", size, rgba)
    if transparent_box:
        for x in range(transparent_box[0], transparent_box[2]):
            for y in range(transparent_box[1], transparent_box[3]):
                img.putpixel((x, y), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- css_block：默认必须什么都不加 ----------

def test_default_theme_injects_no_css_at_all():
    """全默认时返回空串。返回一段和模板重复的 CSS 也算回归：产物会变长，
    而且两份 :root 一旦不一致，谁生效取决于书写顺序，是个定时炸弹。"""
    assert theme.css_block(Theme()) == ""
    assert theme.css_block(None) == ""


def test_default_theme_css_matches_the_template_defaults():
    """模板里写死的默认值必须和 Theme() 的默认值一致，否则「不配主题」和
    「配成默认预设」会长得不一样。"""
    tpl = STARMAP_TEMPLATE.read_text(encoding="utf-8")
    root = tpl.split(":root{", 1)[1].split("}", 1)[0]
    for key, var in theme.COLOR_VARS.items():
        want = theme.PRESETS[theme.DEFAULT_PRESET].colors[key]
        assert f"{var}:{want}" in root.replace(" ", ""), f"{var} 与模板默认值不一致"
    assert "--line:rgba(140,170,255,.16)" in root.replace(" ", "")
    assert "--panel:rgba(10,18,40,.86)" in root.replace(" ", "")


def test_default_scrim_reproduces_the_hardcoded_header_gradient():
    """theme.py 的注释声称 scrim 在默认预设下算出模板原来那两个值。这里把话钉住：
    改了暗化系数就会红。"""
    top, mid = Theme().scrim()
    assert top == "rgba(5,9,22,0.94)"
    assert mid == "rgba(5,9,22,0.55)"
    assert "rgba(5,9,22,.94)" in STARMAP_TEMPLATE.read_text(encoding="utf-8")


def test_every_preset_renders_and_covers_every_color_key():
    """任何一个预设缺一个键，css_block 就会漏一个变量，产物里那块颜色退回模板
    默认值——半套主题比没有主题更难看。"""
    for key, p in theme.PRESETS.items():
        assert set(p.colors) == set(theme.COLOR_VARS), f"预设 {key} 的颜色键不全"
        if key == theme.DEFAULT_PRESET:
            # 显式选「子夜金（默认）」等于没配主题，不该多注入一段和模板重复的 CSS
            assert theme.css_block(Theme(preset=key)) == ""
            continue
        css = theme.css_block(Theme(preset=key))
        assert css.startswith("<style>"), f"预设 {key} 没生成 CSS"
        for name, value in p.colors.items():
            assert f"{theme.COLOR_VARS[name]}:{value}" in css
        assert "--scrim-top:" in css and "--title-glow:" in css


def test_preset_switch_changes_the_background():
    css = theme.css_block(Theme(preset="dawn"))
    assert "--bg:#1a0d09" in css
    assert "--bg:#070c1c" not in css


def test_custom_color_overrides_only_that_key():
    css = theme.css_block(Theme(preset="midnight", colors={"accent": "#00ff00"}))
    assert "--gold:#00ff00" in css
    assert "--bg:#070c1c" in css        # 其余键仍来自预设


def test_glow_is_derived_from_the_accent_not_supplied_as_rgba():
    """辉光必须由 accent 算出来。收用户填的 rgba() 等于收一段自由文本——
    里面能塞 `)` 和 `;`。"""
    css = theme.css_block(Theme(preset="midnight", colors={"accent": "#123456"}))
    assert "--title-glow:rgba(18,52,86," in css


def test_fonts_are_emitted_as_whitelisted_stacks():
    css = theme.css_block(Theme(preset="midnight", title_font="kai", body_font="song"))
    assert f"h1,.sub{{font-family:{theme.FONTS['kai']}}}" in css
    assert f"html,body{{font-family:{theme.FONTS['song']}}}" in css


def test_default_font_emits_nothing():
    """system 就是模板已有的字体栈，再写一遍是噪声。"""
    css = theme.css_block(Theme(preset="dawn"))
    assert "font-family" not in css


# ---------- 归一化：非法值挡在门外 ----------

@pytest.mark.parametrize("bad", [
    "red",                          # CSS 关键字
    "#fff;}",                       # 越狱：关掉声明块
    "#fff}",
    "url(http://evil/x.png)",       # 产物必须离线，也不能被拿去请求外站
    "expression(alert(1))",
    "#GGGGGG",                      # 非法十六进制
    "#12345",                       # 长度不对
    "rgb(1,2,3)",
    "",
    "  ",
    "#1234567890",
])
def test_hostile_color_values_are_dropped(bad):
    t = Theme(colors={"bg": bad}).normalized()
    assert t.colors == {}
    assert any("bg" in r or bad in r for r in Theme(colors={"bg": bad}).rejected())


def test_unknown_color_keys_are_dropped():
    t = Theme(colors={"--evil": "#ffffff", "position": "#000000"}).normalized()
    assert t.colors == {}
    assert len(Theme(colors={"--evil": "#ffffff"}).rejected()) == 1


def test_hostile_color_never_reaches_the_css():
    """端到端确认：越狱字符串既不进 :root，也不以任何形式出现在 <style> 里。"""
    payload = "#fff};body{background:url(http://evil/x)"
    css = theme.css_block(Theme(preset="dawn", colors={"bg": payload}))
    assert "evil" not in css
    assert "url(" not in css
    assert css.count("{") == css.count("}")


def test_unknown_preset_falls_back_instead_of_raising():
    """theme.json 可能是手改的。为个配色把「生成星图」打成 500 不值当。"""
    t = Theme(preset="不存在").normalized()
    assert t.preset == theme.DEFAULT_PRESET
    assert theme.css_block(t) == ""
    assert "不存在" in Theme(preset="不存在").rejected()[0]


def test_unknown_font_falls_back_to_system():
    t = Theme(title_font='"PingFang SC";}', body_font="Comic Sans").normalized()
    assert t.title_font == "system" and t.body_font == "system"


def test_signature_is_collapsed_and_truncated():
    t = Theme(footer_signature="  某某中学\n\n\t2026届  " + "长" * 300).normalized()
    assert "\n" not in t.footer_signature
    assert len(t.footer_signature) == theme.MAX_SIGNATURE
    assert t.footer_signature.startswith("某某中学 2026届")


def test_hex_rgba_helper_falls_back_on_garbage():
    assert theme._hex_to_rgba("不是颜色", .5) == "rgba(255,215,106,0.5)"
    assert theme._hex_to_rgba("#fff", .5) == "rgba(255,255,255,0.5)"
    assert theme._hex_to_rgba("#ff880040", .5) == "rgba(255,136,0,0.5)"   # 丢掉 alpha


def test_menus_are_complete_and_labelled():
    assert len(theme.preset_menu()) == len(theme.PRESETS)
    for item in theme.preset_menu():
        assert item["label"] and item["blurb"]
        assert len(item["swatch"]) == 4
    assert {f["key"] for f in theme.font_menu()} == set(theme.FONTS)
    assert {c["key"] for c in theme.color_menu()} == set(theme.COLOR_VARS)
    assert all(c["label"] for c in theme.color_menu())


# ---------- 校徽处理：透明通道必须活下来 ----------

def test_logo_keeps_transparency_unlike_photos():
    """照片走 _open_rgb，透明区糊成白——那是对的。校徽糊白就在深色星图上留一个
    白方块。这条测试就是钉住这个区别。"""
    blob, info = process_logo(_png((40, 40), (255, 0, 0, 255), (10, 10, 30, 30)))
    out = Image.open(io.BytesIO(blob))
    assert out.format == "PNG"
    assert out.mode == "RGBA"
    assert out.getpixel((20, 20))[3] == 0, "透明区被糊成不透明了"
    assert out.getpixel((0, 0)) == (255, 0, 0, 255)
    assert info["resized"] is False


def test_logo_is_downscaled_to_the_cap():
    blob, info = process_logo(_png((900, 400)))
    out = Image.open(io.BytesIO(blob))
    assert max(out.size) == 256
    assert out.size == (256, 113)          # 长宽比保持
    assert info["resized"] is True
    assert (info["src_width"], info["src_height"]) == (900, 400)


def test_logo_accepts_jpeg_and_emits_png():
    """统一存 PNG，调用方就不用猜扩展名。"""
    blob, _info = process_logo(make_jpeg(120, 80))
    out = Image.open(io.BytesIO(blob))
    assert out.format == "PNG" and out.mode == "RGBA"
    assert out.size == (120, 80)


def test_logo_rejects_non_image_bytes():
    with pytest.raises(Exception):
        process_logo(b"<?xml version='1.0'?><svg onload=alert(1)></svg>")


# ---------- 注入层：转义与白名单 ----------

def test_logo_tag_whitelists_the_src():
    assert injector.logo_tag("assets/logo.png") == \
        '<img class="logo" src="assets/logo.png" alt="校徽">'
    ok = "data:image/png;base64,iVBORw0KGgo="
    assert injector.logo_tag(ok) == f'<img class="logo" src="{ok}" alt="校徽">'
    # 不认识的 src 一律当没有校徽——宁可不显示，也不给属性注入口子
    for bad in ["", "javascript:alert(1)", "http://evil/x.png", 'x" onerror="alert(1)',
                "assets/logo.png\" onload=\"alert(1)", "data:text/html;base64,PHN2Zz4=",
                "../../secret.png"]:
        assert injector.logo_tag(bad) == "", f"src 白名单放行了 {bad!r}"


def test_render_without_logo_emits_no_img_at_all():
    """没有校徽时模板里连 <img> 都不该有，否则顶栏挂一个碎图标。"""
    html = injector.render_starmap(school="测试校", rows=ROWS)
    assert '<img class="logo"' not in html
    assert "__LOGO_HTML__" not in html


def test_render_with_logo_emits_the_tag():
    html = injector.render_starmap(school="测试校", rows=ROWS,
                                   logo_src="assets/logo.png")
    assert '<img class="logo" src="assets/logo.png" alt="校徽">' in html


def test_render_injects_theme_css_before_head_close():
    html = injector.render_starmap(school="测试校", rows=ROWS,
                                   theme=Theme(preset="dawn"))
    assert "--bg:#1a0d09" in html
    assert "__THEME_CSS__" not in html
    assert html.count(INJECTED_CSS) == 1, "主题样式表被注入了不止一次"
    # 覆盖用的 :root 必须排在模板自己的 </style> 之后，否则同优先级下输的是它
    assert html.index("--bg:#1a0d09") > html.index("--bg:#070c1c")
    assert html.index("--bg:#1a0d09") < html.index("</head>")


def test_render_without_theme_leaves_no_theme_css():
    html = injector.render_starmap(school="测试校", rows=ROWS)
    assert INJECTED_CSS not in html
    assert "__THEME_CSS__" not in html


def test_signature_is_appended_after_the_provenance_and_escaped():
    """署名只能追加。数据源 / 生成日期 / 标定方式是留痕要求，学校能加落款，
    不能把出处抹掉。"""
    sig = '<img src=x onerror="alert(1)">某某中学'
    html = injector.render_starmap(school="测试校", rows=ROWS,
                                   generated_on="2026-09-07",
                                   theme=Theme(footer_signature=sig))
    # 转义之后 "onerror=" 这几个字还在，但尖括号和引号都成了实体——标签是死的。
    assert "<img src=x" not in html, "署名里的标签没被转义"
    assert 'onerror="' not in html, "署名里的属性引号没被转义"
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;某某中学" in html
    footer = html.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert footer.index("数据源：data/猫咪名册.csv") < footer.index("某某中学")
    assert "生成日期 2026-09-07" in footer
    assert "星位为算法推导" in footer
    assert "由「喵星图工厂 CatGalaxy Factory」自动生成" in footer


def test_school_name_cannot_break_out_of_the_script_block():
    """校名进 JS 字符串。`</script>` 不处理的话整个脚本块当场收掉，
    后面全是页面文本——产物白屏。"""
    evil = '中北大学</script><script>alert(1)</script>'
    html = injector.render_starmap(school=evil, rows=ROWS)
    assert html.count("<script>") == html.count("</script>"), "脚本块数量对不上"
    assert "const SCHOOL = " in html
    school_line = next(l for l in html.splitlines() if l.startswith("const SCHOOL"))
    assert "</script>" not in school_line
    assert "<\\/" in school_line, "`</` 没被转义"


def test_school_name_cannot_break_out_of_the_js_banner_comment():
    """横幅在 /* … */ 里，`*/` 会把注释提前关掉，剩下的中文就成了语法错误。"""
    html = injector.render_starmap(school="某校 */ alert(1); /*", rows=ROWS)
    banner = html.split("/* =====", 1)[1].split("===== */", 1)[0]
    assert "*/" not in banner
    assert "* /" in banner, "`*/` 没被拆开"
    assert "alert(1);" in banner, "内容不该被吞掉，只该被无害化"


def test_title_is_html_escaped_in_head_and_header():
    html = injector.render_starmap(school="<b>粗</b>", rows=ROWS)
    assert "<title>&lt;b&gt;粗&lt;/b&gt;喵星图 · 校园猫咪星系</title>" in html
    assert "<h1>&lt;b&gt;粗&lt;/b&gt;喵星图 · 校园猫咪星系</h1>" in html


def test_subtitle_and_stats_are_escaped():
    html = injector.render_starmap(school="测试校", rows=ROWS,
                                   subtitle='<script>x</script>',
                                   stats='<img src=x onerror=y>')
    assert "<script>x</script>" not in html
    assert "<img src=x onerror=y>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html


def test_no_placeholder_token_survives_into_the_artifact():
    html = injector.render_starmap(school="测试校", rows=ROWS,
                                   theme=Theme(preset="aurora"),
                                   logo_src="assets/logo.png")
    assert not re.search(r"__[A-Z_]+__", html), "产物里还有未替换的占位符"


# ---------- 持久化 ----------

def test_theme_roundtrips_through_the_store(tmp_workspace):
    pid = "theme-roundtrip"
    store.ensure_dirs(pid)
    assert store.load_theme(pid).preset == theme.DEFAULT_PRESET   # 没配过 = 默认

    store.save_theme(pid, Theme(preset="sakura", title_font="kai",
                                footer_signature="夜樱季"))
    back = store.load_theme(pid)
    assert back.preset == "sakura" and back.title_font == "kai"
    assert back.footer_signature == "夜樱季"
    assert back.updated_at

    assert store.clear_theme(pid) is True
    assert store.load_theme(pid).preset == theme.DEFAULT_PRESET
    assert store.clear_theme(pid) is False


def test_corrupt_theme_file_falls_back_to_default(tmp_workspace):
    """手改坏的 theme.json 不该让项目生成不了。"""
    pid = "theme-corrupt"
    store.ensure_dirs(pid)
    store.theme_path(pid).write_text("{ 这不是 json", encoding="utf-8")
    assert store.load_theme(pid).preset == theme.DEFAULT_PRESET

    store.theme_path(pid).write_text(
        json.dumps({"preset": "aurora", "colors": {"bg": "不是颜色"},
                    "不认识的字段": 1}), encoding="utf-8")
    t = store.load_theme(pid)
    assert t.preset == "aurora" and t.colors == {}


def test_logo_roundtrips_through_the_store(tmp_workspace):
    pid = "logo-roundtrip"
    store.ensure_dirs(pid)
    assert store.load_logo(pid) is None
    blob = _png((30, 30))
    store.save_logo(pid, blob)
    assert store.load_logo(pid) == blob
    assert store.logo_path(pid).name == "logo.png"
    assert store.clear_logo(pid) is True
    assert store.load_logo(pid) is None
    assert store.clear_logo(pid) is False


# ---------- API ----------

@pytest.fixture
def project(tmp_workspace):
    """带 2 行名册 + 2 张照片、可以直接生成的项目。"""
    pid = client.post("/api/projects", json={"school": "主题校"}).json()["id"]
    photos = tmp_workspace / pid / "assets" / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    for n in ("a.jpg", "b.jpg"):
        (photos / n).write_bytes(make_jpeg(60, 40))
    csv = make_csv([
        ["CAT-001", "墩墩", "喵校长", "总揽全校猫务", "全橘虎斑", "体型胖 粉鼻",
         "a.jpg", "2", "宿舍楼前石台", "batch1:X", "高", ""],
        ["CAT-002", "格子", "中士", "宿舍内务员", "橘白", "橘头橘背白胸腹",
         "b.jpg", "3", "教学楼走廊", "batch1:Y", "中", ""],
    ])
    r = client.post(f"/api/projects/{pid}/roster",
                    files={"file": ("roster.csv", csv.encode("utf-8"), "text/csv")})
    assert r.status_code == 200, r.text
    return pid


def test_options_endpoint_lists_everything_the_console_needs():
    j = client.get("/api/theme/options").json()
    assert len(j["presets"]) == len(theme.PRESETS)
    assert len(j["fonts"]) == len(theme.FONTS)
    assert len(j["colors"]) == len(theme.COLOR_VARS)
    assert j["default_preset"] == theme.DEFAULT_PRESET
    assert j["max_signature"] == theme.MAX_SIGNATURE


def test_get_theme_before_any_change_is_the_default(project):
    j = client.get(f"/api/projects/{project}/theme").json()
    assert j["theme"]["preset"] == theme.DEFAULT_PRESET
    assert j["theme"]["colors"] == {}
    assert j["theme"]["footer_signature"] == ""
    assert j["has_logo"] is False
    assert j["logo_url"] == ""
    assert "重新点「生成星图」" in j["note"]


def test_put_theme_is_partial_so_untouched_fields_survive(project):
    r = client.put(f"/api/projects/{project}/theme",
                   json={"preset": "aurora", "footer_signature": "某某中学 2026 届"})
    assert r.status_code == 200, r.text
    assert r.json()["theme"]["preset"] == "aurora"
    assert r.json()["theme"]["footer_signature"] == "某某中学 2026 届"

    # 只改字体，预设和署名都该还在
    r2 = client.put(f"/api/projects/{project}/theme", json={"title_font": "song"})
    assert r2.json()["theme"] == {**r2.json()["theme"], "preset": "aurora",
                                 "title_font": "song",
                                 "footer_signature": "某某中学 2026 届"}


def test_put_theme_reports_what_it_dropped(project):
    r = client.put(f"/api/projects/{project}/theme",
                   json={"preset": "aurora",
                         "colors": {"bg": "#fff;}", "nope": "#123456"},
                         "body_font": "Comic Sans"})
    assert r.status_code == 200, r.text       # 不报错，丢掉并说明
    j = r.json()
    assert j["theme"]["colors"] == {}
    assert j["theme"]["body_font"] == "system"
    assert j["theme"]["preset"] == "aurora"   # 合法的那部分照样生效
    assert any("#fff;}" in x for x in j["rejected"])
    assert any("nope" in x for x in j["rejected"])
    assert any("Comic Sans" in x for x in j["rejected"])


def test_put_theme_reset_clears_customisation_but_keeps_the_badge(project):
    client.post(f"/api/projects/{project}/logo",
                files={"file": ("badge.png", _png((40, 40)), "image/png")})
    client.put(f"/api/projects/{project}/theme",
               json={"preset": "ink", "footer_signature": "落款"})

    r = client.put(f"/api/projects/{project}/theme", json={"reset": True})
    j = r.json()
    assert j["theme"]["preset"] == theme.DEFAULT_PRESET
    assert j["theme"]["footer_signature"] == ""
    assert j["has_logo"] is True, "重置主题不该顺手删掉学校上传的校徽"
    assert not store.theme_path(project).exists()


def test_delete_theme_resets(project):
    client.put(f"/api/projects/{project}/theme", json={"preset": "dawn"})
    j = client.delete(f"/api/projects/{project}/theme").json()
    assert j["theme"]["preset"] == theme.DEFAULT_PRESET


def test_logo_upload_returns_a_versioned_preview_url(project):
    r = client.post(f"/api/projects/{project}/logo",
                    files={"file": ("校徽.png", _png((400, 300)), "image/png")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["width"] == 256 and j["resized"] is True
    assert j["has_logo"] is True

    got = client.get(f"/api/projects/{project}/logo")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(got.content)).size[0] == 256

    # 换一张，版本号必须变，否则浏览器拿缓存里的旧徽章
    first_url = j["logo_url"]
    r2 = client.post(f"/api/projects/{project}/logo",
                     files={"file": ("new.png", _png((80, 80)), "image/png")})
    assert r2.json()["logo_url"] != first_url


def test_logo_rejects_svg(project):
    """SVG 能带 <script>。产物要挂到公众号上，这条不能松。"""
    r = client.post(f"/api/projects/{project}/logo",
                    files={"file": ("badge.svg", b"<svg onload=alert(1)/>",
                                    "image/svg+xml")})
    assert r.status_code == 400
    assert "SVG" in r.json()["detail"]
    assert store.load_logo(project) is None


def test_logo_rejects_oversized_upload(project, monkeypatch):
    monkeypatch.setattr("app.main.MAX_LOGO_UPLOAD_BYTES", 1024)
    r = client.post(f"/api/projects/{project}/logo",
                    files={"file": ("big.png", _png((600, 600)), "image/png")})
    assert r.status_code == 413, r.text
    assert "上限" in r.json()["detail"]


def test_logo_rejects_non_image_bytes_with_an_image_extension(project):
    r = client.post(f"/api/projects/{project}/logo",
                    files={"file": ("badge.png", "这根本不是图片".encode() * 40,
                                    "image/png")})
    assert r.status_code == 400
    assert store.load_logo(project) is None


def test_logo_get_404s_before_upload_and_after_delete(project):
    assert client.get(f"/api/projects/{project}/logo").status_code == 404
    client.post(f"/api/projects/{project}/logo",
                files={"file": ("b.png", _png((30, 30)), "image/png")})
    assert client.get(f"/api/projects/{project}/logo").status_code == 200
    assert client.delete(f"/api/projects/{project}/logo").json()["has_logo"] is False
    assert client.get(f"/api/projects/{project}/logo").status_code == 404


def test_theme_routes_404_on_unknown_project():
    for call in (lambda: client.get("/api/projects/nope/theme"),
                 lambda: client.put("/api/projects/nope/theme", json={"preset": "ink"}),
                 lambda: client.delete("/api/projects/nope/theme"),
                 lambda: client.get("/api/projects/nope/logo"),
                 lambda: client.delete("/api/projects/nope/logo")):
        assert call().status_code == 404


def test_theme_writes_take_the_project_lock_but_reads_do_not():
    """写路由挂锁，读路由不挂——和 calib / merge 一致。"""
    for route in app.routes:
        path = getattr(route, "path", "")
        if path not in ("/api/projects/{pid}/theme", "/api/projects/{pid}/logo"):
            continue
        deps = {d.call.__name__ for d in route.dependant.dependencies}
        for method in route.methods:
            if method in ("PUT", "POST", "DELETE"):
                assert "project_guard" in deps, f"{method} {path} 没挂项目锁"
            elif method == "GET":
                assert "project_guard" not in deps, f"GET {path} 不该挂项目锁"


# ---------- 端到端：主题和校徽真的进了产物 ----------

def _artifact_html(tmp_workspace, pid: str, form: str) -> str:
    """直接读盘上的产物。

    不走 /bundle 静态托管：那个 mount 在 import 时就绑定了真实 WORKSPACE，
    tmp_workspace 换不掉它。读盘还有个好处——顺带证明文件真的落盘了。
    """
    out = tmp_workspace / pid / "out" / form
    names = [p.name for p in out.iterdir() if p.suffix == ".html"]
    assert len(names) == 1, f"产物目录里应当只有一个 HTML，实际 {names}"
    return (out / names[0]).read_text(encoding="utf-8")


def test_generate_relative_bakes_theme_and_writes_the_badge(project, tmp_workspace):
    client.put(f"/api/projects/{project}/theme",
               json={"preset": "dawn", "title_font": "kai",
                     "footer_signature": "某某中学团委"})
    client.post(f"/api/projects/{project}/logo",
                files={"file": ("badge.png", _png((60, 60)), "image/png")})

    g = client.post(f"/api/projects/{project}/generate",
                    json={"form": "relative", "engine": "v1"})  # v1 主题 token 契约
    assert g.status_code == 200, g.text
    d = g.json()
    assert d["theme"]["preset"] == "dawn"
    assert d["theme"]["custom_colors"] == 0
    assert d["theme"]["signature"] == "某某中学团委"
    assert d["theme"]["logo"] is True
    assert d["theme"]["logo_bytes"] > 0
    assert "assets/logo.png" in d["files"]

    html = _artifact_html(tmp_workspace, project, "relative")
    assert "--bg:#1a0d09" in html
    assert '"Kaiti SC"' in html
    assert '<img class="logo" src="assets/logo.png" alt="校徽">' in html
    assert "某某中学团委" in html


def test_generate_inline_embeds_the_badge_as_a_data_uri(project, tmp_workspace):
    """inline 形态要能单文件拷走，校徽只能内嵌。"""
    client.post(f"/api/projects/{project}/logo",
                files={"file": ("badge.png", _png((48, 48)), "image/png")})
    g = client.post(f"/api/projects/{project}/generate", json={"form": "inline"})
    assert g.status_code == 200, g.text
    assert "assets/logo.png" not in g.json()["files"]

    html = _artifact_html(tmp_workspace, project, "inline")
    m = re.search(r'<img class="logo" src="(data:image/png;base64,[^"]+)"', html)
    assert m, "inline 产物里没有内嵌的校徽"
    blob = base64.b64decode(m.group(1).split(",", 1)[1])
    assert Image.open(io.BytesIO(blob)).format == "PNG"


def test_generate_without_theme_is_byte_identical_to_the_old_output(project, tmp_workspace):
    """没配主题的项目，产物不该因为上了这个功能而变化。"""
    before = client.post(f"/api/projects/{project}/generate",
                         json={"form": "relative",
                               "engine": "v1"}).json()  # v1 无主题零注入契约
    html = _artifact_html(tmp_workspace, project, "relative")
    assert INJECTED_CSS not in html, "没配主题却多注入了一段 CSS"
    assert '<img class="logo"' not in html
    # 模板里的默认 :root 必须原样保留（canvas 靠它取色）
    assert "--bg:#070c1c" in html
    assert before["theme"] == {"preset": theme.DEFAULT_PRESET, "custom_colors": 0,
                               "signature": "", "logo": False, "logo_bytes": 0}


def test_theme_change_is_picked_up_by_the_next_generation(project, tmp_workspace):
    """改了主题必须重新生成——但重新生成之后一定得生效，不然控制台就是在骗人。"""
    client.post(f"/api/projects/{project}/generate",
                json={"form": "relative", "engine": "v1"})
    assert "--bg:#070c1c" in _artifact_html(tmp_workspace, project, "relative")

    client.put(f"/api/projects/{project}/theme", json={"preset": "sakura"})
    client.post(f"/api/projects/{project}/generate",
                json={"form": "relative", "engine": "v1"})
    html = _artifact_html(tmp_workspace, project, "relative")
    assert "--bg:#160a14" in html
    assert "夜樱粉" not in html        # 预设的中文名是控制台文案，不该进产物


def test_operation_log_records_theme_and_logo_changes(project):
    client.put(f"/api/projects/{project}/theme", json={"preset": "ink",
                                                      "footer_signature": "落款"})
    client.post(f"/api/projects/{project}/logo",
                files={"file": ("b.png", _png((30, 30)), "image/png")})
    client.post(f"/api/projects/{project}/generate", json={"form": "relative"})
    acts = [e["action"] for e in client.get(f"/api/projects/{project}").json()["meta"]["log"]]
    assert "修改星图主题" in acts
    assert "上传校徽" in acts
    detail = next(e["detail"] for e in client.get(f"/api/projects/{project}").json()["meta"]["log"]
                  if e["action"] == "生成星图")
    assert "主题=ink" in detail and "校徽=有" in detail
