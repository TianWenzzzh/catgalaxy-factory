"""F6 · 普查 batch 解析器单测（含 E 盘真实 batch1 回归）。"""
import csv
import io
from pathlib import Path

import pytest

from app.census_parser import (main_coat, merge_suggestions, parse_batch,
                               records_to_draft_csv, scene_to_area)
from app.config import ROSTER_COLUMNS

SAMPLE = """校园猫咪照片普查 batch1（格式：文件名 | 猫数量 | 主体毛色花纹 | 显著特征 | 场景线索 | 画质 | 正脸 | 人脸）

0FEE7CB5FD21467F63DAA083BA1FDDFA.jpg | 1 | 橘白（橘背橘头，胸腹及四肢内侧发白） | 短毛成年，体型中等，从侧后方拍摄 | 室内宿舍：红白格桌布木桌下、军绿暖水瓶（vivo水印 太原 2026-03-06 19:20） | A | 否 | 无
50E8904B46C698659B377B114F19A118.jpg | 1 | 橘白（橘头橘背，白嘴白胸） | 短毛成年偏瘦，仰头看镜头 | 同0FEE宿舍场景：红白格桌布木桌下、暖瓶、人腿 | A | 是 | 无
13951BEAD8371B46AB8351276772BF48.jpg | 1 | 狸花 | 短毛，体型偏小似青年猫，趴卧揣手 | 室内白色抛光瓷砖地面（教学楼/食堂大厅一类） | B | 否 | 无
5F9B576FCD0F435CEB1DC58553937C7C.jpg | 1 | 纯白 | 蓝眼睛、粉鼻，短毛成年，体态圆润 | 户外石砖路边，柏树/灌木旁，石质矮台 | A | 是 | 无
AAAA1111BBBB2222CCCC3333DDDD4444.jpg | 2 | 三花（橘黑白） | 两只同框，一大一小 | 宿舍楼前石台 | C | 否 | 有
"""


def test_parse_sample():
    records, warnings = parse_batch(SAMPLE)
    assert len(records) == 5
    assert records[0].filename == "0FEE7CB5FD21467F63DAA083BA1FDDFA.jpg"
    assert records[0].cat_count == 1
    assert records[0].quality == "A"
    assert records[0].front_face == "否"
    assert records[0].human_face == "无"
    assert records[0].line == 3


def test_header_line_skipped():
    records, warnings = parse_batch(SAMPLE)
    assert all("格式" not in r.filename for r in records)
    assert not any("字段数" in w for w in warnings)


def test_quality_maps_to_confidence():
    records, _ = parse_batch(SAMPLE)
    conf = {r.filename[0]: r.confidence for r in records}
    assert records[0].confidence == "高"      # A
    assert records[2].confidence == "中"      # B
    assert records[4].confidence == "低"      # C


def test_main_coat_strips_parens():
    assert main_coat("橘白（橘背橘头，胸腹及四肢内侧发白）") == "橘白"
    assert main_coat("橘白(半角括号)") == "橘白"
    assert main_coat("狸花") == "狸花"
    assert main_coat("") == "未定"


def test_records_get_main_coat():
    records, _ = parse_batch(SAMPLE)
    assert records[0].coat_main == "橘白"
    assert records[4].coat_main == "三花"


def test_scene_to_area():
    assert scene_to_area("室内宿舍：红白格桌布木桌下") == "宿舍区"
    assert scene_to_area("室内白色抛光瓷砖地面（教学楼/食堂大厅一类）") == "瓷砖地面"
    assert scene_to_area("户外石砖路边，柏树/灌木旁") == "石砖路边"
    assert scene_to_area("教学楼东侧绿化带") == "教学楼"
    assert scene_to_area("") == "未定区域"
    assert scene_to_area("一个完全没有关键词的角落") == "一个完全没有关键词的角落"[:12]


def test_multi_cat_warning():
    records, warnings = parse_batch(SAMPLE)
    assert records[4].cat_count == 2
    assert any("含 2 只猫" in w for w in warnings)


