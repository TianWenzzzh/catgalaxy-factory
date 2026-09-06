"""F3 · CSV 行 → 星图 CATS 条目（毛色→星色、照片数→星等、出没区→星位分区）。"""
from __future__ import annotations

import math
import re
from typing import Iterable, Optional

from .csv_loader import id_number, normalize_id
from .models import CatRow

# ---- 毛色分组：顺序即优先级，先命中先归类 ----
COAT_GROUPS: list[tuple[str, tuple[str, ...], str]] = [
    ("三花", ("三花", "三色", "calico"), "255,170,200"),
    ("玳瑁", ("玳瑁", "龟壳"), "210,160,120"),
    ("奶牛", ("奶牛", "黑白花", "宾士"), "225,235,250"),
    ("纯白", ("纯白", "全白", "白猫"), "235,245,255"),
    ("纯黑", ("纯黑", "全黑", "黑猫", "通黑"), "150,160,200"),
    ("重点色", ("重点色", "暹罗", "布偶", "海豹"), "190,200,235"),
    ("狸花", ("狸花", "狸貓", "虎斑狸", "彩狸", "麻狸"), "168,190,220"),
    ("橘", ("橘", "桔", "黄", "奶油", "金"), "255,190,110"),
]
FALLBACK_GROUP = "其他"
FALLBACK_COLOR = "200,200,220"

# ---- 出没区域 → 分区锚点（归一化坐标，落在核心区）----
ZONES: list[tuple[str, tuple[str, ...], float, float]] = [
    ("宿舍居民区", ("宿舍", "居民", "公寓", "寝室", "楼内", "窗台"), 0.62, 0.53),
    ("教学楼食堂区", ("教学", "食堂", "教室", "大厅", "走廊", "门厅", "楼梯"), 0.42, 0.43),
    ("道路沿线", ("道路", "路边", "马路", "小径", "人行道", "水泥"), 0.52, 0.36),
    ("草地植被区", ("草地", "绿化", "草坪", "树", "灌木", "花"), 0.38, 0.58),
    ("车库停车区", ("车库", "电动", "停车", "自行车", "车棚"), 0.70, 0.46),
    ("围墙护栏区", ("围墙", "护栏", "栅栏", "铁", "施工", "墙"), 0.79, 0.47),
    ("投喂点", ("投喂", "喂食", "猫粮", "饭点"), 0.57, 0.55),
    ("石台高台区", ("石台", "高台", "台阶", "平台", "石砖", "花岗岩"), 0.47, 0.55),
    ("老旧房区", ("老旧", "废弃", "平房", "锅炉", "仓库"), 0.37, 0.70),
    ("排水格栅区", ("排水", "格栅", "下水", "沟"), 0.36, 0.42),
]
# 未命中任何关键词时使用的备用锚点环
GENERIC_ANCHORS = [
    (0.30, 0.30), (0.50, 0.25), (0.70, 0.30), (0.85, 0.40),
    (0.30, 0.75), (0.50, 0.80), (0.70, 0.75), (0.20, 0.50),
    (0.85, 0.60), (0.45, 0.65), (0.60, 0.20), (0.25, 0.62),
]

BOUND_X = (0.10, 0.90)
BOUND_Y = (0.12, 0.88)


def classify_coat(coat: str) -> str:
    """毛色文本 → 分组名。"""
    text = (coat or "").strip().lower()
    if not text:
        return FALLBACK_GROUP
    for group, keys, _ in COAT_GROUPS:
        for k in keys:
            if k.lower() in text:
                return group
    return FALLBACK_GROUP


def coat_color(group: str) -> str:
    for g, _, rgb in COAT_GROUPS:
        if g == group:
            return rgb
    return FALLBACK_COLOR


def coat_rgb(group: str) -> tuple[int, int, int]:
    r, g, b = (int(x) for x in coat_color(group).split(","))
    return r, g, b


def brightness_from_count(n: int) -> float:
    """照片数 → 星等。与中北版实测值逐项对齐：1→.50 2→.61 … 5→.94 8→1.05。"""
    n = max(1, int(n or 1))
    return round(min(0.5 + 0.11 * (n - 1), 1.05), 2)


def star_radius(brightness: float) -> float:
    return round(2.2 + 3.4 * brightness, 2)


def normalize_area(area: str) -> str:
    return re.sub(r"\s+", "", (area or "").strip())


def zone_of(area: str) -> Optional[str]:
    text = normalize_area(area)
    if not text:
        return None
    for name, keys, _, _ in ZONES:
        for k in keys:
            if k in text:
                return name
    return None


def split_features(features: str) -> list[str]:
    """CSV 里用空格分隔特征 → 列表。"""
    parts = re.split(r"[\s,，、;；/]+", (features or "").strip())
    return [p for p in parts if p]


def join_features(features: str) -> str:
    return "、".join(split_features(features))


def make_bio(row: CatRow) -> str:
    """只用 CSV 中的事实字段合成小传，不虚构行为故事。"""
    name = row.name or row.id
    bits = [f"{name}，编制军衔「{row.rank or '未定衔'}」，职务「{row.title or '未定岗'}」。"]
    if row.area:
        bits.append(f"常驻{normalize_area(row.area)}。")
    feats = join_features(row.features)
    if feats:
        bits.append(f"可辨识特征：{feats}。")
    if row.photo_count:
        bits.append(f"收录实拍{row.photo_count}张。")
    if row.note:
        bits.append(f"档案备注：{row.note.strip()}。")
    return "".join(bits)


