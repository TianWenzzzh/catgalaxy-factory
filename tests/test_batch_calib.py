"""F8 批量标定：Shift+拖拽 = 整出没分区平移。

76 只逐星拖点要十几分钟，标定漂移往往是「整片偏」——按分区整体平移能把
十几分钟压到一两分钟（第二轮 §七-3）。这里按 test_console.py 的先例做
**源码级接线断言**：模板是浏览器里的 canvas 渲染器，pytest 跑不了 JS，
能锁的是「handler 真在、坐标真写进 USER、文案真告诉用户有这回事」。
行为证据由 scripts/shot.py 的分区平移截图补（docs/截图/）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "app" / "templates" / "starmap.html").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _between(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_shift_drag_starts_a_zone_drag_not_a_single_star_drag():
    """标定模式下 Shift 按下拖星，起的是「整分区」拖动而不是单星拖动。"""
    down = _between(TEMPLATE, 'addEventListener("pointerdown"', 'addEventListener("pointermove"')
    assert "shiftKey" in down, "pointerdown 里没有 Shift 分支"
    assert "dragZone" in down, "pointerdown 没有建立分区拖动状态"
    assert "pick(" in down, "分区拖动也要先拾取锚点星"


def test_zone_drag_moves_every_member_of_the_anchor_zone():
    """平移量作用到锚点星所在分区的**每一只**，各自 clamp 进 [0,1]。"""
    move = _between(TEMPLATE, 'addEventListener("pointermove"', 'addEventListener("pointerup"')
    zone_block = _between(move, "dragZone", "lx = e.clientX")
    assert "c.zone ===" in zone_block, "成员判定要按锚点星的分区筛"
    assert "USER[" in zone_block, "平移结果要写进 USER（保存链路才有得读）"
    assert "clamp(" in zone_block, "每只成员都要 clamp，不能平移出底图"


def test_zone_drag_persists_on_pointerup():
    up = _between(TEMPLATE, 'addEventListener("pointerup"', 'addEventListener("wheel"')
    assert "dragZone" in up and "saveUserPos()" in up, "松手要落盘，否则刷新就丢"
    assert "dragZone = null" in up, "松手要清状态，否则下一轮拖拽接着漂"


def test_plain_click_still_opens_card_when_zone_drag_did_not_move():
    """Shift 点一下没拖 = 没意图平移，不该吞掉原来的「点开档案」。"""
    up = _between(TEMPLATE, 'addEventListener("pointerup"', 'addEventListener("wheel"')
    lines = up.splitlines()
    guard = [i for i, l in enumerate(lines) if l.strip().startswith("if(dragging && !moved")]
    open_at = [i for i, l in enumerate(lines) if "openCard(hit)" in l]
    assert guard and open_at and guard[0] < open_at[0], "点开档案要受「没在拖」的守卫罩着"
    assert "!dragCat" in lines[guard[0]] and "!dragZone" in lines[guard[0]], \
        "守卫条件里要同时带上两种拖动状态"


def test_hover_tip_and_f8_card_both_mention_shift_drag():
    """能力不写出来等于没有：hover 提示与 F8 卡片都要说清 Shift+拖。"""
    assert "Shift" in _between(TEMPLATE, "tip.textContent = state.calib", "tip.style.left")
    card = _between(INDEX, "星位标定", "标定写入项目的")
    assert "Shift" in card and "平移" in card, "F8 卡片没说明 Shift+拖拽的批量能力"
