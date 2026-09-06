"""照片处理单测：压缩（长边≤1200px、单张≤200KB）、zip 解包、底图生成。"""
import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.config import MAX_PHOTO_BYTES, MAX_PHOTO_SIDE
from app.image_proc import (compress_photo, extract_photo_zip, generate_default_map,
                            is_zip, looks_like_image, map_dimensions, process_and_save,
                            safe_filename)
from conftest import make_jpeg


# ---------- 压缩硬指标 ----------

def test_large_photo_meets_both_limits():
    blob = make_jpeg(3000, 2000, quality=97)
    assert len(blob) > MAX_PHOTO_BYTES
    out, info = compress_photo(blob)
    assert len(out) <= MAX_PHOTO_BYTES
    im = Image.open(io.BytesIO(out))
    assert max(im.size) <= MAX_PHOTO_SIDE
    assert im.format == "JPEG"
    assert info["resized"] is True
    assert info["out_bytes"] == len(out)


def test_huge_photo_still_meets_limits():
    blob = make_jpeg(4600, 3400, quality=98)
    out, info = compress_photo(blob)
    assert len(out) <= MAX_PHOTO_BYTES
    assert max(Image.open(io.BytesIO(out)).size) <= MAX_PHOTO_SIDE


def test_tall_photo_long_side_limited():
    blob = make_jpeg(900, 3600)
    out, _ = compress_photo(blob)
    im = Image.open(io.BytesIO(out))
    assert max(im.size) <= MAX_PHOTO_SIDE
    assert im.size[1] == MAX_PHOTO_SIDE          # 长边就是高
    assert im.size[0] < im.size[1]


def test_small_photo_not_upscaled():
    blob = make_jpeg(400, 300)
    out, info = compress_photo(blob)
    im = Image.open(io.BytesIO(out))
    assert im.size == (400, 300)
    assert info["resized"] is False
    assert len(out) <= MAX_PHOTO_BYTES


def test_png_with_alpha_flattened_to_jpeg():
    img = Image.new("RGBA", (1600, 1200), (255, 120, 40, 128))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out, _ = compress_photo(buf.getvalue())
    im = Image.open(io.BytesIO(out))
    assert im.mode == "RGB"
    assert im.format == "JPEG"
    assert max(im.size) <= MAX_PHOTO_SIDE


def test_palette_png_handled():
    img = Image.new("P", (1500, 1000))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out, _ = compress_photo(buf.getvalue())
    assert Image.open(io.BytesIO(out)).mode == "RGB"


def test_custom_limits():
    blob = make_jpeg(2000, 1500)
    out, _ = compress_photo(blob, max_side=640, max_bytes=40 * 1024)
    im = Image.open(io.BytesIO(out))
    assert max(im.size) <= 640
    assert len(out) <= 40 * 1024


def test_output_is_loadable_and_not_corrupt():
    out, _ = compress_photo(make_jpeg(2200, 1600))
    im = Image.open(io.BytesIO(out))
    im.load()
    assert im.size[0] > 0 and im.size[1] > 0


# ---------- 文件名安全 ----------

@pytest.mark.parametrize("raw,expected", [
    ("a.jpg", "a.jpg"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\windows\\system32\\x.jpg", "x.jpg"),
    ("/absolute/path/cat.jpg", "cat.jpg"),
    ("微信图片_2026.jpg", "微信图片_2026.jpg"),
    ("bad<>:|name.jpg", "bad____name.jpg"),
    ("", "photo.jpg"),
    ("...", "photo.jpg"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_blocks_traversal(tmp_path):
    dest = tmp_path / "photos"
    p, _ = process_and_save(make_jpeg(200, 200), dest, "../../../evil.jpg")
    assert p.parent == dest
    assert ".." not in str(p)


# ---------- 落盘 ----------

def test_process_and_save_normalizes_to_jpg(tmp_path):
    dest = tmp_path / "photos"
    img = Image.new("RGB", (1600, 1200), (10, 200, 90))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    p, info = process_and_save(buf.getvalue(), dest, "cat.PNG")
    assert p.name == "cat.jpg"
    assert p.exists()
    assert info["filename"] == "cat.jpg"
    assert p.stat().st_size <= MAX_PHOTO_BYTES


def test_process_and_save_creates_dir(tmp_path):
    p, _ = process_and_save(make_jpeg(100, 100), tmp_path / "a" / "b" / "c", "x.jpg")
    assert p.exists()


# ---------- zip 解包 ----------

def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_photo_zip(tmp_path):
    payload = _zip({
        "photos/a.jpg": make_jpeg(1800, 1200),
        "photos/b.jpeg": make_jpeg(900, 700),
        "photos/note.txt": b"not an image",
        "__MACOSX/._a.jpg": b"junk",
        "photos/": b"",
    })
    dest = tmp_path / "out"
    results = extract_photo_zip(payload, dest)
    names = sorted(p.name for p, _ in results)
    assert names == ["a.jpg", "b.jpg"]
    assert all(p.stat().st_size <= MAX_PHOTO_BYTES for p, _ in results)
    assert not (dest / "note.txt").exists()


def test_extract_photo_zip_flattens_nested_dirs(tmp_path):
    payload = _zip({"deep/nested/dir/cat.jpg": make_jpeg(400, 300)})
    results = extract_photo_zip(payload, tmp_path / "o")
    assert len(results) == 1
    assert results[0][0].parent == tmp_path / "o"
    assert results[0][0].name == "cat.jpg"


def test_extract_photo_zip_dedupes_same_basename(tmp_path):
    payload = _zip({"x/cat.jpg": make_jpeg(300, 200), "y/cat.jpg": make_jpeg(300, 200)})
    results = extract_photo_zip(payload, tmp_path / "o")
    assert len(results) == 2
    assert results[0][0] == results[1][0]        # 后者覆盖前者（幂等）


def test_extract_photo_zip_empty(tmp_path):
    assert extract_photo_zip(_zip({}), tmp_path / "o") == []


# ---------- 探测 ----------

def test_is_zip():
    assert is_zip(_zip({"a": b"1"})) is True
    assert is_zip(make_jpeg(50, 50)) is False
    assert is_zip(b"") is False


def test_looks_like_image():
    assert looks_like_image(make_jpeg(50, 50)) is True
    assert looks_like_image(b"definitely not an image") is False


# ---------- 底图 ----------

def test_generate_default_map(tmp_path):
    dest = tmp_path / "assets" / "map.jpg"
    p = generate_default_map(dest, width=640, height=420)
    assert p == dest
    assert dest.exists()
    assert map_dimensions(dest) == (640, 420)
    im = Image.open(dest)
    assert im.format == "JPEG"
    # 不能是纯色（要真有星野内容）
    assert len(im.convert("RGB").getcolors(maxcolors=100000)) > 20


def test_generate_default_map_is_deterministic(tmp_path):
    a = generate_default_map(tmp_path / "a.jpg", width=320, height=200, seed=7)
    b = generate_default_map(tmp_path / "b.jpg", width=320, height=200, seed=7)
    assert a.read_bytes() == b.read_bytes()
