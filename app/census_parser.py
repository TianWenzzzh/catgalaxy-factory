"""F6 · 普查 batch 解析器：`文件名 | 猫数 | 毛色 | 特征 | 场景 | 画质 | 正脸 | 人脸` → 名册草稿。"""
from __future__ import annotations

import csv
import io
import re
from typing import Optional

from .config import ROSTER_COLUMNS

FIELD_COUNT = 8
QUALITY_TO_CONFIDENCE = {"A": "高", "B": "中", "C": "低"}

# 场景关键词 → 出没区域短语
SCENE_KEYWORDS = [
    ("宿舍", "宿舍区"), ("教学楼", "教学楼"), ("食堂", "食堂区"), ("教室", "教学楼"),
    ("大厅", "楼内大厅"), ("走廊", "走廊"), ("窗台", "窗台"), ("车库", "车库区"),
    ("电动", "电动车停放区"), ("停车", "停车区"), ("草地", "草地植被区"),
    ("绿化", "绿化带"), ("树", "树下绿化带"), ("灌木", "灌木区"),
    ("石台", "石台区"), ("石砖", "石砖路边"), ("花岗岩", "花岗岩地面"),
    ("瓷砖", "瓷砖地面"), ("水泥", "水泥地面"), ("道路", "道路沿线"),
    ("路边", "道路沿线"), ("围", "围墙区"), ("栅栏", "栅栏区"),
    ("施工", "施工区"), ("仓库", "老旧房区"), ("平房", "老旧房区"),
]

# 「室内/户外」只是拍摄环境修饰词，仅在没有任何具体地点线索时兜底
GENERIC_SCENE_KEYWORDS = [("室内", "室内"), ("户外", "户外")]


class CensusRecord:
    __slots__ = ("filename", "cat_count", "coat_raw", "features", "scene",
                 "quality", "front_face", "human_face", "line", "coat_main",
                 "area", "confidence")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k, ""))

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def main_coat(coat_raw: str) -> str:
    """`橘白（橘背橘头，胸腹发白）` → `橘白`"""
    text = (coat_raw or "").strip()
    text = re.split(r"[（(【\[]", text)[0].strip()
    return text or "未定"


def _earliest(text: str, table) -> Optional[str]:
    best_pos, best_area = None, None
    for kw, area in table:
        idx = text.find(kw)
        if idx < 0:
            continue
        if best_pos is None or idx < best_pos:
            best_pos, best_area = idx, area
    return best_area


def scene_to_area(scene: str) -> str:
    """场景线索 → 出没区域短语。取文本中**最早出现**的具体地点关键词作为主场景。"""
    text = (scene or "").strip()
    if not text:
        return "未定区域"
    area = _earliest(text, SCENE_KEYWORDS) or _earliest(text, GENERIC_SCENE_KEYWORDS)
    return area or text[:12]


def parse_batch(text: str, batch_label: Optional[str] = None) -> tuple[list[CensusRecord], list[str]]:
    """解析 batch 文本。返回 (records, 解析警告)。"""
    records: list[CensusRecord] = []
    warnings: list[str] = []

    if batch_label is None:
        m = re.search(r"batch\s*([0-9]+)", text[:200], re.IGNORECASE)
        batch_label = f"batch{m.group(1)}" if m else "batch"

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < FIELD_COUNT:
            # 标题行（格式说明）常含全角｜或缺列，跳过但记录
            if "格式" in line or "文件名" in line:
                continue
            warnings.append(f"第 {lineno} 行字段数 {len(parts)} < {FIELD_COUNT}，已跳过：{line[:40]}")
            continue

        fname = parts[0]
        if not re.search(r"\.(jpg|jpeg|png|webp|bmp)$", fname, re.IGNORECASE):
            warnings.append(f"第 {lineno} 行首列不像文件名，已跳过：{fname[:40]}")
            continue

        cnt_m = re.search(r"\d+", parts[1])
        cat_count = int(cnt_m.group()) if cnt_m else 1

        quality = (parts[5] or "").strip().upper()[:1]
        rec = CensusRecord(
            filename=fname,
            cat_count=cat_count,
            coat_raw=parts[2],
            features=parts[3],
            scene=parts[4],
            quality=quality or "?",
            front_face=parts[6],
            human_face=parts[7],
            line=lineno,
        )
        rec.coat_main = main_coat(rec.coat_raw)
        rec.area = scene_to_area(rec.scene)
        rec.confidence = QUALITY_TO_CONFIDENCE.get(quality, "中")
        records.append(rec)

        if rec.human_face and rec.human_face not in ("无", "", "-"):
            warnings.append(f"第 {lineno} 行 {fname} 含人脸（{rec.human_face}）——数据红线：不收录含人脸照片")
        if cat_count > 1:
            warnings.append(f"第 {lineno} 行 {fname} 含 {cat_count} 只猫，需人工归并后拆分编号")

    return records, warnings


def records_to_draft_csv(records: list[CensusRecord], batch_label: str = "batch",
                         start_index: int = 1) -> str:
    """records → 名册草稿 CSV（12 列，昵称/军衔/工位留空待人工填）。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(ROSTER_COLUMNS)
    for i, r in enumerate(records, start=start_index):
        stem = re.sub(r"\.(jpg|jpeg|png|webp|bmp)$", "", r.filename, flags=re.IGNORECASE)
        related = f"{batch_label}:{stem[:4].upper()}"
        note_bits = [f"画质{r.quality}", f"正脸={r.front_face or '?'}", f"人脸={r.human_face or '无'}"]
        if r.cat_count > 1:
            note_bits.append(f"含{r.cat_count}只待归并")
        note_bits.append("草稿待人工确认")
        w.writerow([
            f"CAT-{i:03d}", "", "", "", r.coat_main,
            re.sub(r"\s+", " ", r.features)[:60],
            r.filename, r.cat_count, r.area, related, r.confidence,
            " ".join(note_bits),
        ])
    return buf.getvalue()


def merge_suggestions(records: list[CensusRecord]) -> list[dict]:
    """疑似同猫归并建议：主色 + 区域相同的照片聚成候选组。"""
    groups: dict[tuple[str, str], list[str]] = {}
    for r in records:
        groups.setdefault((r.coat_main, r.area), []).append(r.filename)
    out = []
    for (coat, area), files in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(files) < 2:
            continue
        out.append({"coat": coat, "area": area, "count": len(files), "files": files})
    return out
