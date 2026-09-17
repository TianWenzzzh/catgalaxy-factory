#!/usr/bin/env python3
"""T6 F5 证据：full76 v29 产物的浏览器截图四件套（开场/影廊/护照/分享卡）。

对 workspace 里最新的中北大学 inline 产物跑 Playwright，截图存
docs/screenshots-f5/。纯本地证据脚本，不参与 CI。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots-f5"


def main() -> int:
    from playwright.sync_api import sync_playwright

    products = sorted(ROOT.glob("workspace/*-中北大学/out/inline/*.html"),
                      key=lambda p: p.stat().st_mtime)
    if not products:
        print("找不到中北大学 inline 产物，先跑 acceptance --case full76 --engine v29")
        return 1
    target = products[-1].resolve()
    SHOTS.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(target.as_uri(), wait_until="networkidle", timeout=60000)

        def shot(name: str) -> None:
            page.screenshot(path=str(SHOTS / f"{name}.png"))
            print(f"✓ {name}.png")

        shot("f5-01-开场屏")                      # 极光开场 + 底部署名行
        page.click("#introBtn")
        page.wait_for_selector("#intro.fade", timeout=8000)
        page.wait_for_timeout(1500)               # 星点入场动画
        shot("f5-02-星图主界面")

        page.evaluate("document.getElementById('btnGallery').click()")
        page.wait_for_selector("#lightbox.open #lbImg.show", timeout=15000)
        page.wait_for_function(
            "document.getElementById('lbImg').naturalWidth>0", timeout=10000)
        shot("f5-03-影廊灯箱")
        page.evaluate("document.getElementById('lbClose').click()")
        page.wait_for_timeout(300)

        page.evaluate("document.getElementById('btnMore').click()")
        page.evaluate("document.getElementById('btnPassport').click()")
        page.wait_for_selector("#passP", timeout=8000)
        page.wait_for_timeout(600)
        shot("f5-04-喵星护照")

        # 分享卡：开一张档案卡再点生成
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        page.evaluate("document.getElementById('btnFind').click()")
        page.wait_for_selector("#fdBody .fRow", timeout=8000)
        page.evaluate(
            "document.querySelector('#fdBody .fRow').click()")
        page.wait_for_selector("#card.open", timeout=8000)
        page.wait_for_function(
            "document.getElementById('cardImg').naturalWidth>0", timeout=10000)
        page.evaluate("document.getElementById('cardShare').click()")
        time.sleep(1.5)                           # 等画布渲染
        shot("f5-05-分享卡")
        browser.close()

    for f in sorted(SHOTS.glob("f5-*.png")):
        if f.stat().st_size < 20_000:
            failures.append(f"{f.name} 疑似空白（{f.stat().st_size}B）")
    print("完成" if not failures else "；".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
