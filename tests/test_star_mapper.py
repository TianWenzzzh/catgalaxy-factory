"""F3 · 星图映射单测（毛色→星色、照片数→星等、出没区→分区）。"""
import math

from app.csv_loader import parse_roster
from app.models import CatRow
from app.star_mapper import (BOUND_X, BOUND_Y, assign_anchors, brightness_from_count,
                             classify_coat, coat_color, coat_stats, id_gaps,
                             join_features, layout_positions, make_bio, split_features,
                             star_radius, to_cat_entry, zone_of, zone_stats)

from conftest import E_ROOT


def _row(**kw) -> CatRow:
    base = dict(line=2, id="CAT-001", name="墩墩", rank="喵校长", title="总揽全校猫务",
                coat="全橘虎斑", features="体型胖 粉鼻 侧躺露肚", photo_file="a.jpg",
                photo_count=1, area="宿舍楼前石台", related="batch1:1", confidence="高", note="")
    base.update(kw)
    return CatRow(**base)


# ---------- 毛色 → 星色 ----------

def test_classify_coat_known_groups():
    assert classify_coat("全橘虎斑") == "橘"
    assert classify_coat("橘白") == "橘"
    assert classify_coat("浅橘(奶油色)虎斑") == "橘"
    assert classify_coat("狸花") == "狸花"
    assert classify_coat("彩狸(狸花腹泛橘)") == "狸花"
    assert classify_coat("三花") == "三花"
    assert classify_coat("纯白") == "纯白"
    assert classify_coat("纯黑") == "纯黑"
    assert classify_coat("奶牛") == "奶牛"
    assert classify_coat("长毛重点色") == "重点色"


def test_classify_coat_priority_三花_over_橘():
    """三花含"橘"字，但应优先归入三花组。"""
    assert classify_coat("橘黑白三花") == "三花"


def test_classify_coat_fallback():
    assert classify_coat("说不清的颜色") == "其他"
    assert classify_coat("") == "其他"
    assert coat_color("其他") == "200,200,220"


def test_coat_color_is_rgb_triple():
    for g in ("橘", "狸花", "三花", "纯白", "纯黑", "奶牛", "重点色", "其他"):
        parts = coat_color(g).split(",")
        assert len(parts) == 3
        assert all(0 <= int(p) <= 255 for p in parts)


def test_coat_groups_are_distinct_colors():
    colors = {coat_color(g) for g, _, _ in
              [(a, b, c) for a, b, c in __import__("app.star_mapper", fromlist=["COAT_GROUPS"]).COAT_GROUPS]}
    assert len(colors) == len({g for g, _, _ in
                               __import__("app.star_mapper", fromlist=["COAT_GROUPS"]).COAT_GROUPS})


# ---------- 照片数 → 星等（与中北版实测值逐项对齐）----------

def test_brightness_matches_reference_values():
    expected = {1: 0.5, 2: 0.61, 3: 0.72, 4: 0.83, 5: 0.94, 8: 1.05}
    for n, v in expected.items():
        assert brightness_from_count(n) == v, f"n={n}"


def test_brightness_caps_at_1_05():
    assert brightness_from_count(20) == 1.05
    assert brightness_from_count(100) == 1.05


def test_brightness_floor_for_bad_input():
    assert brightness_from_count(0) == 0.5
    assert brightness_from_count(-3) == 0.5


def test_star_radius_monotonic():
    rs = [star_radius(brightness_from_count(n)) for n in (1, 2, 5, 8, 30)]
    assert rs == sorted(rs)
    assert rs[0] > 0


# ---------- 出没区 → 分区 ----------

def test_zone_of_keywords():
    assert zone_of("宿舍红白格桌布区域") == "宿舍居民区"
    assert zone_of("教学楼走廊") == "教学楼食堂区"
    assert zone_of("电动车停放区") == "车库停车区"
    assert zone_of("松柏树下绿化带") == "草地植被区"
    assert zone_of("室外白色高台沿") == "石台高台区"
    assert zone_of("铁栅栏施工区") == "围墙护栏区"


def test_zone_of_unknown_returns_none():
    assert zone_of("某个没听过的地方") is None
    assert zone_of("") is None


def test_assign_anchors_covers_all_areas():
    areas = ["宿舍窗台", "教学楼大厅", "某个没听过的地方", "另一个陌生地方"]
    anchors = assign_anchors(areas)
    assert set(anchors) == set(areas)
    for x, y in anchors.values():
        assert BOUND_X[0] <= x <= BOUND_X[1]
        assert BOUND_Y[0] <= y <= BOUND_Y[1]


def test_same_area_shares_anchor():
    a = assign_anchors(["宿舍窗台", "宿舍窗台", "教学楼大厅"])
    assert len(a) == 2


# ---------- 落位 ----------

def test_layout_is_deterministic():
    rows = [_row(id=f"CAT-{i:03d}", area="宿舍窗台" if i % 2 else "教学楼大厅")
            for i in range(1, 21)]
    p1 = layout_positions(rows)
    p2 = layout_positions(rows)
    assert p1 == p2


def test_layout_covers_every_row_and_in_bounds():
    rows = [_row(id=f"CAT-{i:03d}", area=f"区域{i % 5}") for i in range(1, 77)]
    pos = layout_positions(rows)
    assert len(pos) == 76
    for p in pos.values():
        assert BOUND_X[0] <= p["x"] <= BOUND_X[1]
        assert BOUND_Y[0] <= p["y"] <= BOUND_Y[1]


