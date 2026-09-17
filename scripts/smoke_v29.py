#!/usr/bin/env python3
"""v29 引擎双形态浏览器冒烟（T6 F3 出门证据）。

流程：临时工作区内种一个 2 猫项目 → API 生成 v29 inline(lazy) 与 relative
两份产物 → Playwright 以 http:// 与 file:// 两种协议各开一遍：
  - 0 pageerror / 0 请求失败；
  - 开场屏进主界面、图鉴计数 = / 2；
  - inline：初始仅 photo-data-00.js（底图关键片），开影廊后猫片按需到达并解码；
  - relative：全程零分片请求，灯箱照片直接按相对路径解码。

任一断言失败即退出码 1。证据（JSON 摘要 + 截图路径）打印到 stdout，
并写入 docs/验收证据-F3-双形态冒烟.md。
用法：uv run --with playwright --with pillow python scripts/smoke_v29.py [--port 8152]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

CSV_TEXT = (
    "编号,昵称,军衔,工位,毛色,特征描述,代表照片文件,照片数量,"
    "出没区域,关联照片编号,置信度,备注\n"
    "CAT-001,墩墩,喵校长,总揽全校猫务,全橘虎斑,体型胖 粉鼻,demo-001.jpg,1,"
    "宿舍楼前石台,batch1:DEMO,高,示例数据\n"
    "CAT-002,格子,中士,宿舍内务员,橘白,橘头橘背白胸腹,demo-002.jpg,3,"
    "教学楼走廊,batch1:DEMO,中,同场景连拍确认\n")


def make_jpeg(width: int = 900, height: int = 700,
              color=(200, 140, 60)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    px = img.load()
    for y in range(0, height, 3):
        for x in range(0, width, 3):
            px[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 5) % 256)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class _Server(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_products(ws: Path) -> dict[str, Path]:
    """临时工作区种项目并生成 v29 两形态，返回 {形态: 产物 HTML 路径}。"""
    from fastapi.testclient import TestClient
    from app import config, store
    from app.main import app

    config.WORKSPACE = ws
    store.WORKSPACE = ws
    client = TestClient(app)
    pid = client.post("/api/projects", json={"school": "示例校"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/roster",
                    files={"file": ("猫咪名册.csv", CSV_TEXT.encode("utf-8-sig"),
                                    "text/csv")})
    assert r.status_code == 200, r.text
    files = [("files", (n, make_jpeg(), "image/jpeg"))
             for n in ("demo-001.jpg", "demo-002.jpg")]
    r = client.post(f"/api/projects/{pid}/photos", files=files)
    assert r.json()["report"]["summary"]["ok"] is True, r.text

    out: dict[str, Path] = {}
    for form in ("inline", "relative"):
        g = client.post(f"/api/projects/{pid}/generate",
                        json={"form": form, "engine": "v29"})
        assert g.status_code == 200, g.text
        d = g.json()
        assert d["engine"] == "v29"
        zr = client.get(f"/api/projects/{pid}/download?form={form}")
        assert zr.status_code == 200, zr.text
        zf = zipfile.ZipFile(io.BytesIO(zr.content))
        zf.extractall(ws / "unzipped" / form)
        html = ws / "unzipped" / form / d["html_name"]
        assert html.exists(), html
        out[form] = html
    return out


def smoke(browser, targets: dict[str, list[str]], shots: Path,
          mode: str) -> tuple[list[str], dict]:
    """对每份产物按一种协议跑断言。targets: {form: [file_url 或 http_url, tag]}"""
    failures: list[str] = []
    summary: dict = {}
    for form, (target, tag) in targets.items():
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errs: list[str] = []
        failed: list[str] = []
        shards: list[str] = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("requestfailed", lambda r: failed.append(r.url))
        page.on("response", lambda r: shards.append(r.url.split("/")[-1])
                if "photo-data-" in r.url else None)
        try:
            page.goto(target, wait_until="networkidle", timeout=45000)
            page.click("#introBtn")
            page.wait_for_selector("#intro.fade", timeout=8000)
            seen = page.inner_text("#seenCount")
            if not seen.endswith("/ 2"):
                failures.append(f"{tag} 计数错误：{seen!r}")

            init = sorted(set(shards))
            if form == "inline":
                if init != ["photo-data-00.js"]:
                    failures.append(f"{tag} 初始分片应为仅 00，实际 {init}")
            elif init:
                failures.append(f"{tag} relative 不应有分片请求：{init}")

            page.evaluate("document.getElementById('btnGallery').click()")
            page.wait_for_selector("#lightbox.open #lbImg.show", timeout=15000)
            nw = page.evaluate(
                "document.getElementById('lbImg').naturalWidth")
            if nw < 100:
                failures.append(f"{tag} 灯箱照片未解码：naturalWidth={nw}")
            if form == "inline" and len(sorted(set(shards))) < 2:
                failures.append(f"{tag} 开影廊后未按需拉取猫片：{sorted(set(shards))}")

            if errs:
                failures.append(f"{tag} pageerror：{errs[:3]}")
            if failed:
                failures.append(f"{tag} 失败请求：{sorted(set(failed))[:3]}")
            page.screenshot(path=str(shots / f"smoke_{mode}_{form}.png"))
            summary[tag] = {"seen": seen, "initial_shards": init,
                            "lightbox_natural_width": nw,
                            "shards_after_lightbox": sorted(set(shards))}
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{tag} 冒烟异常：{exc}")
            try:
                page.screenshot(path=str(shots / f"fail_{mode}_{form}.png"))
            except Exception:
                pass
        finally:
            page.close()
    return failures, summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8152)
    ap.add_argument("--shots", default=str(ROOT / "docs" / "screenshots-f3"))
    args = ap.parse_args()

    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix="smoke_v29_"))

    products = build_products(ws)
    targets_file = {f: [p.as_uri(), f"file/{f}"]
                    for f, p in products.items()}
    httpd = _Server(("127.0.0.1", args.port),
                    partial(SimpleHTTPRequestHandler, directory=str(ws)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    targets_http = {f: [f"http://127.0.0.1:{args.port}/unzipped/{f}/"
                        f"{products[f].name}", f"http/{f}"]
                    for f in products}

    from playwright.sync_api import sync_playwright
    failures: list[str] = []
    summary: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        f1, s1 = smoke(browser, targets_file, shots, "file")
        f2, s2 = smoke(browser, targets_http, shots, "http")
        failures, summary = f1 + f2, {**s1, **s2}
        browser.close()
    httpd.shutdown()

    evidence = ROOT / "docs" / "验收证据-F3-双形态冒烟.md"
    lines = [
        "# T6 F3 · v29 双形态浏览器冒烟证据",
        "",
        f"- 日期：{__import__('datetime').date.today().isoformat()}　引擎：v29",
        f"- 产物：示例校 2 猫项目 inline(lazy) + relative，协议 http:// 与 file:// 各一遍",
        f"- 结果：**{'全部通过' if not failures else f'{len(failures)} 项失败'}**",
        f"- 截图目录：`{shots}`",
        "",
        "```json",
        json.dumps(summary, ensure_ascii=False, indent=1),
        "```",
    ]
    if failures:
        lines += ["", "## 失败项"] + [f"- {x}" for x in failures]
    evidence.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"ok": not failures, "summary": summary,
                      "failures": failures, "evidence": str(evidence)},
                     ensure_ascii=False, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
