"""项目列表分页 + workspace 清理脚本。

分页是为了项目攒多了以后下拉框还能用；清理脚本是为了磁盘不被历次试验产物
撑爆。两者都必须有明确的边界：limit/offset 越界要报错而不是静默返回空，
清理默认干跑、路径守卫必须拦住「工作区被指到项目外」的情况。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, store
from app.main import app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cleanup  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def ws(tmp_workspace, tmp_path, monkeypatch):
    """临时工作区，同时把 config.ROOT 指过去，好让路径守卫在测试里说得通。"""
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return tmp_workspace


def make_project(school: str, age_days: float = 0.0, photos: int = 0) -> str:
    pid = store.create_project(school).id
    d = store.project_dir(pid)
    for i in range(photos):
        (d / "assets" / "photos" / f"p{i}.jpg").write_bytes(b"x" * 1024)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(d, (old, old))
    meta = store.load_meta(pid)
    meta.photo_count = photos
    store.save_meta(meta)
    if age_days:      # save_meta 会重写 project.json，把目录时间又拉回现在
        old = time.time() - age_days * 86400
        os.utime(d, (old, old))
    return pid


# ---------- 分页 ----------

def test_empty_workspace_returns_envelope(client, tmp_workspace):
    j = client.get("/api/projects").json()
    assert j == {"items": [], "total": 0, "limit": 50, "offset": 0, "has_more": False}


def test_default_limit_is_50(client, tmp_workspace):
    make_project("甲校")
    assert client.get("/api/projects").json()["limit"] == 50


def test_pagination_slices_and_reports_has_more(client, tmp_workspace):
    for name in ("一校", "二校", "三校"):
        make_project(name)
    first = client.get("/api/projects?limit=2&offset=0").json()
    assert first["total"] == 3 and len(first["items"]) == 2 and first["has_more"] is True
    second = client.get("/api/projects?limit=2&offset=2").json()
    assert len(second["items"]) == 1 and second["has_more"] is False
    ids = [m["id"] for m in first["items"]] + [m["id"] for m in second["items"]]
    assert len(set(ids)) == 3, "两页拼起来应该正好是全部项目，不重不漏"


def test_offset_beyond_the_end_yields_an_empty_page(client, tmp_workspace):
    make_project("甲校")
    j = client.get("/api/projects?limit=10&offset=99").json()
    assert j["items"] == [] and j["total"] == 1 and j["has_more"] is False


def test_newest_project_comes_first(client, tmp_workspace):
    old = make_project("老校", age_days=10)
    new = make_project("新校")
    items = client.get("/api/projects").json()["items"]
    assert [m["id"] for m in items][0] == new
    assert [m["id"] for m in items][-1] == old


def test_search_matches_school_case_insensitively(client, tmp_workspace):
    make_project("中北大学")
    make_project("清华大学")
    j = client.get("/api/projects?q=北").json()
    assert j["total"] == 1 and j["items"][0]["school"] == "中北大学"


def test_search_matches_project_id(client, tmp_workspace):
    pid = make_project("甲校")
    make_project("乙校")
    j = client.get(f"/api/projects?q={pid}").json()
    assert j["total"] == 1 and j["items"][0]["id"] == pid


def test_search_on_the_shared_timestamp_prefix_matches_every_project_of_that_second(
        client, tmp_workspace):
    """id 前缀是 epoch 秒，同一秒建的项目会一起命中——这是预期语义，不是 bug。"""
    a = make_project("甲校")
    b = make_project("乙校")
    j = client.get(f"/api/projects?q={a[:10]}").json()
    assert j["total"] == 2
    assert {m["id"] for m in j["items"]} == {a, b}


def test_search_with_no_hit(client, tmp_workspace):
    make_project("甲校")
    j = client.get("/api/projects?q=不存在的学校").json()
    assert j == {"items": [], "total": 0, "limit": 50, "offset": 0, "has_more": False}


def test_search_applies_before_pagination(client, tmp_workspace):
    for i in range(5):
        make_project(f"甲校{i}")
    make_project("乙校")
    j = client.get("/api/projects?q=甲校&limit=2").json()
    assert j["total"] == 5 and len(j["items"]) == 2 and j["has_more"] is True


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "limit=-1", "offset=-1"])
def test_out_of_range_paging_args_are_400(client, tmp_workspace, query):
    r = client.get(f"/api/projects?{query}")
    assert r.status_code == 400
    assert r.json()["detail"]


def test_listed_items_carry_the_fields_the_dropdown_needs(client, tmp_workspace):
    make_project("中北大学", photos=3)
    m = client.get("/api/projects").json()["items"][0]
    assert m["school"] == "中北大学" and m["photo_count"] == 3
    assert m["updated_at"] and m["id"]


# ---------- cleanup：时间与体积工具 ----------

@pytest.mark.parametrize("text,seconds", [("7d", 7 * 86400), ("24h", 86400),
                                          ("2w", 14 * 86400), ("1D", 86400)])
def test_parse_before(text, seconds):
    assert cleanup.parse_before(text) == seconds


@pytest.mark.parametrize("bad", ["", "7", "d", "7x", "0d", "-1d", "七天", "7 d", "7m"])
def test_parse_before_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        cleanup.parse_before(bad)


@pytest.mark.parametrize("n,human", [(0, "0B"), (512, "512B"), (2048, "2.0KB"),
                                     (5 * 1024 ** 2, "5.0MB"), (3 * 1024 ** 3, "3.0GB")])
def test_fmt_bytes(n, human):
    assert cleanup.fmt_bytes(n) == human


def test_dir_size_sums_files_only(ws):
    d = ws / "blob"
    (d / "sub").mkdir(parents=True)
    (d / "a.bin").write_bytes(b"x" * 100)
    (d / "sub" / "b.bin").write_bytes(b"y" * 50)
    assert cleanup.dir_size(d) == 150


def test_is_safe_workspace(ws, tmp_path):
    assert cleanup.is_safe_workspace(ws, tmp_path) is True
    assert cleanup.is_safe_workspace(tmp_path, tmp_path) is True
    assert cleanup.is_safe_workspace(tmp_path, tmp_path / "workspace") is False
    assert cleanup.is_safe_workspace(Path(r"E:\猫咪星图_总库"), tmp_path) is False


def test_guard_refuses_a_workspace_outside_the_project(ws, monkeypatch, capsys):
    monkeypatch.setattr(config, "ROOT", ws / "workspace" / "nope")
    assert cleanup.guard_workspace() is None
    assert "拒绝执行" in capsys.readouterr().err


# ---------- cleanup：扫描与挑选 ----------

def test_scan_reports_projects_and_orphans(ws):
    pid = make_project("甲校", age_days=3, photos=2)
    (ws / "随手丢的文件夹").mkdir()
    rows = cleanup.scan()
    by_id = {r["id"]: r for r in rows}
    assert by_id[pid]["is_project"] is True
    assert by_id[pid]["school"] == "甲校" and by_id[pid]["photo_count"] == 2
    assert by_id[pid]["bytes"] >= 2048
    assert by_id["随手丢的文件夹"]["is_project"] is False
    assert rows == sorted(rows, key=lambda r: r["mtime"]), "scan 应按最后活动时间升序"


def test_scan_flags_projects_that_have_a_bundle(ws):
    with_out = make_project("有产物校")
    (store.project_dir(with_out) / "dist").mkdir(exist_ok=True)
    (store.project_dir(with_out) / "dist" / "x.zip").write_bytes(b"z")
    plain = make_project("无产物校")
    rows = {r["id"]: r for r in cleanup.scan()}
    assert rows[with_out]["has_output"] is True
    assert rows[plain]["has_output"] is False


def test_scan_on_a_missing_workspace_is_empty(ws, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", ws / "还不存在")
    assert cleanup.scan() == []


def test_select_only_picks_the_stale(ws):
    old = make_project("老校", age_days=10)
    mid = make_project("中校", age_days=3)
    new = make_project("新校")
    rows = cleanup.scan()
    picked = cleanup.select(rows, 7 * 86400, 0, time.time())
    assert [r["id"] for r in picked] == [old]
    picked = cleanup.select(rows, 2 * 86400, 0, time.time())
    assert {r["id"] for r in picked} == {old, mid}


def test_select_keep_latest_spares_the_newest_even_if_stale(ws):
    ids = [make_project(f"老校{i}", age_days=30) for i in range(4)]
    rows = cleanup.scan()
    picked = cleanup.select(rows, 7 * 86400, 2, time.time())
    assert len(picked) == 2
    assert ids[-1] not in [r["id"] for r in picked], "最近的那个必须保住"


def test_select_with_nothing_stale(ws):
    make_project("新校")
    assert cleanup.select(cleanup.scan(), 7 * 86400, 0, time.time()) == []


# ---------- cleanup：main ----------

def test_main_dry_run_deletes_nothing(ws, capsys):
    old = make_project("老校", age_days=10, photos=1)
    assert cleanup.main(["--before", "7d"]) == 0
    out = capsys.readouterr().out
    assert "将被删除（干跑）" in out and old in out and "加 --apply" in out
    assert store.project_dir(old).exists(), "干跑绝不能动磁盘"


def test_main_apply_removes_only_the_stale(ws, capsys):
    old = make_project("老校", age_days=10)
    new = make_project("新校")
    assert cleanup.main(["--before", "7d", "--apply"]) == 0
    assert not store.project_dir(old).exists()
    assert store.project_dir(new).exists()
    out = capsys.readouterr().out
    assert "已删除 1 个项目" in out and "回收" in out


def test_main_apply_requires_before(ws, capsys):
    pid = make_project("老校", age_days=10)
    assert cleanup.main(["--apply"]) == 2
    assert "--apply 必须配 --before" in capsys.readouterr().err
    assert store.project_dir(pid).exists(), "被拒之后不该删掉任何东西"


def test_main_rejects_a_bad_before(ws, capsys):
    assert cleanup.main(["--before", "七天"]) == 2
    assert "--before 要写成" in capsys.readouterr().err


def test_main_rejects_a_negative_keep_latest(ws, capsys):
    assert cleanup.main(["--before", "7d", "--keep-latest", "-1"]) == 2
    assert "不能为负" in capsys.readouterr().err


def test_main_refuses_when_the_workspace_leaves_the_project(ws, monkeypatch, capsys):
    monkeypatch.setattr(config, "ROOT", ws / "不存在的根")
    make_project("老校", age_days=10)
    assert cleanup.main(["--before", "7d", "--apply"]) == 2
    assert "拒绝执行" in capsys.readouterr().err


def test_main_no_candidates(ws, capsys):
    make_project("新校")
    assert cleanup.main(["--before", "7d"]) == 0
    assert "没有符合条件的项目" in capsys.readouterr().out


def test_main_json_output_parses(ws, capsys):
    old = make_project("老校", age_days=10, photos=2)
    assert cleanup.main(["--before", "7d", "--apply", "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["applied"] is True and j["deleted"] == 1
    assert j["candidates"] == 1 and j["before"] == "7d"
    assert j["bytes"] >= 2048 and j["bytes_human"].endswith(("KB", "MB", "B"))
    assert j["remaining_projects"] == 0
    assert [r["id"] for r in j["rows"]] == [old]
    assert not store.project_dir(old).exists()


def test_main_stats_lists_totals(ws, capsys):
    make_project("甲校", age_days=1, photos=3)
    make_project("乙校")
    assert cleanup.main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "项目 2 个" in out and "最久没动" in out and "最近动过" in out


def test_main_stats_json(ws, capsys):
    make_project("甲校", photos=1)
    assert cleanup.main(["--stats", "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["projects"] == 1 and j["orphans"] == 0
    assert j["total_bytes"] >= 1024 and len(j["rows"]) == 1


def test_main_without_before_falls_back_to_stats(ws, capsys):
    make_project("甲校")
    assert cleanup.main([]) == 0
    out = capsys.readouterr().out
    assert "加 --before 7d" in out


def test_main_marks_orphan_directories(ws, capsys):
    (ws / "不是项目的目录").mkdir()
    cleanup.main(["--stats"])
    assert "不是项目目录" in capsys.readouterr().out


def test_main_can_reclaim_orphan_directories(ws):
    orphan = ws / "残留"
    orphan.mkdir()
    (orphan / "junk.bin").write_bytes(b"x" * 10)
    old = time.time() - 10 * 86400
    os.utime(orphan, (old, old))
    assert cleanup.main(["--before", "7d", "--apply"]) == 0
    assert not orphan.exists(), "工作区里的非项目目录属于垃圾，应该一并回收"


# ---------- cleanup：--exclude（保护名单） ----------

def test_parse_excludes_splits_commas_and_repeats():
    assert cleanup.parse_excludes(["甲校, 乙校", "丙校"]) == ["甲校", "乙校", "丙校"]
    assert cleanup.parse_excludes(None) == []
    assert cleanup.parse_excludes(["  ,  ", ""]) == []


def test_select_skips_excluded_by_id_or_by_school(ws):
    old_a = make_project("甲校", age_days=10)
    old_b = make_project("乙校", age_days=10)
    old_c = make_project("丙校", age_days=10)
    rows = cleanup.scan()
    picked = cleanup.select(rows, 7 * 86400, 0, time.time(),
                            excludes=[old_a, "乙校"])
    assert [r["id"] for r in picked] == [old_c], "按 id 和按学校名都该能排除"


def test_select_exclude_matches_a_substring(ws):
    keep = make_project("中北大学", age_days=10)
    drop = make_project("清华大学", age_days=10)
    picked = cleanup.select(cleanup.scan(), 7 * 86400, 0, time.time(), excludes=["中北"])
    assert [r["id"] for r in picked] == [drop]
    assert keep not in [r["id"] for r in picked]


def test_select_exclude_also_spares_a_non_project_directory(ws):
    orphan = ws / "证据残留目录"
    orphan.mkdir()
    old = time.time() - 10 * 86400
    os.utime(orphan, (old, old))
    pid = make_project("甲校", age_days=10)
    picked = cleanup.select(cleanup.scan(), 7 * 86400, 0, time.time(),
                            excludes=["证据残留"])
    assert [r["id"] for r in picked] == [pid]


def test_main_exclude_spares_the_project_even_with_apply(ws, capsys):
    keep = make_project("被报告引用的校", age_days=30)
    drop = make_project("跑测留下的校", age_days=30)
    assert cleanup.main(["--before", "7d", "--apply", "--exclude", keep]) == 0
    assert store.project_dir(keep).exists(), "保护名单里的项目绝不能被删"
    assert not store.project_dir(drop).exists()
    out = capsys.readouterr().out
    assert keep in out and "排除" in out


def test_main_exclude_shows_up_in_json(ws, capsys):
    keep = make_project("甲校", age_days=30)
    make_project("乙校", age_days=30)
    assert cleanup.main(["--before", "7d", "--apply", "--exclude", "甲校", "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["exclude_patterns"] == ["甲校"]
    assert j["excluded"] == [keep]
    assert j["unused_excludes"] == []
    assert j["deleted"] == 1


def test_main_dry_run_warns_when_an_exclude_matches_nothing(ws, capsys):
    make_project("甲校", age_days=30)
    assert cleanup.main(["--before", "7d", "--exclude", "写错的名字"]) == 0
    err = capsys.readouterr().err
    assert "写错的名字" in err and "没匹配到" in err


def test_main_apply_refuses_when_an_exclude_matches_nothing(ws, capsys):
    """保护名单对不上就别动手：那说明用户想保的东西不在（写错了或已被清掉），
    此时照删等于把「我以为保住了」变成一句空话，而删除是不可逆的。"""
    pid = make_project("甲校", age_days=30)
    assert cleanup.main(["--before", "7d", "--apply", "--exclude", "写错的名字"]) == 2
    err = capsys.readouterr().err
    assert "拒绝执行" in err and "没匹配到" in err
    assert store.project_dir(pid).exists(), "被拒之后一个都不该删"


def test_delete_reports_no_errors_on_a_clean_removal(ws):
    pid = make_project("甲校")
    assert cleanup.delete(pid) == []
    assert not store.project_dir(pid).exists()