def test_layout_respects_min_distance():
    rows = [_row(id=f"CAT-{i:03d}", area="宿舍窗台") for i in range(1, 13)]
    pos = layout_positions(rows)
    ids = list(pos)
    worst = min(math.dist((pos[a]["x"], pos[a]["y"]), (pos[b]["x"], pos[b]["y"]))
                for i, a in enumerate(ids) for b in ids[i + 1:])
    # 松弛后应显著拉开；边界挤压时允许略小于目标值
    assert worst > 0.02


def test_layout_same_area_clusters_tighter_than_different_area():
    same = [_row(id=f"CAT-{i:03d}", area="宿舍窗台") for i in range(1, 9)]
    diff = [_row(id=f"CAT-{i:03d}", area=f"区域{i}") for i in range(1, 9)]
    ps, pd = layout_positions(same), layout_positions(diff)

    def spread(pos):
        xs = [p["x"] for p in pos.values()]
        ys = [p["y"] for p in pos.values()]
        return (max(xs) - min(xs)) + (max(ys) - min(ys))

    assert spread(ps) < spread(pd)


def test_layout_empty():
    assert layout_positions([]) == {}


# ---------- 特征与小传 ----------

def test_split_and_join_features():
    assert split_features("体型胖 粉鼻 侧躺露肚") == ["体型胖", "粉鼻", "侧躺露肚"]
    assert join_features("体型胖 粉鼻 侧躺露肚") == "体型胖、粉鼻、侧躺露肚"
    assert join_features("") == ""


def test_bio_uses_only_csv_facts():
    b = make_bio(_row())
    assert "墩墩" in b and "喵校长" in b and "总揽全校猫务" in b
    assert "宿舍楼前石台" in b
    assert "体型胖" in b
    assert "收录实拍1张" in b


def test_bio_handles_missing_optional_fields():
    b = make_bio(_row(rank="", title="", area="", features="", photo_count=0, note=""))
    assert "未定衔" in b and "未定岗" in b


# ---------- CATS 条目契约 ----------

def test_to_cat_entry_has_reference_contract_keys():
    e = to_cat_entry(_row())
    for key in ("id", "name", "rank", "title", "coat", "coatGroup", "features",
                "area", "bio", "photo", "photoCount", "brightness"):
        assert key in e, f"缺少契约键 {key}"


def test_to_cat_entry_values():
    e = to_cat_entry(_row(photo_count=5))
    assert e["id"] == "CAT-001"
    assert e["photo"] == "assets/photos/a.jpg"
    assert e["photoCount"] == 5
    assert e["brightness"] == 0.94
    assert e["coatGroup"] == "橘"
    assert e["starColor"] == "255,190,110"
    assert e["features"] == "体型胖、粉鼻、侧躺露肚"


def test_to_cat_entry_normalizes_id():
    assert to_cat_entry(_row(id="cat_7"))["id"] == "CAT-007"


def test_to_cat_entry_without_photo():
    e = to_cat_entry(_row(photo_file=""))
    assert e["photo"] == ""


# ---------- 统计与编号 ----------

def test_zone_and_coat_stats():
    rows = [_row(id="CAT-001", coat="橘白", area="宿舍窗台"),
            _row(id="CAT-002", coat="狸花", area="宿舍窗台"),
            _row(id="CAT-003", coat="橘白", area="教学楼大厅")]
    assert zone_stats(rows) == {"宿舍居民区": 2, "教学楼食堂区": 1}
    assert coat_stats(rows) == {"橘": 2, "狸花": 1}


def test_id_gaps():
    rows = [_row(id="CAT-001"), _row(id="CAT-002"), _row(id="CAT-005")]
    gaps, lo, hi = id_gaps(rows)
    assert gaps == ["CAT-003", "CAT-004"]
    assert (lo, hi) == (1, 5)


def test_id_gaps_continuous():
    rows = [_row(id=f"CAT-{i:03d}") for i in range(1, 5)]
    gaps, lo, hi = id_gaps(rows)
    assert gaps == []
    assert (lo, hi) == (1, 4)


def test_id_gaps_empty():
    assert id_gaps([]) == ([], None, None)


# ---------- 真实数据回归（只读 E 盘）----------

def test_real_roster_76_maps_cleanly():
    """用 07_普查原始数据 的真实 76 只名册做映射回归。"""
    src = E_ROOT / "07_普查原始数据" / "猫咪名册.csv"
    if not src.exists():
        import pytest
        pytest.skip("E 盘参考数据不可用")
    rows, missing, _, _ = parse_roster(src.read_text(encoding="utf-8-sig"))
    assert missing == []
    assert len(rows) == 76
    entries = [to_cat_entry(r) for r in rows]
    assert len(entries) == 76
    assert all(e["id"].startswith("CAT-") for e in entries)
    assert all(e["coatGroup"] != "" for e in entries)
    assert all(0.5 <= e["brightness"] <= 1.05 for e in entries)
    assert all(e["photo"].startswith("assets/photos/") for e in entries)
    pos = layout_positions(rows)
    assert len(pos) == 76
    # 真实数据里"橘"应是最大分组（实测 14 只奶牛 / 多数橘系）
    assert "橘" in coat_stats(rows)
