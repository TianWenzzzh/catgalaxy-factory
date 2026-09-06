"""数据模型。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class CatRow(BaseModel):
    """名册一行（CSV 12 列的规整化结果）。"""

    line: int = 0                       # CSV 中的物理行号（表头为第 1 行）
    id: str = ""                        # 编号 CAT-001
    name: str = ""                      # 昵称
    rank: str = ""                      # 军衔
    title: str = ""                     # 工位
    coat: str = ""                      # 毛色
    features: str = ""                  # 特征描述（原文）
    photo_file: str = ""                # 代表照片文件
    photo_count: int = 0                # 照片数量
    photo_count_raw: str = ""           # 原始文本（用于报错）
    area: str = ""                      # 出没区域
    related: str = ""                   # 关联照片编号
    confidence: str = ""                # 置信度
    note: str = ""                      # 备注


class Issue(BaseModel):
    level: str                          # error / warning / info
    code: str
    message: str
    line: Optional[int] = None
    cat_id: Optional[str] = None
    field: Optional[str] = None


class Summary(BaseModel):
    total_rows: int = 0
    valid_rows: int = 0
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    photos_uploaded: int = 0
    photos_referenced: int = 0
    photos_unused: int = 0
    id_min: Optional[str] = None
    id_max: Optional[str] = None
    id_gaps: list[str] = Field(default_factory=list)
    low_confidence: list[str] = Field(default_factory=list)
    ok: bool = False


class ValidationReport(BaseModel):
    project_id: str = ""
    school: str = ""
    summary: Summary = Summary()
    issues: list[Issue] = Field(default_factory=list)
    rows: list[CatRow] = Field(default_factory=list)


class ProjectMeta(BaseModel):
    id: str
    school: str = "示例校"
    subtitle: str = ""
    motto: str = ""
    created_at: str = ""
    updated_at: str = ""
    has_roster: bool = False
    photo_count: int = 0
    has_map: bool = False
    generated_form: Optional[str] = None
    log: list[dict[str, Any]] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    form: str = "relative"              # relative | inline
    exclude_low_confidence: bool = False
    school: Optional[str] = None
    subtitle: Optional[str] = None


class CreateProjectRequest(BaseModel):
    school: str = "示例校"
    subtitle: str = ""
    motto: str = ""
