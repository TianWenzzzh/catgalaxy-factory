# -*- coding: utf-8 -*-
"""真浏览器截图取证：headless Edge + CDP，把控制台各卡片拍成 docs/截图/*.png。

第二轮时内置浏览器 viewport 是 0×0，take_screenshot 直接报
NATIVE_BROWSER_VIEWPORT_UNAVAILABLE，整轮没有一张图。这里换一条路：
自己起一个 headless Edge（有真实视口、真实排版、真实 canvas），用 CDP 的
Page.captureScreenshot 出图。页面状态全部由 Runtime.evaluate 触发真实 DOM
事件造出来（select 的 change、按钮的 click、input 的 input），跑的是前端
自己的处理函数，不是往 DOM 里塞假节点。

起浏览器（脚本不负责起，也不负责关）：

    "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" \\
        --headless=new --remote-debugging-port=9222 \\
        --user-data-dir=/tmp/bs-cdp-profile --window-size=1280,1600

用法：.venv\\Scripts\\python.exe scripts\\shot.py [场景名...]
场景名见 SHOTS；不传参数则全拍。拍之前先起服务：
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8013
"""
import asyncio
import base64
import json
import sys
import urllib.request
from pathlib import Path

import websockets

CDP = "http://127.0.0.1:9222"
APP = "http://127.0.0.1:8013/"
OUT = Path(__file__).resolve().parent.parent / "docs" / "截图"
W, H, DPR = 1080, 1350, 1.5      # 与人工复核时的桌面缩放一致，字才够大看得清

# 两个演示项目：视觉验收校（4 只、可控的视觉排序样本）、中北大学（76 只真实名册）
P_VIS = "视觉验收校"
P_BIG = "中北大学"


