"""F1-F5 全链路端到端单测（含验收场景 1：空模板 + 2 行示例数据）。"""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import make_jpeg


@pytest.fixture
def client(tmp_workspace):
    with TestClient(app) as c:
        yield c


def _create(client, school="示例校"):
    r = client.post("/api/projects", json={"school": school})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload_roster(client, pid, text):
    return client.post(f"/api/projects/{pid}/roster",
                       files={"file": ("猫咪名册.csv", text.encode("utf-8-sig"), "text/csv")})


def _upload_photos(client, pid, names, size=(900, 700)):
    files = [("files", (n, make_jpeg(*size), "image/jpeg")) for n in names]
    return client.post(f"/api/projects/{pid}/photos", files=files)


# ---------- 基础 ----------

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "喵星图工厂" in r.text


def test_template_download(client):
    r = client.get("/api/template")
    assert r.status_code == 200
    assert "编号,昵称,军衔" in r.text
    assert r.text.count("\n") >= 3


def test_project_crud(client):
    pid = _create(client, "测试校")
    assert client.get(f"/api/projects/{pid}").status_code == 200
    assert any(p["id"] == pid for p in client.get("/api/projects").json())
    assert client.delete(f"/api/projects/{pid}").json()["deleted"] == pid
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_unknown_project_404(client):
    assert client.get("/api/projects/nope").status_code == 404
    assert client.post("/api/projects/nope/validate").status_code == 404


# ---------- F1 导入 ----------

def test_roster_upload(client, two_row_csv):
    pid = _create(client)
    r = _upload_roster(client, pid, two_row_csv)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rows"] == 2
    assert d["missing_columns"] == []
    assert d["report"]["summary"]["total_rows"] == 2


def test_roster_upload_rejects_empty(client):
    pid = _create(client)
    r = client.post(f"/api/projects/{pid}/roster", files={"file": ("e.csv", b"", "text/csv")})
    assert r.status_code == 400


def test_photo_upload_compresses(client, two_row_csv):
    pid = _create(client)
    _upload_roster(client, pid, two_row_csv)
    r = _upload_photos(client, pid, ["demo-001.jpg", "demo-002.jpg"], size=(2600, 1900))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["saved"] == 2
    assert set(d["photos"]) == {"demo-001.jpg", "demo-002.jpg"}
    assert d["out_bytes"] < d["src_bytes"]
    assert d["max_side"] <= 1200
    assert d["report"]["summary"]["ok"] is True


def test_photo_upload_skips_non_images(client):
    pid = _create(client)
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", ("note.txt", b"hello", "text/plain"))])
    assert r.status_code == 200
    assert r.json()["saved"] == 0
    assert r.json()["skipped"] == ["note.txt"]


def test_photo_upload_accepts_zip(client, two_row_csv):
    pid = _create(client)
    _upload_roster(client, pid, two_row_csv)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("demo-001.jpg", make_jpeg(800, 600))
        zf.writestr("demo-002.jpg", make_jpeg(800, 600))
        zf.writestr("readme.txt", b"x")
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", ("pack.zip", buf.getvalue(), "application/zip"))])
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 2
    assert set(r.json()["photos"]) == {"demo-001.jpg", "demo-002.jpg"}


def test_map_upload_and_default(client, two_row_csv):
    pid = _create(client)
    _upload_roster(client, pid, two_row_csv)
    d = client.get(f"/api/projects/{pid}").json()
    assert d["has_map"] is False
    _upload_photos(client, pid, ["demo-001.jpg", "demo-002.jpg"])
    client.post(f"/api/projects/{pid}/generate", json={"form": "relative"})
    assert client.get(f"/api/projects/{pid}").json()["has_map"] is True


# ---------- F2 校验 ----------

def test_validate_reports_photo_missing(client, two_row_csv):
    pid = _create(client)
    _upload_roster(client, pid, two_row_csv)
    r = client.post(f"/api/projects/{pid}/validate")
    assert r.status_code == 200
    rep = r.json()
    assert rep["summary"]["ok"] is False
    assert "E_PHOTO_MISSING" in [i["code"] for i in rep["issues"] if i["level"] == "error"]


def test_validate_requires_roster(client):
    pid = _create(client)
    assert client.post(f"/api/projects/{pid}/validate").status_code == 400


def test_report_markdown_endpoint(client, two_row_csv):
    pid = _create(client)
    _upload_roster(client, pid, two_row_csv)
    r = client.get(f"/api/projects/{pid}/report.md")
    assert r.status_code == 200
    assert "校验报告" in r.text