# ---------- 分区与落位 ----------

def assign_anchors(areas: Iterable[str]) -> dict[str, tuple[float, float]]:
    """给每个出现过的 area 分配一个锚点坐标。"""
    uniq: list[str] = []
    for a in areas:
        key = normalize_area(a)
        if key and key not in uniq:
            uniq.append(key)

    anchors: dict[str, tuple[float, float]] = {}
    generic_i = 0
    for area in uniq:
        zone = zone_of(area)
        if zone:
            for name, _, ax, ay in ZONES:
                if name == zone:
                    anchors[area] = (ax, ay)
                    break
        else:
            anchors[area] = GENERIC_ANCHORS[generic_i % len(GENERIC_ANCHORS)]
            generic_i += 1
    return anchors


def _hash_str(s: str) -> int:
    """FNV-1a，与模板 JS 端 hashStr 同构，保证坐标稳定可复现。"""
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _rand2(seed: str) -> tuple[float, float]:
    h1 = _hash_str(seed)
    h2 = _hash_str(seed + "::y")
    return (h1 % 10000) / 10000.0, (h2 % 10000) / 10000.0


def layout_positions(rows: list[CatRow]) -> dict[str, dict[str, float]]:
    """按 area 聚簇 + 黄金角螺旋散开 + 最小间距松弛，输出 {id: {x,y}}。"""
    if not rows:
        return {}

    anchors = assign_anchors([r.area for r in rows])
    groups: dict[str, list[CatRow]] = {}
    for r in rows:
        groups.setdefault(normalize_area(r.area), []).append(r)

    pos: dict[str, dict[str, float]] = {}
    golden = math.pi * (3 - math.sqrt(5))
    for area, members in groups.items():
        ax, ay = anchors.get(area, (0.5, 0.5))
        n = len(members)
        # 半径随成员数增长，避免过密
        spread = min(0.10, 0.022 + 0.012 * math.sqrt(n))
        for i, r in enumerate(members):
            jx, jy = _rand2(r.id)
            ang = i * golden + jx * 1.7
            rad = spread * math.sqrt((i + 0.5) / n)
            x = ax + rad * math.cos(ang) + (jx - 0.5) * 0.012
            y = ay + rad * math.sin(ang) * 0.72 + (jy - 0.5) * 0.012
            pos[r.id] = {"x": round(_clamp(x, *BOUND_X), 3),
                         "y": round(_clamp(y, *BOUND_Y), 3)}

    _relax(pos, min_dist=0.032, iterations=6)
    return pos


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _relax(pos: dict[str, dict[str, float]], min_dist: float, iterations: int) -> None:
    ids = list(pos)
    for _ in range(iterations):
        moved = False
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = pos[ids[i]], pos[ids[j]]
                dx, dy = b["x"] - a["x"], b["y"] - a["y"]
                d = math.hypot(dx, dy)
                if d >= min_dist:
                    continue
                moved = True
                if d < 1e-6:
                    dx, dy, d = 0.01, 0.01, 0.0141
                push = (min_dist - d) / 2
                ux, uy = dx / d, dy / d
                a["x"] = round(_clamp(a["x"] - ux * push, *BOUND_X), 3)
                a["y"] = round(_clamp(a["y"] - uy * push, *BOUND_Y), 3)
                b["x"] = round(_clamp(b["x"] + ux * push, *BOUND_X), 3)
                b["y"] = round(_clamp(b["y"] + uy * push, *BOUND_Y), 3)
        if not moved:
            break


# ---------- CATS 条目 ----------

def to_cat_entry(row: CatRow, photo_prefix: str = "assets/photos") -> dict:
    """CatRow → CATS 数组单条（12 键契约）。"""
    group = classify_coat(row.coat)
    b = brightness_from_count(row.photo_count)
    fname = row.photo_file.strip().replace("\\", "/").split("/")[-1]
    photo = f"{photo_prefix}/{fname}" if fname else ""
    return {
        "id": normalize_id(row.id) or row.id,
        "name": row.name,
        "rank": row.rank,
        "title": row.title,
        "coat": row.coat,
        "coatGroup": group,
        "features": join_features(row.features),
        "area": normalize_area(row.area),
        "bio": make_bio(row),
        "photo": photo,
        "photoCount": max(1, row.photo_count),
        "brightness": b,
        "starColor": coat_color(group),
        "starRadius": star_radius(b),
        "confidence": row.confidence,
        "related": row.related,
        "note": row.note,
    }


def zone_stats(rows: list[CatRow]) -> dict[str, int]:
    """分区统计，供图例与报告使用。"""
    out: dict[str, int] = {}
    for r in rows:
        z = zone_of(r.area) or "未归类区"
        out[z] = out.get(z, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def coat_stats(rows: list[CatRow]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        g = classify_coat(r.coat)
        out[g] = out.get(g, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def id_gaps(rows: list[CatRow]) -> tuple[list[str], Optional[int], Optional[int]]:
    """编号连续性：返回 (空缺编号列表, 最小号, 最大号)。弃用编号允许空缺。"""
    nums = sorted(n for n in (id_number(r.id) for r in rows) if n is not None)
    if not nums:
        return [], None, None
    lo, hi = nums[0], nums[-1]
    gaps = [f"CAT-{n:03d}" for n in range(lo, hi + 1) if n not in set(nums)]
    return gaps, lo, hi
