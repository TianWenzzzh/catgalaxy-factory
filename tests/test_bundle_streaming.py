"""产物落盘流式化：两种形态都改成「写一张读一张」，峰值不再随图廊大小增长。

inline 是重灾区。改动前 generate_bundle 同时握着两份完整图廊——原始字节字典，
加上 base64 分片文本（后者还比前者大 1/3）。75 张真照片实测 tracemalloc 峰值
87.21MB；而照片上限是 3000 张、单张 200KB，顶格就是 3.5GB。现在先只 stat 出
字节数，按「base64 长度恒为 4*ceil(n/3)」算准行长、定好分片边界，再交给生成器
逐片读盘、编码、写出：同一场景 11.4MB，且 10/30/75 张分别是 7.43/10.22/10.92MB
——峰值由分片大小决定，不由图廊大小决定。relative 原本也要把整册读成字典
（14.81MB），改成逐张读盘后是 0.98MB。耗时两边都没变（0.88s / 0.63s），
这一改省的是内存不是时间。

代价是多出一条必须成立的不变量：规划阶段只 stat 不 read，它算出的分片边界
必须和真正编码时的边界一模一样，否则 HTML 里的 <script src> 会指向不存在的
文件，星图打开就是一片黑。本文件主要就是盯这条。
"""
import base64
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app import packager, store
from app.config import PHOTO_CHUNK_BYTES
from app.injector import (_js, _photo_line, _photo_line_len, iter_photo_chunks,
                          plan_photo_chunks)
from app.main import app
from app.packager import build_inline_bundle
from conftest import make_csv, make_jpeg


def _blob(n: int) -> bytes:
    """长度精确为 n 的假照片。base64 行长只取决于长度，与内容无关。"""
    return b"\xa5" * n


@pytest.fixture
def client(tmp_workspace):
    with TestClient(app) as c:
        yield c


# ---------- 行长必须算得准 ----------

@pytest.mark.parametrize("nbytes", [0, 1, 2, 3, 4, 5, 6, 7, 100, 999, 4096, 200_000])
@pytest.mark.parametrize("name", [
    "a.jpg", "map.jpg", 'q"uote.jpg', "a</script>.jpg",
    "中文名.jpg", "space name.jpg", "back\\slash.jpg",
])
def test_line_length_is_exact_without_encoding(name, nbytes):
    """规划阶段拿不到字节，只能靠 4*ceil(n/3) 推 base64 长度——必须一个字符都不差。

    差一个字符，分片边界就会和实际写出的内容错位。
    """
    blob = _blob(nbytes)
    assert len(_photo_line(name, blob)) == _photo_line_len(name, len(blob))


# ---------- 规划与流式必须同边界 ----------

@pytest.mark.parametrize("count", [0, 1, 2, 3, 7, 10, 33])
@pytest.mark.parametrize("chunk_bytes", [1, 60, 45_000, PHOTO_CHUNK_BYTES])
def test_plan_and_stream_agree_on_chunk_names(count, chunk_bytes):
    sizes = [(f"p{i:03d}.jpg", 3000 + i) for i in range(count)]
    blobs = {n: _blob(s) for n, s in sizes}
    assert plan_photo_chunks(sizes, chunk_bytes) == [
        name for name, _ in iter_photo_chunks(sizes, blobs.__getitem__, chunk_bytes)]


def test_plan_does_not_read_any_photo():
    """规划只吃 (名字, 字节数)：给它一个一读就炸的 read，它照样能出分片名。"""
    sizes = [(f"p{i:02d}.jpg", 3000) for i in range(12)]

    def explode(name):
        raise AssertionError(f"规划阶段不该读照片，却读了 {name}")

    names = plan_photo_chunks(sizes, 12500)     # 一行 4046 字符，一片装 3 张
    assert names == ["photo-data-01.js", "photo-data-02.js", "photo-data-03.js",
                     "photo-data-04.js"]
    with pytest.raises(AssertionError):
        list(iter_photo_chunks(sizes, explode, 12500))


