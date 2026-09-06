"""F2 · 校验引擎单测。"""
from app.csv_loader import parse_roster
from app.models import CatRow
from app.validate import (error_codes, passing_rows, report_markdown, validate)


def _rows(csv_text):
    rows, missing, unknown, _ = parse_roster(csv_text)
    return rows, missing, unknown


def test_happy_path(two_row_csv):
    rows, missing, unknown = _rows(two_row_csv)
    rep = validate(rows, {"demo-001.jpg", "demo-002.jpg"},
                   missing_columns=missing, unknown_columns=unknown)
    assert rep.summary.ok is True
    assert rep.summary.error_count == 0
    assert rep.summary.total_rows == 2
    assert rep.summary.valid_rows == 2
    assert rep.summary.photos_uploaded == 2
    assert rep.summary.photos_referenced == 2
    assert rep.summary.photos_unused == 0
    assert rep.summary.id_min == "CAT-001"
    assert rep.summary.id_max == "CAT-002"


def test_missing_column_is_error(two_row_csv):
    rows, _, _ = _rows(two_row_csv)
    rep = validate(rows, {"demo-001.jpg", "demo-002.jpg"}, missing_columns=["置信度"])
    assert rep.summary.ok is False
    assert "E_MISSING_COLUMN" in error_codes(rep)


def test_photo_missing_is_error(two_row_csv):
    rows, missing, unknown = _rows(two_row_csv)
    rep = validate(rows, {"demo-001.jpg"}, missing_columns=missing, unknown_columns=unknown)
    codes = error_codes(rep)
    assert "E_PHOTO_MISSING" in codes
    assert rep.summary.ok is False
    hit = [i for i in rep.issues if i.code == "E_PHOTO_MISSING"][0]
    assert hit.line == 3
    assert hit.cat_id == "CAT-002"


def test_duplicate_id(two_row_csv, csv_factory):
    text = csv_factory([
        ["CAT-001", "a", "b", "c", "橘", "特征描述", "p1.jpg", "1", "宿舍", "r", "高", ""],
        ["CAT-001", "b", "b", "c", "橘白", "特征描述", "p2.jpg", "1", "草地", "r", "高", ""],
    ])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, {"p1.jpg", "p2.jpg"}, missing_columns=missing, unknown_columns=unknown)
    assert "E_DUP_ID" in error_codes(rep)
    assert rep.summary.ok is False


def test_bad_id_format(csv_factory):
    text = csv_factory([["DOG-9", "a", "b", "c", "橘", "特征描述", "p.jpg", "1", "宿舍", "r", "高", ""]])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, {"p.jpg"}, missing_columns=missing, unknown_columns=unknown)
    assert "E_BAD_ID" in error_codes(rep)


def test_required_field_empty(csv_factory):
    text = csv_factory([["CAT-001", "", "b", "c", "", "特征描述", "", "1", "宿舍", "r", "高", ""]])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, set(), missing_columns=missing, unknown_columns=unknown)
    req = [i for i in rep.issues if i.code == "E_REQUIRED_FIELD"]
    assert {i.field for i in req} == {"昵称", "毛色", "代表照片文件"}


def test_low_confidence_warning(two_row_csv, csv_factory):
    text = csv_factory([
        ["CAT-001", "a", "b", "c", "橘", "特征描述", "p1.jpg", "1", "宿舍", "r", "低", ""],
        ["CAT-002", "b", "b", "c", "橘白", "特征描述", "p2.jpg", "1", "草地", "r", "高", ""],
    ])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, {"p1.jpg", "p2.jpg"}, missing_columns=missing, unknown_columns=unknown)
    assert rep.summary.ok is True                      # 低置信度只警告不拦截
    assert rep.summary.low_confidence == ["CAT-001"]
    assert "W_LOW_CONFIDENCE" in [i.code for i in rep.issues]


def test_bad_confidence_value(csv_factory):
    text = csv_factory([["CAT-001", "a", "b", "c", "橘", "特征描述", "p.jpg", "1", "宿舍", "r", "很高", ""]])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, {"p.jpg"}, missing_columns=missing, unknown_columns=unknown)
    assert "W_BAD_CONFIDENCE" in [i.code for i in rep.issues]
    assert rep.summary.ok is True


