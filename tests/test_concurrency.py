"""并发保护：项目级锁 + 原子写 + 读侧重试。

这一组测试对应三个实测出来的真 bug，不是假想需求：

1. **丢更新**。24 个并发 PUT /calib 各写一个不同编号，最后只剩 4 个；
   24 条操作日志只剩 2 条。根因是「load_meta → 改 → save_meta 整份覆盖」
   被 FastAPI 丢进线程池并发跑。
2. **读到半截**。并发读 project.json 有 12/800 次读到写了一半的内容，
   load_meta 返回 None，于是一个明明存在的项目对客户端回了 404。
3. **Windows 共享冲突**。原子替换生效后半截读归零，但换成 573/93186 次
   PermissionError(errno 13)——写方 os.replace 的一瞬间读方 open 被拒。
   load_meta 把它当「文件坏了」，症状和 bug 2 一模一样。
"""
from __future__ import annotations

import errno
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, locking, store
from app.config import (atomic_replace, atomic_write_bytes, atomic_write_text,
                        retry_read_bytes, retry_read_text, tmp_sibling)
from app.main import app
from conftest import make_csv


# ---------- 夹具与工具 ----------

@pytest.fixture
def client():
    return TestClient(app)


def _roster(n: int, shared_photos: bool = False) -> str:
    """n 只猫的名册。shared_photos=True 时每两只共用一张代表照片，
    这样 merge.candidate_groups 会给出 n/2 个 same-photo 候选组。"""
    rows = []
    for i in range(1, n + 1):
        photo = f"shared{i // 2}.jpg" if shared_photos else f"p{i}.jpg"
        rows.append([f"CAT-{i:03d}", f"猫{i}", "喵战士", f"工位{i}", "橘白",
                     f"特征{i}", photo, "1", f"区域{i}", "", "高", ""])
    return make_csv(rows)


def _upload_roster(client, pid: str, text: str) -> None:
    r = client.post(f"/api/projects/{pid}/roster",
                    files={"file": ("roster.csv", text.encode("utf-8"), "text/csv")})
    assert r.status_code == 200, r.text


@pytest.fixture
def pid(client, tmp_workspace):
    """一个已上传名册并校验过的项目——多数写路由都要求 report.json 存在。"""
    p = client.post("/api/projects", json={"school": "并发校"}).json()["id"]
    _upload_roster(client, p, _roster(12))
    assert client.post(f"/api/projects/{p}/validate").status_code == 200
    return p


def _put_calib(client, pid: str, cid: str, i: int):
    return client.put(f"/api/projects/{pid}/calib", json={
        "positions": {cid: {"x": round(i / 1000, 4), "y": round(i / 2000, 4)}},
        "merge": True, "note": f"t{i}",
    })


class _FlakyOs:
    """只替换 config 模块眼里的 os，不动全局 os——patch 全局会波及整个测试进程。"""

    def __init__(self, real, fail_times: int, exc: OSError):
        self._real = real
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0

    def replace(self, src, dst):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return self._real.replace(src, dst)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _sharing_error() -> OSError:
    return PermissionError(errno.EACCES, "Permission denied")


def _always_raises(exc: Exception):
    """造一个「一调用就抛」的替身，用来模拟读侧持续失败（而非瞬态冲突）。"""
    def boom(*_args, **_kwargs):
        raise exc
    return boom


def _tmp_residue(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if ".tmp-" in p.name)


# ---------- 锁注册表 ----------

def test_same_pid_always_gets_the_same_lock():
    assert locking.project_lock("p1") is locking.project_lock("p1")


def test_different_pids_get_different_locks():
    assert locking.project_lock("p1") is not locking.project_lock("p2")


def test_lock_is_reentrant_so_nested_helpers_cannot_self_deadlock():
    lock = locking.project_lock("reentrant-demo")
    assert lock.acquire()
    try:
        assert lock.acquire(timeout=0.01), "同一线程重复 acquire 不该把自己锁死"
        lock.release()
    finally:
        lock.release()


def test_lock_count_grows_with_distinct_pids():
    before = locking.lock_count()
    locking.project_lock(f"counted-{before}")
    assert locking.lock_count() == before + 1


def test_held_by_others_reports_a_lock_taken_by_another_thread():
    p = "held-by-others-demo"
    assert locking.held_by_others(p) is False
    taken = threading.Event()
    release = threading.Event()

    def hold():
        locking.project_lock(p).acquire()
        taken.set()
        release.wait(5)
        locking.project_lock(p).release()

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert taken.wait(5)
        assert locking.held_by_others(p) is True
    finally:
        release.set()
        t.join(5)
    assert locking.held_by_others(p) is False


