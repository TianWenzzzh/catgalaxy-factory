"""F1 · 名册 CSV 解析。"""
from __future__ import annotations

import csv
import io
import re
from typing import Optional

from .config import ROSTER_COLUMNS
from .models import CatRow

ID_RE = re.compile(r"^CAT[-_ ]?(\d{1,4})$", re.IGNORECASE)
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin-1")

# 列名别名（容忍模板差异）
ALIASES = {
    "编号": ("编号", "id", "ID", "cat_id", "序号"),
    "昵称": ("昵称", "名字", "name", "Name"),
    "军衔": ("军衔", "职级", "rank"),
    "工位": ("工位", "职务", "title", "岗位"),
    "毛色": ("毛色", "coat", "花色"),
    "特征描述": ("特征描述", "特征", "features"),
    "代表照片文件": ("代表照片文件", "代表照片", "photo", "照片文件"),
    "照片数量": ("照片数量", "照片数", "photoCount", "photo_count"),
    "出没区域": ("出没区域", "区域", "area", "出没地点"),
    "关联照片编号": ("关联照片编号", "关联照片", "related"),
    "置信度": ("置信度", "confidence", "可信度"),
    "备注": ("备注", "note", "说明"),
}


def decode_bytes(data: bytes) -> tuple[str, str]:
    """自动探测编码，返回 (文本, 编码名)。"""
    for enc in ENCODINGS:
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def build_column_map(header: list[str]) -> tuple[dict[str, int], list[str], list[str]]:
    """把实际表头映射到标准 12 列。返回 (映射, 缺失列, 未知列)。"""
    norm = [(h or "").strip().lstrip("\ufeff") for h in header]
    mapping: dict[str, int] = {}
    for std, alts in ALIASES.items():
        for idx, h in enumerate(norm):
            if h in alts and std not in mapping:
                mapping[std] = idx
                break
    missing = [c for c in ROSTER_COLUMNS if c not in mapping]
    known = {i for i in mapping.values()}
    unknown = [h for i, h in enumerate(norm) if i not in known and h]
    return mapping, missing, unknown


def _cell(row: list[str], mapping: dict[str, int], col: str) -> str:
    idx = mapping.get(col)
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _to_int(text: str) -> int:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else 0


def parse_roster(text: str) -> tuple[list[CatRow], list[str], list[str], list[str]]:
    """解析 CSV 文本。

    返回 (rows, missing_columns, unknown_columns, header)。
    """
    reader = csv.reader(io.StringIO(text))
    raw = [r for r in reader]
    if not raw:
        return [], list(ROSTER_COLUMNS), [], []

    header = [c.strip().lstrip("\ufeff") for c in raw[0]]
    mapping, missing, unknown = build_column_map(header)

    rows: list[CatRow] = []
    for i, line in enumerate(raw[1:], start=2):
        if not any((c or "").strip() for c in line):
            continue  # 跳过空行
        rows.append(
            CatRow(
                line=i,
                id=_cell(line, mapping, "编号"),
                name=_cell(line, mapping, "昵称"),
                rank=_cell(line, mapping, "军衔"),
                title=_cell(line, mapping, "工位"),
                coat=_cell(line, mapping, "毛色"),
                features=_cell(line, mapping, "特征描述"),
                photo_file=_cell(line, mapping, "代表照片文件"),
                photo_count=_to_int(_cell(line, mapping, "照片数量")),
                photo_count_raw=_cell(line, mapping, "照片数量"),
                area=_cell(line, mapping, "出没区域"),
                related=_cell(line, mapping, "关联照片编号"),
                confidence=_cell(line, mapping, "置信度"),
                note=_cell(line, mapping, "备注"),
            )
        )
    return rows, missing, unknown, header


def normalize_id(raw: str) -> Optional[str]:
    """把 'CAT-001' / 'cat_1' / 'CAT 001' 规整为 'CAT-001'。"""
    if not raw:
        return None
    m = ID_RE.match(raw.strip())
    if not m:
        return None
    return f"CAT-{int(m.group(1)):03d}"


def id_number(cat_id: str) -> Optional[int]:
    m = ID_RE.match((cat_id or "").strip())
    return int(m.group(1)) if m else None


def empty_template() -> str:
    """生成空白名册模板 CSV（带 2 行示例，与 12_跨校复制包 骨架对齐）。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(ROSTER_COLUMNS)
    w.writerow([
        "CAT-001", "示例墩墩", "喵校长", "总揽全校猫务", "全橘虎斑",
        "体型胖 粉鼻 侧躺露肚", "demo-cat-001.jpg", "1", "宿舍楼前石台",
        "batch1:DEMO", "高", "示例数据 可删除",
    ])
    w.writerow([
        "CAT-002", "示例格子", "中士", "宿舍内务员", "橘白",
        "橘头橘背白胸腹 常钻桌布下", "demo-cat-002.jpg", "3", "教学楼走廊",
        "batch1:DEMO", "中", "示例数据 可删除",
    ])
    return buf.getvalue()
