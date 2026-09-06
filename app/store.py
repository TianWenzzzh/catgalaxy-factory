"""项目存储：文件系统即数据库（无 DB）。"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import (WORKSPACE, atomic_write_text, ensure_dirs, project_dir,
                     retry_read_text)
from .models import CalibData, MergeBook, ProjectMeta

_SLUG_BAD = re.compile(r'[\\/:*?"<>|\s]+')


def new_project_id(school: str) -> str:
    slug = _SLUG_BAD.sub("-", (school or "").strip()).strip("-")[:24] or "school"
    return f"{int(time.time())}-{slug}"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_project(school: str, subtitle: str = "", motto: str = "") -> ProjectMeta:
    pid = new_project_id(school)
    ensure_dirs(pid)
    meta = ProjectMeta(id=pid, school=school or "示例校", subtitle=subtitle, motto=motto,
                       created_at=_now(), updated_at=_now())
    save_meta(meta)
    log(meta, "创建项目", f"校名={meta.school}")
    return meta


def meta_path(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def save_meta(meta: ProjectMeta) -> None:
    ensure_dirs(meta.id)
    meta.updated_at = _now()
    atomic_write_text(
        meta_path(meta.id), json.dumps(meta.model_dump(), ensure_ascii=False, indent=2))


def load_meta(pid: str) -> Optional[ProjectMeta]:
    """读项目元数据。只有文件确实不存在才返回 None。

    从前这里是 exists() + read_text + 一把 except Exception → None，
    把两种完全不同的情况混成了一个返回值：项目真的不存在，和「写方正在
    原子替换、读方 open 被 Windows 拒了」。后者会让一个好好存在的项目
    从下拉框里消失，甚至对客户端回 404。共享冲突交给 retry_read_text 重试。
    """
    try:
        text = retry_read_text(meta_path(pid))
    except FileNotFoundError:
        return None
    try:
        return ProjectMeta.model_validate(json.loads(text))
    except Exception:
        return None      # 内容确实坏了，这跟「暂时读不到」是两回事


def list_projects() -> list[ProjectMeta]:
    if not WORKSPACE.exists():
        return []
    out: list[ProjectMeta] = []
    for d in sorted(WORKSPACE.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        m = load_meta(d.name)
        if m:
            out.append(m)
    return out


def log(meta: ProjectMeta, action: str, detail: str = "") -> None:
    """追加操作日志（供 F7 归并决策摘要使用）。"""
    meta.log.append({"at": _now(), "action": action, "detail": detail})
    meta.log = meta.log[-400:]
    save_meta(meta)


def photo_files(pid: str) -> list[Path]:
    d = project_dir(pid) / "assets" / "photos"
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in
                  {".jpg", ".jpeg", ".png", ".webp", ".bmp"})


def photo_names(pid: str) -> set[str]:
    return {p.name for p in photo_files(pid)}


def map_path(pid: str) -> Path:
    return project_dir(pid) / "assets" / "map.jpg"


def calib_path(pid: str) -> Path:
    return project_dir(pid) / "calib.json"


def load_calib(pid: str) -> Optional[CalibData]:
    try:
        text = retry_read_text(calib_path(pid))
    except FileNotFoundError:
        return None      # 没人标定过，星位走算法推导
    try:
        return CalibData.model_validate(json.loads(text))
    except Exception:
        return None


def save_calib(pid: str, calib: CalibData) -> CalibData:
    ensure_dirs(pid)
    calib.updated_at = _now()
    atomic_write_text(
        calib_path(pid), json.dumps(calib.model_dump(), ensure_ascii=False, indent=2))
    return calib


def clear_calib(pid: str) -> bool:
    p = calib_path(pid)
    if p.exists():
        p.unlink()
        return True
    return False


def now() -> str:
    return _now()


def merge_path(pid: str) -> Path:
    return project_dir(pid) / "merge.json"


def load_merge(pid: str) -> MergeBook:
    """读归并判定账本。共享冲突必须重试，不能退化成空账本——
    put_merge 是「load → 加一条 → 整份存回」，一次瞬时读失败就会
    把之前所有人工判定抹掉。"""
    try:
        text = retry_read_text(merge_path(pid))
    except FileNotFoundError:
        return MergeBook()
    try:
        return MergeBook.model_validate(json.loads(text))
    except Exception:
        return MergeBook()


def save_merge(pid: str, book: MergeBook) -> MergeBook:
    ensure_dirs(pid)
    atomic_write_text(
        merge_path(pid), json.dumps(book.model_dump(), ensure_ascii=False, indent=2))
    return book


def drop_merge(pid: str, gid: str) -> bool:
    book = load_merge(pid)
    if gid not in book.decisions:
        return False
    del book.decisions[gid]
    save_merge(pid, book)
    return True


def safe_name(name: str, fallback: str = "校园") -> str:
    """校名 → 文件名安全串。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", (name or "").strip())
    s = re.sub(r"\s+", "", s)
    return s[:40] or fallback
