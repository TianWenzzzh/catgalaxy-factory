"""F9 · 视觉预排序：用感知哈希把「最像的一对」排到候选组最前面。

同色同区的候选组，人工判定量是组内两两比对。第二轮报告 §七 第 2 条要的就是把这个
「先看谁」从人的眼里挪到代码里。用 dHash（9×8 灰度差分 → 64 位）：不引新依赖
（Pillow 已在），且对缩放与重压缩不敏感——而这正是工作区里照片的实际形态
（长边 ≤1200px 的压缩副本）。

哈希只用来**排序**，不用来下结论：判定仍然是人的事（数据红线）。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app import merge, phash
from app.main import app
from conftest import make_csv, make_jpeg


@pytest.fixture
def client():
    return TestClient(app)


def make_scene(kind: str, width: int = 1200, height: int = 900,
               quality: int = 95) -> bytes:
    """造一张「像照片」的图：渐变底 + 几个确定性的椭圆块。

    不用随机噪点——噪点在缩放时会互相抵消，量不出感知哈希真正的本事；真实照片是
    低频结构（身体、背景、光影）。渐变也不逐像素画：先画 20×15 再放大，同样是低频，
    却省掉二十几万次循环。
    """
    gw, gh = max(2, width // 60), max(2, height // 60)
    small = Image.new("RGB", (gw, gh))
    sp = small.load()
    for y in range(gh):
        for x in range(gw):
            sp[x, y] = (x * 255 // (gw - 1), y * 255 // (gh - 1), 128)
    img = small.resize((width, height), Image.BILINEAR)
    shapes = {
        "橘猫": [(0.30, 0.45, 0.34, 0.30, (220, 150, 60)),
                 (0.34, 0.24, 0.14, 0.14, (235, 170, 80)),
                 (0.62, 0.62, 0.20, 0.12, (180, 120, 50))],
        "三花": [(0.55, 0.30, 0.22, 0.40, (60, 60, 70)),
                 (0.20, 0.60, 0.30, 0.22, (240, 240, 240)),
                 (0.70, 0.70, 0.16, 0.16, (120, 80, 60))],
    }[kind]
    d = ImageDraw.Draw(img)
    for cx, cy, rw, rh, color in shapes:
        x0, y0 = int((cx - rw / 2) * width), int((cy - rh / 2) * height)
        x1, y1 = int((cx + rw / 2) * width), int((cy + rh / 2) * height)
        d.ellipse([x0, y0, x1, y1], fill=color)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def recode(blob: bytes, scale: float, quality: int) -> bytes:
    """缩放 + 重压一遍：模拟入库照片（原图 → 长边 1200px、≤200KB 的副本）。"""
    im = Image.open(io.BytesIO(blob))
    im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def flat_jpeg(color=(200, 200, 200), width=400, height=300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def overexposed(blob: bytes) -> bytes:
    """整片过曝：结构还在，但灰度全压到 240 以上，高通之后几乎没有残差。"""
    im = Image.open(io.BytesIO(blob)).convert("L").point(lambda v: min(255, 240 + v // 16))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=95)
    return buf.getvalue()


# ---------- 感知哈希本身 ----------

def test_hash_is_a_64_bit_int():
    h = phash.dhash_bytes(make_scene("橘猫"))
    assert isinstance(h, int) and 0 <= h < 2 ** 64


def test_identical_bytes_hash_identically():
    blob = make_scene("橘猫")
    assert phash.dhash_bytes(blob) == phash.dhash_bytes(blob)
    assert phash.visual_similarity(phash.dhash_bytes(blob),
                                   phash.dhash_bytes(blob)) == 1.0


def test_a_resized_recompressed_copy_stays_similar():
    """同一张图缩到一半、质量压到 70，还得认得出是同一张——这是入库照片的常态。"""
    orig = make_scene("橘猫")
    copy = recode(orig, 0.5, 70)
    sim = phash.visual_similarity(phash.dhash_bytes(orig), phash.dhash_bytes(copy))
    assert sim >= 0.8, f"重压副本相似度只有 {sim:.3f}，排序就不可信了"


def test_different_scenes_are_clearly_less_similar_than_the_same_scene():
    orig = make_scene("橘猫")
    same = recode(orig, 0.5, 70)
    other = make_scene("三花")
    s_same = phash.visual_similarity(phash.dhash_bytes(orig), phash.dhash_bytes(same))
    s_other = phash.visual_similarity(phash.dhash_bytes(orig), phash.dhash_bytes(other))
    assert s_other < 0.75, f"两张不同的图相似度 {s_other:.3f}，分不开就没有预排序价值"
    assert s_same > s_other


def test_unreadable_bytes_hash_to_none():
    assert phash.dhash_bytes("这可不是图片".encode()) is None
    assert phash.dhash_bytes(b"") is None


NO_STRUCTURE = {
    "纯色灰": lambda: flat_jpeg((200, 200, 200)),
    "纯白": lambda: flat_jpeg((255, 255, 255)),
    "纯黑": lambda: flat_jpeg((0, 0, 0)),
    "整片过曝": lambda: overexposed(make_scene("橘猫")),
    "高频噪点": lambda: make_jpeg(640, 480),
}


@pytest.mark.parametrize("name", sorted(NO_STRUCTURE))
def test_an_image_with_no_structure_left_has_no_fingerprint(name):
    """没有结构就不给指纹，别给个假的。

    退化实测（9×8 高通残差离 128 最远多少）：纯色 0、整片过曝 0、合成噪点 0~1，
    而有结构的场景 6~13。若不挡住，纯色图会算出全 0 指纹——于是一张白图和一张
    黑图会报「视觉相似度 100%」，还被排到候选组最前面，那是自信地答错。

    图片字节在测试体里现造：parametrize 会把参数值编进 test id，几万字节的 JPEG
    在 Windows 上会撑爆环境变量。
    """
    assert phash.dhash_bytes(NO_STRUCTURE[name]()) is None, f"{name} 本该算不出指纹"


def test_a_structured_photo_always_yields_a_fingerprint():
    """反过来也得成立：真有结构的照片不能被判成「算不出」，否则功能是哑的。"""
    for kind in ("橘猫", "三花"):
        assert phash.dhash_bytes(make_scene(kind)) is not None
        assert phash.dhash_bytes(recode(make_scene(kind), 0.25, 50)) is not None


def test_a_missing_file_hashes_to_none(tmp_path):
    assert phash.dhash_file(tmp_path / "根本没有这张.jpg") is None


def test_a_file_that_is_not_an_image_hashes_to_none(tmp_path):
    p = tmp_path / "假照片.jpg"
    p.write_bytes(b"\x00\x01\x02\x03")
    assert phash.dhash_file(p) is None


def test_hamming_counts_the_differing_bits():
    assert phash.hamming(0, 0) == 0
    assert phash.hamming(0b1010, 0b0001) == 3
    assert phash.hamming(0, (1 << 64) - 1) == 64


def test_visual_similarity_maps_hamming_onto_zero_to_one():
    assert phash.visual_similarity(0, 0) == 1.0
    assert phash.visual_similarity(0, (1 << 64) - 1) == 0.0
    assert phash.visual_similarity(0, 0b1) == pytest.approx(1 - 1 / 64)


# ---------- 候选组内的视觉预排序 ----------

def _group(*photos: str) -> dict:
    """造一个三人候选组，成员编号 CAT-00i，代表照片按参数给。"""
    return {"gid": "M-deadbeef", "kind": "coat-area", "score": 0.9,
            "reason": "毛色相同",
            "members": [{"id": f"CAT-{i + 1:03d}", "line": i + 2,
                         "name": f"猫{i + 1}", "photo": p}
                        for i, p in enumerate(photos)]}


def test_members_are_ordered_by_how_close_they_look():
    """两只几乎一样的排前面，长得不一样的沉到最后。"""
    g = merge.annotate_visual(_group("a.jpg", "far.jpg", "near.jpg"), {
        "a.jpg": 0,
        "near.jpg": 0b1,                 # 与 a 只差 1 位 → 0.98
        "far.jpg": (1 << 64) - 1,        # 与谁都不像
    }.get)
    assert [m["id"] for m in g["members"]] == ["CAT-001", "CAT-003", "CAT-002"]


def test_each_member_names_its_closest_peer():
    g = merge.annotate_visual(_group("a.jpg", "far.jpg", "near.jpg"), {
        "a.jpg": 0, "near.jpg": 0b1, "far.jpg": (1 << 64) - 1,
    }.get)
    by_id = {m["id"]: m for m in g["members"]}
    assert by_id["CAT-001"]["visual_peer"] == "CAT-003"
    assert by_id["CAT-003"]["visual_peer"] == "CAT-001"
    assert by_id["CAT-001"]["visual"] == pytest.approx(1 - 1 / 64, abs=1e-3)


def test_group_reports_the_best_pair_as_its_visual_top():
    g = merge.annotate_visual(_group("a.jpg", "near.jpg", "far.jpg"), {
        "a.jpg": 0, "near.jpg": 0b11, "far.jpg": (1 << 64) - 1,
    }.get)
    assert g["visual_top"] == pytest.approx(1 - 2 / 64, abs=1e-3)
    assert g["visual_hashed"] == 3


def test_a_group_without_any_photo_keeps_its_order_and_zero_visual():
    g = merge.annotate_visual(_group("", "", ""), {}.get)
    assert [m["id"] for m in g["members"]] == ["CAT-001", "CAT-002", "CAT-003"]
    assert all(m["visual"] == 0.0 and m["visual_peer"] == "" for m in g["members"])
    assert g["visual_top"] == 0.0 and g["visual_hashed"] == 0


def test_a_member_whose_photo_file_is_unreadable_sinks_but_stays_listed():
    """坏图/缺图不该让整组消失，只是排不上序——人还是得自己看一眼。"""
    g = merge.annotate_visual(_group("a.jpg", "坏了.jpg", "b.jpg"),
                              {"a.jpg": 0, "b.jpg": 0b1}.get)
    assert [m["id"] for m in g["members"]] == ["CAT-001", "CAT-003", "CAT-002"]
    assert g["visual_hashed"] == 2
    assert g["members"][-1]["visual"] == 0.0
    assert g["members"][-1]["visual_peer"] == ""


def test_annotate_leaves_score_gid_reason_and_member_fields_alone():
    """视觉分只加不改：可疑度、组 id、理由都是既有契约，F7 摘要还依赖它们。"""
    src = _group("a.jpg", "near.jpg")
    before = {k: v for k, v in src.items() if k != "members"}
    g = merge.annotate_visual(src, {"a.jpg": 0, "near.jpg": 0b1}.get)
    assert {k: v for k, v in g.items() if k not in ("members", "visual_top",
                                                     "visual_hashed")} == before
    assert g["members"][0]["line"] == 2 and g["members"][0]["name"] == "猫1"


def test_a_two_member_group_with_the_same_photo_is_a_perfect_match():
    g = merge.annotate_visual(_group("dup.jpg", "dup.jpg"), {"dup.jpg": 42}.get)
    assert g["visual_top"] == 1.0
    assert all(m["visual"] == 1.0 for m in g["members"])


def test_a_single_member_group_has_no_peer():
    g = merge.annotate_visual(_group("only.jpg"), {"only.jpg": 7}.get)
    assert g["members"][0]["visual"] == 0.0
    assert g["members"][0]["visual_peer"] == ""
    assert g["visual_top"] == 0.0


# ---------- 接口 ----------

def _row(cid, name, coat, area, features, photo):
    return [cid, name, "喵员", "巡校内务", coat, features, photo, "1", area,
            "batch1:TEST", "高", ""]


@pytest.fixture
def trio_project(client, tmp_workspace):
    """三只同色同区的猫：甲乙用的是同一只猫的两个角度，丙明显是另一只。"""
    pid = client.post("/api/projects", json={"school": "视觉校"}).json()["id"]
    text = make_csv([
        _row("CAT-001", "甲", "全橘虎斑", "宿舍楼前石台", "体型胖 粉鼻 侧躺露肚", "jia.jpg"),
        _row("CAT-003", "丙", "全橘虎斑", "宿舍楼前石台", "体型胖 粉鼻 侧躺露肚", "bing.jpg"),
        _row("CAT-002", "乙", "全橘虎斑", "宿舍楼前石台", "体型胖 粉鼻 侧躺露肚", "yi.jpg"),
    ])
    client.post(f"/api/projects/{pid}/roster",
                files={"file": ("r.csv", text.encode("utf-8-sig"), "text/csv")})
    scene = make_scene("橘猫")
    client.post(f"/api/projects/{pid}/photos", files=[
        ("files", ("jia.jpg", scene, "image/jpeg")),
        ("files", ("yi.jpg", recode(scene, 0.6, 72), "image/jpeg")),
        ("files", ("bing.jpg", make_scene("三花"), "image/jpeg")),
    ])
    client.post(f"/api/projects/{pid}/validate")
    return pid


def test_the_merge_endpoint_ranks_the_lookalikes_first(client, trio_project):
    j = client.get(f"/api/projects/{trio_project}/merge").json()
    g = j["groups"][0]
    ids = [m["id"] for m in g["members"]]
    assert ids[:2] == ["CAT-001", "CAT-002"], f"最像的两只没排前面，实际顺序 {ids}"
    assert ids[-1] == "CAT-003"


def test_the_merge_endpoint_reports_visual_similarity_per_member(client, trio_project):
    g = client.get(f"/api/projects/{trio_project}/merge").json()["groups"][0]
    by_id = {m["id"]: m for m in g["members"]}
    assert by_id["CAT-001"]["visual_peer"] == "CAT-002"
    assert by_id["CAT-001"]["visual"] >= 0.8
    assert by_id["CAT-003"]["visual"] < by_id["CAT-001"]["visual"]
    assert g["visual_hashed"] == 3
    assert 0.0 <= g["visual_top"] <= 1.0


def test_the_merge_endpoint_still_works_when_no_photo_was_uploaded(client, tmp_workspace):
    pid = client.post("/api/projects", json={"school": "无图校"}).json()["id"]
    text = make_csv([
        _row("CAT-001", "甲", "全橘虎斑", "石台", "体型胖 粉鼻 侧躺露肚", ""),
        _row("CAT-002", "乙", "全橘虎斑", "石台", "体型胖 粉鼻 侧躺露肚", ""),
    ])
    client.post(f"/api/projects/{pid}/roster",
                files={"file": ("r.csv", text.encode("utf-8-sig"), "text/csv")})
    client.post(f"/api/projects/{pid}/validate")
    g = client.get(f"/api/projects/{pid}/merge").json()["groups"][0]
    assert g["visual_top"] == 0.0 and g["visual_hashed"] == 0
    assert [m["id"] for m in g["members"]] == ["CAT-001", "CAT-002"]


def test_a_corrupt_photo_does_not_break_the_merge_endpoint(client, tmp_workspace):
    """坏图只该让那只猫排不上序，不能让整张 F9 卡片 500。"""
    pid = client.post("/api/projects", json={"school": "坏图校"}).json()["id"]
    text = make_csv([
        _row("CAT-001", "甲", "全橘虎斑", "石台", "体型胖 粉鼻 侧躺露肚", "a.jpg"),
        _row("CAT-002", "乙", "全橘虎斑", "石台", "体型胖 粉鼻 侧躺露肚", "b.jpg"),
    ])
    client.post(f"/api/projects/{pid}/roster",
                files={"file": ("r.csv", text.encode("utf-8-sig"), "text/csv")})
    d = tmp_workspace / pid / "assets" / "photos"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.jpg").write_bytes(make_scene("橘猫"))
    (d / "b.jpg").write_bytes("扩展名是图，内容不是".encode())
    client.post(f"/api/projects/{pid}/validate")
    r = client.get(f"/api/projects/{pid}/merge")
    assert r.status_code == 200
    g = r.json()["groups"][0]
    assert g["visual_hashed"] == 1 and g["visual_top"] == 0.0
