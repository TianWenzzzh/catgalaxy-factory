"""v34 STAR_BOX：建成区框令牌。缺省=整幅画四个常量 ⇒ 不设框的学校零回归。"""
from __future__ import annotations

import pytest

from app import starmap_render as sr


def _inp(**kw) -> sr.V29RenderInput:
    cats = [{"id": "CAT-001", "name": "猫一", "rank": "中士", "title": "巡逻",
             "coat": "橘白", "coatGroup": "橘", "features": "体型胖 粉鼻",
             "area": "宿舍窗台", "bio": "测试条目",
             "photo": "assets/photos/p1.jpg", "photoCount": 1,
             "brightness": 1}]
    base = dict(school="示例校", cats=cats, photos={"p1.jpg": b"jpg"},
                map_bytes=b"jpg")
    base.update(kw)
    return sr.V29RenderInput(**base)


def test_default_star_box_is_byte_identical_to_v33():
    """不传 star_box 的渲染必须与「显式传默认框」逐字节相同 ⇒ 不设框的学校零回归。"""
    a = sr.render(_inp(star_box=None))
    b = sr.render(_inp(star_box={"x0": .07, "y0": .09, "w": .86, "h": .82}))
    assert a.html == b.html
    assert "const STAR_BOX={x0:.07,y0:.09,w:.86,h:.82};" in a.html
    assert "x:.07+r()*.86" not in a.html         # 老写法必须已经消失（防"改了但没咬到"）
    assert a.html.count("const STAR_BOX=") == 1  # 字面量唯一，且是活代码不是注释
    assert "/* const STAR_BOX=" not in a.html    # 防止常量被裹进注释成死代码


def test_star_box_rejects_out_of_range():
    for bad in ({"x0": 1.2, "y0": .09, "w": .1, "h": .1},
                {"x0": .5, "y0": .5, "w": .9, "h": .1},
                {"x0": "0.5", "y0": .09, "w": .86, "h": .82}):
        with pytest.raises(ValueError):
            sr.render_star_box(bad)


def test_star_box_renders_js_num3_shape():
    """nuc 的框（build_meta 值）渲染成 .154 这种去前导零形状。"""
    out = sr.render_star_box({"x0": 0.154, "y0": 0.294, "w": 0.646, "h": 0.448})
    assert out == "const STAR_BOX={x0:.154,y0:.294,w:.646,h:.448};"


def test_star_box_token_fully_replaced():
    """模板 token 必须被替换干净（渲染层已有残留 token 门禁，这里钉死本令牌）。"""
    html = sr.render(_inp()).html
    assert "__STAR_BOX__" not in html
    assert "STAR_BOX.x0+r()*STAR_BOX.w" in html   # basePos 确实改读框