# ---------- F3 + F4 + F5 ----------

def _ready_project(client, school="示例校", csv_text=None, size=(700, 520)):
    pid = _create(client, school)
    _upload_roster(client, pid, csv_text)
    r = _upload_photos(client, pid, ["demo-001.jpg", "demo-002.jpg"], size=size)
    assert r.json()["report"]["summary"]["ok"] is True
    return pid


def test_generate_relative_full_flow(client, two_row_csv):
    """验收场景 1：空模板 + 2 行示例数据走通 F1-F5。"""
    pid = _ready_project(client, "示例校", two_row_csv)

    g = client.post(f"/api/projects/{pid}/generate", json={"form": "relative"})
    assert g.status_code == 200, g.text
    d = g.json()
    assert d["form"] == "relative"
    assert d["cats"] == 2
    assert d["photos_embedded"] == 2
    assert d["missing_photos"] == []
    assert d["html_name"] == "示例校喵星图.html"
    assert d["zip_name"].startswith("示例校-校园猫咪星图-")
    assert d["zip_name"].endswith(".zip")
    assert d["preview_url"].startswith(f"/bundle/{pid}/out/relative/")
    assert d["stats"]["zones"]

    # F4 预览地址
    p = client.get(f"/api/projects/{pid}/preview?form=relative")
    assert p.status_code == 200
    assert p.json()["url"] == d["preview_url"]

    # F5 下载
    dl = client.get(f"/api/projects/{pid}/download?form=relative")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    names = set(zf.namelist())
    assert "示例校喵星图.html" in names
    assert "assets/photos/demo-001.jpg" in names
    assert "assets/photos/demo-002.jpg" in names
    assert "assets/map.jpg" in names
    assert "data/猫咪名册.csv" in names
    assert "校验报告.md" in names
    assert "归并决策摘要.md" in names

    html = zf.read("示例校喵星图.html").decode("utf-8")
    assert "示例校喵星图 · 校园猫咪星系" in html
    assert '"CAT-001"' in html and '"CAT-002"' in html
    assert "墩墩" in html and "格子" in html
    assert "assets/photos/demo-001.jpg" in html
    assert "__CATS__" not in html


def test_generate_inline_full_flow(client, two_row_csv):
    pid = _ready_project(client, "内嵌校", two_row_csv)
    g = client.post(f"/api/projects/{pid}/generate", json={"form": "inline"})
    assert g.status_code == 200, g.text
    d = g.json()
    assert d["form"] == "inline"
    assert any(f.startswith("assets/photo-data-") for f in d["files"])

    dl = client.get(f"/api/projects/{pid}/download?form=inline")
    assert dl.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    names = zf.namelist()
    assert not any(n.startswith("assets/photos/") for n in names)
    chunk_names = [n for n in names if n.startswith("assets/photo-data-")]
    assert chunk_names
    joined = "".join(zf.read(n).decode("utf-8") for n in chunk_names)
    assert "window.__PHOTOS" in joined
    assert "data:image/jpeg;base64," in joined
    assert '"demo-001.jpg"' in joined
    assert '"map.jpg"' in joined            # 底图也内嵌，双击即开

    html = zf.read("内嵌校喵星图.html").decode("utf-8")
    for cn in chunk_names:
        assert f'<script src="{cn}"></script>' in html


def test_inline_bundle_is_self_contained(client, two_row_csv):
    """inline 版 HTML + photo-data 分片即可离线运行，不依赖任何外部图片文件。"""
    pid = _ready_project(client, "自足校", two_row_csv)
    client.post(f"/api/projects/{pid}/generate", json={"form": "inline"})
    art = client.get(f"/api/projects/{pid}/artifacts").json()
    inline_files = art["bundles"]["inline"]
    assert all(not f.startswith("assets/photos/") for f in inline_files)
    assert any(f.endswith(".html") for f in inline_files)


def test_generate_rejects_bad_form(client, two_row_csv):
    pid = _ready_project(client, "示例校", two_row_csv)
    assert client.post(f"/api/projects/{pid}/generate",
                       json={"form": "wat"}).status_code == 400


def test_generate_blocked_when_validation_fails(client, two_row_csv):
    pid = _create(client, "示例校")
    _upload_roster(client, pid, two_row_csv)     # 未上传照片
    r = client.post(f"/api/projects/{pid}/generate", json={"form": "relative"})
    assert r.status_code == 409


