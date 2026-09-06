"""zip 上传：递归解包 + 体积/数量上限。

覆盖三类真实风险：
1. zip 套 zip（学校交来的照片常是「打包再打包」）——要能递归展开，也要有层数上限；
2. zip 炸弹 / 巨包——解压字节、条目数、张数、单文件体积四道闸；
3. 半截入库——超限必须回滚，项目里不能留一半照片让人以为传全了。
"""
from __future__ import annotations

import io
import struct
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import image_proc
from app import main as main_mod
from app import store
from app.image_proc import ZipBudget, extract_photo_zip
from app.main import app
from conftest import make_jpeg


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _zip_with_lying_size(good: dict[str, bytes], bad_name: str, bad_data: bytes) -> bytes:
    """造一个「中心目录谎报大小」的 zip：真实数据大，声明 file_size 只有 10 字节。

    zipfile 按声明值截断输出，随后 CRC 校验失败抛 BadZipFile——解包路径必须
    接住它，只丢这一条，而不是整个请求 500。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in good.items():
            zf.writestr(name, data)
        zf.writestr(bad_name, bad_data)     # 最后写入 → 最后一条中心目录
    data = bytearray(buf.getvalue())
    cd = data.rfind(b"PK\x01\x02")
    assert cd > 0, "没找到中心目录，测试夹具本身坏了"
    struct.pack_into("<I", data, cd + 24, 10)   # uncompressed size → 10
    return bytes(data)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def pid(client, tmp_workspace):
    r = client.post("/api/projects", json={"school": "限额校"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def photos_in(pid: str) -> list[str]:
    d = store.project_dir(pid) / "assets" / "photos"
    return sorted(p.name for p in d.glob("*.jpg")) if d.exists() else []


def upload(client, pid, name: str, data: bytes):
    return client.post(f"/api/projects/{pid}/photos",
                       files={"files": (name, data, "application/octet-stream")})


# ---------- ZipBudget 单元 ----------

def test_budget_defaults_resolve_from_module_constants(monkeypatch):
    monkeypatch.setattr(image_proc, "MAX_PHOTOS_PER_UPLOAD", 7)
    monkeypatch.setattr(image_proc, "MAX_ZIP_INFLATED_BYTES", 700)
    monkeypatch.setattr(image_proc, "MAX_ZIP_ENTRIES", 70)
    monkeypatch.setattr(image_proc, "MAX_ZIP_DEPTH", 7)
    b = ZipBudget()
    assert (b.max_files, b.max_inflated, b.max_entries, b.max_depth) == (7, 700, 70, 7)
    assert b.stop_reason == "" and b.stopped is False


def test_budget_explicit_limits_win():
    b = ZipBudget(max_files=3, max_inflated=99, max_entries=9, max_depth=1)
    assert (b.max_files, b.max_inflated, b.max_entries, b.max_depth) == (3, 99, 9, 1)


def test_charge_file_stops_at_cap_without_consuming_a_slot():
    b = ZipBudget(max_files=2)
    assert b.charge_file() is True and b.files == 1
    assert b.charge_file() is True and b.files == 2
    assert b.charge_file() is False
    assert b.files == 2, "被拒的那次不该占名额"
    assert b.stopped and "上限 2 张" in b.stop_reason


def test_release_file_gives_the_slot_back_and_never_goes_negative():
    b = ZipBudget(max_files=2)
    b.charge_file()
    b.release_file()
    assert b.files == 0
    b.release_file()
    assert b.files == 0


def test_stop_keeps_the_first_reason():
    b = ZipBudget()
    b.stop("先来的原因")
    b.stop("后来的原因")
    assert b.stop_reason == "先来的原因"


# ---------- 递归解包 ----------

def test_nested_zip_is_unpacked(tmp_path):
    inner = _zip({"里层/猫.jpg": make_jpeg(300, 200)})
    outer = _zip({"外层.zip": inner, "表层.jpg": make_jpeg(300, 200)})
    b = ZipBudget()
    results = extract_photo_zip(outer, tmp_path / "o", budget=b)
    assert len(results) == 2 and b.nested_zips == 1
    assert sorted(p.name for p, _ in results) == ["猫.jpg", "表层.jpg"]
    assert not b.stopped


def test_two_levels_of_nesting_are_unpacked(tmp_path):
    level2 = _zip({"最里.jpg": make_jpeg(200, 200)})
    level1 = _zip({"二层.zip": level2})
    level0 = _zip({"一层.zip": level1})
    b = ZipBudget(max_depth=3)
    results = extract_photo_zip(level0, tmp_path / "o", budget=b)
    assert [p.name for p, _ in results] == ["最里.jpg"]
    assert b.nested_zips == 2


def test_depth_cap_leaves_the_innermost_zip_unopened(tmp_path):
    level2 = _zip({"最里.jpg": make_jpeg(200, 200)})
    level1 = _zip({"二层.zip": level2})
    level0 = _zip({"一层.zip": level1})
    b = ZipBudget(max_depth=1)
    results = extract_photo_zip(level0, tmp_path / "o", budget=b)
    assert results == []
    assert b.too_deep and b.too_deep[0].endswith("二层.zip")
    assert not b.stopped, "超层数是跳过，不是失败"


def test_budget_is_optional_for_backwards_compat(tmp_path):
    payload = _zip({"a.jpg": make_jpeg(200, 200)})
    assert len(extract_photo_zip(payload, tmp_path / "o")) == 1


# ---------- 上限 ----------

def test_photo_count_cap_stops_midway(tmp_path):
    payload = _zip({f"c{i}.jpg": make_jpeg(200, 200) for i in range(3)})
    b = ZipBudget(max_files=2)
    results = extract_photo_zip(payload, tmp_path / "o", budget=b)
    assert len(results) == 2
    assert b.stopped and "张数达到上限 2" in b.stop_reason


def test_inflated_bytes_cap_catches_a_zip_bomb(tmp_path):
    # 高度可压缩的纯色大块 → 压缩后很小、解压后很大
    payload = _zip({"bomb.jpg": b"\x00" * 400_000})
    b = ZipBudget(max_inflated=50_000)
    results = extract_photo_zip(payload, tmp_path / "o", budget=b)
    assert results == []
    assert b.stopped and "zip 炸弹" in b.stop_reason
    # 计数以块为单位，超出量最多一个 ZIP_READ_CHUNK；这里块比条目还大，
    # 所以整条 400KB 会先落地再判超限。
    assert b.inflated <= 50_000 + image_proc.ZIP_READ_CHUNK


def test_inflated_cap_stops_mid_entry_when_the_chunk_is_small(tmp_path, monkeypatch):
    """块调小 → 证明是边解边计数，不是「先解完再看」。"""
    monkeypatch.setattr(image_proc, "ZIP_READ_CHUNK", 4096)
    payload = _zip({"bomb.jpg": b"\x00" * 400_000})
    b = ZipBudget(max_inflated=50_000)
    assert extract_photo_zip(payload, tmp_path / "o", budget=b) == []
    assert b.stopped
    assert b.inflated <= 50_000 + 4096, f"实际解出 {b.inflated} 字节，说明没有中途收手"
    assert b.inflated < 400_000


def test_inflated_cap_accumulates_across_entries(tmp_path):
    payload = _zip({f"c{i}.jpg": make_jpeg(400, 300) for i in range(4)})
    tight = ZipBudget(max_inflated=1)
    extract_photo_zip(payload, tmp_path / "a", budget=tight)
    assert tight.stopped and tight.entries >= 1


def test_entry_count_cap(tmp_path):
    payload = _zip({f"c{i}.txt": b"x" for i in range(5)})
    b = ZipBudget(max_entries=2)
    extract_photo_zip(payload, tmp_path / "o", budget=b)
    assert b.stopped and "条目数超过上限 2" in b.stop_reason


def test_lying_header_is_dropped_but_the_rest_survives(tmp_path):
    payload = _zip_with_lying_size({"好猫.jpg": make_jpeg(300, 200)},
                                   "谎报.jpg", make_jpeg(600, 400))
    b = ZipBudget()
    results = extract_photo_zip(payload, tmp_path / "o", budget=b)
    assert [p.name for p, _ in results] == ["好猫.jpg"]
    assert not b.stopped, "单条坏不该拖垮整包"
    assert any("谎报" in c for c in b.corrupt)


def test_corrupt_image_inside_zip_is_skipped_not_fatal(tmp_path):
    payload = _zip({"坏图.jpg": b"not an image at all", "好猫.jpg": make_jpeg(300, 200)})
    b = ZipBudget()
    results = extract_photo_zip(payload, tmp_path / "o", budget=b)
    assert [p.name for p, _ in results] == ["好猫.jpg"]
    assert b.corrupt == ["坏图.jpg"] and b.files == 1
    assert not b.stopped


def test_broken_zip_is_reported_not_raised(tmp_path):
    b = ZipBudget()
    assert extract_photo_zip(b"PK\x03\x04truncated", tmp_path / "o", budget=b) == []
    assert b.bad_zips == ["zip"] and b.stopped and "打不开" in b.stop_reason


def test_directories_and_macosx_and_dotfiles_are_ignored(tmp_path):
    payload = _zip({
        "folder/": b"",
        "__MACOSX/._cat.jpg": b"junk",
        ".hidden.jpg": b"junk",
        "cat.jpg": make_jpeg(200, 200),
        "notes.txt": b"plain text, not an image",
    })
    b = ZipBudget()
    results = extract_photo_zip(payload, tmp_path / "o", budget=b)
    assert [p.name for p, _ in results] == ["cat.jpg"]
    assert b.corrupt == []


# ---------- API ----------

def test_upload_reports_limits_and_no_warnings(client, pid):
    r = upload(client, pid, "猫.zip", _zip({"a.jpg": make_jpeg(300, 200)}))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["saved"] == 1 and j["warnings"] == []
    lim = j["limits"]
    assert lim["files_seen"] == 1 and lim["zip_entries"] == 1
    assert lim["project_total"] == 1
    assert lim["project_room_left"] == lim["max_files_per_project"] - 1
    assert lim["max_upload_bytes"] == main_mod.MAX_UPLOAD_BYTES
    assert lim["max_zip_depth"] == image_proc.ZipBudget().max_depth


def test_nested_zip_through_the_api(client, pid):
    inner = _zip({"里层猫.jpg": make_jpeg(300, 200)})
    r = upload(client, pid, "外层.zip", _zip({"内层.zip": inner}))
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 1
    assert photos_in(pid) == ["里层猫.jpg"]


def test_oversize_single_file_is_413(client, pid, monkeypatch):
    monkeypatch.setattr(main_mod, "MAX_UPLOAD_BYTES", 1024)
    r = upload(client, pid, "大.zip", b"PK\x03\x04" + b"x" * 4096)
    assert r.status_code == 413
    assert "超过单文件上限" in r.json()["detail"]
    assert photos_in(pid) == []


def test_photo_count_cap_is_413_and_rolls_back(client, pid, monkeypatch):
    monkeypatch.setattr(main_mod, "MAX_PHOTOS_PER_UPLOAD", 2)
    payload = _zip({f"c{i}.jpg": make_jpeg(300, 200) for i in range(4)})
    r = upload(client, pid, "很多猫.zip", payload)
    assert r.status_code == 413
    assert "张数达到上限 2" in r.json()["detail"]
    assert photos_in(pid) == [], "超限必须回滚，不能留半截"


def test_zip_bomb_is_413_and_rolls_back(client, pid, monkeypatch):
    monkeypatch.setattr(image_proc, "MAX_ZIP_INFLATED_BYTES", 20_000)
    payload = _zip({"bomb.jpg": b"\x00" * 400_000, "c1.jpg": make_jpeg(300, 200)})
    r = upload(client, pid, "炸弹.zip", payload)
    assert r.status_code == 413
    assert "zip 炸弹" in r.json()["detail"]
    assert photos_in(pid) == []


def test_project_cumulative_cap_blocks_the_second_upload(client, pid, monkeypatch):
    monkeypatch.setattr(main_mod, "MAX_PHOTOS_PER_PROJECT", 1)
    first = upload(client, pid, "a.zip", _zip({"a.jpg": make_jpeg(300, 200)}))
    assert first.status_code == 200 and first.json()["limits"]["project_room_left"] == 0
    second = upload(client, pid, "b.zip", _zip({"b.jpg": make_jpeg(300, 200)}))
    assert second.status_code == 413
    assert "累计上限 1 张" in second.json()["detail"]
    assert photos_in(pid) == ["a.jpg"], "被拒的第二次不该动已有照片"


def test_broken_zip_is_413_not_500(client, pid):
    r = upload(client, pid, "坏.zip", b"PK\x03\x04truncated-no-central-dir")
    assert r.status_code == 413
    assert "打不开" in r.json()["detail"]


def test_corrupt_image_upload_is_skipped_not_500(client, pid):
    r = client.post(f"/api/projects/{pid}/photos", files=[
        ("files", ("坏图.jpg", b"definitely not an image", "image/jpeg")),
        ("files", ("好猫.jpg", make_jpeg(300, 200), "image/jpeg")),
    ])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["saved"] == 1
    assert any("坏图.jpg" in s and "不是有效的图片文件" in s for s in j["skipped"]), j["skipped"]
    assert j["limits"]["files_seen"] == 1, "坏图不占张数额度"


def test_depth_overflow_surfaces_as_a_warning(client, pid, monkeypatch):
    monkeypatch.setattr(main_mod, "MAX_PHOTOS_PER_UPLOAD", 100)
    level2 = _zip({"最里.jpg": make_jpeg(200, 200)})
    level1 = _zip({"二层.zip": level2})
    payload = _zip({"一层.zip": level1, "表层.jpg": make_jpeg(200, 200)})
    monkeypatch.setattr(image_proc, "MAX_ZIP_DEPTH", 1)
    r = upload(client, pid, "套娃.zip", payload)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["saved"] == 1
    assert any("递归上限" in w and "二层.zip" in w for w in j["warnings"]), j["warnings"]


def test_rejected_upload_leaves_no_operation_log_entry(client, pid, monkeypatch):
    monkeypatch.setattr(main_mod, "MAX_PHOTOS_PER_UPLOAD", 1)
    payload = _zip({f"c{i}.jpg": make_jpeg(300, 200) for i in range(3)})
    assert upload(client, pid, "很多猫.zip", payload).status_code == 413
    meta = store.load_meta(pid)
    assert not [e for e in meta.log if e["action"] == "上传照片"]
    assert meta.photo_count == 0


def test_successful_upload_logs_the_inflated_total(client, pid):
    upload(client, pid, "a.zip", _zip({"a.jpg": make_jpeg(300, 200)}))
    meta = store.load_meta(pid)
    entry = [e for e in meta.log if e["action"] == "上传照片"][-1]["detail"]
    assert "张数=1" in entry and "解压=" in entry and "嵌套zip=0" in entry
