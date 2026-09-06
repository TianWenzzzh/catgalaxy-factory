"""批量并行压缩：PhotoBatcher 与它在 zip 解包 / 上传路由里的接线。

为什么值得单独一个文件：这条路径默认是「测不到」的。批大小 16 张，测试里造
十六张大图既慢又占地，于是最容易出错的分支（进程池）反而没人跑。所以这里的
做法是把 image_proc.PARALLEL_BATCH 调到 4，用四张小图走完整条并行路径——
真起进程池、真跨进程传字节、真按提交顺序落盘。

顺带盯住一件事：小于一批的上传必须继续走串行的 process_and_save。
tests/test_progress.py 靠替换这个函数来模拟慢压缩，子进程里的替换传不过去，
一旦小上传也并行了，那个测试就会「因为错误的原因而通过」。
"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import image_proc, store
from app.image_proc import PhotoBatcher, ZipBudget, extract_photo_zip
from app.main import app
from conftest import make_jpeg


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def pid(client, tmp_workspace):
    r = client.post("/api/projects", json={"school": "并行校"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture
def small_batch(monkeypatch):
    """把批大小与「值得起池」的门槛一起压到 4，四张小图就能走完整的进程池路径。"""
    monkeypatch.setattr(image_proc, "PARALLEL_BATCH", 4)
    monkeypatch.setattr(image_proc, "PARALLEL_MIN_ITEMS", 4)
    return 4


class _PoolSpy:
    """替身进程池工厂：数建了几个池、关了几个。

    真起池的测试只能证明「并行了」，证明不了「没有一批一个池」和「没有把工人
    进程留在外头」——后两条才是这次改动最容易悄悄退化的地方。
    """

    def __init__(self, real):
        self.real = real
        self.made = 0
        self.closed = 0

    def __call__(self, *args, **kwargs):
        self.made += 1
        ex = self.real(*args, **kwargs)
        original = ex.shutdown

        def shutdown(*a, **k):
            self.closed += 1
            return original(*a, **k)

        ex.shutdown = shutdown
        return ex


def _photos(n: int, size: int = 60) -> list[tuple[str, bytes]]:
    return [(f"c{i:02d}.jpg", make_jpeg(size, size - 10, color=(i * 9 % 255, 70, 120)))
            for i in range(n)]


# ---------- PhotoBatcher 本体 ----------

def test_a_full_batch_really_goes_through_the_process_pool(tmp_path, small_batch):
    """先把「并行到底有没有发生」钉死：后面的等价性断言只有在真并行了才有意义。"""
    b = PhotoBatcher(tmp_path / "out")
    for name, data in _photos(4):
        b.add(name, data)
    saved, failed = b.take()
    assert b.parallel_batches == 1
    assert failed == []
    assert [p.name for p, _ in saved] == [f"c{i:02d}.jpg" for i in range(4)]


def test_parallel_output_is_byte_identical_to_serial(tmp_path, small_batch):
    """并行只是换了个进程算，产物必须一模一样——同一张图两条路径压出两个字节数，
    就说明压缩参数在跨进程时丢了（比如子进程读到的常量不是父进程 monkeypatch 后的）。"""
    items = _photos(4, size=300)

    par = PhotoBatcher(tmp_path / "par")
    for name, data in items:
        par.add(name, data)
    par_saved, _ = par.take()

    ser = PhotoBatcher(tmp_path / "ser")
    ser.batch = 99                      # 永远凑不满一批 → 串行
    for name, data in items:
        ser.add(name, data)
    ser_saved, _ = ser.take()

    assert [p.name for p, _ in par_saved] == [p.name for p, _ in ser_saved]
    for (pp, pinfo), (sp, sinfo) in zip(par_saved, ser_saved):
        assert pp.read_bytes() == sp.read_bytes(), pp.name
        # path 是绝对路径，两边目录本来就不同；其余字段必须一模一样
        assert {k: v for k, v in pinfo.items() if k != "path"} == \
               {k: v for k, v in sinfo.items() if k != "path"}


def test_results_come_back_in_submission_order(tmp_path, small_batch):
    """并行完成是乱序的，但报给用户的名单必须和它上传的顺序对得上。"""
    items = _photos(8)
    b = PhotoBatcher(tmp_path / "out")
    for name, data in items:
        b.add(name, data)
    saved, _ = b.take()
    assert [p.name for p, _ in saved] == [name for name, _ in items]
    assert [i["filename"] for _, i in saved] == [name for name, _ in items]


def test_a_bad_photo_in_a_parallel_batch_is_reported_not_fatal(tmp_path, small_batch):
    """一张坏图不能让整批上传失败——它只是「跳过」，其余三张照收。"""
    items = _photos(3) + [("坏图.jpg", "这不是图片".encode("utf-8"))]
    b = PhotoBatcher(tmp_path / "out")
    for name, data in items:
        b.add(name, data)
    saved, failed = b.take()
    assert failed == ["坏图.jpg"]
    assert [p.name for p, _ in saved] == [name for name, _ in items[:3]]
    assert not (tmp_path / "out" / "坏图.jpg").exists()


def test_progress_fires_once_per_successful_photo(tmp_path, small_batch):
    seen: list[str] = []
    items = _photos(3) + [("坏图.jpg", b"not an image at all")]
    b = PhotoBatcher(tmp_path / "out", on_item=seen.append)
    for name, data in items:
        b.add(name, data)
    b.take()
    assert sorted(seen) == sorted(name for name, _ in items[:3])


def test_a_broken_progress_callback_cannot_break_the_batch(tmp_path, small_batch):
    def boom(*_args):
        raise OSError("磁盘满了")

    b = PhotoBatcher(tmp_path / "out", on_item=boom)
    for name, data in _photos(4):
        b.add(name, data)
    saved, _ = b.take()
    assert len(saved) == 4


def test_under_the_threshold_stays_serial_and_honours_a_patched_process_and_save(
        tmp_path, monkeypatch):
    """没到「值得起池」的张数就走串行，并且必须走模块级的 process_and_save。

    这是 test_progress.py 那个「压缩期间进度条还活着」测试的前提：它靠替换
    process_and_save 来把耗时放大到可测。子进程看不见父进程的替换，所以小批量
    一旦也并行了，那个测试就会静静地失去意义。
    """
    calls: list[str] = []
    real = image_proc.process_and_save

    def spy(data, dest, name):
        calls.append(name)
        return real(data, dest, name)

    monkeypatch.setattr(image_proc, "process_and_save", spy)
    b = PhotoBatcher(tmp_path / "out")            # 默认门槛 4 张
    for name, data in _photos(3):
        b.add(name, data)
    saved, _ = b.take()
    assert len(saved) == 3
    assert calls == [name for name, _ in _photos(3)]
    assert b.parallel_batches == 0


def test_one_pool_serves_every_batch_of_an_upload(tmp_path, monkeypatch):
    """一批一个池的话，启动开销会吃掉大半收益：实测 75 张分四批、每批新起一个池
    要 10.5s，四批共用一个池只要 6.2s。顺带钉住「尾巴也并行」——最后不满一批的
    那几张往往有十来张，为它复用同一个池是白赚的。"""
    spy = _PoolSpy(image_proc.ProcessPoolExecutor)
    monkeypatch.setattr(image_proc, "ProcessPoolExecutor", spy)
    b = PhotoBatcher(tmp_path / "out", batch=8)
    with b:
        for name, data in _photos(12):
            b.add(name, data)
        saved, failed = b.take()
    assert failed == []
    assert [p.name for p, _ in saved] == [name for name, _ in _photos(12)]
    assert b.parallel_batches == 2, "满批 8 张 + 尾巴 4 张，两批都该走并行"
    assert spy.made == 1, f"建了 {spy.made} 个池，应该整趟上传共用一个"
    assert spy.closed == 1, "with 出去了还没关池，工人进程就留在外头了"


def test_a_pool_that_dies_once_is_not_retried(tmp_path, small_batch, monkeypatch):
    """环境不给起子进程时，每一批都再试一次就是每一批都白付一次 spawn。"""
    attempts: list[int] = []

    def no_pool(*_a, **_k):
        attempts.append(1)
        raise OSError("这个环境不给起子进程")

    monkeypatch.setattr(image_proc, "ProcessPoolExecutor", no_pool)
    b = PhotoBatcher(tmp_path / "out")
    for name, data in _photos(12):
        b.add(name, data)
    saved, failed = b.take()
    assert failed == []
    assert len(saved) == 12
    assert len(attempts) == 1, f"池已经罢工了还试了 {len(attempts)} 次"
    assert b.parallel_batches == 0


def test_a_dead_process_pool_falls_back_to_serial(tmp_path, small_batch, monkeypatch):
    """并行是优化，不是新的失败面。

    进程池会因为一堆环境原因起不来：主模块没有 __main__ 保护（Windows spawn 会
    无限递归）、子进程被杀、临时目录不可写。这些都不该让用户整批照片白传。
    """
    def no_pool(*_a, **_k):
        raise OSError("这个环境不给起子进程")

    monkeypatch.setattr(image_proc, "ProcessPoolExecutor", no_pool)
    b = PhotoBatcher(tmp_path / "out")
    for name, data in _photos(4):
        b.add(name, data)
    saved, failed = b.take()
    assert failed == []
    assert [p.name for p, _ in saved] == [f"c{i:02d}.jpg" for i in range(4)]
    assert b.parallel_batches == 0
    for p, _ in saved:
        assert p.stat().st_size > 0


def test_take_is_incremental_and_does_not_double_count(tmp_path, small_batch):
    """分次收货：上传路由要在中途回滚时拿到已经落盘的那些，不能重复计数。"""
    b = PhotoBatcher(tmp_path / "out")
    for name, data in _photos(4):
        b.add(name, data)
    first, _ = b.take()
    assert len(first) == 4
    second, _ = b.take()
    assert second == []


def test_drop_pending_discards_uncompressed_photos_and_reports_how_many(tmp_path):
    b = PhotoBatcher(tmp_path / "out", batch=99)
    for name, data in _photos(3):
        b.add(name, data)
    assert b.drop_pending() == 3
    assert b.take() == ([], [])
    assert list(tmp_path.glob("out/*")) == []


def test_batcher_creates_the_destination_directory(tmp_path):
    b = PhotoBatcher(tmp_path / "a" / "b" / "c", batch=1)
    b.add("x.jpg", make_jpeg(40, 30))
    saved, _ = b.take()
    assert saved[0][0].parent == tmp_path / "a" / "b" / "c"


# ---------- zip 解包侧的接线 ----------

def test_zip_with_a_batcher_matches_the_no_batcher_result(tmp_path, small_batch):
    entries = {f"c{i}.jpg": make_jpeg(200, 150, color=(i * 21 % 255, 90, 40))
               for i in range(5)}
    plain = extract_photo_zip(_zip(entries), tmp_path / "plain")

    b = PhotoBatcher(tmp_path / "batched")
    got = extract_photo_zip(_zip(entries), tmp_path / "batched", batcher=b)

    assert [p.name for p, _ in got] == [p.name for p, _ in plain]
    assert b.parallel_batches == 1
    for (bp, _), (pp, _) in zip(got, plain):
        assert bp.read_bytes() == pp.read_bytes(), bp.name


def test_nested_zips_share_one_batcher_and_one_pool(tmp_path, small_batch):
    inner = _zip({f"in{i}.jpg": make_jpeg(80, 60) for i in range(2)})
    outer = _zip({"a.jpg": make_jpeg(80, 60), "b.jpg": make_jpeg(80, 60),
                  "nested.zip": inner})
    b = PhotoBatcher(tmp_path / "out")
    got = extract_photo_zip(outer, tmp_path / "out",
                            budget=ZipBudget(max_depth=3), batcher=b)
    assert len(got) == 4
    # 三层 zip 也只该起一批池，而不是一层一个
    assert b.parallel_batches == 1
    assert b.take() == ([], [])          # 顶层已经排空，不会再收到重复结果


def test_a_batched_zip_still_reports_undecodable_files_as_corrupt(tmp_path, small_batch):
    entries = {f"c{i}.jpg": make_jpeg(60, 50) for i in range(3)}
    entries["坏图.jpg"] = "这不是图片".encode("utf-8")
    budget = ZipBudget()
    got = extract_photo_zip(_zip(entries), tmp_path / "out", budget=budget,
                            batcher=PhotoBatcher(tmp_path / "out"))
    assert len(got) == 3
    assert budget.corrupt == ["坏图.jpg"]
    assert budget.files == 3             # 坏图不占名额


def test_pending_photos_are_dropped_once_the_budget_stops(tmp_path):
    """超限额停手之后，还攥在 batcher 里的原图不该再被压——它们注定要回滚删掉。

    名额也要退回去，否则 files_seen 会把没入库的照片算成占了额度。
    """
    entries = {f"c{i}.jpg": make_jpeg(60, 50) for i in range(6)}
    budget = ZipBudget(max_files=2)
    b = PhotoBatcher(tmp_path / "out")     # 默认批 16，六张全攒着不压
    got = extract_photo_zip(_zip(entries), tmp_path / "out", budget=budget, batcher=b)
    assert budget.stopped
    assert got == []
    assert budget.files == 0
    out = tmp_path / "out"
    assert not out.exists() or list(out.glob("*")) == [], "停手之后还压了照片，白花时间"


# ---------- 上传路由侧的接线 ----------

def test_upload_route_uses_the_pool_and_records_it_in_the_log(
        client, pid, tmp_workspace, small_batch):
    photos = _photos(4, size=120)
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", (name, data, "image/jpeg")) for name, data in photos])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["saved"] == 4
    assert j["src_bytes"] > 0 and j["out_bytes"] > 0
    assert sorted(j["photos"]) == sorted(name for name, _ in photos)

    logged = store.load_meta(pid).log
    assert any("并行批=1" in e["detail"] for e in logged), logged


def test_upload_route_batches_loose_files_across_the_request(
        client, pid, tmp_workspace, small_batch):
    """76 张照片是散着传的，不攒批就等于没有并行——这是实测里最慢的那一段。"""
    photos = _photos(9, size=100)
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", (name, data, "image/jpeg")) for name, data in photos])
    assert r.json()["saved"] == 9
    logged = store.load_meta(pid).log
    # 9 张、批大小 4 → 两批满的走并行，剩 1 张收尾走串行
    assert any("并行批=2" in e["detail"] for e in logged), logged


def test_upload_route_keeps_the_skipped_wording_for_a_bad_loose_file(
        client, pid, tmp_workspace, small_batch):
    files = [("files", (name, data, "image/jpeg")) for name, data in _photos(3)]
    files.append(("files", ("坏图.jpg", "这不是图片".encode("utf-8"), "image/jpeg")))
    j = client.post(f"/api/projects/{pid}/photos", files=files).json()
    assert j["saved"] == 3
    assert any("坏图.jpg" in s and "不是有效的图片文件" in s for s in j["skipped"]), j["skipped"]
    assert j["limits"]["files_seen"] == 3


def test_upload_route_still_rolls_back_everything_on_a_413(
        client, pid, tmp_workspace, small_batch, monkeypatch):
    """回滚要连 batcher 已经落盘的那几批一起删，不能只删它自己数过的那部分。"""
    monkeypatch.setattr(image_proc, "PARALLEL_BATCH", 2)
    monkeypatch.setattr(image_proc, "PARALLEL_MIN_ITEMS", 2)
    photos = _photos(6, size=100)
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", (name, data, "image/jpeg")) for name, data in photos])
    assert r.status_code == 200
    assert len(store.photo_files(pid)) == 6

    monkeypatch.setattr("app.main.MAX_PHOTOS_PER_UPLOAD", 3)
    more = _photos(6, size=100)
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", (f"n{name}", data, "image/jpeg"))
                           for name, data in more])
    assert r.status_code == 413, r.text
    assert len(store.photo_files(pid)) == 6, "回滚没删干净，项目里留下了半截照片"


def test_the_worker_pool_is_closed_even_when_the_upload_rolls_back(
        client, pid, tmp_workspace, monkeypatch):
    """413 是从 with 里抛出去的：这条路径上没人关池的话，每回滚一次就漏一批工人进程。

    学生机上传一个超限的 zip、退回去改、再传，来回几次就能把内存吃光——而且
    界面上什么都看不出来。
    """
    spy = _PoolSpy(image_proc.ProcessPoolExecutor)
    monkeypatch.setattr(image_proc, "ProcessPoolExecutor", spy)
    monkeypatch.setattr(image_proc, "PARALLEL_BATCH", 2)
    monkeypatch.setattr(image_proc, "PARALLEL_MIN_ITEMS", 2)
    monkeypatch.setattr("app.main.MAX_PHOTOS_PER_UPLOAD", 3)

    files = [("files", (name, data, "image/jpeg"))
             for name, data in _photos(6, size=100)]
    r = client.post(f"/api/projects/{pid}/photos", files=files)
    assert r.status_code == 413, r.text
    assert spy.made >= 1, "这一趟根本没走到并行，测不到关池"
    assert spy.closed == spy.made, f"建了 {spy.made} 个池只关了 {spy.closed} 个"
