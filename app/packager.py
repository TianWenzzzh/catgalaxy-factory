"""F5 · 打包：输出可离线运行的 zip（relative / inline 两种形态）。"""
from __future__ import annotations

import re
import shutil
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def zip_basename(school: str, date_str: Optional[str] = None) -> str:
    """{校名}-校园猫咪星图-{日期}"""
    name = _UNSAFE.sub("", re.sub(r"\s+", "", school or "校园")).strip(".") or "校园"
    return f"{name[:40]}-校园猫咪星图-{date_str or date.today().isoformat()}"


def html_basename(school: str) -> str:
    name = _UNSAFE.sub("", re.sub(r"\s+", "", school or "校园")).strip(".") or "校园"
    return f"{name[:40]}喵星图.html"


def _write(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")


def build_relative_bundle(dest: Path, *, html: str, html_name: str,
                          photos: dict[str, bytes],
                          map_bytes: Optional[bytes] = None,
                          roster_csv: Optional[str] = None,
                          report_md: Optional[str] = None,
                          summary_md: Optional[str] = None) -> list[str]:
    """HTML + assets 形态。返回写入的相对路径清单。"""
    if dest.exists():
        shutil.rmtree(dest)
    written: list[str] = []

    _write(dest / html_name, html)
    written.append(html_name)

    if map_bytes:
        _write(dest / "assets" / "map.jpg", map_bytes)
        written.append("assets/map.jpg")

    for name, blob in sorted(photos.items()):
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
                        chunks: list[tuple[str, str]],
                        roster_csv: Optional[str] = None,
                        report_md: Optional[str] = None,
                        summary_md: Optional[str] = None) -> list[str]:
    """纯文本（base64 内嵌）形态。返回写入的相对路径清单。"""
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
    """把目录压成 zip（zip 内不带顶层目录，解压即用）。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src_dir).as_posix())
    return zip_path


def bundle_size(dest: Path) -> int:
    return sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
