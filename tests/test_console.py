"""控制台前端接线检查。

前端是原生 JS、没有构建、没有类型检查，所以有两类 bug 只会在线上出现：

1. `$("#someId")` 拿一个 index.html 里已经被改名/删掉的 id —— 返回 null，
   下一行 `.addEventListener` 直接 TypeError，整张卡片的按钮全哑掉。
2. `fetch("/api/...")` 打一个后端根本没有的路径 —— 404，被 catch 成一句
   toast，用户只看到「失败」，看不出是自己人写错了地址。

这两件事都能靠比对两个文件静态查出来，比开浏览器点一遍便宜得多，也不会
因为某次重构悄悄退化。真正的交互（拖拽标定、iframe 通信）还是得手工验。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import theme as th
from app.main import app

client = TestClient(app)

STATIC = Path(__file__).resolve().parents[1] / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

# 只认「纯 id 选择器」：$("#foo") / $$("#foo")。
# $("#report .filters button") 这种复合选择器不比——里面的后代是 JS 现生成的。
PURE_ID = re.compile(r"""\$\$?\(\s*["']#([A-Za-z][\w-]*)["']\s*\)""")
API_PATH = re.compile(r"""[`"'](/api/[^`"'\s?]*)[`"']""")
SEG = re.compile(r"\{[^}]*\}")          # 路径参数名两边不一样（前端 ${state.pid} → {x}，后端 {pid}）

ALL_IDS = re.findall(r'''\bid="([\w-]+)"''', HTML)


@pytest.fixture(scope="module")
def routes() -> dict:
    return client.get("/openapi.json").json()["paths"]


# ---------- id 接线 ----------

def test_every_static_id_lookup_resolves():
    """app.js 里写死的每个 #id 都得在 index.html 里存在。"""
    ids = set(ALL_IDS)
    missing = sorted(i for i in set(PURE_ID.findall(JS)) if i not in ids)
    assert not missing, f"app.js 引用了 index.html 里不存在的 id：{missing}"


def test_no_duplicate_ids():
    """重复 id 时 querySelector 只返回第一个，第二个控件永远收不到事件。"""
    dupes = sorted({i for i in ALL_IDS if ALL_IDS.count(i) > 1})
    assert not dupes, f"index.html 有重复 id：{dupes}"


def test_theme_card_is_present_and_wired():
    """F11 的每个控件都要有 JS 接。少一个就是「面板上摆着一个不动的按钮」。"""
    for pid in ("presetBox", "colorBox", "titleFont", "bodyFont", "signature",
                "dropLogo", "logoFile", "logoState", "logoPreview",
                "btnThemeReset", "btnLogoDel", "themeState", "themeRejected"):
        assert pid in ALL_IDS, f"index.html 缺 #{pid}"
        assert f"#{pid}" in JS, f"#{pid} 在 app.js 里没人管"


def test_theme_panel_is_reloaded_when_a_project_is_opened():
    """切换项目不重载主题，面板会一直显示上一个项目的预设和校徽——那是骗人的。"""
    assert JS.count("await loadTheme()") >= 2, "创建项目和打开项目两处都该调 loadTheme()"


# ---------- 路由接线 ----------

def canon(path: str) -> str:
    """把 ${...} / {pid} / {gid} 一律压成 {}，两边才能对上。"""
    return SEG.sub("{}", re.sub(r"\$\{[^}]*\}", "{}", path))


def api_paths_in_js() -> set[str]:
    return {canon(p) for p in API_PATH.findall(JS)}


def test_every_api_path_the_console_calls_exists(routes):
    """前端调的每个后端路径都得真的注册了。"""
    known = {canon(p) for p in routes}
    missing = sorted(p for p in api_paths_in_js() if p not in known)
    assert not missing, f"app.js 调了不存在的路径：{missing}"


def test_theme_endpoints_offer_the_methods_the_console_uses(routes):
    by_canon = {canon(p): v for p, v in routes.items()}
    expect = {
        "/api/theme/options": ["get"],
        "/api/projects/{}/theme": ["get", "put", "delete"],
        "/api/projects/{}/logo": ["get", "post", "delete"],
    }
    for path, methods in expect.items():
        assert path in by_canon, f"缺路由 {path}"
        for m in methods:
            assert m in by_canon[path], f"{path} 没有 {m.upper()}"


# ---------- 载荷字段 ----------

def test_theme_payload_carries_everything_the_panel_paints(tmp_workspace):
    """paintTheme() 读的字段一个都不能少——少一个就是一块永远空白的 UI。"""
    pid = client.post("/api/projects", json={"school": "接线校"}).json()["id"]
    j = client.get(f"/api/projects/{pid}/theme").json()
    for key in ("theme", "effective_colors", "has_logo", "logo_url", "rejected", "note"):
        assert key in j, f"响应缺 {key}"
    for key in ("preset", "colors", "title_font", "body_font", "footer_signature"):
        assert key in j["theme"], f"theme 缺 {key}"

    opts = client.get("/api/theme/options").json()
    # buildThemeUI 逐个读这些字段来生成控件
    assert opts["presets"][0].keys() >= {"key", "label", "blurb", "swatch"}
    assert opts["fonts"][0].keys() >= {"key", "label"}
    assert opts["colors"][0].keys() >= {"key", "label", "css"}
    # 颜色控件的值直接取 effective_colors，缺一个键就有一个色块永远停在黑色
    assert {c["key"] for c in opts["colors"]} <= set(j["effective_colors"])


def test_effective_colors_track_preset_and_custom_override(tmp_workspace):
    """生效色 = 预设打底 + 自定义覆盖。前端不再自己拼预设调色板，全靠这个字段。"""
    pid = client.post("/api/projects", json={"school": "生效色校"}).json()["id"]
    url = f"/api/projects/{pid}/theme"
    base = client.get(url).json()["effective_colors"]
    assert base == th.PRESETS[th.DEFAULT_PRESET].colors

    client.put(url, json={"preset": "ink"})
    ink = client.get(url).json()["effective_colors"]
    assert ink == th.PRESETS["ink"].colors
    assert ink != base

    # 覆盖一项：只有这一项变，其余仍是 ink 预设的值
    client.put(url, json={"colors": {"bg": "#123456"}})
    j = client.get(url).json()
    assert j["effective_colors"]["bg"] == "#123456"
    assert {k: v for k, v in j["effective_colors"].items() if k != "bg"} == \
        {k: v for k, v in th.PRESETS["ink"].colors.items() if k != "bg"}
    assert j["theme"]["colors"] == {"bg": "#123456"}      # 存的是覆盖项，不是全量