class Tab:
    """一个 CDP 会话：只用到 Runtime / Page / Emulation 三组命令。"""

    def __init__(self, ws):
        self.ws = ws
        self.seq = 0

    async def cmd(self, method, **params):
        self.seq += 1
        await self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.seq:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            # 其余是事件帧（load / network），忽略

    async def ev(self, js, await_promise=False):
        r = await self.cmd("Runtime.evaluate", expression=js,
                           returnByValue=True, awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            raise RuntimeError(js[:80] + " → " + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:300])
        return r.get("result", {}).get("value")

    async def wait(self, js, timeout=120.0, step=0.5):
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if await self.ev(js):
                return True
            await asyncio.sleep(step)
        raise TimeoutError(js[:100])

    async def shot(self, path: Path):
        r = await self.cmd("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(r["data"]))
        return path


async def open_tab() -> Tab:
    tabs = json.load(urllib.request.urlopen(CDP + "/json/list"))
    page = next(t for t in tabs if t["type"] == "page")
    ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
    tab = Tab(ws)
    await tab.cmd("Page.enable")
    await tab.cmd("Emulation.setDeviceMetricsOverride",
                  width=W, height=H, deviceScaleFactor=DPR, mobile=False)
    await tab.cmd("Page.navigate", url=APP)
    await tab.wait("document.readyState === 'complete' && !!document.getElementById('btnGenerate')")
    return tab


async def open_project(tab: Tab, school: str):
    """下拉选项目：value 用项目 id 前缀匹配（id 是时间戳，每次重建都变）。"""
    pid = await tab.ev(f"""(() => {{
      const sel = document.querySelector('select');
      const opt = [...sel.options].find(o => o.textContent.startsWith({school!r}));
      if (!opt) throw new Error('下拉里没有 {school}');
      sel.value = opt.value;
      sel.dispatchEvent(new Event('change', {{bubbles: true}}));
      return opt.value;
    }})()""")
    await tab.wait("document.getElementById('pidHint').textContent.includes('已打开')")
    await asyncio.sleep(1.0)            # 打开项目还有一串收尾请求（报告/归并/主题）
    return pid


async def scroll_to(tab: Tab, js: str):
    await tab.ev(js)
    await asyncio.sleep(0.9)          # 等图片懒加载与 toast 动画落定


async def gen_and_preview(tab: Tab):
    await tab.ev("document.getElementById('btnGenerate').click()")
    await tab.wait("!document.getElementById('btnGenerate').disabled", timeout=240)
    await tab.ev("document.getElementById('btnPreview').click()")
    await tab.wait("document.getElementById('frame').contentDocument.readyState === 'complete'")
    await asyncio.sleep(2.0)          # canvas 首帧 + 照片解码


# ---------- 六个场景 ----------

async def shot_merge_order(tab: Tab, out: Path):
    """F9 组内视觉预排序：甲↔乙 100% 排最前，丙 61%，丁纯色沉底。"""
    await open_project(tab, P_VIS)
    await tab.ev("document.getElementById('btnMergeScan').click()")
    await tab.wait("document.getElementById('mergeList').children.length > 0")
    await scroll_to(tab, "document.getElementById('c8').scrollIntoView({block:'start'}); window.scrollBy(0,-8)")
    await tab.shot(out)


async def shot_merge_nostructure(tab: Tab, out: Path):
    """F9 无结构照片不发假指纹：丁的纯色图解不开，卡片写明原因。"""
    await scroll_to(tab, "document.getElementById('c8').scrollIntoView({block:'end'}); window.scrollBy(0,12)")
    await tab.shot(out)


async def shot_preview(tab: Tab, out: Path):
    """F4 76 只真实星图渲染：分区聚拢 + 页脚注明标定方式。"""
    await open_project(tab, P_BIG)
    await gen_and_preview(tab)
    await scroll_to(tab, "document.getElementById('frame').closest('.card').scrollIntoView({block:'start'}); window.scrollBy(0,-8)")
    await tab.shot(out)


async def shot_calib(tab: Tab, out: Path):
    """F8 人工标定：在预览里真拖两颗星 → 保存 → 重新生成 → 页脚烘焙 2/4。"""
    await open_project(tab, P_VIS)
    await gen_and_preview(tab)
    # 服务端对 PUT 的标定做合并而不是覆盖：不清干净，页脚就会读出上一轮留下的
    # 4/4，跟这张证据的文件名（2 颗）对不上。
    await tab.ev("(() => { const b = document.getElementById('btnCalibClear'); if (!b.disabled) b.click(); })()")
    await tab.wait("document.getElementById('calibState').textContent.includes('算法推导')")
    await asyncio.sleep(1.0)
    await tab.ev("document.getElementById('btnCalibMode').click()")
    await tab.wait("document.getElementById('frame').contentWindow.__catgalaxy.info().calibMode")
    # 星星是 canvas 画的，没有 DOM 节点可点：用 pointermove 扫网格，
    # 读页面自己的 hover 提示（#tip）认星，再对认到的那颗走真拖拽事件链。
    dragged = await tab.ev("""(async () => {
      const w = document.getElementById('frame').contentWindow, d = w.document;
      const cv = d.getElementById('sky'), tip = d.getElementById('tip');
      cv.setPointerCapture = () => {};        // 合成指针没有活跃 pointerId，这步必然抛
      const r = cv.getBoundingClientRect();
      const fire = (t, x, y, tgt) => (tgt || cv).dispatchEvent(new w.PointerEvent(
        t, {clientX: x, clientY: y, bubbles: true, pointerId: 7, isPrimary: true}));
      const found = [];
      outer:
      for (let y = r.top + 24; y < r.bottom - 24; y += 16)
        for (let x = r.left + 24; x < r.right - 24; x += 16) {
          fire('pointermove', x, y);
          const name = (tip.textContent || '').split(' ·')[0];
          if ((tip.textContent || '').includes('拖动到真实位置') && !found.some(p => p.name === name))
            found.push({x, y, name});
          if (found.length >= 2) break outer;
        }
      const targets = [[r.left + r.width * .30, r.top + r.height * .30],
                       [r.left + r.width * .68, r.top + r.height * .62]];
      found.forEach((p, i) => {
        const [tx, ty] = targets[i];
        fire('pointerdown', p.x, p.y);
        for (let s = 1; s <= 4; s++) fire('pointermove', p.x + (tx - p.x) * s / 4, p.y + (ty - p.y) * s / 4);
        fire('pointerup', tx, ty, w);
      });
      await new Promise(res => setTimeout(res, 400));
      return found.map(p => p.name);
    })()""", await_promise=True)
    if len(dragged) < 2:
        raise RuntimeError(f"只拖到 {dragged}，星没认全")
    await tab.ev("document.getElementById('btnCalibSave').click()")
    await tab.wait("document.getElementById('calibState').textContent.includes('人工')")
    await gen_and_preview(tab)          # 标定要重新生成才进产物页脚
    await scroll_to(tab, "document.getElementById('frame').closest('.card').scrollIntoView({block:'start'}); window.scrollBy(0,-8)")
    await tab.shot(out)


async def shot_roster_edit(tab: Tab, out: Path):
    """F10 问题行摊开改：两行带 W_BAD_CONFIDENCE，输入框已改未应用。"""
    await open_project(tab, P_BIG)
    await asyncio.sleep(1.0)            # 等打开项目的收尾请求落地，别和校验抢跑
    await tab.ev("document.getElementById('btnValidate').click()")
    # 「错误/警告」两个字在卡片静态说明里就有，不能当完成信号；校验完必弹 toast。
    await tab.wait("document.getElementById('toast').textContent.includes('校验')")
    await tab.ev("document.getElementById('btnRosterEdit').click()")
    await tab.wait("/第 \\d+ 行/.test(document.getElementById('editList').innerText)")
    await tab.ev("""(() => {
      const ins = [...document.querySelectorAll('#editList input')].slice(0, 2);
      ins.forEach(i => { i.value = i.value + '（验收笔迹）';
                         i.dispatchEvent(new Event('input', {bubbles: true})); });
    })()""")
    await scroll_to(tab, "document.getElementById('c3').scrollIntoView({block:'start'}); window.scrollBy(0,-8)")
    await tab.shot(out)


async def shot_theme(tab: Tab, out: Path):
    """F11 主题面板：切到晨曦橘预设，五个预设按钮 + 字体 + 八色 + 署名同框。"""
    await tab.ev("""(() => {
      const b = [...document.querySelectorAll('#presetBox .preset')]
        .find(x => x.textContent.includes('晨曦橘'));
      b.click();
    })()""")
    await tab.wait("document.getElementById('themeState').textContent.includes('晨曦橘')")
    await scroll_to(tab, "document.getElementById('c9').scrollIntoView({block:'start'}); window.scrollBy(0,-8)")
    await tab.shot(out)


SHOTS = [
    ("F8-人工标定2颗-烘焙进页脚", shot_calib),
    ("F9-归并工作台-视觉预排序", shot_merge_order),
    ("F9-无结构照片不发假指纹", shot_merge_nostructure),
    ("F4-星图预览-真实渲染", shot_preview),
    ("F10-名册在线编辑-问题行摊开改", shot_roster_edit),
    ("F11-主题-晨曦橘预设", shot_theme),
]


async def main(pick: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tab = await open_tab()
    todo = [(n, f) for n, f in SHOTS if not pick or n in pick]
    for name, fn in todo:
        path = OUT / f"{name}.png"
        await fn(tab, path)
        print(f"已拍 {path.name}（{path.stat().st_size // 1024}KB）")
    await tab.ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
