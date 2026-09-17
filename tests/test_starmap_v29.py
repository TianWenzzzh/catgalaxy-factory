"""T6 F2/F3 · v29 全特性引擎（starmap_render + injector 适配）单测。

覆盖：三种分片形态、F11 空值零差异与有值注入、XSS 转义、校徽白名单、
模板 token 全替换、与旧引擎共有的猫字段一致性。
"""
from __future__ import annotations

import re

import pytest

from app import starmap_render as sr
from app.injector import logo_tag, render_starmap_v29
from app.models import CatRow
from app.theme import Theme


def _row(i: int = 1, **kw) -> CatRow:
    base = dict(line=i + 1, id=f"CAT-{i:03d}", name=f"猫{i}", rank="中士",
                title="巡逻", coat="橘白", features="体型胖 粉鼻",
                photo_file=f"p{i}.jpg", photo_count=2, area="宿舍窗台",
                related="batch1:1", confidence="高", note="")
    base.update(kw)
    return CatRow(**base)


JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c23"
    "1c1c2837292c30313434341f27393d38323c2e333432ffc0000b08000100010101"
    "1100ffc4001f000001050101010101010000000000000000010203040506070809"
    "0a0bffda0008010100003f00fbd0ffd9")


def _photos(n: int) -> dict[str, bytes]:
    return {f"p{i}.jpg": JPEG for i in range(1, n + 1)}


def _bundle(**kw):
    rows = [_row(i) for i in range(1, 3)]
    kw0 = dict(school="示例校", rows=rows, photos=_photos(2), map_bytes=JPEG,
               generated_on="2026-09")
    kw0.update(kw)
    return render_starmap_v29(**kw0)


# ---------- 三种分片形态 ----------

def test_lazy_layout_only_critical_chunk_eager():
    b = _bundle(photo_loading="lazy")
    assert re.findall(r'<script src="assets/(photo-data-\d+\.js)">', b.html) == [
        "photo-data-00.js"]
    assert "const __PM=" in b.html
    assert b.first_index == 0
    assert b.groups[0] == ["map.jpg"]
    assert b.key_chunk["map.jpg"] == 0
    assert set(b.key_chunk) == {"map.jpg", "p1.jpg", "p2.jpg"}
    # 猫照片分片编号 1 起
    assert sorted(b.key_chunk[k] for k in ("p1.jpg", "p2.jpg")) == [1, 1]


def test_eager_layout_keeps_legacy_numbering():
    b = _bundle(photo_loading="eager")
    tags = re.findall(r'<script src="assets/(photo-data-\d+\.js)">', b.html)
    assert tags[0] == "photo-data-01.js"
    assert "const __PM=" not in b.html
    assert b.first_index == 1


def test_relative_layout_no_inline_chunks():
    b = _bundle(photo_loading="relative")
    # 懒加载器代码驻留模板（休眠），但不得有分片脚本标签与 __PM 清单
    assert '<script src="assets/photo-data-' not in b.html
    assert "const __PM=" not in b.html
    assert b.groups == []
    assert b.first_index == 0


def test_chunk_text_roundtrip_keys():
    b = _bundle(photo_loading="lazy")
    chunks = b.iter_chunks(lambda k: JPEG if k == "map.jpg" else _photos(2)[k])
    names = [n for n, _ in chunks]
    assert names == ["photo-data-00.js", "photo-data-01.js"]
    assert "__PHOTOS[\"map.jpg\"]" in chunks[0][1]
    assert "__PHOTOS[\"p1.jpg\"]" in chunks[1][1]


# ---------- 模板完整性 ----------

def test_no_unreplaced_tokens_and_features_present():
    html = _bundle().html
    assert not re.findall(r"__[A-Z_]{3,}__", html)
    for needle in ('id="btnGallery"', 'id="passP"', 'id="soulP"',
                   'function __ensurePhoto(', 'const MAP_SRC = "map.jpg";'):
        assert needle in html, needle
    # 计数来自真实行数
    assert "已遇见 0 / 2" in html


def test_canonical_cats_have_only_v29_fields():
    b = _bundle()
    m = re.search(r"const CATS = \[(.*?)\];", b.html, re.S)
    assert m and 'id:"CAT-001"' in m.group(1)   # v2.7 风格：裸键 + 双引号值
    # 工厂扩展键不得泄漏进 v29 CATS
    for forbidden in ('starColor', 'starRadius', 'confidence',
                      'related', 'zone'):
        assert forbidden not in m.group(1), forbidden


# ---------- F11 空值/有值 ----------

def test_f11_default_is_byte_clean():
    b = _bundle()
    # 默认 Theme（midnight）→ css_block 空；无校徽
    assert "/* 主题：由喵星图工厂按项目配置注入 */" not in b.html
    assert '<img class="logo"' not in b.html
    assert b.html.count('<div id="intro">') == 1
    assert b.html.count('<div class="brand">') == 1


def test_f11_custom_theme_and_logo_injected():
    theme = Theme(preset="ink")   # 非默认预设 → 产生 CSS 覆盖块
    tag = logo_tag("assets/logo.png")
    b = _bundle(theme=theme, logo_tag_html=tag)
    assert "/* 主题：由喵星图工厂按项目配置注入 */" in b.html
    assert b.html.count(tag) == 2          # 开场 + 顶栏
    assert '<div id="intro">' + tag in b.html
    assert '<div class="brand">' + tag in b.html