def test_generate_blocked_without_roster(client):
    pid = _create(client)
    assert client.post(f"/api/projects/{pid}/generate",
                       json={"form": "relative"}).status_code == 400


def test_exclude_low_confidence(client, csv_factory):
    text = csv_factory([
        ["CAT-001", "甲", "喵校长", "职务", "橘白", "体型胖 粉鼻", "demo-001.jpg", "2",
         "宿舍窗台", "b1:1", "高", ""],
        ["CAT-002", "乙", "中士", "职务", "狸花", "偏瘦 揣手", "demo-002.jpg", "1",
         "教学楼走廊", "b1:2", "低", "疑似身份存疑"],
    ])
    pid = _ready_project(client, "排除校", text)
    keep = client.post(f"/api/projects/{pid}/generate",
                       json={"form": "relative", "exclude_low_confidence": False}).json()
    drop = client.post(f"/api/projects/{pid}/generate",
                       json={"form": "relative", "exclude_low_confidence": True}).json()
    assert keep["cats"] == 2
    assert drop["cats"] == 1


def test_generate_with_school_override(client, two_row_csv):
    pid = _ready_project(client, "旧校名", two_row_csv)
    d = client.post(f"/api/projects/{pid}/generate",
                    json={"form": "relative", "school": "新校名"}).json()
    assert d["school"] == "新校名"
    assert d["html_name"] == "新校名喵星图.html"
    assert d["zip_name"].startswith("新校名-校园猫咪星图-")


def test_preview_before_generate_404(client, two_row_csv):
    pid = _create(client)
    assert client.get(f"/api/projects/{pid}/preview").status_code == 404


def test_artifacts_endpoint(client, two_row_csv):
    pid = _ready_project(client, "示例校", two_row_csv)
    client.post(f"/api/projects/{pid}/generate", json={"form": "relative"})
    a = client.get(f"/api/projects/{pid}/artifacts").json()
    assert len(a["zips"]) == 1
    assert a["zips"][0]["name"].endswith(".zip")
    assert "relative" in a["bundles"]


# ---------- F6 / F7 ----------

CENSUS_TXT = """校园猫咪照片普查 batch1（格式：文件名 | 猫数量 | 主体毛色花纹 | 显著特征 | 场景线索 | 画质 | 正脸 | 人脸）

AAA111.jpg | 1 | 橘白（橘背橘头） | 短毛成年 体型中等 | 室内宿舍桌下 | A | 否 | 无
BBB222.jpg | 1 | 橘白（白嘴白胸） | 短毛偏瘦 仰头 | 室内宿舍桌下 | A | 是 | 无
CCC333.jpg | 1 | 狸花 | 体型偏小 趴卧 | 教学楼瓷砖地面 | B | 否 | 无
"""


