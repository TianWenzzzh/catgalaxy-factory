"""v35 STAR_ZONE：多边形撒点区。缺省 None=纯矩形（与 v34 逐字节一致）。"""
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


ZONE = [[.138, .159], [.841, .159], [.841, .808], [.138, .808]]


def test_default_zone_is_null_and_byte_identical():
    """不传 star_zone 与显式 None 逐字节相同，且字面量是 null（矩形行为原样）。"""
    a = sr.render(_inp(star_zone=None))
    b = sr.render(_inp(star_zone=[]))
    assert a.html == b.html
    assert "const STAR_ZONE=null;" in a.html
    assert a.html.count("const STAR_ZONE=") == 1
    assert "__STAR_ZONE__" not in a.html


def test_zone_renders_js_shape_and_rejection_hook():
    """zone 字面量走 js_num3 形状；basePos 带 inZone 拒绝采样钩子。"""
    html = sr.render(_inp(star_zone=ZONE)).html
    assert "const STAR_ZONE=[[.138,.159],[.841,.159],[.841,.808],[.138,.808]];" in html
    assert "if(STAR_ZONE)" in html and "inZone(" in html


def test_zone_validation():
    with pytest.raises(ValueError):
        sr.render_star_zone([[.1, .1], [.9, .9]])              # 少于 3 点
    with pytest.raises(ValueError):
        sr.render_star_zone([[.1, .1], [1.2, .5], [.5, .9]])   # 坐标越界
    with pytest.raises(ValueError):
        sr.render_star_zone([["0.1", ".5"], [.9, .5], [.5, .9]])
    # zone 必须落在 STAR_BOX 采样包络内，否则拒绝采样可能死循环
    with pytest.raises(ValueError):
        sr.render(_inp(star_box={"x0": .0, "y0": .0, "w": .3, "h": .3},
                       star_zone=ZONE))
