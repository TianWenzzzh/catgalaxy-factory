"""F5 · 打包：输出可离线运行的 zip（relative / inline 两种形态）。"""
from __future__ import annotations

import re
import shutil
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from .config import atomic_replace, atomic_write_bytes, atomic_write_text, tmp_sibling

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def zip_basename(school: str, date_str: Optional[str] = None) -> str:
    """{校名}-校园猫咪星图-{日期}"""
    name = _UNSAFE.sub("", re.sub(r"\s+", "", school or "校园")).strip(".") or "校园"
    return f"{name[:40]}-校园猫咪星图-{date_str or date.today().isoformat()}"


def html_basename(school: str) -> str:
    name = _UNSAFE.sub("", re.sub(r"\s+", "", school or "校园")).strip(".") or "校园"
    return f"{name[:40]}喵星图.html"


def _write(path: Path, content) -> None:
    """原子写。产物目录会在生成过程中被 rmtree 重建，
    这时候如果有人正在点预览/下载，非原子的写法会让他拿到半截文件。"""
    if isinstance(content, bytes):
        atomic_write_bytes(path, content)
    else:
        atomic_write_text(path, content, newline="\n")


def build_relative_bundle(dest: Path, *, html: str, html_name: str,
                          photos: Iterable[tuple[str, bytes]],
                          map_bytes: Optional[bytes] = None,
                          logo_bytes: Optional[bytes] = None,
                          roster_csv: Optional[str] = None,
                          report_md: Optional[str] = None,
                          summary_md: Optional[str] = None) -> list[str]:
    """HTML + assets 形态。返回写入的相对路径清单。

    ``photos`` 是 (名字, 字节) 的可迭代对象，可以是生成器：写一张读一张，
    整册图廊不必同时在内存里。因此这里不再排序——写入顺序（也就是返回清单里
    assets/photos/* 的顺序）由调用方给定，main.py 传的是按名字排好序的。
    """
    if dest.exists():
        shutil.rmtree(dest)
    written: list[str] = []

    _write(dest / html_name, html)
    written.append(html_name)

    if map_bytes:
        _write(dest / "assets" / "map.jpg", map_bytes)
        written.append("assets/map.jpg")

    # 文件名必须是 assets/logo.png——injector 的 src 白名单只认这一个相对路径。
    if logo_bytes:
        _write(dest / "assets" / "logo.png", logo_bytes)
        written.append("assets/logo.png")

    for name, blob in photos:
        rel = f"assets/photos/{name}"
        _write(dest / "assets" / "photos" / name, blob)
        written.append(rel)

    if roster_csv:
        _write(dest / "data" / "猫咪名册.csv", roster_csv)
        written.append("data/猫咪名册.csv")
    if report_md:
        _write(dest / "校验报告.md", report_md)
        written.append("校验报告.md")
    if summary_md:
        _write(dest / "归并决策摘要.md", summary_md)
        written.append("归并决策摘要.md")
    return written


def build_inline_bundle(dest: Path, *, html: str, html_name: str,
                        chunks: Iterable[tuple[str, str]],
                        roster_csv: Optional[str] = None,
                        report_md: Optional[str] = None,
                        summary_md: Optional[str] = None) -> list[str]:
    """纯文本（base64 内嵌）形态。返回写入的相对路径清单。

    ``chunks`` 可以是生成器：只遍历一次、边来边写，调用方就不必把整册分片
    攒在内存里。rmtree 发生在遍历之前，且分片读的是 assets/photos，与 dest 无关。
    """
    if dest.exists():
        shutil.rmtree(dest)
    written: list[str] = []

    _write(dest / html_name, html)
    written.append(html_name)

    for name, content in chunks:
        _write(dest / "assets" / name, content)
        written.append(f"assets/{name}")

    if roster_csv:
        _write(dest / "data" / "猫咪名册.csv", roster_csv)
        written.append("data/猫咪名册.csv")
    if report_md:
        _write(dest / "校验报告.md", report_md)
        written.append("校验报告.md")
    if summary_md:
        _write(dest / "归并决策摘要.md", summary_md)
        written.append("归并决策摘要.md")
    return written


def make_zip(src_dir: Path, zip_path: Path) -> Path:
    """把目录压成 zip（zip 内不带顶层目录，解压即用）。

    压到临时文件再原子替换。旧写法是「unlink 目标 → 流式写」，几十 MB 的包
    要写好几秒，这期间点下载的人会拿到半截 zip（解压报错）或者直接 404。
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_sibling(zip_path)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(src_dir.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(src_dir).as_posix())
        atomic_replace(tmp, zip_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return zip_path


def bundle_size(dest: Path) -> int:
    return sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