def test_census_parse(client):
    r = client.post("/api/census/parse",
                    files={"file": ("普查-batch1.txt", CENSUS_TXT.encode("utf-8"), "text/plain")},
                    data={"as_csv": "false"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 3
    assert d["records"][0]["coat_main"] == "橘白"
    assert d["records"][0]["confidence"] == "高"
    assert any(g["count"] == 2 for g in d["suggestions"])
    assert "编号,昵称,军衔" in d["draft_csv"]


def test_census_parse_as_csv(client):
    r = client.post("/api/census/parse",
                    files={"file": ("b.txt", CENSUS_TXT.encode("utf-8"), "text/plain")},
                    data={"as_csv": "true"})
    assert r.status_code == 200
    assert r.json()["count"] == 3
    assert "records" not in r.json()


def test_summary_endpoint(client, two_row_csv):
    pid = _ready_project(client, "摘要校", two_row_csv)
    client.post(f"/api/projects/{pid}/generate", json={"form": "relative"})
    r = client.post(f"/api/projects/{pid}/summary")
    assert r.status_code == 200
    md = r.json()["markdown"]
    assert "# 摘要校 · 归并决策摘要" in md
    assert "数据总览" in md
    assert "数据红线自检" in md
    assert "操作记录" in md
    assert "生成星图" in md            # 自动采集的操作日志


def test_summary_without_report(client):
    pid = _create(client, "空项目")
    r = client.post(f"/api/projects/{pid}/summary")
    assert r.status_code == 200
    assert "尚未运行校验" in r.json()["markdown"]


# ---------- 验收场景 2：76 只全量回归 ----------

def test_full_76_regression(client):
    """用 E 盘真实名册 76 只走通全流程，校验数据完整、照片全部可显示。"""
    from pathlib import Path
    src = Path(r"E:\猫咪星图_总库\07_普查原始数据\猫咪名册.csv")
    if not src.exists():
        pytest.skip("E 盘参考数据不可用")

    csv_text = src.read_bytes().decode("utf-8-sig")
    pid = _create(client, "中北大学")
    r = _upload_roster(client, pid, csv_text)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == 76

    # 为名册引用的每张照片生成同尺寸规格的替身图（只读 E 盘，不复制原图）
    import csv as _csv
    names = [row["代表照片文件"].strip() for row in
             _csv.DictReader(io.StringIO(csv_text)) if row["代表照片文件"].strip()]
    assert len(names) == 76
    unique_names = sorted(set(names))
    # 真实名册中有 1 张代表照被两只猫共用（去重后 75 个文件），应触发警告而非错误
    assert len(unique_names) == 75
    dup = [n for n in unique_names if names.count(n) > 1]
    assert len(dup) == 1

    files = [("files", (n, make_jpeg(1600, 1100, color=((i * 37) % 255, (i * 61) % 255,
                                                        (i * 97) % 255)), "image/jpeg"))
             for i, n in enumerate(unique_names)]
    up = client.post(f"/api/projects/{pid}/photos", files=files)
    assert up.status_code == 200, up.text
    assert up.json()["saved"] == 75
    assert up.json()["max_side"] <= 1200

    rep = client.post(f"/api/projects/{pid}/validate").json()
    assert rep["summary"]["total_rows"] == 76
    assert rep["summary"]["error_count"] == 0, [i for i in rep["issues"] if i["level"] == "error"][:5]
    assert rep["summary"]["photos_uploaded"] == 75
    assert rep["summary"]["photos_referenced"] == 75
    assert rep["summary"]["photos_unused"] == 0
    assert rep["summary"]["ok"] is True
    shared = [i for i in rep["issues"] if i["code"] == "W_PHOTO_SHARED"]
    assert len(shared) == 1

    g = client.post(f"/api/projects/{pid}/generate", json={"form": "relative"})
    assert g.status_code == 200, g.text
    d = g.json()
    assert d["cats"] == 76
    assert d["photos_embedded"] == 75  # 76 只猫共用 75 个照片文件
    assert d["missing_photos"] == []

    dl = client.get(f"/api/projects/{pid}/download?form=relative")
    assert dl.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    names_in_zip = set(zf.namelist())
    # 名册里 24 条带目录前缀（如 `补充视频照片/xxx.jpg`），bundle 内一律平铺，故按 basename 比对
    bases = sorted({n.replace("\\", "/").split("/")[-1] for n in unique_names})
    assert len(bases) == 75
    for n in bases:
        assert f"assets/photos/{n}" in names_in_zip, f"照片 {n} 未进包"

    html = zf.read("中北大学喵星图.html").decode("utf-8")
    import re as _re
    m = _re.search(r"const CATS = (\[.*?\]);\nconst CALIB", html, _re.S)
    assert m
    cats = json.loads(m.group(1).replace("<\\/", "</"))
    assert len(cats) == 76
    assert len({c["id"] for c in cats}) == 76
    assert all(c["photo"].startswith("assets/photos/") for c in cats)
    assert all(c["photo"].count("/") == 2 for c in cats), "photo 路径残留目录前缀，离线会死链"
    assert all(0.5 <= c["brightness"] <= 1.05 for c in cats)
    assert all(c["starColor"] for c in cats)
    assert all(c["bio"] for c in cats)

    mc = _re.search(r"const CALIB = (\{.*?\});\nconst MAP_SRC", html, _re.S)
    calib = json.loads(mc.group(1))
    assert len(calib) == 76
    assert set(calib) == {c["id"] for c in cats}

    # inline 形态同样跑通
    gi = client.post(f"/api/projects/{pid}/generate", json={"form": "inline"})
    assert gi.status_code == 200
    dli = client.get(f"/api/projects/{pid}/download?form=inline")
    zfi = zipfile.ZipFile(io.BytesIO(dli.content))
    chunks = [n for n in zfi.namelist() if n.startswith("assets/photo-data-")]
    assert chunks
    joined = "".join(zfi.read(n).decode("utf-8") for n in chunks)
    for n in bases:
        assert f'"{n}"' in joined, f"inline 版缺少 {n} 的 base64"
