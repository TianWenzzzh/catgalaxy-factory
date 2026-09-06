"""F5 · 打包单测。"""
import re
import zipfile
from datetime import date
from pathlib import Path

from app.packager import (build_inline_bundle, build_relative_bundle, bundle_size,
                          html_basename, make_zip, zip_basename)


def test_zip_basename_format():
    name = zip_basename("中北大学", "2026-09-06")
    assert name == "中北大学-校园猫咪星图-2026-09-06"
    assert re.fullmatch(r".+-校园猫咪星图-\d{4}-\d{2}-\d{2}", name)


def test_zip_basename_defaults_to_today():
    assert zip_basename("示例校").endswith(date.today().isoformat())


def test_zip_basename_sanitizes_school():
    assert zip_basename('a/b:c*d?"e<f>g|h', "2026-01-01") == "abcdefgh-校园猫咪星图-2026-01-01"
    assert zip_basename("   ", "2026-01-01").startswith("校园-")
    assert zip_basename("", "2026-01-01").startswith("校园-")


def test_zip_basename_strips_spaces():
    assert zip_basename("中北 大学", "2026-01-01") == "中北大学-校园猫咪星图-2026-01-01"


def test_html_basename():
    assert html_basename("中北大学") == "中北大学喵星图.html"
    assert html_basename("") == "校园喵星图.html"


def test_build_relative_bundle(tmp_path):
    dest = tmp_path / "b"
    written = build_relative_bundle(
        dest, html="<html>x</html>", html_name="校喵星图.html",
        photos=[("a.jpg", b"AAA"), ("b.jpg", b"BBB")], map_bytes=b"MAP",
        roster_csv="编号\n", report_md="# 报告", summary_md="# 摘要")
    assert (dest / "校喵星图.html").read_text(encoding="utf-8") == "<html>x</html>"
    assert (dest / "assets" / "photos" / "a.jpg").read_bytes() == b"AAA"
    assert (dest / "assets" / "photos" / "b.jpg").read_bytes() == b"BBB"
    assert (dest / "assets" / "map.jpg").read_bytes() == b"MAP"
    assert (dest / "data" / "猫咪名册.csv").read_text(encoding="utf-8") == "编号\n"
    assert (dest / "校验报告.md").exists()
    assert (dest / "归并决策摘要.md").exists()
    assert "assets/photos/a.jpg" in written
    assert "assets/map.jpg" in written
    assert "校喵星图.html" in written


def test_build_relative_bundle_streams_photos_in_the_given_order(tmp_path):
    """照片可以是生成器：写一张读一张，且顺序由调用方定（函数内部不再排序）。"""
    dest = tmp_path / "b4"
    reads: list[str] = []

    def gen():
        for name in ("z.jpg", "a.jpg", "m.jpg"):
            reads.append(name)
            yield name, name.encode("ascii")

    written = build_relative_bundle(dest, html="h", html_name="x.html", photos=gen())
    assert [w for w in written if w.startswith("assets/photos/")] == [
        "assets/photos/z.jpg", "assets/photos/a.jpg", "assets/photos/m.jpg"]
    assert reads == ["z.jpg", "a.jpg", "m.jpg"]
    assert (dest / "assets" / "photos" / "m.jpg").read_bytes() == b"m.jpg"


def test_build_relative_bundle_optional_files(tmp_path):
    dest = tmp_path / "b2"
    written = build_relative_bundle(dest, html="h", html_name="x.html", photos=())
    assert written == ["x.html"]
    assert not (dest / "assets").exists()


def test_build_relative_bundle_clears_stale(tmp_path):
    dest = tmp_path / "b3"
    dest.mkdir()
    (dest / "old.html").write_text("stale")
    build_relative_bundle(dest, html="h", html_name="new.html", photos=())
    assert not (dest / "old.html").exists()
    assert (dest / "new.html").exists()


def test_build_inline_bundle(tmp_path):
    dest = tmp_path / "i"
    chunks = [("photo-data-01.js", "window.__PHOTOS={};"), ("photo-data-02.js", "//2")]
    written = build_inline_bundle(dest, html="h", html_name="校喵星图.html", chunks=chunks,
                                  roster_csv="编号\n", report_md="# 报告")
    assert (dest / "assets" / "photo-data-01.js").read_text(encoding="utf-8") == "window.__PHOTOS={};"
    assert (dest / "assets" / "photo-data-02.js").exists()
    assert not (dest / "assets" / "photos").exists()   # inline 不带原图目录
    assert "assets/photo-data-01.js" in written


def test_make_zip(tmp_path):
    src = tmp_path / "s"
    (src / "assets" / "photos").mkdir(parents=True)
    (src / "index.html").write_text("<html>")
    (src / "assets" / "photos" / "a.jpg").write_bytes(b"AAA")
    zp = make_zip(src, tmp_path / "dist" / "out.zip")
    assert zp.exists()
    with zipfile.ZipFile(zp) as zf:
        names = set(zf.namelist())
        assert "index.html" in names
        assert "assets/photos/a.jpg" in names
        assert zf.read("assets/photos/a.jpg") == b"AAA"


def test_make_zip_overwrites(tmp_path):
    src = tmp_path / "s2"
    src.mkdir()
    (src / "a.txt").write_text("1")
    zp = tmp_path / "o.zip"
    make_zip(src, zp)
    (src / "b.txt").write_text("2")
    make_zip(src, zp)
    with zipfile.ZipFile(zp) as zf:
        assert set(zf.namelist()) == {"a.txt", "b.txt"}


def test_make_zip_uses_forward_slashes(tmp_path):
    src = tmp_path / "s3"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "x.txt").write_text("x")
    zp = make_zip(src, tmp_path / "o3.zip")
    with zipfile.ZipFile(zp) as zf:
        assert "sub/x.txt" in zf.namelist()
        assert all("\\" not in n for n in zf.namelist())


def test_bundle_size(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"12345")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"678")
    assert bundle_size(tmp_path) == 5 + 3