def test_guard_timeout_is_read_at_call_time_so_tests_can_shrink_it(monkeypatch):
    """超时值必须在调用时才从 config 读；写成默认参数的话这里就调不小了。

    锁得由**另一个**线程持有——RLock 对同线程可重入，自己拿着锁再进 guard
    会立刻通过，测不到超时分支。
    """
    monkeypatch.setattr(config, "PROJECT_LOCK_TIMEOUT", 0.05)
    p = "timeout-read-at-call-time"
    taken = threading.Event()
    release = threading.Event()

    def hold():
        locking.project_lock(p).acquire()
        taken.set()
        release.wait(10)
        locking.project_lock(p).release()

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert taken.wait(5)
        t0 = time.time()
        with pytest.raises(Exception) as ei:
            next(locking.project_guard(p))
        waited = time.time() - t0
        assert ei.value.status_code == 409
        assert 0.04 <= waited < 5.0, \
            f"应当按调小后的 0.05 秒放弃，实际等了 {waited:.2f} 秒"
    finally:
        release.set()
        t.join(10)


def test_guard_releases_the_lock_when_the_handler_succeeds():
    p = "guard-release-ok"
    gen = locking.project_guard(p)
    next(gen)
    assert locking.held_by_others(p) is False   # 当前线程持有，对「别人」而言不算占用
    with pytest.raises(StopIteration):
        next(gen)
    assert locking.project_lock(p).acquire(timeout=0.01)
    locking.project_lock(p).release()


def test_guard_releases_the_lock_when_the_handler_raises():
    """处理函数抛异常也必须放锁——漏了的话一次报错就让项目永久卡死。"""
    p = "guard-release-raise"
    gen = locking.project_guard(p)
    next(gen)
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("炸了"))
    assert locking.project_lock(p).acquire(timeout=0.01)
    locking.project_lock(p).release()


# ---------- 原子写 ----------

def test_atomic_write_text_roundtrips(tmp_path):
    dest = tmp_path / "a.json"
    atomic_write_text(dest, '{"x": 1}')
    assert json.loads(dest.read_text(encoding="utf-8")) == {"x": 1}


def test_atomic_write_bytes_roundtrips(tmp_path):
    dest = tmp_path / "b.bin"
    atomic_write_bytes(dest, b"\x00\x01\x02")
    assert dest.read_bytes() == b"\x00\x01\x02"


def test_atomic_write_creates_missing_parents(tmp_path):
    dest = tmp_path / "deep" / "deeper" / "c.json"
    atomic_write_text(dest, "ok")
    assert dest.read_text(encoding="utf-8") == "ok"


def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    atomic_write_text(tmp_path / "d.json", "ok")
    assert _tmp_residue(tmp_path) == []


def test_atomic_write_overwrites_an_existing_file(tmp_path):
    dest = tmp_path / "e.json"
    atomic_write_text(dest, "旧")
    atomic_write_text(dest, "新")
    assert dest.read_text(encoding="utf-8") == "新"
    assert _tmp_residue(tmp_path) == []


def test_tmp_sibling_stays_in_the_same_directory(tmp_path):
    dest = tmp_path / "sub" / "f.json"
    sib = tmp_sibling(dest)
    assert sib.parent == dest.parent, "跨卷 rename 不原子，临时文件必须同目录"
    assert sib.name != dest.name


def test_tmp_sibling_names_are_unique(tmp_path):
    dest = tmp_path / "g.json"
    names = {tmp_sibling(dest).name for _ in range(50)}
    assert len(names) == 50, "两个并发写者不能共用一个临时名"


def test_failed_write_cleans_up_its_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "os", _FlakyOs(os, 999, _sharing_error()))
    monkeypatch.setattr(config, "REPLACE_ATTEMPTS", 3)
    monkeypatch.setattr(config, "REPLACE_SLEEP", 0)
    with pytest.raises(PermissionError):
        atomic_write_text(tmp_path / "h.json", "写不进去")
    assert not (tmp_path / "h.json").exists()
    assert _tmp_residue(tmp_path) == [], "写失败也不能把临时文件留在项目里"


