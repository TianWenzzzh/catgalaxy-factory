"""长任务进度：上传压缩与生成打包都是几十秒量级，前端得看得见它在走。

这里盯四件事：
1. Reporter 的状态机——终态必须被写下，异常路径不能把记录留在 running；
2. 真实张数——上传一个 zip 时前端只数得出「1 个文件」，展开后必须把里面的
   张数报上去，否则进度条显示 1/1 然后再也没动静；
3. 进度接口不能被项目锁堵住——挂了锁它就会排在自己要汇报的那个上传后面，
   前端什么也看不到，进度条彻底失去意义；
4. 也不能被事件循环堵住——入库的同步重活必须挪到线程池，理由同上：
   循环被占住时，不挂锁的进度接口照样一个请求都答不上来。
"""
from __future__ import annotations

import io
import json
import threading
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import config, locking, progress
from app.image_proc import ZipBudget, extract_photo_zip
from app.main import GENERATE_STAGES, app, generate_bundle
from conftest import make_csv, make_jpeg


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def client(tmp_workspace):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pid(client):
    """一个干净的空项目——没名册，所以照片入库后不会顺手重跑校验。"""
    r = client.post("/api/projects", json={"school": "进度校"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _roster_two() -> str:
    return make_csv([
        ["CAT-001", "墩墩", "喵校长", "总揽全校猫务", "全橘虎斑",
         "体型胖 粉鼻", "demo-001.jpg", "1", "宿舍楼前", "", "高", ""],
        ["CAT-002", "格子", "中士", "宿舍内务员", "橘白",
         "橘头白胸腹", "demo-002.jpg", "1", "教学楼走廊", "", "高", ""],
    ])


@pytest.fixture
def ready(client):
    """名册 + 照片齐备、校验通过、可以直接生成的项目。"""
    p = client.post("/api/projects", json={"school": "进度校"}).json()["id"]
    client.post(f"/api/projects/{p}/roster",
                files={"file": ("名册.csv", _roster_two().encode("utf-8-sig"), "text/csv")})
    r = client.post(f"/api/projects/{p}/photos",
                    files=[("files", (n, make_jpeg(400, 300), "image/jpeg"))
                           for n in ("demo-001.jpg", "demo-002.jpg")])
    assert r.json()["report"]["summary"]["ok"] is True, r.text
    return p


# ---------- Reporter 状态机 ----------

def test_reporter_writes_a_running_record_as_soon_as_it_is_entered(tmp_workspace):
    with progress.Reporter("p1", "上传并压缩照片", total=3):
        rec = progress.read("p1")
        assert rec["state"] == "running"
        assert rec["task"] == "上传并压缩照片"
        assert rec["total"] == 3


def test_reporter_marks_done_on_normal_exit(tmp_workspace):
    with progress.Reporter("p2", "压缩", total=2) as rep:
        rep.bump("a.jpg")
        rep.bump("b.jpg")
    rec = progress.read("p2")
    assert rec["state"] == "done"
    assert (rec["done"], rec["total"], rec["fraction"]) == (2, 2, 1.0)


def test_reporter_marks_failed_when_the_body_raises(tmp_workspace):
    """异常路径也必须落终态，否则 progress.json 永远停在 running，
    下一个打开页面的人会看到一条走不完的进度条。"""
    with pytest.raises(RuntimeError):
        with progress.Reporter("p3", "压缩", total=5) as rep:
            rep.bump("a.jpg")
            raise RuntimeError("Pillow 炸了")
    rec = progress.read("p3")
    assert rec["state"] == "failed"
    assert "Pillow 炸了" in rec["message"]
    assert rec["done"] == 1          # 崩之前确实只压完一张


def test_reporter_does_not_swallow_the_exception(tmp_workspace):
    """记进度不能改变业务行为：HTTPException 该回什么状态码还得回什么。"""
    with pytest.raises(ValueError):
        with progress.Reporter("p4", "x"):
            raise ValueError("boom")


def test_finish_message_overrides_the_default(tmp_workspace):
    with progress.Reporter("p5", "压缩", total=1) as rep:
        rep.finish("76 张已入库")
    assert progress.read("p5")["message"] == "76 张已入库"


def test_finish_clamps_done_to_total_so_the_bar_reaches_the_end(tmp_workspace):
    """跳过的文件不会被 bump，done 会小于 total；终态得把它补齐，
    否则条子永远停在 96%。"""
    with progress.Reporter("p6", "压缩", total=10) as rep:
        rep.bump()
        rep.bump()
        rep.finish("2 张已入库，8 个被跳过")
    rec = progress.read("p6")
    assert (rec["done"], rec["total"], rec["fraction"]) == (10, 10, 1.0)


def test_each_run_gets_its_own_id(tmp_workspace):
    """前端要靠 run_id 分清「这次的任务」和「上次崩掉留下的尸体」。"""
    a = progress.Reporter("p7", "x").run_id
    b = progress.Reporter("p7", "x").run_id
    assert a and b and a != b


def test_fraction_is_capped_when_stages_overshoot_the_total(tmp_workspace):
    rep = progress.Reporter("p8", "生成", total=2)
    rep.bump(); rep.bump(); rep.bump()
    assert rep.fraction == 1.0


def test_fraction_is_none_when_the_total_is_unknown(tmp_workspace):
    """总量未知（zip 还没打开）时返回 None，前端据此画不确定态条纹，
    而不是编一个假百分比。"""
    assert progress.Reporter("p9", "压缩", total=None).fraction is None
    assert progress.Reporter("p9", "压缩", total=0).fraction is None


def test_grow_total_accumulates_across_nested_zips(tmp_workspace):
    rep = progress.Reporter("p10", "压缩", total=1)
    rep.grow_total(10)
    rep.grow_total(5)
    assert rep.total == 16


def test_grow_total_ignores_garbage(tmp_workspace):
    rep = progress.Reporter("p11", "压缩", total=3)
    rep.grow_total(0)
    rep.grow_total(-5)
    assert rep.total == 3


def test_drop_total_never_goes_negative(tmp_workspace):
    rep = progress.Reporter("p12", "压缩", total=1)
    rep.drop_total()
    rep.drop_total()
    assert rep.total == 0
    assert rep.fraction is None      # 0 是「不知道」，不是「已完成」


def test_flush_is_throttled_so_200_photos_do_not_mean_200_disk_writes(tmp_workspace,
                                                                     monkeypatch):
    written: list[str] = []
    real = config.atomic_write_text
    monkeypatch.setattr(progress, "atomic_write_text",
                        lambda p, t, **kw: (written.append(t), real(p, t, **kw))[1])
    monkeypatch.setattr(progress, "FLUSH_INTERVAL", 0.05)
    with progress.Reporter("p13", "压缩", total=200) as rep:
        for i in range(200):
            rep.bump(f"p{i}.jpg")
    assert len(written) < 40, f"节流没生效：200 张照片写了 {len(written)} 次盘"
    assert json.loads(written[-1])["state"] == "done"


def test_terminal_states_are_flushed_immediately_not_throttled(tmp_workspace,
                                                              monkeypatch):
    """done/failed 必须立刻可见：节流掉的话前端最后一次轮询可能还停在 97%，
    然后请求回来了，看着像跳变。"""
    monkeypatch.setattr(progress, "FLUSH_INTERVAL", 60.0)
    with progress.Reporter("p14", "压缩", total=2) as rep:
        rep.bump(); rep.bump()
        rec = progress.read("p14")
        assert rec["state"] == "running"     # 这两次 bump 被节流掉了
    assert progress.read("p14")["state"] == "done"


# ---------- 读侧的容错 ----------

def test_read_returns_idle_for_a_project_that_never_ran_anything(tmp_workspace):
    rec = progress.read("never-existed")
    assert rec["state"] == "idle"
    assert rec["run_id"] == ""


def test_read_treats_a_corrupt_record_as_no_progress(tmp_workspace):
    """读到写坏的文件时当成没进度，别让前端崩在解析上。"""
    p = progress.progress_path("bad")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{这不是 json", encoding="utf-8")
    assert progress.read("bad")["state"] == "idle"


def test_read_defaults_fields_missing_from_an_older_record(tmp_workspace):
    p = progress.progress_path("old")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"task":"旧版"}', encoding="utf-8")
    rec = progress.read("old")
    assert rec["state"] == "idle" and rec["run_id"] == ""


def test_snapshot_carries_the_server_clock(tmp_workspace):
    """带 server_now 是为了让前端不必信自己的本地时钟——学生机器时间不准时，
    拿 Date.now() 减 updated_at 会算出负数，好好在跑的任务被报成已中断。"""
    with progress.Reporter("p15", "x", total=1) as rep:
        rep.bump()
    snap = progress.snapshot_for_client("p15")
    assert snap["stale_after"] == progress.STALE_AFTER
    assert 0 <= snap["server_now"] - snap["updated_at"] < 5


# ---------- 解包侧的回调 ----------

def test_zip_expansion_reports_the_entry_count_before_the_slow_loop(tmp_path):
    """先报总量再开始压：不然前端在整个压缩过程中都只能画不确定态条纹。"""
    seen_total: list[int] = []
    seen_items: list[str] = []
    blob = _zip({f"c{i}.jpg": make_jpeg(30, 20) for i in range(4)})
    budget = ZipBudget(on_item=seen_items.append, on_total=seen_total.append)
    got = extract_photo_zip(blob, tmp_path, budget=budget)
    assert len(got) == 4
    assert seen_total == [4]
    assert sorted(seen_items) == [f"c{i}.jpg" for i in range(4)]


def test_a_photo_that_fails_to_decode_is_not_reported_as_done(tmp_path):
    """解不开的文件是「跳过」不是「完成」，报成完成会让进度条虚高。"""
    seen: list[str] = []
    blob = _zip({"good.jpg": make_jpeg(30, 20), "bad.jpg": "这不是图片".encode("utf-8")})
    budget = ZipBudget(on_item=seen.append)
    extract_photo_zip(blob, tmp_path, budget=budget)
    assert seen == ["good.jpg"]
    assert budget.corrupt == ["bad.jpg"]


def test_a_broken_progress_callback_cannot_break_the_upload(tmp_path):
    """进度是参考信息：写 progress.json 撞上磁盘满，该失败的是进度条，
    不是用户那几张照片的入库。"""
    def boom(*_args):
        raise OSError("磁盘满了")

    blob = _zip({"a.jpg": make_jpeg(30, 20), "b.jpg": make_jpeg(30, 20)})
    budget = ZipBudget(on_item=boom, on_total=boom)
    got = extract_photo_zip(blob, tmp_path, budget=budget)
    assert len(got) == 2


def test_no_callback_means_no_overhead_and_no_crash(tmp_path):
    blob = _zip({"a.jpg": make_jpeg(30, 20)})
    assert len(extract_photo_zip(blob, tmp_path, budget=ZipBudget())) == 1


# ---------- 路由 ----------

def test_progress_route_returns_idle_for_a_fresh_project(client, pid):
    rec = client.get(f"/api/projects/{pid}/progress").json()
    assert rec["state"] == "idle"
    assert "server_now" in rec and "stale_after" in rec


def test_progress_route_404s_for_an_unknown_project_without_creating_a_directory(
        client, tmp_workspace):
    """Reporter 一进去就写 progress.json，而 atomic_write_text 会 mkdir 父目录。
    顺序反了的话，随便探一个不存在的 pid 就能在 workspace/ 里留下一个空目录。"""
    assert client.get("/api/projects/ghost/progress").status_code == 404
    assert not (tmp_workspace / "ghost").exists()


def test_photo_upload_to_an_unknown_project_does_not_create_a_directory(
        client, tmp_workspace):
    r = client.post("/api/projects/ghost/photos",
                    files=[("files", ("a.jpg", make_jpeg(30, 20), "image/jpeg"))])
    assert r.status_code == 404
    assert not (tmp_workspace / "ghost").exists()


def test_generate_on_an_unknown_project_does_not_create_a_directory(client, tmp_workspace):
    r = client.post("/api/projects/ghost/generate", json={"form": "relative"})
    assert r.status_code == 404
    assert not (tmp_workspace / "ghost").exists()


def test_uploading_loose_photos_leaves_a_done_record(client, pid):
    files = [("files", (f"p{i}.jpg", make_jpeg(60, 40), "image/jpeg")) for i in range(3)]
    assert client.post(f"/api/projects/{pid}/photos", files=files).status_code == 200
    rec = client.get(f"/api/projects/{pid}/progress").json()
    assert rec["state"] == "done"
    assert rec["task"] == "上传并压缩照片"
    assert "3 张已入库" in rec["message"]
    assert rec["done"] == rec["total"] == 3
    assert rec["run_id"]


def test_a_zip_upload_reports_the_real_photo_count_not_one(client, pid):
    """这条是进度条存在的理由：上传一个 zip 时前端只数得出「1 个文件」，
    服务端展开出 5 张。不修正总量的话进度条会显示 1/1，然后在接下来的
    几十秒里一动不动——正好是用户以为卡死了去刷新的那个场景。"""
    blob = _zip({f"c{i}.jpg": make_jpeg(50, 40) for i in range(5)})
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", ("pack.zip", blob, "application/zip"))])
    assert r.status_code == 200 and r.json()["saved"] == 5, r.text
    rec = client.get(f"/api/projects/{pid}/progress").json()
    assert rec["total"] == 5, f"zip 展开后总量没被修正：{rec}"
    assert rec["done"] == 5


def test_a_rejected_upload_is_recorded_as_failed_not_left_running(client, pid,
                                                                 monkeypatch):
    """413 回滚是一次正常的业务结局，但进度记录必须落终态。

    patch 的是 app.main 的绑定而不是 config 的：main.py 用 from .config import
    把它拿到了自己的命名空间里，改 config 对路由没有影响。
    """
    monkeypatch.setattr("app.main.MAX_PHOTOS_PER_PROJECT", 1)
    blob = _zip({f"c{i}.jpg": make_jpeg(40, 30) for i in range(3)})
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", ("pack.zip", blob, "application/zip"))])
    assert r.status_code == 413, r.text
    rec = client.get(f"/api/projects/{pid}/progress").json()
    assert rec["state"] == "failed"
    assert "上限" in rec["message"]


def test_generate_leaves_a_done_record_with_the_output_stats(client, ready):
    assert client.post(f"/api/projects/{ready}/generate",
                       json={"form": "relative"}).status_code == 200
    rec = client.get(f"/api/projects/{ready}/progress").json()
    assert rec["state"] == "done"
    assert rec["task"] == "生成星图"
    assert "2 颗星" in rec["message"]
    assert rec["done"] == rec["total"] == GENERATE_STAGES


def test_generate_without_a_roster_is_recorded_as_failed(client, pid):
    """没名册 → generate 回 400；进度不能停在 running。"""
    r = client.post(f"/api/projects/{pid}/generate", json={"form": "relative"})
    assert r.status_code == 400
    rec = client.get(f"/api/projects/{pid}/progress").json()
    assert rec["state"] == "failed"
    assert "名册" in rec["message"]


def test_generate_rejects_a_bad_form_and_records_the_failure(client, ready):
    assert client.post(f"/api/projects/{ready}/generate",
                       json={"form": "hologram"}).status_code == 400
    assert client.get(f"/api/projects/{ready}/progress").json()["state"] == "failed"


def test_generate_bundle_works_without_a_reporter(client, ready):
    """预览路由的「产物不在就顺手生成一次」也调 generate_bundle，
    那条路径没人轮询进度，不传 rep 必须照常工作。"""
    out = generate_bundle(ready, "relative")
    assert out["cats"] == 2


class _Recording(progress.Reporter):
    """只记 stage 调用、不落盘的替身，用来数 generate_bundle 走了几个阶段。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen: list[str] = []

    def stage(self, note: str) -> None:
        self.seen.append(note)

    def _flush(self, *, throttle: bool) -> None:
        pass


def test_generate_stages_constant_matches_the_real_number_of_stage_calls(client, ready):
    """GENERATE_STAGES 是进度条的分母，对不上就会「条子走到 83% 就停了」。
    谁往 generate_bundle 里加/删一个阶段而忘了改这个常量，这条会拦住他。"""
    rec = _Recording(ready, "生成星图", total=GENERATE_STAGES)
    generate_bundle(ready, "relative", rep=rec)
    assert len(rec.seen) == GENERATE_STAGES, (
        f"generate_bundle 实际报了 {len(rec.seen)} 个阶段，"
        f"GENERATE_STAGES 却写着 {GENERATE_STAGES}：{rec.seen}")


def test_progress_is_readable_while_the_project_lock_is_held(client, pid, monkeypatch):
    """进度接口故意不挂项目锁。挂了锁它就会堵在自己要汇报的那个操作后面——
    上传正拿着锁，进度请求排在锁外面等超时，前端于是什么也看不到。

    把超时压到 3 秒：万一将来有人给这条路由挂上锁，测试会快速失败并回 409，
    而不是干等 180 秒。
    """
    monkeypatch.setattr(config, "PROJECT_LOCK_TIMEOUT", 3.0)
    taken = threading.Event()
    release = threading.Event()

    def hold():
        lock = locking.project_lock(pid)
        lock.acquire()
        taken.set()
        release.wait(15)
        lock.release()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert taken.wait(5), "测试夹具没能拿到锁"
    try:
        t0 = time.perf_counter()
        r = client.get(f"/api/projects/{pid}/progress")
        waited = time.perf_counter() - t0
        assert r.status_code == 200, f"进度接口被项目锁挡住了：{r.status_code} {r.text}"
        assert waited < 2.0, f"进度接口等了 {waited:.1f} 秒——它在排队等锁"
    finally:
        release.set()
        t.join(5)


def test_progress_route_carries_no_guard(client, pid):
    """与 test_concurrency 里那条读路由断言互为冗余，但这条把理由写在现场。"""
    for route in app.routes:
        if getattr(route, "path", "") == "/api/projects/{pid}/progress":
            deps = {d.call.__name__ for d in route.dependant.dependencies}
            assert "project_guard" not in deps
            break
    else:
        pytest.fail("找不到 /api/projects/{pid}/progress 路由")


def test_progress_is_answered_while_photos_are_being_ingested(client, pid, monkeypatch):
    """光「不挂锁」还不够：照片入库这条路由是 async 的，而解压 / Pillow 压缩 /
    落盘全是同步活。直接调用会把事件循环整个占住，同一时间 /progress 一个请求
    都答不上来——浏览器实测 60 张的 zip 让进度条哑了 6.6 秒，第二段（恰恰是
    进度条唯一存在的理由）从头到尾没显示过。

    这里让 process_and_save 同步 sleep 0.05 秒来把耗时放大到确定可测，
    再用两条并发协线（一条上传、一条轮询）看轮询到底能不能穿过去。
    """
    import asyncio

    import httpx

    from app import image_proc

    real = image_proc.process_and_save

    def slow(*a, **k):
        time.sleep(0.05)        # 同步 sleep 会占住事件循环，正好模拟压缩耗时
        return real(*a, **k)

    monkeypatch.setattr(image_proc, "process_and_save", slow)
    blob = _zip({f"c{i}.jpg": make_jpeg(60, 50) for i in range(12)})

    async def scenario() -> tuple[int, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            upload = asyncio.create_task(ac.post(
                f"/api/projects/{pid}/photos",
                files=[("files", ("pack.zip", blob, "application/zip"))]))
            running = 0
            while not upload.done():
                r = await ac.get(f"/api/projects/{pid}/progress")
                assert r.status_code == 200, r.text
                if r.json()["state"] == "running":
                    running += 1
                await asyncio.sleep(0.02)
            return running, await upload

    running, res = asyncio.run(scenario())
    assert res.status_code == 200, res.text
    assert res.json()["saved"] == 12, res.text
    assert running >= 2, (
        f"入库的 0.6 秒里只有 {running} 次轮询读到 running——事件循环被同步活占住了，"
        f"进度条在这期间是哑的。检查 _ingest_photos 是否漏了 await run_in_threadpool。")