def test_human_face_redline_warning():
    records, warnings = parse_batch(SAMPLE)
    assert any("人脸" in w and "红线" in w for w in warnings)


def test_malformed_line_warned():
    text = "a.jpg | 1 | 橘 | 特征\n"
    records, warnings = parse_batch(text)
    assert records == []
    assert any("字段数" in w for w in warnings)


def test_non_filename_first_col_skipped():
    text = "这不是文件名 | 1 | 橘 | 特征 | 宿舍 | A | 是 | 无\n"
    records, warnings = parse_batch(text)
    assert records == []
    assert any("不像文件名" in w for w in warnings)


def test_empty_input():
    assert parse_batch("") == ([], [])


def test_batch_label_autodetect():
    _, w = parse_batch("普查 batch7 标题\n" + SAMPLE.split("\n", 2)[2])
    # 标签由 records_to_draft_csv 使用，这里只验证解析不因标题行失败
    assert isinstance(w, list)


def test_draft_csv_has_12_columns():
    records, _ = parse_batch(SAMPLE)
    text = records_to_draft_csv(records, "batch1")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ROSTER_COLUMNS
    assert len(rows) == 1 + len(records)
    assert all(len(r) == 12 for r in rows)


def test_draft_csv_content():
    records, _ = parse_batch(SAMPLE)
    text = records_to_draft_csv(records, "batch1")
    rows = list(csv.DictReader(io.StringIO(text)))
    r0 = rows[0]
    assert r0["编号"] == "CAT-001"
    assert r0["昵称"] == ""                       # 待人工命名
    assert r0["军衔"] == "" and r0["工位"] == ""
    assert r0["毛色"] == "橘白"
    assert r0["代表照片文件"] == "0FEE7CB5FD21467F63DAA083BA1FDDFA.jpg"
    assert r0["照片数量"] == "1"
    assert r0["关联照片编号"] == "batch1:0FEE"
    assert r0["置信度"] == "高"
    assert "画质A" in r0["备注"] and "草稿待人工确认" in r0["备注"]


def test_draft_csv_ids_are_sequential_from_start_index():
    records, _ = parse_batch(SAMPLE)
    rows = list(csv.DictReader(io.StringIO(records_to_draft_csv(records, "b1", start_index=5))))
    assert [r["编号"] for r in rows] == [f"CAT-{i:03d}" for i in range(5, 5 + len(records))]


def test_draft_csv_is_loadable_by_roster_parser():
    from app.csv_loader import parse_roster
    records, _ = parse_batch(SAMPLE)
    rows, missing, unknown, _ = parse_roster(records_to_draft_csv(records, "batch1"))
    assert missing == []
    assert unknown == []
    assert len(rows) == len(records)


def test_merge_suggestions_groups_same_coat_and_area():
    records, _ = parse_batch(SAMPLE)
    groups = merge_suggestions(records)
    assert groups, "同色同区的两张橘白宿舍照应被聚成候选组"
    top = groups[0]
    assert top["coat"] == "橘白"
    assert top["count"] == 2
    assert len(top["files"]) == 2


def test_merge_suggestions_skips_singletons():
    records, _ = parse_batch(SAMPLE)
    for g in merge_suggestions(records):
        assert g["count"] >= 2


def test_merge_suggestions_empty():
    assert merge_suggestions([]) == []


# ---------- 真实数据回归（只读 E 盘）----------

def test_real_batch1_parses():
    src = Path(r"E:\猫咪星图_总库\07_普查原始数据\普查-batch1.txt")
    if not src.exists():
        pytest.skip("E 盘参考数据不可用")
    text = src.read_text(encoding="utf-8", errors="replace")
    records, warnings = parse_batch(text)
    assert len(records) > 0
    assert all(r.filename.lower().endswith((".jpg", ".jpeg", ".png")) for r in records)
    assert all(r.confidence in ("高", "中", "低") for r in records)
    draft = records_to_draft_csv(records, "batch1")
    rows = list(csv.reader(io.StringIO(draft)))
    assert rows[0] == ROSTER_COLUMNS
    assert all(len(r) == 12 for r in rows)
