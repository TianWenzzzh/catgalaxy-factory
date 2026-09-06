"""F8 · 底图标定：人工坐标覆盖算法推导坐标，并持久化到项目 calib.json。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import injector, store
from app.main import app
from app.models import CalibData, CalibPoint, CatRow

from conftest import make_csv, make_jpeg

client = TestClient(app)


def _row(cid: str, name: str = "墩墩", area: str = "宿舍楼前石台",
         photo: str = "a.jpg", coat: str = "全橘虎斑") -> CatRow:
    return CatRow(line=2, id=cid, name=name, rank="喵校长", title="总揽全校猫务",
                  coat=coat, features="体型胖 粉鼻", photo_file=photo, photo_count=2,
                  photo_count_raw="2", area=area, related="batch1:X", confidence="高",
                  note="")


ROWS = [_row("CAT-001"), _row("CAT-002", "格子", "教学楼走廊", "b.jpg", "橘白")]


# ---------- 注入层 ----------

def test_calib_override_replaces_derived_position():
    _entries, derived = injector.build_cat_entries(ROWS)
    assert set(derived) == {"CAT-001", "CAT-002"}

    override = {"CAT-001": {"x": 0.111, "y": 0.222}}
    _e2, calib = injector.build_cat_entries(ROWS, calib_override=override)
    assert calib["CAT-001"] == {"x": 0.111, "y": 0.222}
    # 未标定的那只仍走算法推导，坐标不变
    assert calib["CAT-002"] == derived["CAT-002"]


def test_normalize_calib_accepts_multiple_shapes():
    out = injector.normalize_calib({
        "A": {"x": 0.1, "y": 0.2},
        "B": (0.3, 0.4),
        "C": [0.5, 0.6],
        "D": CalibPoint(x=0.7, y=0.8),
        "E": {"x": "not-a-number", "y": 0},
        "F": None,
    })
    assert out["A"] == {"x": 0.1, "y": 0.2}
    assert out["B"] == {"x": 0.3, "y": 0.4}
    assert out["C"] == {"x": 0.5, "y": 0.6}
    assert out["D"] == {"x": 0.7, "y": 0.8}
    assert "E" not in out and "F" not in out


def test_calib_stats_counts_manual_and_reports_unknown_ids():
    s = injector.calib_stats(ROWS, {"CAT-001": {"x": 0.1, "y": 0.1},
                                    "CAT-999": {"x": 0.2, "y": 0.2}})
    assert s == {"manual": 1, "derived": 1, "total": 2, "missing": ["CAT-999"]}


def test_render_injects_manual_calib_and_map_size():
    html = injector.render_starmap(school="测试校", rows=ROWS,
                                   calib={"CAT-001": {"x": 0.123, "y": 0.456}},
                                   map_size=(800, 600))
    assert '"CAT-001":{"x":0.123,"y":0.456}' in html
    assert "let MW = 800, MH = 600;" in html
    assert "星位人工标定 1/2" in html


def test_render_without_calib_says_derived_and_uses_default_map_size():
    html = injector.render_starmap(school="测试校", rows=ROWS)
    assert "星位为算法推导（未人工标定）" in html
    assert "let MW = 1920, MH = 1239;" in html


def test_render_drops_calib_for_ids_not_in_roster():
    """名册改过后残留的旧编号不应被注入产物。"""
    html = injector.render_starmap(school="测试校", rows=ROWS,
                                   calib={"CAT-777": {"x": 0.5, "y": 0.5}})
    assert "CAT-777" not in html
    assert "星位为算法推导（未人工标定）" in html


def test_template_has_no_unreplaced_tokens():
    html = injector.render_starmap(school="测试校", rows=ROWS, map_size=(1024, 768))
    for token in ("__MAP_W__", "__MAP_H__", "__CALIB_MODE__", "__LS_KEY__",
                  "__TITLE__", "__MAP_SRC__", "__SCHOOL__"):
        assert token not in html, f"token 未替换：{token}"


def test_template_exposes_programming_hook_and_calib_chips():
    html = injector.render_starmap(school="测试校", rows=ROWS)
    assert "window.__catgalaxy" in html
    for needed in ("getCalib", "setCalib", "clearCalib", "setCalibMode"):
        assert needed in html
    assert 'id="btnCalib"' in html and 'id="btnCalibClear"' in html


def _ls_key(html: str) -> str:
    import re
    return re.search(r'const LS_KEY = "([^"]*)";', html).group(1)


def test_ls_key_carries_calib_fingerprint():
    """坐标一变，本机存储键就变，旧的手拖标定不会遮蔽新烘焙的 CALIB。"""
    a = _ls_key(injector.render_starmap(school="测试校", rows=ROWS))
    b = _ls_key(injector.render_starmap(
        school="测试校", rows=ROWS, calib={"CAT-001": {"x": 0.5, "y": 0.5}}))
    assert a != b
    assert a.startswith("catgalaxy-测试校-2-")


def test_ls_key_is_reproducible():
    """指纹取自内容而非时钟，同样输入两次渲染结果一致（产物可复现）。"""
    kw = dict(school="测试校", rows=ROWS, calib={"CAT-002": {"x": 0.25, "y": 0.75}})
    assert _ls_key(injector.render_starmap(**kw)) == _ls_key(injector.render_starmap(**kw))


def test_ls_key_sanitizes_hostile_school_name():
    html = injector.render_starmap(school='恶"意<校>\\名', rows=ROWS)
    key = _ls_key(html)
    for ch in ('"', "<", ">", "\\"):
        assert ch not in key
    # 产物里不能因为校名带引号而多出未闭合的字符串字面量
    assert html.count('const LS_KEY = "') == 1
    assert 'const LS_KEY = "catgalaxy-' in html


# ---------- 存储层 ----------

def test_calib_roundtrip_and_clear(tmp_workspace):
    pid = "t-calib"
    store.ensure_dirs(pid)
    assert store.load_calib(pid) is None

    store.save_calib(pid, CalibData(positions={"CAT-001": CalibPoint(x=0.25, y=0.75)}))
    got = store.load_calib(pid)
    assert got is not None and got.source == "manual"
    assert got.positions["CAT-001"].x == 0.25
    assert got.updated_at

    assert store.clear_calib(pid) is True
    assert store.load_calib(pid) is None
    assert store.clear_calib(pid) is False


def test_load_calib_tolerates_corrupt_file(tmp_workspace):
    pid = "t-broken"
    store.ensure_dirs(pid)
    (tmp_workspace / pid / "calib.json").write_text("{ 这不是 JSON", encoding="utf-8")
    assert store.load_calib(pid) is None


# ---------- API 层 ----------

@pytest.fixture
def project(tmp_workspace):
    """带 2 行名册 + 2 张照片的项目。"""
    pid = client.post("/api/projects", json={"school": "标定校"}).json()["id"]
    photos = tmp_workspace / pid / "assets" / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    for n in ("a.jpg", "b.jpg"):
        (photos / n).write_bytes(make_jpeg(60, 40))
    csv = make_csv([
        ["CAT-001", "墩墩", "喵校长", "总揽全校猫务", "全橘虎斑", "体型胖 粉鼻",
         "a.jpg", "2", "宿舍楼前石台", "batch1:X", "高", ""],
        ["CAT-002", "格子", "中士", "宿舍内务员", "橘白", "橘头橘背白胸腹",
         "b.jpg", "3", "教学楼走廊", "batch1:Y", "中", ""],
    ])
    r = client.post(f"/api/projects/{pid}/roster",
                    files={"file": ("roster.csv", csv.encode("utf-8"), "text/csv")})
    assert r.status_code == 200, r.text
    return pid


def test_get_calib_before_any_manual_work(project):
    r = client.get(f"/api/projects/{project}/calib")
    assert r.status_code == 200
    j = r.json()
    assert j["source"] == "derived"
    assert j["positions"] == {}
    assert j["stats"] == {"manual": 0, "derived": 2, "total": 2, "missing": []}
    assert sorted(j["ids"]) == ["CAT-001", "CAT-002"]


def test_get_calib_404_for_unknown_project():
    assert client.get("/api/projects/nope-不存在/calib").status_code == 404


def test_put_calib_persists_and_merges(project):
    r = client.put(f"/api/projects/{project}/calib",
                   json={"positions": {"CAT-001": {"x": 0.31, "y": 0.62}}})
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 1 and r.json()["total"] == 1

    r = client.put(f"/api/projects/{project}/calib",
                   json={"positions": {"CAT-002": {"x": 0.4, "y": 0.5}}})
    assert r.json()["total"] == 2, "默认 merge=True 应累加而非覆盖"

    j = client.get(f"/api/projects/{project}/calib").json()
    assert j["source"] == "manual"
    assert j["positions"]["CAT-001"] == {"x": 0.31, "y": 0.62}
    assert j["positions"]["CAT-002"] == {"x": 0.4, "y": 0.5}
    assert j["stats"]["manual"] == 2 and j["stats"]["derived"] == 0


def test_put_calib_replace_mode(project):
    client.put(f"/api/projects/{project}/calib",
               json={"positions": {"CAT-001": {"x": 0.31, "y": 0.62}}})
    r = client.put(f"/api/projects/{project}/calib",
                   json={"positions": {"CAT-002": {"x": 0.4, "y": 0.5}}, "merge": False})
    assert r.json()["total"] == 1
    j = client.get(f"/api/projects/{project}/calib").json()
    assert "CAT-001" not in j["positions"]


def test_put_calib_rejects_out_of_range(project):
    r = client.put(f"/api/projects/{project}/calib",
                   json={"positions": {"CAT-001": {"x": 1.4, "y": 0.2}}})
    assert r.status_code == 400
    assert "归一化" in r.json()["detail"]
    # 越界请求不应留下半成品
    assert client.get(f"/api/projects/{project}/calib").json()["positions"] == {}


def test_put_calib_accepts_boundary_values(project):
    r = client.put(f"/api/projects/{project}/calib",
                   json={"positions": {"CAT-001": {"x": 0.0, "y": 1.0}}})
    assert r.status_code == 200
    assert r.json()["saved"] == 1


def test_put_calib_ignores_unknown_ids_but_reports_them(project):
    r = client.put(f"/api/projects/{project}/calib",
                   json={"positions": {"CAT-001": {"x": 0.2, "y": 0.3},
                                       "CAT-404": {"x": 0.5, "y": 0.5}}})
    assert r.status_code == 200
    j = r.json()
    assert j["saved"] == 1 and j["ignored_unknown_ids"] == ["CAT-404"]
    got = client.get(f"/api/projects/{project}/calib").json()
    assert "CAT-404" not in got["positions"]


def test_delete_calib_resets_to_derived(project):
    client.put(f"/api/projects/{project}/calib",
               json={"positions": {"CAT-001": {"x": 0.31, "y": 0.62}}})
    assert client.delete(f"/api/projects/{project}/calib").json()["cleared"] is True
    j = client.get(f"/api/projects/{project}/calib").json()
    assert j["source"] == "derived" and j["positions"] == {}
    assert client.delete(f"/api/projects/{project}/calib").json()["cleared"] is False


def test_generate_bakes_manual_calib_into_html(project, tmp_workspace):
    client.put(f"/api/projects/{project}/calib",
               json={"positions": {"CAT-001": {"x": 0.147, "y": 0.258}},
                     "note": "实地蹲点标定"})
    r = client.post(f"/api/projects/{project}/generate", json={"form": "relative"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["calib"] == {"manual": 1, "derived": 1, "total": 2, "missing": []}
    assert j["map_size"], "自动生成底图后应报告真实尺寸"

    htmls = list((tmp_workspace / project / "out" / "relative").glob("*.html"))
    assert len(htmls) == 1
    html = htmls[0].read_text(encoding="utf-8")
    assert '"CAT-001":{"x":0.147,"y":0.258}' in html
    assert "星位人工标定 1/2" in html


def test_generate_injects_real_map_size(project, tmp_workspace):
    """上传一张非默认尺寸的底图，模板必须按它的真实长宽比绘制。"""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (1000, 400), (20, 30, 50)).save(buf, "JPEG")
    r = client.post(f"/api/projects/{project}/map",
                    files={"file": ("map.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200 and r.json()["size"] == [1000, 400]

    j = client.post(f"/api/projects/{project}/generate", json={"form": "relative"}).json()
    assert j["map_size"] == [1000, 400]
    html = next((tmp_workspace / project / "out" / "relative").glob("*.html")) \
        .read_text(encoding="utf-8")
    assert "let MW = 1000, MH = 400;" in html


def test_calib_written_to_operation_log(project):
    client.put(f"/api/projects/{project}/calib",
               json={"positions": {"CAT-001": {"x": 0.1, "y": 0.2}}})
    meta = store.load_meta(project)
    actions = [e["action"] for e in meta.log]
    assert "保存星位标定" in actions
    entry = [e for e in meta.log if e["action"] == "保存星位标定"][-1]
    assert "本次=1" in entry["detail"]


def test_calib_survives_regeneration_across_forms(project, tmp_workspace):
    """标定落盘后，重新生成（甚至换形态）都仍生效。"""
    client.put(f"/api/projects/{project}/calib",
               json={"positions": {"CAT-002": {"x": 0.9, "y": 0.1}}})
    for form in ("relative", "inline"):
        r = client.post(f"/api/projects/{project}/generate", json={"form": form})
        assert r.status_code == 200, r.text
        assert r.json()["calib"]["manual"] == 1
        html = next((tmp_workspace / project / "out" / form).glob("*.html")) \
            .read_text(encoding="utf-8")
        assert '"CAT-002":{"x":0.9,"y":0.1}' in html


def test_calib_json_is_valid_utf8_file(project, tmp_workspace):
    client.put(f"/api/projects/{project}/calib",
               json={"positions": {"CAT-001": {"x": 0.5, "y": 0.5}}, "note": "宿舍楼东侧"})
    p = tmp_workspace / project / "calib.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["source"] == "manual"
    assert data["note"] == "宿舍楼东侧"
    assert data["positions"]["CAT-001"] == {"x": 0.5, "y": 0.5}
