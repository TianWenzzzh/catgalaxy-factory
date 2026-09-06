"""项目存储：文件系统即数据库（无 DB）。"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import WORKSPACE, ensure_dirs, project_dir
from .models import CalibData, ProjectMeta

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
    meta_path(meta.id).write_text(
        json.dumps(meta.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_meta(pid: str) -> Optional[ProjectMeta]:
    p = meta_path(pid)
    if not p.exists():
        return None
    try:
        return ProjectMeta.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


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
    p = calib_path(pid)
    if not p.exists():
        return None
    try:
        return CalibData.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_calib(pid: str, calib: CalibData) -> CalibData:
    ensure_dirs(pid)
    calib.updated_at = _now()
    calib_path(pid).write_text(
        json.dumps(calib.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return calib


def clear_calib(pid: str) -> bool:
    p = calib_path(pid)
    if p.exists():
        p.unlink()
        return True
    return False


def safe_name(name: str, fallback: str = "校园") -> str:
    """校名 → 文件名安全串。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", (name or "").strip())
    s = re.sub(r"\s+", "", s)
    return s[:40] or fallback
