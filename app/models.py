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
    engine: Optional[str] = None        # v1 | v29；缺省用 config.ACTIVE_ENGINE
    survey_date: Optional[str] = None   # v29 文案用的普查年月（YYYY-MM）


class CreateProjectRequest(BaseModel):
    school: str = "示例校"
    subtitle: str = ""
    motto: str = ""


class CalibPoint(BaseModel):
    """归一化坐标，原点为底图左上角，x/y ∈ [0,1]。"""

    x: float
    y: float


class CalibData(BaseModel):
    """F8 · 人工在真实底图上标定出的星位，优先级高于算法推导坐标。"""

    positions: dict[str, CalibPoint] = Field(default_factory=dict)
    source: str = "manual"            # manual（人工标定）| derived（算法推导）
    updated_at: str = ""
    note: str = ""


class CalibUpdateRequest(BaseModel):
    positions: dict[str, CalibPoint] = Field(default_factory=dict)
    note: str = ""
    merge: bool = True                # False = 全量替换已存标定


class MergeDecision(BaseModel):
    """F9 · 一条归并判定。人工签字，理由会进 F7 摘要。"""

    gid: str
    verdict: str = "unsure"           # same | different | unsure
    keep: str = ""                    # 判定同猫时保留的编号
    drop: list[str] = Field(default_factory=list)
    reason: str = ""
    members: list[str] = Field(default_factory=list)
    kind: str = ""                    # same-photo | coat-area
    updated_at: str = ""


class MergeBook(BaseModel):
    """一个项目的全部归并判定，按候选组 id 索引。"""

    decisions: dict[str, MergeDecision] = Field(default_factory=dict)


class MergeDecisionRequest(BaseModel):
    gid: str
    verdict: str = "unsure"
    keep: str = ""
    drop: list[str] = Field(default_factory=list)
    reason: str = ""


class RosterEdit(BaseModel):
    """F10 · 一处单元格改动。line 是 CSV 物理行号（表头为第 1 行）。"""

    line: int
    field: str                        # 12 列标准列名
    value: str = ""


class RosterPatch(BaseModel):
    edits: list[RosterEdit] = Field(default_factory=list)
    revalidate: bool = True


class RosterRowInsert(BaseModel):
    """F10 · 在名册里插一行。`after` 是 CSV 物理行号（表头为第 1 行），新行落在它后面。

    `values` 只填用户碰过的列，其余留空。**不替用户编编号**：编号留空就是留空，
    由校验报 E_BAD_ID / E_DUP_ID，人来决定——工具自动补号会让「弃用编号不复用」
    这条数据红线悄悄失效。
    """

    after: int
    values: dict[str, str] = Field(default_factory=dict)
    revalidate: bool = True


class ThemeUpdateRequest(BaseModel):
    """F11 · 主题改动。留 None 表示「这项不动」，前端只发用户碰过的字段。

    真正的合法性判定在 app/theme.py：颜色只收 #RRGGBB，字体只收白名单 key，
    署名转义后截断。这一层只管形状。
    """

    preset: Optional[str] = None
    colors: Optional[dict[str, str]] = None
    title_font: Optional[str] = None
    body_font: Optional[str] = None
    footer_signature: Optional[str] = None
    reset: bool = False               # True = 回到默认预设，清掉所有自定义
