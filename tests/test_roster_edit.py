"""F10 · 名册在线编辑：改一格就重跑校验，不用下载-改-上传绕一圈。"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import store
from app.csv_loader import parse_roster
from app.main import app
from conftest import CSV_HEADER, make_csv, make_jpeg


@pytest.fixture
def client():
    return TestClient(app)


def _row(cid, name, coat="全橘虎斑", photo="a.jpg", conf="高", area="宿舍楼前石台"):
    return [cid, name, "喵员", "巡校内务", coat, "体型胖 粉鼻 侧躺露肚", photo,
            "1", area, "batch1:TEST", conf, ""]


@pytest.fixture
def broken_project(client, tmp_workspace):
    """第 2 行昵称空缺、第 3 行编号格式错 —— 两个 error 都能靠改单元格修好。"""
    pid = client.post("/api/projects", json={"school": "编辑校"}).json()["id"]
    text = make_csv([
        _row("CAT-001", ""),
        _row("猫二", "格子", photo="b.jpg"),
    ])
    r = client.post(f"/api/projects/{pid}/roster",
                    files={"file": ("r.csv", text.encode("utf-8-sig"), "text/csv")})
    assert r.status_code == 200
    client.post(f"/api/projects/{pid}/photos",
                files=[("files", ("a.jpg", make_jpeg(640, 480), "image/jpeg")),
                       ("files", ("b.jpg", make_jpeg(640, 480), "image/jpeg"))])
    client.post(f"/api/projects/{pid}/validate")
    return pid


# ---------- GET ----------

def test_get_roster_404_before_upload(client, tmp_workspace):
    pid = client.post("/api/projects", json={"school": "空校"}).json()["id"]
    assert client.get(f"/api/projects/{pid}/roster").status_code == 404


def test_get_roster_returns_header_rows_and_mapping(client, broken_project):
    j = client.get(f"/api/projects/{broken_project}/roster").json()
    assert j["header"] == CSV_HEADER.split(",")
    assert len(j["rows"]) == 2 and j["line_offset"] == 2
    assert j["mapping"]["昵称"] == 1 and j["mapping"]["编号"] == 0
    assert j["editable_columns"][0] == "编号"
    assert j["rows"][0][0] == "CAT-001"


def test_get_roster_carries_last_report(client, broken_project):
    j = client.get(f"/api/projects/{broken_project}/roster").json()
    assert j["report"]["summary"]["error_count"] == 2


# ---------- PATCH：修好错误 ----------

def test_patch_fills_missing_name_and_clears_error(client, broken_project):
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 2, "field": "昵称", "value": "墩墩"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == [{"line": 2, "field": "昵称", "old": "", "new": "墩墩"}]
    assert body["rejected"] == []
    assert body["report"]["summary"]["error_count"] == 1


def test_patch_fixes_bad_id_and_makes_validation_pass(client, broken_project):
    client.patch(f"/api/projects/{broken_project}/roster",
                 json={"edits": [{"line": 2, "field": "昵称", "value": "墩墩"}]})
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 3, "field": "编号", "value": "CAT-002"}]})
    s = r.json()["report"]["summary"]
    assert s["error_count"] == 0 and s["ok"] is True and s["valid_rows"] == 2


def test_patch_applies_several_edits_at_once(client, broken_project):
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 2, "field": "昵称", "value": "墩墩"},
                                     {"line": 3, "field": "编号", "value": "CAT-002"},
                                     {"line": 3, "field": "置信度", "value": "中"}]})
    assert len(r.json()["applied"]) == 3
    assert r.json()["report"]["summary"]["error_count"] == 0


def test_patch_persists_to_both_csv_copies(client, broken_project, tmp_workspace):
    client.patch(f"/api/projects/{broken_project}/roster",
                 json={"edits": [{"line": 2, "field": "昵称", "value": "墩墩"}]})
    root = store.project_dir(broken_project)
    for path in (root / "roster.csv", root / "data" / "猫咪名册.csv"):
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 应带 BOM，Excel 才不乱码"
        rows, _m, _u, _h = parse_roster(raw.decode("utf-8-sig"))
        assert rows[0].name == "墩墩"


def test_patch_keeps_other_columns_intact(client, broken_project):
    client.patch(f"/api/projects/{broken_project}/roster",
                 json={"edits": [{"line": 2, "field": "昵称", "value": "墩墩"}]})
    text = store.project_dir(broken_project).joinpath("roster.csv").read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == CSV_HEADER.split(",")
    assert rows[1][4] == "全橘虎斑" and rows[1][6] == "a.jpg" and rows[1][10] == "高"
    assert rows[2][0] == "猫二"          # 没动的行原样保留


def test_patch_writes_operation_log(client, broken_project):
    client.patch(f"/api/projects/{broken_project}/roster",
                 json={"edits": [{"line": 2, "field": "昵称", "value": "墩墩"}]})
    log = store.load_meta(broken_project).log
    assert any(e["action"] == "在线编辑名册" and "行2·昵称" in e["detail"] for e in log)


def test_patch_trims_surrounding_space(client, broken_project):
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 2, "field": "昵称", "value": "  墩墩  "}]})
    assert r.json()["applied"][0]["new"] == "墩墩"


def test_patch_can_blank_a_cell(client, broken_project):
    client.patch(f"/api/projects/{broken_project}/roster",
                 json={"edits": [{"line": 2, "field": "昵称", "value": "墩墩"}]})
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 2, "field": "备注", "value": ""}]})
    assert r.json()["applied"][0] == {"line": 2, "field": "备注", "old": "", "new": ""}


# ---------- PATCH：拒绝非法改动 ----------

def test_patch_unknown_field_is_rejected_not_applied(client, broken_project):
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 2, "field": "星座", "value": "狮子"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == [] and body["report"] is None
    assert "星座" in body["rejected"][0]["why"]


def test_patch_out_of_range_line_is_rejected(client, broken_project):
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 99, "field": "昵称", "value": "x"}]})
    assert r.json()["applied"] == []
    assert "行号越界" in r.json()["rejected"][0]["why"]


def test_patch_header_line_is_not_editable(client, broken_project):
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 1, "field": "昵称", "value": "x"}]})
    assert r.json()["applied"] == [] and "行号越界" in r.json()["rejected"][0]["why"]


def test_patch_mixed_valid_and_invalid(client, broken_project):
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 2, "field": "昵称", "value": "墩墩"},
                                     {"line": 42, "field": "昵称", "value": "不存在"},
                                     {"line": 2, "field": "血型", "value": "O"}]})
    body = r.json()
    assert len(body["applied"]) == 1 and len(body["rejected"]) == 2
    assert body["report"]["summary"]["error_count"] == 1


def test_patch_empty_edits_400(client, broken_project):
    r = client.patch(f"/api/projects/{broken_project}/roster", json={"edits": []})
    assert r.status_code == 400


def test_patch_404_without_roster(client, tmp_workspace):
    pid = client.post("/api/projects", json={"school": "空校"}).json()["id"]
    r = client.patch(f"/api/projects/{pid}/roster",
                     json={"edits": [{"line": 2, "field": "昵称", "value": "x"}]})
    assert r.status_code == 404


def test_patch_revalidate_false_skips_report(client, broken_project):
    r = client.patch(f"/api/projects/{broken_project}/roster",
                     json={"edits": [{"line": 2, "field": "昵称", "value": "墩墩"}],
                           "revalidate": False})
    assert r.json()["report"] is None
    rows, _m, _u, _h = parse_roster(
        store.project_dir(broken_project).joinpath("roster.csv").read_text(encoding="utf-8-sig"))
    assert rows[0].name == "墩墩"          # 改动照样落盘，只是不重跑校验


def test_patch_short_row_gets_padded(client, tmp_workspace):
    """列数不够的行（脏 CSV 常见）补空格再写，不该 IndexError。"""
    pid = client.post("/api/projects", json={"school": "短行校"}).json()["id"]
    text = CSV_HEADER + "\n" + "CAT-001,墩墩\n"
    client.post(f"/api/projects/{pid}/roster",
                files={"file": ("r.csv", text.encode("utf-8-sig"), "text/csv")})
    r = client.patch(f"/api/projects/{pid}/roster",
                     json={"edits": [{"line": 2, "field": "备注", "value": "补齐了"}]})
    assert r.json()["applied"][0]["new"] == "补齐了"
    rows = list(csv.reader(io.StringIO(
        store.project_dir(pid).joinpath("roster.csv").read_text(encoding="utf-8-sig"))))
    assert len(rows[1]) == 12 and rows[1][11] == "补齐了"


def test_patch_then_generate_uses_new_value(client, broken_project):
    for edit in ({"line": 2, "field": "昵称", "value": "墩墩"},
                 {"line": 3, "field": "编号", "value": "CAT-002"}):
        client.patch(f"/api/projects/{broken_project}/roster", json={"edits": [edit]})
    r = client.post(f"/api/projects/{broken_project}/generate", json={"form": "relative"})
    assert r.status_code == 200 and r.json()["cats"] == 2
    out = store.project_dir(broken_project) / "out" / "relative"
    html = next(out.glob("*.html")).read_text(encoding="utf-8")
    assert "墩墩" in html