def test_atomic_replace_retries_a_sharing_violation_then_succeeds(tmp_path, monkeypatch):
    src = tmp_path / "src.tmp"
    src.write_text("内容", encoding="utf-8")
    dest = tmp_path / "dest.json"
    flaky = _FlakyOs(os, 3, _sharing_error())
    monkeypatch.setattr(config, "os", flaky)
    monkeypatch.setattr(config, "REPLACE_SLEEP", 0)
    atomic_replace(src, dest)
    assert dest.read_text(encoding="utf-8") == "内容"
    assert flaky.calls == 4, "前 3 次被拒、第 4 次成功"


def test_atomic_replace_gives_up_after_the_attempt_budget(tmp_path, monkeypatch):
    src = tmp_path / "src2.tmp"
    src.write_text("x", encoding="utf-8")
    flaky = _FlakyOs(os, 999, _sharing_error())
    monkeypatch.setattr(config, "os", flaky)
    monkeypatch.setattr(config, "REPLACE_ATTEMPTS", 5)
    monkeypatch.setattr(config, "REPLACE_SLEEP", 0)
    with pytest.raises(PermissionError):
        atomic_replace(src, tmp_path / "dest2.json")
    assert flaky.calls == 5


def test_atomic_replace_does_not_retry_an_unrelated_oserror(tmp_path, monkeypatch):
    """真问题（文件不存在、权限不对）要立刻抛出来，不能被重试掩盖 1.2 秒。"""
    flaky = _FlakyOs(os, 999, OSError(errno.ENOENT, "No such file"))
    monkeypatch.setattr(config, "os", flaky)
    monkeypatch.setattr(config, "REPLACE_ATTEMPTS", 5)
    monkeypatch.setattr(config, "REPLACE_SLEEP", 0)
    with pytest.raises(OSError):
        atomic_replace(tmp_path / "nope.tmp", tmp_path / "dest3.json")
    assert flaky.calls == 1


# ---------- 读侧重试 ----------

def test_retry_on_sharing_recovers_from_transient_permission_errors(monkeypatch):
    monkeypatch.setattr(config, "READ_SLEEP", 0)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _sharing_error()
        return "读到了"

    assert config._retry_on_sharing(flaky) == "读到了"
    assert len(calls) == 3


def test_retry_on_sharing_raises_file_not_found_immediately(monkeypatch):
    """「文件不存在」是正常的业务分支，不该被当成共享冲突重试 40 次。"""
    monkeypatch.setattr(config, "READ_ATTEMPTS", 40)
    monkeypatch.setattr(config, "READ_SLEEP", 0)
    calls = []

    def missing():
        calls.append(1)
        raise FileNotFoundError(errno.ENOENT, "No such file")

    with pytest.raises(FileNotFoundError):
        config._retry_on_sharing(missing)
    assert len(calls) == 1


def test_retry_read_text_raises_for_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        retry_read_text(tmp_path / "根本不存在.json")


def test_retry_read_text_returns_content(tmp_path):
    dest = tmp_path / "i.json"
    dest.write_text("你好", encoding="utf-8")
    assert retry_read_text(dest) == "你好"


def test_retry_read_bytes_returns_content(tmp_path):
    dest = tmp_path / "j.bin"
    dest.write_bytes(b"\xff\xfe")
    assert retry_read_bytes(dest) == b"\xff\xfe"


def test_retry_read_survives_a_concurrent_atomic_replace(tmp_path):
    """这就是 bug 3 的回归测试：一边狂写一边狂读，读方一次都不该失败。"""
    dest = tmp_path / "k.json"
    atomic_write_text(dest, json.dumps({"v": 0}))
    failures: list[str] = []
    torn: list[int] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                json.loads(retry_read_text(dest))
            except FileNotFoundError:
                failures.append("missing")
            except PermissionError as exc:
                failures.append(f"permission {exc.errno}")
            except json.JSONDecodeError:
                torn.append(1)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        for i in range(120):
            atomic_write_text(dest, json.dumps({"v": i, "pad": "x" * 2000}))
    finally:
        stop.set()
        for t in threads:
            t.join(10)
    assert torn == [], f"读到半截 {len(torn)} 次——原子替换失效了"
    assert failures == [], f"读失败 {len(failures)} 次：{failures[:5]}"


# ---------- store 加载语义 ----------

def test_load_meta_returns_none_only_when_the_file_is_really_missing(tmp_workspace):
    assert store.load_meta("查无此项目") is None


