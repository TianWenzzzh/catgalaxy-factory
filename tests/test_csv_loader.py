"""F1 · CSV 解析单测。"""
from app.config import ROSTER_COLUMNS
from app.csv_loader import (build_column_map, decode_bytes, empty_template,
                            id_number, normalize_id, parse_roster)


def test_parse_12_columns(two_row_csv):
    rows, missing, unknown, header = parse_roster(two_row_csv)
    assert missing == []
    assert unknown == []
    assert len(header) == 12
    assert len(rows) == 2
    r = rows[0]
    assert r.id == "CAT-001"
    assert r.name == "墩墩"
    assert r.rank == "喵校长"
    assert r.coat == "全橘虎斑"
    assert r.photo_file == "demo-001.jpg"
    assert r.photo_count == 1
    assert r.area == "宿舍楼前石台"
    assert r.confidence == "高"
    assert r.line == 2


def test_parse_handles_bom(two_row_csv):
    rows, missing, _, _ = parse_roster("\ufeff" + two_row_csv)
    assert missing == []
    assert rows[0].id == "CAT-001"


def test_decode_gbk():
    text, enc = decode_bytes("编号,昵称\nCAT-001,墩墩\n".encode("gb18030"))
    assert "编号" in text
    assert enc in ("gb18030", "gbk")


def test_decode_utf8():
    text, enc = decode_bytes("编号,昵称\n".encode("utf-8"))
    assert enc.startswith("utf-8")
    assert "昵称" in text


def test_missing_column_detected(csv_factory):
    header = ",".join(c for c in ROSTER_COLUMNS if c != "置信度")
    text = csv_factory([["CAT-001", "x", "y", "z", "橘", "f", "p.jpg", "1", "a", "r", "备注值"]],
                       header=header)
    rows, missing, _, _ = parse_roster(text)
    assert "置信度" in missing
    assert len(rows) == 1


def test_column_order_insensitive(csv_factory):
    header = ",".join(reversed(ROSTER_COLUMNS))
    text = csv_factory([["备注", "低", "rel", "宿舍", "2", "p.jpg", "特征描述值",
                         "橘白", "工位值", "军衔值", "墩墩", "CAT-001"]], header=header)
    rows, missing, _, _ = parse_roster(text)
    assert missing == []
    assert rows[0].id == "CAT-001"
    assert rows[0].name == "墩墩"
    assert rows[0].photo_count == 2


def test_alias_columns(csv_factory):
    header = "id,name,rank,title,毛色,特征,photo,照片数,area,关联照片,可信度,note"
    text = csv_factory([["CAT-007", "奶油", "中士", "巡逻", "浅橘", "胖胖的",
                         "a.jpg", "8", "宿舍窗台", "b1:1", "高", "x"]], header=header)
    rows, missing, _, _ = parse_roster(text)
    assert rows[0].id == "CAT-007"
    assert rows[0].photo_count == 8


def test_empty_lines_skipped(two_row_csv):
    rows, _, _, _ = parse_roster(two_row_csv + "\n\n   \n")
    assert len(rows) == 2


def test_normalize_id():
    assert normalize_id("CAT-001") == "CAT-001"
    assert normalize_id("cat_1") == "CAT-001"
    assert normalize_id("CAT 12") == "CAT-012"
    assert normalize_id("CAT-080") == "CAT-080"
    assert normalize_id("") is None
    assert normalize_id("DOG-001") is None
    assert normalize_id("CAT-") is None


def test_id_number():
    assert id_number("CAT-076") == 76
    assert id_number("bad") is None


def test_photo_count_non_numeric(csv_factory):
    text = csv_factory([["CAT-001", "a", "b", "c", "橘", "特征描述", "p.jpg",
                         "三张", "宿舍", "r", "高", ""]])
    rows, _, _, _ = parse_roster(text)
    assert rows[0].photo_count == 0
    assert rows[0].photo_count_raw == "三张"


def test_empty_template_is_valid():
    text = empty_template()
    rows, missing, unknown, _ = parse_roster(text)
    assert missing == []
    assert unknown == []
    assert len(rows) == 2
    assert rows[0].id == "CAT-001"


def test_build_column_map_unknown():
    mapping, missing, unknown = build_column_map(ROSTER_COLUMNS + ["多余列"])
    assert len(mapping) == 12
    assert missing == []
    assert unknown == ["多余列"]
