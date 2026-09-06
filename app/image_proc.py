"""F1/F3 · 照片处理：压缩（长边≤1200px、单张≤200KB）、zip 解包、底图生成。"""
from __future__ import annotations

import io
import math
import os
import random
import re
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .config import (IMAGE_EXTS, MAX_MAP_SIDE, MAX_PHOTO_BYTES, MAX_PHOTO_SIDE)

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str, fallback: str = "photo.jpg") -> str:
    """去掉路径与非法字符，保留中文；防目录穿越。"""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = _UNSAFE.sub("_", base).strip(". ")
    return base or fallback


def _open_rgb(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def compress_photo(data: bytes, max_side: int = MAX_PHOTO_SIDE,
                   max_bytes: int = MAX_PHOTO_BYTES) -> tuple[bytes, dict]:
    """压缩单张照片。返回 (jpeg_bytes, info)。

    策略：长边缩到 ≤max_side → JPEG 质量二分逼近 max_bytes → 仍超则逐级降分辨率。
    """
    img = _open_rgb(data)
    src_size = len(data)
    w, h = img.size

    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    side = max_side
    for _ in range(6):
        out, quality = _fit_bytes(img, max_bytes)
        if out is not None:
            return out, {
                "src_bytes": src_size,
                "out_bytes": len(out),
                "width": img.size[0],
                "height": img.size[1],
                "quality": quality,
                "resized": (w, h) != img.size,
            }
        side = int(side * 0.85)
        if side < 320:
            break
        img = img.resize((max(1, int(img.size[0] * 0.85)),
                          max(1, int(img.size[1] * 0.85))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=30, optimize=True, progressive=True)
    return buf.getvalue(), {
        "src_bytes": src_size, "out_bytes": buf.tell(),
        "width": img.size[0], "height": img.size[1],
        "quality": 30, "resized": (w, h) != img.size, "over_limit": True,
    }


def _fit_bytes(img: Image.Image, max_bytes: int) -> tuple[bytes | None, int]:
    """质量二分：找到 ≤max_bytes 的最高质量。"""
    lo, hi, best, best_q = 25, 88, None, 0
    while lo <= hi:
        q = (lo + hi) // 2
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
        size = buf.tell()
        if size <= max_bytes:
            best, best_q = buf.getvalue(), q
            lo = q + 1
        else:
            hi = q - 1
    return best, best_q


def process_and_save(data: bytes, dest_dir: Path, filename: str) -> tuple[Path, dict]:
    """压缩并写入目标目录，统一输出 .jpg。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    stem = Path(name).stem or "photo"
    out_path = dest_dir / f"{stem}.jpg"
    blob, info = compress_photo(data)
    out_path.write_bytes(blob)
    info["filename"] = out_path.name
    info["path"] = str(out_path)
    return out_path, info


def extract_photo_zip(data: bytes, dest_dir: Path) -> list[tuple[Path, dict]]:
    """解包照片 zip（忽略目录项、__MACOSX、非图片），逐张压缩落盘。"""
    results: list[tuple[Path, dict]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            # zip 里的中文可能是 cp437 误编码，尝试还原
            if info.flag_bits & 0x800 == 0:
                try:
                    name = info.filename.encode("cp437").decode("gbk")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    name = info.filename
            if "__MACOSX" in name or name.startswith("."):
                continue
            if Path(name).suffix.lower() not in IMAGE_EXTS:
                continue
            payload = zf.read(info)
            results.append(process_and_save(payload, dest_dir, Path(name).name))
    return results


def is_zip(data: bytes) -> bool:
    return data[:2] == b"PK"


def looks_like_image(data: bytes) -> bool:
    try:
        Image.open(io.BytesIO(data)).verify()
        return True
    except Exception:
        return False


# ---------- 底图：未上传时用 Pillow 程序化生成星野 ----------

def generate_default_map(dest: Path, width: int = MAX_MAP_SIDE,
                         height: int = 1239, seed: int = 20260906) -> Path:
    """生成深空星野底图（含淡网格与星云），保证零素材也能出成品。"""
    rng = random.Random(seed)
    # 竖向渐变：先做 1px 宽的列，再横向拉伸（避免逐像素循环）
    col = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / height
        col.putpixel((0, y), (int(7 + 9 * (1 - t)), int(12 + 12 * (1 - t)),
                              int(28 + 26 * (1 - t))))
    img = col.resize((width, height))

    # 星云团
    neb = Image.new("RGB", (width, height), (0, 0, 0))
    nd = ImageDraw.Draw(neb)
    for _ in range(9):
        cx, cy = rng.randrange(width), rng.randrange(height)
        r = rng.randrange(180, 460)
        col = rng.choice([(70, 52, 20), (18, 52, 50), (44, 26, 62)])
        nd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    neb = neb.filter(ImageFilter.GaussianBlur(140))
    img = Image.blend(img, Image.eval(neb, lambda v: min(255, v + 8)), 0.34)

    # 星点
    d = ImageDraw.Draw(img)
    for _ in range(1500):
        x, y = rng.randrange(width), rng.randrange(height)
        b = rng.randint(40, 190)
        s = 1 if rng.random() > 0.9 else 0
        d.ellipse([x, y, x + s, y + s], fill=(b, b + 8, min(255, b + 24)))

    # 淡网格（校园区块感）
    for gx in range(0, width, width // 12):
        d.line([(gx, 0), (gx, height)], fill=(28, 40, 74), width=1)
    for gy in range(0, height, height // 8):
        d.line([(0, gy), (width, gy)], fill=(28, 40, 74), width=1)

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=86, optimize=True)
    return dest


def map_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size