def test_load_meta_does_not_mask_a_read_failure_as_a_missing_project(tmp_workspace, monkeypatch):
    """读不出来 ≠ 项目不存在。

    从前 load_meta 一把 except Exception → None，共享冲突会被翻译成
    「查无此项目」，客户端收到 404，项目还会从下拉框里消失。
    现在只有 FileNotFoundError 才是 None，别的读失败必须响亮地抛出去。
    （瞬态冲突由 retry_read_text 内部吸收，见 test_retry_read_survives_*。）
    """
    meta = store.create_project("瞬态校")
    monkeypatch.setattr(store, "retry_read_text", _always_raises(_sharing_error()))
    with pytest.raises(PermissionError):
        store.load_meta(meta.id)


def test_load_meta_returns_none_for_a_genuinely_missing_file(tmp_workspace, monkeypatch):
    monkeypatch.setattr(store, "retry_read_text",
                        _always_raises(FileNotFoundError(errno.ENOENT, "No such file")))
    assert store.load_meta("查无此项目") is None


def test_load_merge_does_not_fall_back_to_an_empty_book_on_read_failure(tmp_workspace, monkeypatch):
    """最要命的一处：读到空账本后 put_merge 会整份存回，把已有人工判定全抹掉。

    所以读失败必须抛，不能退化成 MergeBook()。
    """
    meta = store.create_project("归并校")
    from app.models import MergeBook, MergeDecision
    book = MergeBook()
    book.decisions["g1"] = MergeDecision(gid="g1", verdict="unsure", reason="先记一笔")
    store.save_merge(meta.id, book)
    assert "g1" in store.load_merge(meta.id).decisions, "夹具本身没存进去"

    monkeypatch.setattr(store, "retry_read_text", _always_raises(_sharing_error()))
    with pytest.raises(PermissionError):
        store.load_merge(meta.id)


def test_load_calib_returns_none_when_never_calibrated(tmp_workspace):
    meta = store.create_project("未标定校")
    assert store.load_calib(meta.id) is None


def test_saved_state_files_are_valid_json_with_no_temp_residue(tmp_workspace):
    meta = store.create_project("落盘校")
    d = store.project_dir(meta.id)
    assert json.loads((d / "project.json").read_text(encoding="utf-8"))["id"] == meta.id
    assert _tmp_residue(d) == []


# ---------- API：并发写不再丢数据 ----------

def test_concurrent_calib_writes_all_survive(client, pid):
    """bug 1 的回归测试。加锁前这里 24 个只剩 4 个。"""
    ids = [f"CAT-{i:03d}" for i in range(1, 13)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        pairs = [(cid, i) for i, cid in enumerate(ids, 1)]
        codes = [r.status_code for r in
                 ex.map(lambda t: _put_calib(client, pid, t[0], t[1]), pairs)]
    assert codes == [200] * len(ids), f"有请求没成功：{codes}"
    saved = store.load_calib(pid)
    assert len(saved.positions) == len(ids), \
        f"期望 {len(ids)} 颗星，实际 {len(saved.positions)}，丢了 {len(ids) - len(saved.positions)} 颗"
    assert set(saved.positions) == set(ids)


def test_concurrent_calib_writes_keep_every_log_entry(client, pid):
    ids = [f"CAT-{i:03d}" for i in range(1, 13)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        pairs = [(cid, i) for i, cid in enumerate(ids, 1)]
        list(ex.map(lambda t: _put_calib(client, pid, t[0], t[1]), pairs))
    entries = [e for e in store.load_meta(pid).log if e["action"] == "保存星位标定"]
    assert len(entries) == len(ids), f"操作日志丢了 {len(ids) - len(entries)} 条"


def test_concurrent_reads_never_see_a_missing_project(client, pid):
    """bug 2 的回归测试。加锁前并发读会让存在的项目回 404。"""
    misses: list[int] = []
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            code = client.get(f"/api/projects/{pid}").status_code
            if code != 200:
                misses.append(code)

    threads = [threading.Thread(target=hammer) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda t: _put_calib(client, pid, t[0], t[1]),
                        [(f"CAT-{i:03d}", i) for i in range(1, 13)]))
    finally:
        stop.set()
        for t in threads:
            t.join(15)
    assert misses == [], f"读到了 {len(misses)} 次非 200：{set(misses)}"


def test_concurrent_merge_decisions_all_survive(client, tmp_workspace):
    p = client.post("/api/projects", json={"school": "归并并发校"}).json()["id"]
    _upload_roster(client, p, _roster(8, shared_photos=True))
    assert client.post(f"/api/projects/{p}/validate").status_code == 200
    gids = [g["gid"] for g in client.get(f"/api/projects/{p}/merge").json()["groups"]]
    assert len(gids) >= 3, f"夹具没造出足够的候选组：{gids}"

    def decide(gid: str):
        return client.put(f"/api/projects/{p}/merge", json={
            "gid": gid, "verdict": "unsure", "keep": "", "drop": [],
            "reason": "并发测试留痕",
        })

    with ThreadPoolExecutor(max_workers=6) as ex:
        codes = [r.status_code for r in ex.map(decide, gids)]
    assert codes == [200] * len(gids), f"有判定没成功：{codes}"
    book = store.load_merge(p)
    assert set(book.decisions) == set(gids), \
        f"判定丢了 {sorted(set(gids) - set(book.decisions))}"


def test_concurrent_writes_leave_no_temp_files(client, pid):
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda t: _put_calib(client, pid, t[0], t[1]),
                    [(f"CAT-{i:03d}", i) for i in range(1, 13)]))
    assert _tmp_residue(store.project_dir(pid)) == []


