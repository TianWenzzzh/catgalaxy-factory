"""工厂自家 CSS 的 Safari 前缀防回归（v33 那轮的 5 处漏项就出在这里）。

Safari 只认 `-webkit-backdrop-filter`，漏前缀的面板在 Safari 上背景不糊。
2026-09 深扫数出独立改动面 21 处 / 4 个源文件：v29 模板 15（与 16 侧镜像同处，
由 meow-starmap 的 make_v33 golden 链负责）、16 gallery 1 处（由该仓
tests/test_css_compat_prefixes.py 负责），剩下 **本仓的 static/style.css 2 处 +
v1 引擎模板 3 处** 由这里守——它们不在任何 golden 链里，没人盯就会再漏。

跑：python -m pytest tests/test_css_prefixes.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 本仓手维护的样式源 → 该文件里标准 backdrop-filter 声明的应有条数
# （条数也钉住：整段被搬走或悄悄删掉标准声明，同样要红）
SOURCES: dict[str, int] = {
    "app/templates/starmap_v29.html": 17,
    "app/templates/starmap.html": 3,
    "static/style.css": 2,
}

_DECL = re.compile(r"(?<!-webkit-)backdrop-filter[ \t]*:[ \t]*([^;}\n]+)")


def missing(text: str) -> list[str]:
    """返回「同一规则块内没有 -webkit- 同值兄弟」的标准声明值。"""
    gaps: list[str] = []
    for block in text.split("}"):
        for m in _DECL.finditer(block):
            value = m.group(1).strip()
            # 规则块按 } 切开后，块内最后一条声明没有终止符，故 $ 也算结束
            if not re.search(r"-webkit-backdrop-filter[ \t]*:[ \t]*"
                             + re.escape(value) + r"[ \t]*(?:[;}]|$)", block):
                gaps.append(value)
    return gaps


def test_detector_catches_bare_and_accepts_paired() -> None:
    """检测器自检：裸声明必须被抓到，成对必须放行——否则本门禁在空转。"""
    bare = ".a{backdrop-filter:blur(12px); color:#fff}"
    paired = ".a{backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}"
    cross_rule = ".a{backdrop-filter:blur(9px)}.b{-webkit-backdrop-filter:blur(9px)}"
    assert missing(bare) == ["blur(12px)"], \
        f"检测器漏报裸声明：{missing(bare)}"
    assert missing(paired) == [], f"检测器把成对声明误报：{missing(paired)}"
    # 兄弟声明必须在同一个规则块里，跨规则不算数
    assert len(missing(cross_rule)) == 1, missing(cross_rule)


def test_backdrop_filter_has_webkit_twin_in_same_rule() -> None:
    for rel, expect in SOURCES.items():
        text = (ROOT / rel).read_text("utf-8")
        found = _DECL.findall(text)
        assert len(found) == expect, \
            f"{rel} 有 {len(found)} 处 backdrop-filter，预期 {expect} 处"
        gaps = missing(text)
        assert gaps == [], f"{rel} 缺 -webkit-backdrop-filter 同值兄弟：{gaps}"
