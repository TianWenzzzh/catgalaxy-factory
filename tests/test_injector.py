"""F3 · 模板注入单测。"""
import json
import re

import pytest

from app.injector import (CATS_TOKEN, PHOTO_SCRIPTS_TOKEN, build_cat_entries,
                          iter_photo_chunks, photo_script_tags, plan_photo_chunks,
                          render_starmap)
from app.models import CatRow


def _row(i: int = 1, **kw) -> CatRow:
    base = dict(line=i + 1, id=f"CAT-{i:03d}", name=f"猫{i}", rank="中士", title="巡逻",
                coat="橘白", features="体型胖 粉鼻", photo_file=f"p{i}.jpg",
                photo_count=2, area="宿舍窗台", related="batch1:1", confidence="高", note="")
    base.update(kw)
    return CatRow(**base)


def test_build_cat_entries_and_calib():
    rows = [_row(i) for i in range(1, 6)]
    entries, calib = build_cat_entries(rows)
    assert len(entries) == 5
    assert set(calib) == {e["id"] for e in entries}
    for p in calib.values():
        assert 0.0 <= p["x"] <= 1.0 and 0.0 <= p["y"] <= 1.0
    assert all("zone" in e for e in entries)


def test_entries_are_json_serializable():
    entries, calib = build_cat_entries([_row(1)])
    json.dumps(entries, ensure_ascii=False)
    json.dumps(calib, ensure_ascii=False)


def test_render_replaces_all_tokens():
    html = render_starmap(school="示例校", rows=[_row(1), _row(2)])
    assert CATS_TOKEN not in html
    assert PHOTO_SCRIPTS_TOKEN not in html
    for token in ("__TITLE__", "__SUBTITLE__", "__STATS__", "__FOOTER__",
                  "__SCHOOL__", "__MAP_SRC__", "__LS_KEY__"):
        assert token not in html, f"残留占位符 {token}"
    assert "/*__CALIB__*/" not in html
    assert "/*__ZONES__*/" not in html


def test_render_embeds_school_and_cats():
    html = render_starmap(school="中北大学", rows=[_row(1, name="墩墩")])
    assert "中北大学喵星图" in html
    assert "墩墩" in html
    assert '"CAT-001"' in html
    assert "assets/photos/p1.jpg" in html


def test_render_cat_count_matches_rows():
    rows = [_row(i) for i in range(1, 11)]
    html = render_starmap(school="测试校", rows=rows)
    m = re.search(r"const CATS = (\[.*?\]);\nconst CALIB", html, re.S)
    assert m, "未能从产物中定位 CATS 数组"
    data = json.loads(m.group(1).replace("<\\/", "</"))
    assert len(data) == 10
    assert [d["id"] for d in data] == [f"CAT-{i:03d}" for i in range(1, 11)]


def test_render_calib_matches_cats():
    rows = [_row(i) for i in range(1, 5)]
    html = render_starmap(school="测试校", rows=rows)
    m = re.search(r"const CALIB = (\{.*?\});\nconst MAP_SRC", html, re.S)
    assert m
    calib = json.loads(m.group(1))
    assert len(calib) == 4


def test_render_inline_adds_script_tags():
    html = render_starmap(school="测试校", rows=[_row(1)], form="inline",
                          photo_script_names=["photo-data-01.js", "photo-data-02.js"])
    assert '<script src="assets/photo-data-01.js"></script>' in html
    assert '<script src="assets/photo-data-02.js"></script>' in html


def test_render_relative_has_no_photo_scripts():
    html = render_starmap(school="测试校", rows=[_row(1)], form="relative")
    assert '<script src="assets/photo-data-' not in html


def test_render_empty_rows_still_valid():
    html = render_starmap(school="空校", rows=[])
    assert "const CATS = [];" in html
    assert CATS_TOKEN not in html


def test_script_tag_escaping():
    """CSV 内容里若含 </script> 必须被转义，否则会截断脚本。"""
    evil = _row(1, name='坏猫</script><script>alert(1)</script>')
    html = render_starmap(school="测试校", rows=[evil])
    # CATS 数组内部不能出现裸露的 </script>
    body = html.split("const CATS = ", 1)[1].split(";\nconst CALIB", 1)[0]
    assert "</script>" not in body
    assert "<\\/" in body


def test_iter_photo_chunks_single():
    photos = {"a.jpg": b"\xff\xd8\xff\xe0fakejpeg"}
    sizes = [(n, len(b)) for n, b in photos.items()]
    chunks = list(iter_photo_chunks(sizes, photos.__getitem__))
    assert len(chunks) == 1
    name, content = chunks[0]
    assert name == "photo-data-01.js"
    assert name == plan_photo_chunks(sizes)[0]
    assert content.startswith("window.__PHOTOS=window.__PHOTOS||{};")
    assert 'data:image/jpeg;base64,' in content
    assert '"a.jpg"' in content


def test_iter_photo_chunks_splits_by_size():
    photos = {f"p{i:02d}.jpg": b"x" * 30000 for i in range(10)}
    sizes = [(n, len(b)) for n, b in photos.items()]
    chunks = list(iter_photo_chunks(sizes, photos.__getitem__, chunk_bytes=45000))
    assert len(chunks) > 1
    names = [n for n, _ in chunks]
    assert names == sorted(names)
    assert names == plan_photo_chunks(sizes, 45000)
    assert all(re.fullmatch(r"photo-data-\d{2}\.js", n) for n in names)
    joined = "".join(c for _, c in chunks)
    for i in range(10):
        assert f'"p{i:02d}.jpg"' in joined


def test_iter_photo_chunks_empty():
    assert list(iter_photo_chunks([], lambda name: b"")) == []
    assert plan_photo_chunks([]) == []


def test_photo_script_tags():
    assert photo_script_tags([]) == ""
    assert photo_script_tags(["a.js"]) == '<script src="assets/a.js"></script>'


def test_render_uses_custom_subtitle_and_stats():
    html = render_starmap(school="X校", subtitle="自定义副标题", rows=[_row(1)],
                          stats="自定义统计 42 只")
    assert "自定义副标题" in html
    assert "自定义统计 42 只" in html


def test_render_map_src_injected():
    html = render_starmap(school="X校", rows=[_row(1)], map_filename="assets/map.jpg")
    assert 'const MAP_SRC = "assets/map.jpg";' in html
