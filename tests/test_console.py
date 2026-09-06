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


def test_roster_row_controls_are_present_and_wired():
    """F10 增行表单的每个控件都要有人管，否则面板上摆着一排不动的输入框。"""
    for pid in ("btnAddRow", "addRowBox", "addRowAfter", "addRowFields",
                "btnAddRowGo", "btnAddRowCancel", "addRowState"):
        assert pid in ALL_IDS, f"index.html 缺 #{pid}"
        assert f"#{pid}" in JS, f"#{pid} 在 app.js 里没人管"


def test_delete_row_entry_is_rendered_per_problem_row():
    """每个问题行都要有「删这行」的入口，且带上物理行号。"""
    assert "data-del=" in JS, "renderEditList 没给问题行渲染删除按钮"
    assert "删这行" in JS


def test_delete_row_controls_are_present_and_wired():
    """删除入口不能只挂在「问题行」上。

    名册干净时 renderEditList 一句「无需在线修改」就收工，一个 data-del 都不渲染
    （浏览器里实测过：editList 只有那句空提示）。可现实里最常删的恰恰是没毛病的行
    ——某只猫不再出现了。只有问题行能删，用户就得先把一行改坏才删得掉。
    """
    for pid in ("btnDelRow", "delRowBox", "delRowPick", "btnDelRowGo",
                "btnDelRowCancel", "delRowState"):
        assert pid in ALL_IDS, f"index.html 缺 #{pid}"
        assert f"#{pid}" in JS, f"#{pid} 在 app.js 里没人管"


def test_delete_row_picker_lists_every_row_not_just_problem_rows():
    """下拉要按物理行号列全部行；行号靠 line_offset 换算，别在前端另算一套。"""
    m = re.search(r"function openDelRow\(\)\s*\{(.*?)\n\}", JS, re.S)
    assert m, "app.js 里没有 openDelRow()"
    for token in ("delRowPick", "rows", "line_offset"):
        assert token in m.group(1), \
            f"openDelRow 该用 state.roster.rows + line_offset 生成选项，缺 {token}"


def test_apply_edits_rereads_the_roster_instead_of_repainting_stale_rows():
    """应用改动后必须向服务端重读名册，不能拿改动前那份 state.roster 重画。

    浏览器里实测到的现象：给新增行填上编号 CAT-003、点「应用改动」，报告已经转绿，
    可摊开的那一行标题还写着「(编号空)」——用户会以为自己的改动没生效。
    """
    m = re.search(r"""\$\("#btnApplyEdits"\)\.addEventListener\("click",(.*?)\n\}\);""", JS, re.S)
    assert m, "app.js 里没有 #btnApplyEdits 的点击处理"
    body = m.group(1)
    assert "reloadRoster()" in body, "应用改动后没有重读名册"
    assert "renderEditList(state.roster)" not in body, \
        "还在拿改动前的 state.roster 重画编辑列表（行里的值是旧的）"


def test_console_never_fabricates_a_cat_id():
    """编号由人定（弃用编号不复用是数据红线）。

    后端刻意不补号，前端也不能自作聪明拼一个 CAT-NNN 填进新增行——那样这条红线
    就在没人看得见的地方失效了。
    """
    assert "CAT-" not in JS, "app.js 里出现了写死或拼出来的 CAT- 编号"


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


def test_roster_row_endpoints_offer_the_methods_the_console_uses(routes):
    by_canon = {canon(p): v for p, v in routes.items()}
    expect = {
        "/api/projects/{}/roster/rows": ["post"],
        "/api/projects/{}/roster/rows/{}": ["delete"],
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
