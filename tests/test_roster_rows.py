"""F10 补完 · 名册在线增行/删行。

改单元格已经能修脏数据，但「普查时漏了一只猫」和「这只猫其实不该收」两件事
仍然要下载 CSV → 改 → 重传，绕一圈还会把在线改过的单元格全部冲掉。这两个
端点就是把这一圈抹平：

    POST   /api/projects/{pid}/roster/rows        在第 after 行之后插一行
    DELETE /api/projects/{pid}/roster/rows/{line}  删掉一个物理行

行号沿用 PATCH 的口径：**物理行号，表头是第 1 行**，前端表格上显示的行号
可以直接拿来用，不用换算。

两条不变量：
1. 增行**不替用户编编号**。留空就留空，编号重复就重复——校验会报
   E_ID_MISSING / E_ID_DUP，由人决定怎么改。工具越权补号会让「弃用编号不复用」
   这条数据红线悄悄被破坏。
2. 删行**只删名册里的那一行**，不动已入库的照片。多出来的照片会落进
   「未使用照片」提示，那是事实，不该由删除动作掩盖。
"""
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


def _row(cid, name, photo="a.jpg", conf="高"):
    return [cid, name, "喵员", "巡校内务", "全橘虎斑", "体型胖 粉鼻", photo,
            "1", "宿舍楼前石台", "batch1:TEST", conf, ""]


@pytest.fixture
def roster_project(client, tmp_workspace):
    """两只猫、两张照片、校验全绿的项目——增删行的基线。"""
    pid = client.post("/api/projects", json={"school": "增删校"}).json()["id"]
    text = make_csv([_row("CAT-001", "墩墩", "a.jpg"),
                     _row("CAT-002", "格子", "b.jpg")])
    client.post(f"/api/projects/{pid}/roster",
                files={"file": ("r.csv", text.encode("utf-8-sig"), "text/csv")})
    client.post(f"/api/projects/{pid}/photos",
                files=[("files", ("a.jpg", make_jpeg(640, 480), "image/jpeg")),
                       ("files", ("b.jpg", make_jpeg(640, 480), "image/jpeg"))])
    r = client.post(f"/api/projects/{pid}/validate")
    assert r.json()["summary"]["ok"] is True
    return pid


