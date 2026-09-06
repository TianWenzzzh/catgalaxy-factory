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

from .config import (IMAGE_EXTS, MAX_MAP_SIDE, MAX_PHOTO_BYTES, MAX_PHOTO_SIDE,
                     MAX_PHOTOS_PER_UPLOAD, MAX_ZIP_DEPTH, MAX_ZIP_ENTRIES,
                     MAX_ZIP_INFLATED_BYTES, ZIP_READ_CHUNK, atomic_write_bytes)

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
    atomic_write_bytes(out_path, blob)
    info["filename"] = out_path.name
    info["path"] = str(out_path)
    return out_path, info


class ZipBudget:
    """一次上传请求的解包预算。

    超限不抛异常，而是记下 ``stop_reason`` 并停手——路由层据此回滚已写入的
    文件并回 413，避免「解到一半炸了、项目里留半截照片」。
    """

    def __init__(self, max_files: int | None = None, max_inflated: int | None = None,
                 max_entries: int | None = None, max_depth: int | None = None,
                 on_item=None, on_total=None):
        # 上限在这里才解析到模块常量，而不是写成默认参数值——默认值在函数定义
        # 时就绑定了，测试没法把上限调小来触发限额分支。
        self.max_files = MAX_PHOTOS_PER_UPLOAD if max_files is None else max_files
        self.max_inflated = MAX_ZIP_INFLATED_BYTES if max_inflated is None else max_inflated
        self.max_entries = MAX_ZIP_ENTRIES if max_entries is None else max_entries
        self.max_depth = MAX_ZIP_DEPTH if max_depth is None else max_depth
        self.files = 0
        self.inflated = 0
        self.entries = 0
        self.nested_zips = 0
        self.stop_reason = ""
        self.corrupt: list[str] = []      # 扩展名是图片但解不开的
        self.too_deep: list[str] = []     # 超过递归层数被放弃的嵌套 zip
        self.bad_zips: list[str] = []     # 打不开的 zip
        # 进度回调（见 app/progress.py）。zip 里有 76 张照片时，压缩要几十秒，
        # 而这段全在服务端——浏览器只知道字节传完了，不知道压到第几张。
        self.on_item = on_item            # 每存好一张 → on_item(文件名)
        self.on_total = on_total          # 打开一个 zip → on_total(条目数)

    @staticmethod
    def _notify(cb, *args) -> None:
        """调进度回调，但绝不让它把真正的上传搞崩。

        进度是「参考信息」：写 progress.json 撞上磁盘满或权限问题，该失败的
        是进度条，不是用户那 76 张照片的入库。
        """
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            pass

    @property
    def stopped(self) -> bool:
        return bool(self.stop_reason)

    def stop(self, why: str) -> None:
        if not self.stop_reason:
            self.stop_reason = why

    def charge_file(self) -> bool:
        """申请一个照片名额。满了就停手并返回 False，且不占用名额。"""
        if self.files >= self.max_files:
            self.stop(f"照片张数达到上限 {self.max_files} 张，已停手")
            return False
        self.files += 1
        return True

    def release_file(self) -> None:
        """退回一个名额——照片解不开时不占额度。"""
        self.files = max(0, self.files - 1)


class _BadEntry(Exception):
    """单个 zip 条目读不出来（CRC 不符 / 加密 / 压缩流损坏）——只跳过这一条。"""


def _read_capped(zf: zipfile.ZipFile, info: zipfile.ZipInfo,
                 budget: ZipBudget) -> bytes | None:
    """流式解压并实时计数。返回 None 表示预算耗尽、调用方必须立刻停手。

    不用 zf.read()：要边解边计数才能在超预算的第一时间收手，而不是等一个
    几 GB 的条目全进了内存才发现。单个条目坏了抛 _BadEntry，由调用方跳过。
    """
    out = io.BytesIO()
    try:
        with zf.open(info) as fh:
            while True:
                chunk = fh.read(ZIP_READ_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                budget.inflated += len(chunk)
                if budget.inflated > budget.max_inflated:
                    budget.stop(f"解压后累计超过 {budget.max_inflated // 1024 // 1024}MB "
                                f"上限（疑似 zip 炸弹），已停手")
                    return None
    except zipfile.BadZipFile:
        # zipfile 按中心目录声明的 file_size 截断输出，谎报大小的条目会在
        # CRC 校验处炸在这里——正好说明这个条目不可信，丢掉。
        raise _BadEntry(info.filename) from None
    except RuntimeError:
        raise _BadEntry(info.filename) from None   # 加密 zip 需要密码
    return out.getvalue()


def _zip_name(info: zipfile.ZipInfo) -> str:
    """zip 条目名。中文可能是 cp437 误编码，尝试按 gbk 还原。"""
    if info.flag_bits & 0x800 == 0:
        try:
            return info.filename.encode("cp437").decode("gbk")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return info.filename
    return info.filename


def extract_photo_zip(data: bytes, dest_dir: Path, budget: ZipBudget | None = None,
                      depth: int = 0, label: str = "") -> list[tuple[Path, dict]]:
    """解包照片 zip（忽略目录项、__MACOSX、非图片），逐张压缩落盘。

    嵌套 zip 会递归解包，最多 ``budget.max_depth`` 层。传入的 ``budget`` 会被
    就地更新；不传则内部新建一个（此时调用方看不到限额状态）。
    """
    if budget is None:
        budget = ZipBudget()
    results: list[tuple[Path, dict]] = []
    tag = label or "zip"
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        budget.bad_zips.append(tag)
        budget.stop(f"zip 打不开（文件损坏或不是标准 zip）：{tag}")
        return results

    with zf:
        infos = zf.infolist()
        # 打开 zip 才知道里面有多少张——上传时前端只数得出「1 个文件」。
        # 报的是条目数而非图片数（含目录项与非图片），所以只是个偏大的估计，
        # 进度条封顶在 99% 直到请求真的回来。
        ZipBudget._notify(budget.on_total, len(infos))
        for info in infos:
            if budget.stopped:
                break
            if info.is_dir():
                continue
            budget.entries += 1
            if budget.entries > budget.max_entries:
                budget.stop(f"zip 条目数超过上限 {budget.max_entries} 个，已停手")
                break
            name = _zip_name(info)
            if "__MACOSX" in name or Path(name).name.startswith("."):
                continue
            suffix = Path(name).suffix.lower()

            if suffix == ".zip":
                if depth + 1 > budget.max_depth:
                    budget.too_deep.append(name)
                    continue
                try:
                    nested = _read_capped(zf, info, budget)
                except _BadEntry:
                    budget.corrupt.append(name)
                    continue
                if nested is None:
                    break
                budget.nested_zips += 1
                results += extract_photo_zip(nested, dest_dir, budget,
                                             depth + 1, name)
                continue

            if suffix not in IMAGE_EXTS:
                continue

            try:
                payload = _read_capped(zf, info, budget)
            except _BadEntry:
                budget.corrupt.append(name)
                continue
            if payload is None:
                break
            if not budget.charge_file():
                break
            try:
                results.append(process_and_save(payload, dest_dir, Path(name).name))
            except Exception:
                budget.release_file()
                budget.corrupt.append(name)
            else:
                ZipBudget._notify(budget.on_item, Path(name).name)
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

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=86, optimize=True)
    atomic_write_bytes(dest, buf.getvalue())
    return dest


def map_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size