def _legacy_chunks(photos: dict[str, bytes], chunk_bytes: int) -> list[tuple[str, str]]:
    """重构前 build_photo_chunks 的实现，原样留在这里当对照物。

    换了实现不换产物——这是唯一的硬证据，所以别去「顺手优化」它。
    """
    chunks: list[list[str]] = [[]]
    sizes = [0]
    for name in sorted(photos):
        b64 = base64.b64encode(photos[name]).decode("ascii")
        line = f'__PHOTOS[{_js(name)}]="data:image/jpeg;base64,{b64}";'
        if sizes[-1] + len(line) > chunk_bytes and chunks[-1]:
            chunks.append([])
            sizes.append(0)
        chunks[-1].append(line)
        sizes[-1] += len(line)

    out: list[tuple[str, str]] = []
    for i, lines in enumerate(chunks, start=1):
        if not lines:
            continue
        content = "window.__PHOTOS=window.__PHOTOS||{};\n" + "\n".join(lines) + "\n"
        out.append((f"photo-data-{i:02d}.js", content))
    return out


@pytest.mark.parametrize("chunk_bytes", [1, 400, 45_000, PHOTO_CHUNK_BYTES])
def test_stream_output_is_identical_to_the_pre_refactor_implementation(chunk_bytes):
    """流式写出的每一个字节都必须和旧的「整册在内存里」路径一致。"""
    photos = {f"p{i:02d}.jpg": _blob(3000 + 7 * i) for i in range(12)}
    photos["map.jpg"] = _blob(9999)
    photos['we"ird.jpg'] = _blob(321)
    legacy = _legacy_chunks(photos, chunk_bytes)
    sizes = [(n, len(b)) for n, b in photos.items()]
    assert list(iter_photo_chunks(sizes, photos.__getitem__, chunk_bytes)) == legacy
    # 传进去的顺序不该有影响：切片规则内部按名字排序
    assert list(iter_photo_chunks(list(reversed(sizes)), photos.__getitem__,
                                  chunk_bytes)) == legacy


def test_photos_are_read_only_when_their_chunk_is_emitted():
    """峰值内存的来源：读到第几张，取决于消费到第几片。"""
    sizes = [(f"p{i:02d}.jpg", 3000) for i in range(12)]
    blobs = {n: _blob(s) for n, s in sizes}
    reads: list[str] = []

    def read(name):
        reads.append(name)
        return blobs[name]

    # 一片正好装 3 张
    chunk_bytes = _photo_line_len("p00.jpg", 3000) * 3 + 1
    planned = plan_photo_chunks(sizes, chunk_bytes)
    assert len(planned) == 4

    it = iter_photo_chunks(sizes, read, chunk_bytes)
    assert reads == []                       # 生成器还没被碰过，一张都没读
    first_name, first_text = next(it)
    assert reads == ["p00.jpg", "p01.jpg", "p02.jpg"]
    assert first_name == planned[0]
    assert first_text.count("__PHOTOS[") == 3

    rest = list(it)
    assert [n for n, _ in rest] == planned[1:]
    assert len(reads) == 12


def test_build_inline_bundle_accepts_a_generator(tmp_path):
    """打包端不能要求 list——那等于又把所有分片攒回内存。"""
    dest = tmp_path / "i"

    def gen():
        yield "photo-data-01.js", "window.__PHOTOS={};"
        yield "photo-data-02.js", "//2"

    written = build_inline_bundle(dest, html="h", html_name="x喵星图.html", chunks=gen())
    assert written == ["x喵星图.html", "assets/photo-data-01.js", "assets/photo-data-02.js"]
    assert (dest / "assets" / "photo-data-02.js").read_text(encoding="utf-8") == "//2"


# ---------- 路由级 ----------