# ---------- API：锁的作用域与失败路径 ----------

def test_busy_project_answers_409_instead_of_hanging(client, pid, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_LOCK_TIMEOUT", 0.05)
    lock = locking.project_lock(pid)
    lock.acquire()
    try:
        r = _put_calib(client, pid, "CAT-001", 1)
        assert r.status_code == 409
        assert "项目正忙" in r.json()["detail"]
    finally:
        lock.release()


def test_another_project_is_not_blocked(client, pid, monkeypatch):
    """锁是项目级的，不能因为一所学校在生成就把别的学校也堵死。"""
    monkeypatch.setattr(config, "PROJECT_LOCK_TIMEOUT", 0.05)
    other = client.post("/api/projects", json={"school": "隔壁校"}).json()["id"]
    locking.project_lock(pid).acquire()
    try:
        r = client.delete(f"/api/projects/{other}/calib")
        assert r.status_code == 200
    finally:
        locking.project_lock(pid).release()


def test_read_routes_do_not_wait_for_the_write_lock(client, pid, monkeypatch):
    """读路由故意不挂锁：文件都是原子替换的，没必要跟正在跑的生成抢。"""
    monkeypatch.setattr(config, "PROJECT_LOCK_TIMEOUT", 0.05)
    locking.project_lock(pid).acquire()
    try:
        assert client.get(f"/api/projects/{pid}").status_code == 200
        assert client.get("/api/projects").status_code == 200
        assert client.get(f"/api/projects/{pid}/calib").status_code == 200
        assert client.get(f"/api/projects/{pid}/roster").status_code == 200
        assert client.get(f"/api/projects/{pid}/merge").status_code == 200
    finally:
        locking.project_lock(pid).release()


def test_lock_is_released_after_a_request_that_raises_http_error(client, pid):
    """一次 400 之后项目必须还能用——漏放锁等于把项目永久锁死。"""
    bad = client.put(f"/api/projects/{pid}/calib", json={
        "positions": {"CAT-001": {"x": 9.0, "y": 0.5}}, "merge": True, "note": ""})
    assert bad.status_code == 400
    assert _put_calib(client, pid, "CAT-001", 1).status_code == 200


def test_lock_is_released_after_a_404(client, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_LOCK_TIMEOUT", 0.05)
    assert client.delete("/api/projects/查无此项目/calib").status_code == 404
    assert client.delete("/api/projects/查无此项目/calib").status_code == 404, \
        "第二次不该变成 409——那说明第一次把锁漏下了"


def test_guard_is_attached_to_every_write_route():
    """谁要是新加一个写路由忘了挂锁，这条会把他拦下来。"""
    guarded, unguarded = [], []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api/projects/{pid}") or "GET" in methods:
            continue
        deps = {d.call.__name__ for d in route.dependant.dependencies}
        (guarded if "project_guard" in deps else unguarded).append(f"{sorted(methods)} {path}")
    assert unguarded == [], f"这些写路由没挂项目锁：{unguarded}"
    assert len(guarded) >= 12, f"写路由数量不对劲，只找到 {len(guarded)} 个：{guarded}"


def test_read_routes_carry_no_guard():
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api") or methods != {"GET"}:
            continue
        deps = {d.call.__name__ for d in route.dependant.dependencies}
        assert "project_guard" not in deps, f"读路由 {path} 不该挂锁，会和写操作互相堵"