def _csv_rows(pid):
    text = store.project_dir(pid).joinpath("roster.csv").read_text(encoding="utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


# ---------- POST：插入一行 ----------

def test_insert_appends_after_last_line(client, roster_project):
    r = client.post(f"/api/projects/{roster_project}/roster/rows",
                    json={"after": 3, "values": {"编号": "CAT-003", "昵称": "煤球",
                                                 "代表照片文件": "a.jpg"}})
    assert r.status_code == 200
    body = r.json()
    assert body["line"] == 4
    rows = _csv_rows(roster_project)
    assert len(rows) == 4
    assert rows[3][0] == "CAT-003" and rows[3][1] == "煤球" and rows[3][6] == "a.jpg"


def test_insert_after_header_shifts_existing_rows_down(client, roster_project):
    r = client.post(f"/api/projects/{roster_project}/roster/rows",
                    json={"after": 1, "values": {"编号": "CAT-000", "昵称": "插队的"}})
    assert r.json()["line"] == 2
    rows = _csv_rows(roster_project)
    assert [x[0] for x in rows[1:]] == ["CAT-000", "CAT-001", "CAT-002"]


def test_insert_fills_unlisted_columns_with_empty_strings(client, roster_project):
    client.post(f"/api/projects/{roster_project}/roster/rows",
                json={"after": 3, "values": {"编号": "CAT-003"}})
    rows = _csv_rows(roster_project)
    assert len(rows[3]) == 12
    assert rows[3][1] == "" and rows[3][10] == "" and rows[3][11] == ""


def test_insert_trims_surrounding_space(client, roster_project):
    client.post(f"/api/projects/{roster_project}/roster/rows",
                json={"after": 3, "values": {"昵称": "  墩墩二号  "}})
    assert _csv_rows(roster_project)[3][1] == "墩墩二号"


def test_insert_keeps_header_and_other_rows_byte_identical(client, roster_project):
    before = _csv_rows(roster_project)
    client.post(f"/api/projects/{roster_project}/roster/rows",
                json={"after": 2, "values": {"编号": "CAT-009", "昵称": "塞中间的"}})
    after = _csv_rows(roster_project)
    assert after[0] == before[0] == CSV_HEADER.split(",")
    assert after[1] == before[1]
    assert after[3] == before[2]


def test_insert_persists_to_both_csv_copies_with_bom(client, roster_project, tmp_workspace):
    client.post(f"/api/projects/{roster_project}/roster/rows",
                json={"after": 3, "values": {"编号": "CAT-003", "昵称": "煤球"}})
    root = store.project_dir(roster_project)
    for path in (root / "roster.csv", root / "data" / "猫咪名册.csv"):
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 应带 BOM，Excel 才不乱码"
        rows, _m, _u, _h = parse_roster(raw.decode("utf-8-sig"))
        assert [r.id for r in rows] == ["CAT-001", "CAT-002", "CAT-003"]


def test_insert_writes_operation_log(client, roster_project):
    client.post(f"/api/projects/{roster_project}/roster/rows",
                json={"after": 3, "values": {"编号": "CAT-003", "昵称": "煤球"}})
    log = store.load_meta(roster_project).log
    assert any(e["action"] == "名册增行" and "CAT-003" in e["detail"] for e in log)


def test_insert_revalidates_by_default(client, roster_project):
    r = client.post(f"/api/projects/{roster_project}/roster/rows",
                    json={"after": 3, "values": {"编号": "CAT-003", "昵称": "煤球",
                                                 "毛色": "纯黑", "代表照片文件": "a.jpg"}})
    assert r.json()["report"]["summary"]["valid_rows"] == 3


def test_insert_revalidate_false_skips_report(client, roster_project):
    r = client.post(f"/api/projects/{roster_project}/roster/rows",
                    json={"after": 3, "values": {"编号": "CAT-003"}, "revalidate": False})
    assert r.json()["report"] is None
    assert len(_csv_rows(roster_project)) == 4      # 行照样落盘


def test_insert_does_not_invent_an_id(client, roster_project):
    """留空就是留空：由校验报「编号缺失」，工具不替用户编编号。"""
    r = client.post(f"/api/projects/{roster_project}/roster/rows",
                    json={"after": 3, "values": {"昵称": "没编号的"}})
    assert r.status_code == 200
    assert _csv_rows(roster_project)[3][0] == ""
    issues = r.json()["report"]["issues"]
    assert any(i["code"] == "E_BAD_ID" for i in issues)


def test_insert_duplicate_id_surfaces_as_error_not_silently_fixed(client, roster_project):
    r = client.post(f"/api/projects/{roster_project}/roster/rows",
                    json={"after": 3, "values": {"编号": "CAT-001", "昵称": "冒名顶替"}})
    codes = [i["code"] for i in r.json()["report"]["issues"]]
    assert "E_DUP_ID" in codes
    assert _csv_rows(roster_project)[3][0] == "CAT-001"     # 没被偷偷改成 CAT-003


def test_insert_unknown_field_is_reported_but_row_still_created(client, roster_project):
    r = client.post(f"/api/projects/{roster_project}/roster/rows",
                    json={"after": 3, "values": {"编号": "CAT-003", "星座": "狮子"}})
    body = r.json()
    assert body["line"] == 4
    assert "星座" in body["ignored"][0]["why"]
    assert len(_csv_rows(roster_project)) == 4


def test_insert_after_out_of_range_line_is_400(client, roster_project):
    r = client.post(f"/api/projects/{roster_project}/roster/rows",
                    json={"after": 99, "values": {"编号": "CAT-003"}})
    assert r.status_code == 400
    assert len(_csv_rows(roster_project)) == 3          # 越界不许留下半拉子行


def test_insert_after_zero_or_negative_is_400(client, roster_project):
    for after in (0, -1):
        r = client.post(f"/api/projects/{roster_project}/roster/rows",
                        json={"after": after, "values": {"编号": "CAT-003"}})
        assert r.status_code == 400, f"after={after} 应被拒"


def test_insert_404_without_roster(client, tmp_workspace):
    pid = client.post("/api/projects", json={"school": "空校"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/roster/rows",
                    json={"after": 1, "values": {"编号": "CAT-001"}})
    assert r.status_code == 404
    assert "尚未上传名册" in r.json()["detail"]      # 是「没有名册」，不是「没有这个路由」


def test_insert_then_generate_picks_up_the_new_cat(client, roster_project):
    client.post(f"/api/projects/{roster_project}/roster/rows",
                json={"after": 3, "values": {"编号": "CAT-003", "昵称": "煤球",
                                             "代表照片文件": "a.jpg", "毛色": "纯黑"}})
    r = client.post(f"/api/projects/{roster_project}/generate", json={"form": "relative"})
    assert r.status_code == 200 and r.json()["cats"] == 3
    html = next((store.project_dir(roster_project) / "out" / "relative").glob("*.html"))
    assert "煤球" in html.read_text(encoding="utf-8")


# ---------- DELETE：删掉一行 ----------

def test_delete_removes_that_row_and_keeps_the_rest(client, roster_project):
    r = client.delete(f"/api/projects/{roster_project}/roster/rows/2")
    assert r.status_code == 200
    assert r.json()["deleted"]["row"][0] == "CAT-001"
    rows = _csv_rows(roster_project)
    assert len(rows) == 2
    assert rows[0] == CSV_HEADER.split(",")
    assert rows[1][0] == "CAT-002"


def test_delete_last_data_row_leaves_header_only(client, roster_project):
    client.delete(f"/api/projects/{roster_project}/roster/rows/2")
    r = client.delete(f"/api/projects/{roster_project}/roster/rows/2")
    assert r.status_code == 200
    assert _csv_rows(roster_project) == [CSV_HEADER.split(",")]
    assert r.json()["report"]["summary"]["total_rows"] == 0


def test_delete_persists_to_both_csv_copies(client, roster_project, tmp_workspace):
    client.delete(f"/api/projects/{roster_project}/roster/rows/2")
    root = store.project_dir(roster_project)
    for path in (root / "roster.csv", root / "data" / "猫咪名册.csv"):
        rows, _m, _u, _h = parse_roster(path.read_bytes().decode("utf-8-sig"))
        assert [r.id for r in rows] == ["CAT-002"]


def test_delete_writes_operation_log_with_the_deleted_id(client, roster_project):
    client.delete(f"/api/projects/{roster_project}/roster/rows/3")
    log = store.load_meta(roster_project).log
    assert any(e["action"] == "名册删行" and "CAT-002" in e["detail"] for e in log)


def test_delete_revalidates_by_default(client, roster_project):
    r = client.delete(f"/api/projects/{roster_project}/roster/rows/3")
    assert r.json()["report"]["summary"]["valid_rows"] == 1


def test_delete_revalidate_false_skips_report(client, roster_project):
    r = client.delete(f"/api/projects/{roster_project}/roster/rows/3?revalidate=false")
    assert r.json()["report"] is None
    assert len(_csv_rows(roster_project)) == 2


def test_delete_header_line_is_400(client, roster_project):
    r = client.delete(f"/api/projects/{roster_project}/roster/rows/1")
    assert r.status_code == 400
    assert len(_csv_rows(roster_project)) == 3          # 表头还在，名册没被毁


def test_delete_out_of_range_line_is_400(client, roster_project):
    for line in (0, -1, 4, 99):
        r = client.delete(f"/api/projects/{roster_project}/roster/rows/{line}")
        assert r.status_code == 400, f"line={line} 应被拒"
    assert len(_csv_rows(roster_project)) == 3


def test_delete_404_without_roster(client, tmp_workspace):
    pid = client.post("/api/projects", json={"school": "空校"}).json()["id"]
    r = client.delete(f"/api/projects/{pid}/roster/rows/2")
    assert r.status_code == 404
    assert "尚未上传名册" in r.json()["detail"]      # 是「没有名册」，不是「没有这个路由」


def test_deleted_cat_photo_becomes_unused_not_deleted(client, roster_project, tmp_workspace):
    """删行不动照片：多出来的那张应该落进「未使用照片」提示，而不是被顺手删掉。"""
    r = client.delete(f"/api/projects/{roster_project}/roster/rows/3")
    codes = [i["code"] for i in r.json()["report"]["issues"]]
    assert "I_PHOTO_UNUSED" in codes
    assert (store.project_dir(roster_project) / "assets" / "photos" / "b.jpg").exists()


def test_delete_then_generate_drops_the_cat(client, roster_project):
    client.delete(f"/api/projects/{roster_project}/roster/rows/3")
    r = client.post(f"/api/projects/{roster_project}/generate", json={"form": "relative"})
    assert r.json()["cats"] == 1
    html = next((store.project_dir(roster_project) / "out" / "relative").glob("*.html"))
    assert "格子" not in html.read_text(encoding="utf-8")


# ---------- 两端点合起来 ----------

def test_insert_then_delete_round_trips_to_the_original_roster(client, roster_project):
    before = _csv_rows(roster_project)
    client.post(f"/api/projects/{roster_project}/roster/rows",
                json={"after": 2, "values": {"编号": "CAT-009", "昵称": "路过的"}})
    client.delete(f"/api/projects/{roster_project}/roster/rows/3")
    assert _csv_rows(roster_project) == before