def _ready(client, school, rows, photo_names, size=(900, 700)):
    pid = client.post("/api/projects", json={"school": school}).json()["id"]
    csv_text = make_csv(rows)
    r = client.post(f"/api/projects/{pid}/roster",
                    files={"file": ("猫咪名册.csv", csv_text.encode("utf-8-sig"),
                                    "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == len(rows), r.json()
    assert r.json()["missing_columns"] == [], r.json()
    up = client.post(f"/api/projects/{pid}/photos",
                     files=[("files", (n, make_jpeg(*size), "image/jpeg"))
                            for n in photo_names])
    assert up.status_code == 200, up.text
    assert up.json()["report"]["summary"]["ok"] is True, up.json()
    return pid


def _rows(photo_names):
    return [[f"CAT-{i:03d}", f"猫{i}", "中士", "宿舍内务员", "橘白",
             "橘头白胸腹", n, "1", "教学楼走廊", "batch1:DEMO", "高", ""]
            for i, n in enumerate(photo_names, start=1)]


@pytest.mark.parametrize("form", ["inline", "relative"])
def test_generation_reads_no_photo_before_the_bundle_starts_writing(client, monkeypatch, form):
    """两种形态都改成写一张读一张：开始落盘前不该有任何照片已经在内存里。"""
    names = [f"p{i:02d}.jpg" for i in range(6)]
    pid = _ready(client, f"流式{form}校", _rows(names), names)

    reads: list[str] = []
    real_read = main_mod.retry_read_bytes

    def spy(path):
        p = Path(path)
        if p.parent.name == "photos":
            reads.append(p.name)
        return real_read(p)

    monkeypatch.setattr(main_mod, "retry_read_bytes", spy)

    seen = {}
    real_build = getattr(packager, f"build_{form}_bundle")
    arg = "chunks" if form == "inline" else "photos"

    def build_spy(dest, **kw):
        seen["photo_reads"] = list(reads)
        seen["arg_type"] = type(kw[arg]).__name__
        return real_build(dest, **kw)

    monkeypatch.setattr(packager, f"build_{form}_bundle", build_spy)

    g = client.post(f"/api/projects/{pid}/generate", json={"form": form})
    assert g.status_code == 200, g.text
    assert seen["photo_reads"] == [], "开始落盘前就把照片全读了，等于没做流式"
    assert seen["arg_type"] == "generator"
    assert g.json()["photos_embedded"] == 6
    assert len(reads) == 6, "生成结束后 6 张照片都应该被读过一次"


def test_every_script_tag_in_the_inline_html_has_a_matching_chunk_file(client):
    """分片名对不上的后果是死链：HTML 引了、盘上没有，星图打开是黑的。"""
    names = [f"p{i:02d}.jpg" for i in range(20)]
    pid = _ready(client, "多片校", _rows(names), names, size=(1200, 900))

    g = client.post(f"/api/projects/{pid}/generate", json={"form": "inline"})
    assert g.status_code == 200, g.text

    dest = store.project_dir(pid) / "out" / "inline"
    html = next(dest.glob("*.html")).read_text(encoding="utf-8")
    tags = re.findall(r'<script src="assets/(photo-data-\d{2}\.js)"></script>', html)
    on_disk = sorted(p.name for p in (dest / "assets").glob("photo-data-*.js"))
    assert len(on_disk) >= 2, f"20 张 1200px 照片只分了 {len(on_disk)} 片，用例失去意义"
    pm = re.search(r"const __PM=(\{.*?\});</script>", html)
    if pm:  # v29 lazy：HTML 只引 00 关键片，其余分片由 __PM 清单接管
        import json
        manifest = json.loads(pm.group(1))
        claimed = sorted({f"photo-data-{int(i):02d}.js"
                          for i in manifest["m"].values()})
        assert tags == ["photo-data-00.js"], tags
        assert claimed == on_disk, (claimed, on_disk)
    else:   # v1 eager：HTML 逐片全引
        assert tags == on_disk

    joined = "".join((dest / "assets" / n).read_text(encoding="utf-8") for n in on_disk)
    for n in names:
        assert f'__PHOTOS["{n}"]' in joined, f"{n} 没被内嵌"
    assert '__PHOTOS["map.jpg"]' in joined


def test_a_photo_named_map_jpg_does_not_duplicate_the_map_entry(client):
    """底图自己也叫 map.jpg 并进分片；同名照片必须被底图覆盖，而不是产出两行。

    旧实现是 dict 赋值覆盖，新实现改成按名字取——语义必须一字不差地保留。
    """
    pid = _ready(client, "撞名校", _rows(["map.jpg"]), ["map.jpg"])
    g = client.post(f"/api/projects/{pid}/generate", json={"form": "inline"})
    assert g.status_code == 200, g.text

    dest = store.project_dir(pid) / "out" / "inline"
    joined = "".join(p.read_text(encoding="utf-8")
                     for p in sorted((dest / "assets").glob("photo-data-*.js")))
    assert joined.count('__PHOTOS["map.jpg"]') == 1

    line = next(l for l in joined.splitlines() if l.startswith('__PHOTOS["map.jpg"]'))
    embedded = base64.b64decode(line.split("base64,", 1)[1].rstrip('";'))
    assert embedded == store.map_path(pid).read_bytes(), "内嵌的应是底图，不是那张同名照片"
    assert embedded != (store.project_dir(pid) / "assets" / "photos" / "map.jpg").read_bytes()
