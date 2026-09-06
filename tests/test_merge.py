"""F9 · 归并工作台：候选组识别、判定体检、持久化与 F7 留痕。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import merge, store
from app.main import app
from app.models import CatRow, MergeBook, MergeDecision
from conftest import make_csv, make_jpeg


@pytest.fixture
def client():
    return TestClient(app)


def _cat(cid: str, coat: str = "全橘虎斑", area: str = "宿舍楼前石台",
         features: str = "体型胖 粉鼻 侧躺露肚", photo: str = "", line: int = 2) -> CatRow:
    return CatRow(line=line, id=cid, name=f"猫{cid}", coat=coat, area=area,
                  features=features, photo_file=photo, photo_count=1, confidence="高")


def _row(cid, name, coat, area, features, photo, conf="高"):
    return [cid, name, "喵员", "巡校内务", coat, features, photo, "1", area,
            "batch1:TEST", conf, ""]


@pytest.fixture
def dup_project(client, tmp_workspace):
    """两行共用同一张代表照片 → 必出一个 same-photo 候选组。"""
    pid = client.post("/api/projects", json={"school": "归并校"}).json()["id"]
    text = make_csv([
        _row("CAT-001", "墩墩", "全橘虎斑", "宿舍楼前石台", "体型胖 粉鼻 侧躺露肚", "dup.jpg"),
        _row("CAT-002", "小橘", "全橘虎斑", "宿舍楼前石台", "体型胖 粉鼻 侧躺露肚", "dup.jpg"),
    ])
    client.post(f"/api/projects/{pid}/roster",
                files={"file": ("r.csv", text.encode("utf-8-sig"), "text/csv")})
    client.post(f"/api/projects/{pid}/photos",
                files=[("files", ("dup.jpg", make_jpeg(640, 480), "image/jpeg"))])
    client.post(f"/api/projects/{pid}/validate")
    return pid


# ---------- 相似度 ----------

def test_similarity_identical_is_one():
    assert merge.similarity("体型胖 粉鼻", "体型胖 粉鼻") == 1.0


def test_similarity_disjoint_is_zero():
    assert merge.similarity("全橘虎斑", "三花断尾") == 0.0


def test_similarity_empty_is_zero():
    assert merge.similarity("", "体型胖") == 0.0
    assert merge.similarity("", "") == 0.0


def test_similarity_partial_between_zero_and_one():
    s = merge.similarity("体型胖 粉鼻 侧躺露肚", "体型胖 粉鼻 断尾")
    assert 0.0 < s < 1.0


def test_similarity_ignores_punctuation():
    assert merge.similarity("体型胖，粉鼻", "体型胖 粉鼻") == 1.0


# ---------- 候选组 ----------

def test_shared_photo_makes_strongest_group():
    rows = [_cat("CAT-001", photo="a.jpg", line=2), _cat("CAT-002", photo="a.jpg", line=3)]
    groups = merge.candidate_groups(rows)
    assert groups and groups[0]["kind"] == "same-photo"
    assert groups[0]["score"] == 0.95
    assert {m["id"] for m in groups[0]["members"]} == {"CAT-001", "CAT-002"}


def test_shared_photo_matching_is_case_insensitive():
    rows = [_cat("CAT-001", photo="A.JPG", line=2), _cat("CAT-002", photo="a.jpg", line=3)]
    assert merge.candidate_groups(rows)[0]["kind"] == "same-photo"


def test_same_coat_area_similar_features_groups():
    rows = [_cat("CAT-001", features="体型胖 粉鼻 侧躺露肚", line=2),
            _cat("CAT-002", features="体型胖 粉鼻 断尾", line=3)]
    groups = merge.candidate_groups(rows)
    assert len(groups) == 1 and groups[0]["kind"] == "coat-area"
    assert "相似度" in groups[0]["reason"]


def test_same_coat_area_but_unlike_features_not_grouped():
    rows = [_cat("CAT-001", features="体型胖粉鼻侧躺露肚", line=2),
            _cat("CAT-002", features="瘦长黑尾左耳缺角", line=3)]
    assert merge.candidate_groups(rows) == []


def test_different_area_not_grouped():
    rows = [_cat("CAT-001", area="宿舍楼前石台", line=2),
            _cat("CAT-002", area="食堂后门", line=3)]
    assert merge.candidate_groups(rows) == []


def test_big_coat_cluster_is_skipped():
    """同色同区一次冒出 6 只，那是猫扎堆不是重复建档，比对没有意义。"""
    rows = [_cat(f"CAT-{i:03d}", line=i + 1) for i in range(1, 7)]
    assert merge.candidate_groups(rows) == []


def test_photo_group_wins_over_coat_area_group():
    rows = [_cat("CAT-001", photo="a.jpg", line=2), _cat("CAT-002", photo="a.jpg", line=3)]
    kinds = [g["kind"] for g in merge.candidate_groups(rows)]
    assert kinds == ["same-photo"]


def test_gid_is_content_derived_and_stable():
    rows = [_cat("CAT-001", photo="a.jpg", line=2), _cat("CAT-002", photo="a.jpg", line=3)]
    g1 = merge.candidate_groups(rows)[0]["gid"]
    g2 = merge.candidate_groups(rows)[0]["gid"]
    assert g1 == g2 and g1.startswith("M-")


def test_gid_changes_when_membership_changes():
    a = [_cat("CAT-001", photo="a.jpg", line=2), _cat("CAT-002", photo="a.jpg", line=3)]
    b = [_cat("CAT-001", photo="a.jpg", line=2), _cat("CAT-003", photo="a.jpg", line=3)]
    assert merge.candidate_groups(a)[0]["gid"] != merge.candidate_groups(b)[0]["gid"]


def test_groups_sorted_by_suspicion():
    rows = [_cat("CAT-001", photo="a.jpg", line=2),
            _cat("CAT-002", photo="a.jpg", line=3),
            _cat("CAT-003", coat="橘白", area="食堂后门", features="白胸腹橘头", line=4),
            _cat("CAT-004", coat="橘白", area="食堂后门", features="白胸腹橘头断尾", line=5)]
    groups = merge.candidate_groups(rows)
    assert [g["kind"] for g in groups] == ["same-photo", "coat-area"]


# ---------- 判定体检 ----------

def _group():
    rows = [_cat("CAT-001", photo="a.jpg", line=2), _cat("CAT-002", photo="a.jpg", line=3)]
    return merge.candidate_groups(rows)[0]


def test_valid_same_decision_passes():
    assert merge.check_decision(_group(), "same", "CAT-001", ["CAT-002"], "同场景连拍同一只") == []


def test_valid_different_decision_passes():
    assert merge.check_decision(_group(), "different", "", [], "尾型不同，是两只") == []


def test_unsure_needs_no_reason():
    assert merge.check_decision(_group(), "unsure", "", [], "") == []


def test_bad_verdict_rejected():
    errs = merge.check_decision(_group(), "maybe", "", [], "随便")
    assert any("same/different/unsure" in e for e in errs)


def test_same_without_keep_rejected():
    errs = merge.check_decision(_group(), "same", "", ["CAT-002"], "同一只")
    assert any("保留" in e for e in errs)


def test_same_without_drop_rejected():
    errs = merge.check_decision(_group(), "same", "CAT-001", [], "同一只")
    assert any("弃用" in e for e in errs)


def test_verdict_without_reason_rejected():
    errs = merge.check_decision(_group(), "same", "CAT-001", ["CAT-002"], "")
    assert any("理由" in e for e in errs)


def test_outsider_ids_rejected():
    errs = merge.check_decision(_group(), "same", "CAT-009", ["CAT-010"], "同一只")
    assert len(errs) == 2


def test_keep_also_dropped_rejected():
    errs = merge.check_decision(_group(), "same", "CAT-001", ["CAT-001"], "同一只")
    assert any("既保留又弃用" in e for e in errs)


# ---------- 存储 ----------

def test_merge_book_roundtrip(tmp_workspace):
    pid = "p1"
    book = MergeBook(decisions={"M-1": MergeDecision(gid="M-1", verdict="same",
                                                    keep="CAT-001", drop=["CAT-002"],
                                                    reason="连拍同一只")})
    store.save_merge(pid, book)
    back = store.load_merge(pid)
    assert back.decisions["M-1"].reason == "连拍同一只"
    assert store.merge_path(pid).exists()


def test_corrupt_merge_json_tolerated(tmp_workspace):
    pid = "p2"
    store.save_merge(pid, MergeBook())
    store.merge_path(pid).write_text("{不是 JSON", encoding="utf-8")
    assert store.load_merge(pid).decisions == {}


def test_missing_merge_file_is_empty_book(tmp_workspace):
    assert store.load_merge("不存在的项目").decisions == {}


def test_drop_merge(tmp_workspace):
    pid = "p3"
    store.save_merge(pid, MergeBook(decisions={"M-1": MergeDecision(gid="M-1")}))
    assert store.drop_merge(pid, "M-1") is True
    assert store.drop_merge(pid, "M-1") is False
    assert store.load_merge(pid).decisions == {}


# ---------- API ----------

def test_get_merge_lists_candidates(client, dup_project):
    j = client.get(f"/api/projects/{dup_project}/merge").json()
    assert j["stats"]["groups"] >= 1
    g = j["groups"][0]
    assert g["kind"] == "same-photo" and g["decision"] is None
    assert {m["id"] for m in g["members"]} == {"CAT-001", "CAT-002"}


def test_members_carry_photo_url_pointing_at_a_real_file(client, dup_project):
    """静态挂载绑的是真实 WORKSPACE，测试里只能验 URL 形状 + 落盘文件存在。"""
    j = client.get(f"/api/projects/{dup_project}/merge").json()
    url = j["groups"][0]["members"][0]["photo_url"]
    assert url == f"/bundle/{dup_project}/assets/photos/dup.jpg"
    assert (store.project_dir(dup_project) / "assets" / "photos" / "dup.jpg").exists()


def test_missing_photo_yields_empty_url(client, tmp_workspace):
    """名册引用了没上传的照片时，工作台不该给出一个死链。"""
    pid = client.post("/api/projects", json={"school": "缺图校"}).json()["id"]
    text = make_csv([
        _row("CAT-001", "甲", "全橘虎斑", "宿舍楼前石台", "体型胖 粉鼻", "ghost.jpg"),
        _row("CAT-002", "乙", "全橘虎斑", "宿舍楼前石台", "体型胖 粉鼻", "ghost.jpg"),
    ])
    client.post(f"/api/projects/{pid}/roster",
                files={"file": ("r.csv", text.encode("utf-8-sig"), "text/csv")})
    client.post(f"/api/projects/{pid}/validate")
    j = client.get(f"/api/projects/{pid}/merge").json()
    assert [m["photo_url"] for m in j["groups"][0]["members"]] == ["", ""]


def test_get_merge_404_for_unknown_project(client, tmp_workspace):
    assert client.get("/api/projects/没这个项目/merge").status_code == 404


def test_put_decision_persists_and_counts(client, dup_project):
    gid = client.get(f"/api/projects/{dup_project}/merge").json()["groups"][0]["gid"]
    r = client.put(f"/api/projects/{dup_project}/merge",
                   json={"gid": gid, "verdict": "same", "keep": "CAT-001",
                         "drop": ["CAT-002"], "reason": "同场景连拍，耳纹一致"})
    assert r.status_code == 200
    assert r.json()["saved"]["verdict"] == "same"
    assert r.json()["saved"]["updated_at"]
    j = client.get(f"/api/projects/{dup_project}/merge").json()
    assert j["groups"][0]["decision"]["keep"] == "CAT-001"
    assert j["stats"]["decided"] == 1 and j["stats"]["pending"] == j["stats"]["groups"] - 1
    assert j["stats"]["same"] == 1


def test_put_decision_writes_operation_log(client, dup_project):
    gid = client.get(f"/api/projects/{dup_project}/merge").json()["groups"][0]["gid"]
    client.put(f"/api/projects/{dup_project}/merge",
               json={"gid": gid, "verdict": "different", "reason": "尾型不同"})
    log = store.load_meta(dup_project).log
    assert any(e["action"] == "归并判定" and "不是同一只" in e["detail"]
               and "尾型不同" in e["detail"] for e in log)


def test_put_bad_verdict_400(client, dup_project):
    gid = client.get(f"/api/projects/{dup_project}/merge").json()["groups"][0]["gid"]
    r = client.put(f"/api/projects/{dup_project}/merge",
                   json={"gid": gid, "verdict": "maybe", "reason": "x"})
    assert r.status_code == 400


def test_put_same_without_reason_400_and_saves_nothing(client, dup_project):
    gid = client.get(f"/api/projects/{dup_project}/merge").json()["groups"][0]["gid"]
    r = client.put(f"/api/projects/{dup_project}/merge",
                   json={"gid": gid, "verdict": "same", "keep": "CAT-001",
                         "drop": ["CAT-002"], "reason": ""})
    assert r.status_code == 400 and "理由" in r.json()["detail"]
    assert client.get(f"/api/projects/{dup_project}/merge").json()["stats"]["decided"] == 0


def test_put_unknown_gid_404(client, dup_project):
    r = client.put(f"/api/projects/{dup_project}/merge",
                   json={"gid": "M-deadbeef", "verdict": "unsure"})
    assert r.status_code == 404


def test_put_outsider_keep_400(client, dup_project):
    gid = client.get(f"/api/projects/{dup_project}/merge").json()["groups"][0]["gid"]
    r = client.put(f"/api/projects/{dup_project}/merge",
                   json={"gid": gid, "verdict": "same", "keep": "CAT-777",
                         "drop": ["CAT-002"], "reason": "瞎填"})
    assert r.status_code == 400 and "CAT-777" in r.json()["detail"]


def test_put_overwrites_previous_decision(client, dup_project):
    gid = client.get(f"/api/projects/{dup_project}/merge").json()["groups"][0]["gid"]
    client.put(f"/api/projects/{dup_project}/merge",
               json={"gid": gid, "verdict": "unsure"})
    client.put(f"/api/projects/{dup_project}/merge",
               json={"gid": gid, "verdict": "different", "reason": "改判：耳纹不同"})
    j = client.get(f"/api/projects/{dup_project}/merge").json()
    assert j["groups"][0]["decision"]["verdict"] == "different"
    assert j["stats"]["decided"] == 1


def test_delete_decision(client, dup_project):
    gid = client.get(f"/api/projects/{dup_project}/merge").json()["groups"][0]["gid"]
    client.put(f"/api/projects/{dup_project}/merge", json={"gid": gid, "verdict": "unsure"})
    assert client.delete(f"/api/projects/{dup_project}/merge/{gid}").json()["cleared"] is True
    j = client.get(f"/api/projects/{dup_project}/merge").json()
    assert j["groups"][0]["decision"] is None and j["stats"]["decided"] == 0


def test_delete_unknown_gid_reports_false(client, dup_project):
    assert client.delete(f"/api/projects/{dup_project}/merge/M-nope").json()["cleared"] is False


def test_stale_decision_is_flagged(client, dup_project):
    """名册改了 → 旧组号消失，判定不该悄悄挂到新组上，而要报成 stale。"""
    store.save_merge(dup_project, MergeBook(decisions={"M-gone": MergeDecision(gid="M-gone")}))
    j = client.get(f"/api/projects/{dup_project}/merge").json()
    assert j["stats"]["stale_gids"] == ["M-gone"]


def test_summary_contains_decision_with_reason(client, dup_project):
    gid = client.get(f"/api/projects/{dup_project}/merge").json()["groups"][0]["gid"]
    client.put(f"/api/projects/{dup_project}/merge",
               json={"gid": gid, "verdict": "same", "keep": "CAT-001",
                     "drop": ["CAT-002"], "reason": "同场景连拍，耳纹一致"})
    md = client.post(f"/api/projects/{dup_project}/summary").json()["markdown"]
    assert "归并判定记录" in md and "同场景连拍，耳纹一致" in md and "同一只猫" in md


def test_summary_without_decisions_says_so(client, dup_project):
    md = client.post(f"/api/projects/{dup_project}/summary").json()["markdown"]
    assert "暂无判定" in md


def test_merge_json_is_valid_utf8_with_chinese_reason(client, dup_project, tmp_workspace):
    gid = client.get(f"/api/projects/{dup_project}/merge").json()["groups"][0]["gid"]
    client.put(f"/api/projects/{dup_project}/merge",
               json={"gid": gid, "verdict": "different", "reason": "耳纹与尾型都不同"})
    raw = store.merge_path(dup_project).read_text(encoding="utf-8")
    assert "耳纹与尾型都不同" in raw