def test_id_gaps_are_info_only(csv_factory):
    """弃用编号允许空缺：只出 info，不拦截。"""
    text = csv_factory([
        ["CAT-001", "a", "b", "c", "橘", "特征描述", "p1.jpg", "1", "宿舍", "r", "高", ""],
        ["CAT-004", "d", "b", "c", "橘白", "特征描述", "p4.jpg", "1", "草地", "r", "高", ""],
    ])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, {"p1.jpg", "p4.jpg"}, missing_columns=missing, unknown_columns=unknown)
    assert rep.summary.ok is True
    assert rep.summary.id_gaps == ["CAT-002", "CAT-003"]
    gap_issues = [i for i in rep.issues if i.code == "I_ID_GAP"]
    assert len(gap_issues) == 2
    assert all(i.level == "info" for i in gap_issues)


def test_shared_photo_warning(csv_factory):
    text = csv_factory([
        ["CAT-001", "a", "b", "c", "橘", "特征描述", "same.jpg", "1", "宿舍", "r", "高", ""],
        ["CAT-002", "b", "b", "c", "橘白", "特征描述", "same.jpg", "1", "草地", "r", "高", ""],
    ])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, {"same.jpg"}, missing_columns=missing, unknown_columns=unknown)
    assert "W_PHOTO_SHARED" in [i.code for i in rep.issues]
    assert rep.summary.ok is True


def test_unused_photo_info(two_row_csv):
    rows, missing, unknown = _rows(two_row_csv)
    rep = validate(rows, {"demo-001.jpg", "demo-002.jpg", "extra.jpg"},
                   missing_columns=missing, unknown_columns=unknown)
    assert rep.summary.photos_unused == 1
    assert "I_PHOTO_UNUSED" in [i.code for i in rep.issues]


def test_photo_match_is_case_insensitive(csv_factory):
    text = csv_factory([["CAT-001", "a", "b", "c", "橘", "特征描述", "PHOTO.JPG", "1", "宿舍", "r", "高", ""]])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, {"photo.jpg"}, missing_columns=missing, unknown_columns=unknown)
    assert "E_PHOTO_MISSING" not in error_codes(rep)


def test_bad_photo_count_warning(csv_factory):
    text = csv_factory([["CAT-001", "a", "b", "c", "橘", "特征描述", "p.jpg", "0", "宿舍", "r", "高", ""]])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, {"p.jpg"}, missing_columns=missing, unknown_columns=unknown)
    assert "W_BAD_COUNT" in [i.code for i in rep.issues]


def test_empty_roster_is_error():
    rep = validate([], set(), missing_columns=[])
    assert rep.summary.ok is False
    assert "E_EMPTY_ROSTER" in error_codes(rep)


def test_passing_rows_excludes_bad_lines(csv_factory):
    text = csv_factory([
        ["CAT-001", "a", "b", "c", "橘", "特征描述", "p1.jpg", "1", "宿舍", "r", "高", ""],
        ["CAT-002", "b", "b", "c", "橘白", "特征描述", "missing.jpg", "1", "草地", "r", "高", ""],
        ["CAT-003", "c", "b", "c", "三花", "特征描述", "p3.jpg", "1", "食堂", "r", "低", ""],
    ])
    rows, missing, unknown = _rows(text)
    rep = validate(rows, {"p1.jpg", "p3.jpg"}, missing_columns=missing, unknown_columns=unknown)
    assert [r.id for r in passing_rows(rep)] == ["CAT-001", "CAT-003"]
    assert [r.id for r in passing_rows(rep, exclude_low_confidence=True)] == ["CAT-001"]


def test_report_markdown(two_row_csv):
    rows, missing, unknown = _rows(two_row_csv)
    rep = validate(rows, {"demo-001.jpg", "demo-002.jpg"},
                   missing_columns=missing, unknown_columns=unknown, school="示例校")
    md = report_markdown(rep)
    assert "# 示例校猫咪名册 · 校验报告" in md
    assert "数据行总数" in md
    assert "| 级别 | 代码 |" in md


def test_rows_preserved_in_report(two_row_csv):
    rows, missing, unknown = _rows(two_row_csv)
    rep = validate(rows, {"demo-001.jpg", "demo-002.jpg"},
                   missing_columns=missing, unknown_columns=unknown)
    assert len(rep.rows) == 2
    assert isinstance(rep.rows[0], CatRow)
    assert rep.rows[0].related == "batch1:DEMO"
    assert rep.rows[1].note == "同场景连拍确认"