def test_logo_tag_html_must_come_from_whitelist():
    with pytest.raises(ValueError):
        _bundle(logo_tag_html='<img src="x" onerror="alert(1)">')


# ---------- 安全 ----------

def test_malicious_school_name_is_escaped_in_js():
    rows = [_row(1, name='阿橘"),alert(1),("')]
    b = render_starmap_v29(
        school='测试校</script><script>alert(1)</script>',
        rows=rows, photos=_photos(1), map_bytes=JPEG,
        generated_on="2026-09")
    assert "<script>alert(1)</script>" not in b.html
    assert "<\\/script>" in b.html
    assert not re.findall(r"__[A-Z_]{3,}__", b.html)


# ---------- 与旧引擎共有字段一致 ----------

def test_v1_v29_shared_cat_fields_identical():
    from app.injector import render_starmap
    rows = [_row(1), _row(2)]
    b29 = render_starmap_v29(school="示例校", rows=rows, photos=_photos(2),
                             map_bytes=JPEG, generated_on="2026-09")
    h1 = render_starmap(school="示例校", rows=rows)

    def cats_of(html: str) -> list[dict]:
        """v1 产物是严格 JSON；v29 沿用 v2.7 风格（裸键 + 双引号值 +
        去前导零数字）。剥外层中括号、补键引号、补小数前导零后统一解析。"""
        import json
        m = re.search(r"const CATS = (\[.*?\]);", html, re.S)
        assert m
        body = m.group(1).strip()
        if body.startswith("["):
            body = body[1:-1]
        body = re.sub(r'([{,]\s*)([A-Za-z_]\w*)(\s*:)', r'\1"\2"\3', body)
        body = re.sub(r':\s*\.(\d)', r': 0.\1', body)
        return json.loads("[" + body + "]")

    c1, c29 = cats_of(h1), cats_of(b29.html)
    assert len(c1) == len(c29) == 2
    shared = {"id", "name", "rank", "title", "coat", "features", "area",
              "photo", "photoCount", "brightness"}
    for a, b in zip(c1, c29):
        for k in shared:
            if k == "brightness":
                assert abs(float(a[k]) - float(b[k])) < 1e-9, k
            elif k == "photoCount":
                assert int(a[k]) == int(b[k]), k
            else:
                assert a[k] == b[k], (k, a[k], b[k])
        # v29 独有的小传；v1 用 make_bio 同函数生成时也应一致
        assert "bio" in b


# ---------- 引擎选择（API 层 e2e；默认仍 v1） ----------

@pytest.fixture
def client(tmp_workspace):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def _ready(client, two_row_csv):
    from conftest import make_jpeg
    r = client.post("/api/projects", json={"school": "示例校"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/roster",
                files={"file": ("猫咪名册.csv", two_row_csv.encode("utf-8-sig"),
                                "text/csv")})
    files = [("files", (n, make_jpeg(900, 700), "image/jpeg"))
             for n in ("demo-001.jpg", "demo-002.jpg")]
    up = client.post(f"/api/projects/{pid}/photos", files=files)
    assert up.json()["report"]["summary"]["ok"] is True
    return pid


def _html_of(client, d: dict) -> str:
    """从 zip 产物读 HTML（e2e 惯例：/bundle 静态挂载绑定导入期 workspace，
    tmp_workspace 下直接 GET preview_url 会 404）。"""
    import io
    import zipfile
    zr = client.get(f"/api/projects/{d['project_id']}/download?form={d['form']}")
    assert zr.status_code == 200, zr.text
    with zipfile.ZipFile(io.BytesIO(zr.content)) as zf:
        return zf.read(d["html_name"]).decode("utf-8")


def test_api_engine_v29_inline(client, two_row_csv):
    pid = _ready(client, two_row_csv)
    g = client.post(f"/api/projects/{pid}/generate",
                    json={"form": "inline", "engine": "v29"})
    assert g.status_code == 200, g.text
    d = g.json()
    assert d["engine"] == "v29"
    html = _html_of(client, d)
    assert 'id="btnGallery"' in html                  # v29 全特性（影廊等）
    assert "const __PM=" in html                     # lazy 清单
    assert '<script src="assets/photo-data-00.js"></script>' in html
    assert not re.findall(r"__[A-Z_]{3,}__", html)   # token 全替换
    # 默认引擎仍是 v1：不带 engine 的请求出旧模板产物
    g1 = client.post(f"/api/projects/{pid}/generate", json={"form": "inline"})
    assert g1.status_code == 200, g1.text
    assert g1.json()["engine"] == "v1"
    assert 'id="btnGallery"' not in _html_of(client, g1.json())


def test_api_engine_rejects_bad_and_relative_v29(client, two_row_csv):
    pid = _ready(client, two_row_csv)
    assert client.post(f"/api/projects/{pid}/generate",
                       json={"form": "inline", "engine": "v8"}).status_code == 422
    # v29 的 relative 形态随 F3 开放，此前明确拒绝
    assert client.post(f"/api/projects/{pid}/generate",
                       json={"form": "relative", "engine": "v29"}).status_code == 409
