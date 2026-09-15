"""验收脚本 · 对应 SPEC §7 的两个验收场景。

    python scripts/acceptance.py --case demo2      # 空数据包骨架 + 2 行示例 → F1-F5 全流程
    python scripts/acceptance.py --case full76     # 真实名册 76 只全量回归
    python scripts/acceptance.py --case all

走真实 HTTP 接口（进程内 TestClient），产物落在 workspace/，证据写入 docs/。E 盘全程只读。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app import config, csv_loader  # noqa: E402
from app.main import app  # noqa: E402

E_ROOT = Path(r"E:\猫咪星图_总库")
SKELETON = E_ROOT / "12_跨校复制包" / "示例校_空数据包骨架"
REAL_ROSTER = E_ROOT / "07_普查原始数据" / "猫咪名册.csv"
PHOTO_DIRS = [E_ROOT / "08_原始照片视频" / "照片视频",
              E_ROOT / "08_原始照片视频" / "补充视频照片"]
DOCS = ROOT / "docs"


class Evidence:
    """把每一步的实测结果攒成 Markdown，作为交付报告的验收证据。"""

    def __init__(self, title: str):
        self.title = title
        self.rows: list[tuple[str, str]] = []
        self.failed: list[str] = []

    def check(self, step: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((step, ("✅ " if ok else "❌ ") + detail))
        if not ok:
            self.failed.append(f"{step}: {detail}")
        print(f"[{'PASS' if ok else 'FAIL'}] {step} — {detail}")
        return ok

    def note(self, step: str, detail: str) -> None:
        self.rows.append((step, detail))
        print(f"[ .. ] {step} — {detail}")

    def markdown(self) -> str:
        lines = [f"# {self.title}", "",
                 f"- 执行日期：{date.today().isoformat()}",
                 f"- 结论：{'全部通过' if not self.failed else f'{len(self.failed)} 项未通过'}",
                 "", "| 步骤 | 实测结果 |", "|---|---|"]
        lines += [f"| {s} | {d} |" for s, d in self.rows]
        if self.failed:
            lines += ["", "## 未通过项", ""] + [f"- {f}" for f in self.failed]
        return "\n".join(lines) + "\n"

    def save(self, name: str) -> Path:
        DOCS.mkdir(parents=True, exist_ok=True)
        path = DOCS / name
        path.write_text(self.markdown(), encoding="utf-8")
        return path


def cats_from_html(html: str) -> list[dict]:
    """从生成的星图 HTML 里反解 CATS 数组——验证产物本身，而不是验证代码。"""
    m = re.search(r"const CATS = (\[.*?\]);\s*\nconst CALIB", html, re.S)
    if not m:
        raise AssertionError("生成的 HTML 里找不到 CATS 数组")
    return json.loads(m.group(1))


def make_photo(width: int, height: int, seed: int) -> bytes:
    img = Image.new("RGB", (width, height), (18, 24, 48))
    px = img.load()
    for y in range(height):
        for x in range(0, width, 3):
            px[x, y] = ((x * 7 + seed * 31) % 255, (y * 5 + seed * 17) % 255,
                        ((x + y) * 3 + seed) % 255)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


SCENES = {
    "橘猫": [(0.30, 0.45, 0.34, 0.30, (220, 150, 60)),
             (0.34, 0.24, 0.14, 0.14, (235, 170, 80)),
             (0.62, 0.62, 0.20, 0.12, (180, 120, 50))],
    "三花": [(0.55, 0.30, 0.22, 0.40, (60, 60, 70)),
             (0.20, 0.60, 0.30, 0.22, (240, 240, 240)),
             (0.70, 0.70, 0.16, 0.16, (120, 80, 60))],
}


def make_scene_photo(kind: str, width: int = 1200, height: int = 900,
                     quality: int = 95) -> bytes:
    """有结构、像照片的样本图：渐变底 + 几个确定性的椭圆块。

    不用 `make_photo`：那是高频噪点，缩到 64×64 就整片平坦，感知哈希会（正确地）
    拒绝给它指纹。真实照片是低频结构（身体、背景、光影），这里就用低频结构模拟。
    渐变也不逐像素画：先画 20×15 再放大，同样是低频，省掉二十几万次循环。
    """
    gw, gh = max(2, width // 60), max(2, height // 60)
    small = Image.new("RGB", (gw, gh))
    sp = small.load()
    for y in range(gh):
        for x in range(gw):
            sp[x, y] = (x * 255 // (gw - 1), y * 255 // (gh - 1), 128)
    img = small.resize((width, height), Image.BILINEAR)
    d = ImageDraw.Draw(img)
    for cx, cy, rw, rh, color in SCENES[kind]:
        x0, y0 = int((cx - rw / 2) * width), int((cy - rh / 2) * height)
        x1, y1 = int((cx + rw / 2) * width), int((cy + rh / 2) * height)
        d.ellipse([x0, y0, x1, y1], fill=color)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def recode_jpeg(blob: bytes, scale: float, quality: int) -> bytes:
    """缩放 + 重压一遍：模拟「同一只猫的另一张入库副本」。"""
    im = Image.open(io.BytesIO(blob))
    im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def build_skeleton_csv() -> tuple[str, list[dict], str]:
    """空数据包骨架的表头 + 2 行示例数据，顺带证明骨架的 12 列与本工具完全对齐。"""
    sample_rows = list(csv.DictReader(io.StringIO(csv_loader.empty_template())))
    tpl = SKELETON / "data" / "猫咪名册_模板.csv"
    if tpl.exists():
        header = next(r for r in csv.reader(io.StringIO(read_text(tpl))) if r)
        source = f"表头取自 {tpl.name}（{len(header)} 列）"
    else:
        header = list(config.ROSTER_COLUMNS)
        source = "E 盘不可读，表头回退到内置 12 列"
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=header, extrasaction="ignore")
    w.writeheader()
    for r in sample_rows:
        w.writerow({k: r.get(k, "") for k in header})
    return out.getvalue(), sample_rows, source


def find_real_photos(names: list[str]) -> tuple[dict[str, Path], list[str]]:
    """按 basename 在 E 盘照片库里定位真实照片（名册里有的带 `照片视频/` 前缀）。"""
    index: dict[str, Path] = {}
    for d in PHOTO_DIRS:
        if d.exists():
            for p in d.iterdir():
                if p.is_file():
                    index.setdefault(p.name.lower(), p)
    found: dict[str, Path] = {}
    missing: list[str] = []
    for n in names:
        base = n.replace("\\", "/").split("/")[-1]
        hit = index.get(base.lower())
        if hit:
            found[base] = hit
        else:
            missing.append(base)
    return found, missing


def photo_sizes_on_disk(pid: str) -> tuple[int, int]:
    """返回 (张数, 最大单张字节)，直接量工作区里的成品，验证 200KB 红线。"""
    d = config.ensure_dirs(pid)["photos"]
    sizes = [p.stat().st_size for p in d.iterdir() if p.is_file()]
    return len(sizes), max(sizes, default=0)


def verify_zip_contents(ev: Evidence, zf: zipfile.ZipFile, cats: list[dict],
                        form: str) -> None:
    names = set(zf.namelist())
    htmls = [n for n in names if n.endswith(".html")]
    ev.check("zip 内含星图 HTML", len(htmls) == 1, str(htmls))
    photos = sorted(n for n in names if n.startswith("assets/photos/"))

    if form == "relative":
        missing = [c["photo"] for c in cats if c["photo"] not in names]
        ev.check("每只猫的照片都在 zip 里（相对路径可解析）", not missing,
                 f"照片 {len(photos)} 张 / CATS {len(cats)} 条，缺失 {len(missing)}"
                 + (f"：{missing[:3]}" if missing else ""))
        ev.check("zip 内含底图", "assets/map.jpg" in names, "assets/map.jpg")
    else:
        chunks = sorted(n for n in names if re.fullmatch(r"assets/photo-data-\d+\.js", n))
        blob = "".join(zf.read(c).decode("utf-8") for c in chunks)
        missing = [Path(c["photo"]).name for c in cats
                   if f'"{Path(c["photo"]).name}"' not in blob]
        ev.check("每只猫的照片都已 base64 内嵌", not missing,
                 f"分片 {len(chunks)} 个 / CATS {len(cats)} 条，未内嵌 {len(missing)}"
                 + (f"：{missing[:3]}" if missing else ""))
        ev.check("底图也已内嵌", '"map.jpg"' in blob, "map.jpg")
        ev.check("内嵌形态不含照片目录", not photos, f"assets/photos 条目 {len(photos)} 个")
        html = zf.read(htmls[0]).decode("utf-8")
        refs = re.findall(r'<script src="([^"]+)"', html)
        unresolved = [r for r in refs if r not in names]
        # 内嵌形态下 CATS.photo 仍是 assets/photos/xxx.jpg，但它只是 PH() 查 base64 的键，
        # 不构成文件依赖；真正会断链的是 <img src> 与 CSS url()。
        img_refs = re.findall(r'<img[^>]+src="([^"]+)"', html)
        img_refs += re.findall(r'url\((?!["\']?data:)([^)"\']+)\)', html)
        ev.check("解压后双击即开（不依赖任何外部图片文件）",
                 bool(refs) and not unresolved and not img_refs,
                 f"HTML 仅引用同目录 JS {len(refs)} 个且全部在包内；"
                 f"图片文件引用 {len(img_refs)} 个，照片与底图均为 base64")

    for extra in ("data/猫咪名册.csv", "校验报告.md", "归并决策摘要.md"):
        ev.check(f"zip 内含 {extra}", extra in names, extra)


CALIB_XY = {"x": 0.147, "y": 0.258}


def verify_calib(client: TestClient, ev: Evidence, pid: str, n_cats: int) -> None:
    """F8 · 底图标定：人工星位要能存下来、烘焙进产物，并能一键退回算法推导。"""
    r = client.get(f"/api/projects/{pid}/calib")
    if not ev.check("F8 读取星位标定", r.status_code == 200,
                    "" if r.status_code == 200 else r.text[:200]):
        return
    j = r.json()
    ids = j["ids"][:2]
    s = j["stats"]
    ev.check("F8 初始状态为算法推导",
             j["source"] == "derived" and s["manual"] == 0 and s["total"] == n_cats,
             f"来源 {j['source']}，{s['manual']}/{s['total']} 人工标定，"
             f"底图尺寸 {j['map_size'] or '未上传（用兜底 1920×1239）'}")
    if not ids:
        ev.check("F8 名册里有可标定的编号", False, "ids 为空")
        return

    manual = {ids[0]: CALIB_XY}
    if len(ids) > 1:
        manual[ids[1]] = {"x": 0.803, "y": 0.611}
    r = client.put(f"/api/projects/{pid}/calib",
                   json={"positions": manual, "note": "验收：人工标定 2 颗星"})
    j = r.json() if r.status_code == 200 else {}
    ev.check("F8 保存人工标定", r.status_code == 200 and j.get("saved") == len(manual),
             f"本次 {j.get('saved')} 颗，累计 {j.get('total')} 颗，"
             f"忽略未知编号 {j.get('ignored_unknown_ids') or '无'}")

    r = client.get(f"/api/projects/{pid}/calib")
    j = r.json()
    ev.check("F8 复读来源变为 manual",
             j["source"] == "manual" and j["stats"]["manual"] == len(manual)
             and j["note"] == "验收：人工标定 2 颗星",
             f"来源 {j['source']}，人工 {j['stats']['manual']} / 推导 {j['stats']['derived']}"
             f"，备注「{j['note']}」")

    r = client.put(f"/api/projects/{pid}/calib",
                   json={"positions": {ids[0]: {"x": 1.7, "y": -0.2}}})
    ok = r.status_code == 400
    ev.check("F8 越界坐标被拒（必须是 0~1 归一化值）", ok,
             f"HTTP {r.status_code}" + (f"：{r.json().get('detail', '')[:60]}" if ok else ""))
    r2 = client.get(f"/api/projects/{pid}/calib")
    ev.check("F8 被拒的写入没有污染已存标定",
             r2.json()["stats"]["manual"] == len(manual),
             f"仍是人工 {r2.json()['stats']['manual']} 颗")

    r = client.post(f"/api/projects/{pid}/generate", json={"form": "relative"})
    d = r.json()
    ev.check("F8 标定后重新生成", r.status_code == 200 and d["calib"]["manual"] == len(manual),
             f"烘焙人工星位 {d['calib']['manual']}/{d['calib']['total']} 颗")
    html = client.get(d["preview_url"]).text
    baked = f'"{ids[0]}":{{"x":{CALIB_XY["x"]},"y":{CALIB_XY["y"]}}}'
    ev.check("F8 人工坐标已烘焙进产物 CALIB", baked in html,
             f"产物里找到 {baked}" if baked in html else f"产物里找不到 {baked}")
    ev.check("F8 产物页脚注明标定比例",
             f"星位人工标定 {len(manual)}/{d['calib']['total']}" in html,
             f"星位人工标定 {len(manual)}/{d['calib']['total']}")

    r = client.delete(f"/api/projects/{pid}/calib")
    back = client.get(f"/api/projects/{pid}/calib").json()
    ev.check("F8 清除标定后回到算法推导",
             r.status_code == 200 and r.json()["cleared"] is True
             and back["source"] == "derived" and back["stats"]["manual"] == 0,
             f"cleared={r.json().get('cleared')}，来源 {back['source']}，"
             f"人工 {back['stats']['manual']} 颗")


def make_badge() -> bytes:
    """一张带透明角的校徽样图：透明区必须在重编码后还在，否则深色星图上会出现白方块。"""
    img = Image.new("RGBA", (600, 400), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((40, 20, 560, 380), fill=(200, 160, 60, 255), outline=(255, 255, 255, 255))
    d.text((250, 180), "CAT", fill=(20, 20, 40, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def verify_theme(client: TestClient, ev: Evidence, pid: str,
                 forms: tuple[str, ...]) -> None:
    """F11 · 主题化：配色/字体/署名/校徽要能存下来、烘焙进产物，非法值要挡在门外。"""
    from app.theme import DEFAULT_PRESET, MAX_SIGNATURE, PRESETS

    url = f"/api/projects/{pid}/theme"
    opts = client.get("/api/theme/options").json()
    ev.check("F11 主题选项菜单可用",
             len(opts["presets"]) == len(PRESETS) and len(opts["fonts"]) >= 5
             and len(opts["colors"]) >= 8,
             f"{len(opts['presets'])} 套预设、{len(opts['fonts'])} 种字体栈、"
             f"{len(opts['colors'])} 个可调颜色")

    base = client.get(url).json()
    ev.check("F11 初始主题为默认预设",
             base["theme"]["preset"] == DEFAULT_PRESET and not base["theme"]["colors"]
             and base["has_logo"] is False
             and base["effective_colors"] == PRESETS[DEFAULT_PRESET].colors,
             f"预设 {base['theme']['preset']}，生效色 {len(base['effective_colors'])} 项，"
             f"自定义 0 项，校徽 无")

    sig = "中北大学学生会 · 校园猫咪普查小组 <script>alert(1)</script>"
    r = client.put(url, json={"preset": "dawn", "colors": {"accent": "#ff8800"},
                              "title_font": "kai", "footer_signature": sig})
    j = r.json()
    eff = j["effective_colors"]
    dawn = PRESETS["dawn"].colors
    ev.check("F11 切换预设 + 单项自定义颜色",
             r.status_code == 200 and eff["accent"] == "#ff8800"
             and {k: v for k, v in eff.items() if k != "accent"}
             == {k: v for k, v in dawn.items() if k != "accent"}
             and j["theme"]["colors"] == {"accent": "#ff8800"},
             f"预设 {j['theme']['preset']}，accent 覆盖为 {eff['accent']}，"
             f"其余 {len(eff) - 1} 项仍取预设值（存的是覆盖项而非全量）")

    bad = client.put(url, json={"preset": "neon",
                                "colors": {"bg": "red();", "sky": "#ffffff"},
                                "title_font": "Comic Sans",
                                "footer_signature": "长" * (MAX_SIGNATURE + 40)}).json()
    t = bad["theme"]
    ev.check("F11 非法值被丢弃并逐条说明",
             len(bad["rejected"]) >= 5 and t["preset"] == DEFAULT_PRESET
             and t["colors"] == {} and t["title_font"] == "system"
             and len(t["footer_signature"]) <= MAX_SIGNATURE,
             f"拒收 {len(bad['rejected'])} 条：{'；'.join(bad['rejected'][:3])}…"
             f"｜预设退回 {t['preset']}，字体退回 {t['title_font']}，"
             f"署名截断到 {len(t['footer_signature'])} 字")

    client.put(url, json={"preset": "dawn", "colors": {"accent": "#ff8800"},
                          "title_font": "kai", "footer_signature": sig})

    r = client.post(f"/api/projects/{pid}/logo",
                    files={"file": ("badge.png", make_badge(), "image/png")})
    j = r.json()
    logo = client.get(f"/api/projects/{pid}/logo")
    with Image.open(io.BytesIO(logo.content)) as im:
        has_alpha = im.mode in ("RGBA", "LA") and im.getextrema()[-1][0] < 255
    ev.check("F11 上传校徽（自动缩放转 PNG 并保留透明区）",
             r.status_code == 200 and j["has_logo"] is True
             and max(j["width"], j["height"]) <= config.MAX_LOGO_SIDE
             and logo.status_code == 200 and has_alpha,
             f"600×400 → {j['width']}×{j['height']}，{j['bytes'] // 1024}KB，"
             f"透明区 {'保留' if has_alpha else '丢失'}")

    r = client.post(f"/api/projects/{pid}/logo",
                    files={"file": ("badge.svg", b"<svg onload=alert(1)></svg>",
                                    "image/svg+xml")})
    ev.check("F11 SVG 校徽被拒（产物要挂公众号，不收能带脚本的格式）",
             r.status_code == 400 and "SVG" in r.json()["detail"],
             f"HTTP {r.status_code}：{r.json().get('detail', '')[:60]}")

    for form in forms:
        r = client.post(f"/api/projects/{pid}/generate", json={"form": form})
        if not ev.check(f"F11 带主题重新生成（{form}）",
                        r.status_code == 200 and r.json()["theme"]["preset"] == "dawn",
                        "" if r.status_code == 200 else r.text[:200]):
            continue
        d = r.json()
        html = client.get(d["preview_url"]).text
        want_logo = ('<img class="logo" src="data:image/png;base64,' if form == "inline"
                     else '<img class="logo" src="assets/logo.png"')
        ev.check(f"F11 配色与字体已烘焙进产物（{form}）",
                 "--bg:#1a0d09" in html and "--gold:#ff8800" in html
                 and '"Kaiti SC"' in html,
                 "--bg 取预设晨曦橘 #1a0d09，--gold 取自定义 #ff8800，标题字体栈含 Kaiti SC")
        ev.check(f"F11 署名以转义文本追加在出处之后（{form}）",
                 "&lt;script&gt;alert(1)&lt;/script&gt;" in html
                 and "<script>alert(1)</script>" not in html
                 and html.index("由「喵星图工厂 CatGalaxy Factory」自动生成")
                 < html.index("&lt;script&gt;alert(1)"),
                 f"署名 {len(d['theme']['signature'])} 字，标签已转义为纯文本，出处未被替换")
        ev.check(f"F11 校徽已进产物（{form}）", want_logo in html,
                 "base64 内嵌，单文件双击即开" if form == "inline"
                 else "相对路径 assets/logo.png，随包一起拷")
        zf = zipfile.ZipFile(io.BytesIO(client.get(
            f"/api/projects/{pid}/download?form={form}").content))
        names = zf.namelist()
        if form == "relative":
            ev.check("F11 zip 内带独立校徽文件",
                     "assets/logo.png" in names,
                     f"assets/logo.png（{len(zf.read('assets/logo.png')) // 1024}KB）")
        else:
            ev.check("F11 内嵌版 zip 里不留校徽文件",
                     "assets/logo.png" not in names,
                     "校徽已 base64 进 HTML，压缩包内无冗余文件")

    client.delete(f"/api/projects/{pid}/logo")
    client.delete(url)
    back = client.get(url).json()
    d = client.post(f"/api/projects/{pid}/generate", json={"form": "relative"}).json()
    html = client.get(d["preview_url"]).text
    ev.check("F11 恢复默认主题后产物回到旧版长相",
             back["theme"]["preset"] == DEFAULT_PRESET and back["has_logo"] is False
             and "由喵星图工厂按项目配置注入" not in html
             and "<img class=\"logo\"" not in html
             and "--bg:#1a0d09" not in html,
             "预设回到默认、校徽已删，产物里不再多出主题 <style> 与徽章标签")


def verify_roster_rows(client: TestClient, ev: Evidence, pid: str, n_cats: int) -> None:
    """F10 增行/删行：加一只猫再删掉，名册回到原样，照片留在工作区。

    两处刻意不自动化的地方，都要在这里留下实测证据：
      · 插入时编号留空就是留空 —— 工具替用户补号会让「弃用编号不复用」这条数据
        红线悄悄失效，所以这里先证明它报了 E_BAD_ID、再由人 PATCH 定号；
      · 删行不删照片 —— 孤儿照片必须变成 I_PHOTO_UNUSED 浮到报告上，
        而不是被工具一声不响地清掉。
    """
    j = client.get(f"/api/projects/{pid}/roster").json()
    rows0, id_idx = len(j["rows"]), j["mapping"].get("编号")
    used = []
    for row in j["rows"]:
        v = row[id_idx].strip() if id_idx is not None and len(row) > id_idx else ""
        m = re.fullmatch(r"CAT-(\d+)", v)
        if m:
            used.append(int(m.group(1)))
    new_id = f"CAT-{max(used, default=0) + 1:03d}"

    photo_name = "验收-新增行.jpg"
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", (photo_name, make_photo(900, 700, 99), "image/jpeg"))])
    n_photos, _ = photo_sizes_on_disk(pid)
    ev.check("F10 为待新增的猫先传一张照片",
             r.status_code == 200 and r.json()["saved"] == 1,
             f"{photo_name} 入库 {r.json().get('saved')} 张，工作区共 {n_photos} 张")

    r = client.post(f"/api/projects/{pid}/roster/rows", json={
        "after": rows0 + 1,
        "values": {"昵称": "验收猫", "毛色": "橘白", "代表照片文件": photo_name,
                   "照片数量": "1", "出没区域": "验收脚本", "置信度": "高"},
    })
    j2 = r.json()
    rep = j2["report"]
    codes = sorted({i["code"] for i in rep["issues"] if i["line"] == j2["line"]})
    ev.check("F10 增行不替用户编编号（编号留空就该报错）",
             r.status_code == 200 and j2["line"] == rows0 + 2
             and "E_BAD_ID" in codes and rep["summary"]["ok"] is False
             and rep["summary"]["total_rows"] == rows0 + 1
             and not j2["row"][id_idx].strip(),
             f"新行落在第 {j2['line']} 行，数据行 {rows0} → {rep['summary']['total_rows']}；"
             f"该行问题 {codes}；编号列实值「{j2['row'][id_idx]}」")

    r = client.patch(f"/api/projects/{pid}/roster", json={
        "edits": [{"line": j2["line"], "field": "编号", "value": new_id}]})
    s = r.json()["report"]["summary"]
    ev.check("F10 由人定号后校验转绿",
             r.status_code == 200 and s["ok"] is True and s["error_count"] == 0,
             f"编号 {new_id}（避开已用的 {len(used)} 个号），可入图 {s['valid_rows']} 行，"
             f"错误 {s['error_count']} 警告 {s['warning_count']} 提示 {s['info_count']}")

    d = client.post(f"/api/projects/{pid}/generate", json={"form": "relative"}).json()
    ev.check("F10 新增的猫进了星图产物", d["cats"] == n_cats + 1,
             f"CATS {d['cats']} 条（原 {n_cats} 条 + 新增 1 条），"
             f"zip {d['zip_bytes'] // 1024}KB")

    r = client.delete(f"/api/projects/{pid}/roster/rows/{j2['line']}")
    jd = r.json()
    s = jd["report"]["summary"]
    unused = [i for i in jd["report"]["issues"] if i["code"] == "I_PHOTO_UNUSED"]
    n_after, _ = photo_sizes_on_disk(pid)
    ev.check("F10 删行：名册回到原样，照片留在工作区并报「未使用」",
             r.status_code == 200 and s["total_rows"] == rows0 and s["ok"] is True
             and n_after == n_photos and unused,
             f"删掉第 {jd['deleted']['line']} 行（编号 {jd['deleted']['row'][id_idx]}），"
             f"数据行 {rows0 + 1} → {s['total_rows']}；工作区照片 {n_after} 张未减少；"
             f"I_PHOTO_UNUSED {len(unused)} 条：{unused[0]['message'][:60]}")

    d = client.post(f"/api/projects/{pid}/generate", json={"form": "relative"}).json()
    ev.check("F10 删掉的猫不再出现在星图里", d["cats"] == n_cats,
             f"CATS 回到 {d['cats']} 条（期望 {n_cats}）")


def verify_merge_visual(client: TestClient, ev: Evidence) -> None:
    """F9 视觉预排序：组内谁跟谁最像，先由感知哈希排一遍，人从左边第一对看起。

    自建一个项目而不是复用主场景：四只猫必须同毛色、同出没区域、特征描述完全一样
    才凑成一个候选组，而主场景的名册是真实数据，凑不出这种可控条件。
      · 甲/乙 —— 同一张场景图，乙缩放重压过（模拟同一只猫的另一张入库副本）
      · 丙   —— 明显是另一只
      · 丁   —— 照片是纯色的：没有结构，哈希**该拒绝**给指纹，而不是发一个假的
    """
    pid = client.post("/api/projects", json={"school": "视觉验收校"}).json()["id"]
    scene = make_scene_photo("橘猫")
    flat = io.BytesIO()
    Image.new("RGB", (600, 450), (200, 200, 200)).save(flat, "JPEG", quality=95)
    photos = {"v-jia.jpg": scene, "v-yi.jpg": recode_jpeg(scene, 0.5, 70),
              "v-bing.jpg": make_scene_photo("三花"), "v-ding.jpg": flat.getvalue()}
    rows = [("CAT-001", "甲", "v-jia.jpg"), ("CAT-002", "乙", "v-yi.jpg"),
            ("CAT-003", "丙", "v-bing.jpg"), ("CAT-004", "丁", "v-ding.jpg")]

    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(config.ROSTER_COLUMNS), extrasaction="ignore")
    w.writeheader()
    for cid, name, photo in rows:
        w.writerow({"编号": cid, "昵称": name, "军衔": "喵员", "工位": "巡校内务",
                    "毛色": "全橘虎斑", "特征描述": "体型胖 粉鼻 侧躺露肚",
                    "代表照片文件": photo, "照片数量": "1", "出没区域": "宿舍楼前石台",
                    "关联照片编号": "batch1:VIS", "置信度": "高", "备注": ""})
    client.post(f"/api/projects/{pid}/roster",
                files={"file": ("猫咪名册.csv", out.getvalue().encode("utf-8-sig"),
                                "text/csv")})
    r = client.post(f"/api/projects/{pid}/photos",
                    files=[("files", (n, b, "image/jpeg")) for n, b in photos.items()])
    ev.check("F9 视觉样本入库", r.status_code == 200 and r.json()["saved"] == 4,
             f"{len(photos)} 张（甲原图 / 乙缩放重压 / 丙另一只 / 丁纯色）")
    client.post(f"/api/projects/{pid}/validate")

    j = client.get(f"/api/projects/{pid}/merge").json()
    g = j["groups"][0]
    members = g["members"]
    by_id = {m["id"]: m for m in members}
    order = [m["id"] for m in members]
    vis = "、".join(f"{m['id']} {m['visual']:.0%}" for m in members)

    ev.check("F9 组内按视觉相似度排序，最像的两只排在最前",
             order[:2] == ["CAT-001", "CAT-002"] and order[-1] == "CAT-004",
             f"顺序 {' → '.join(order)}；视觉 {vis}")
    ev.check("F9 每张卡片标出组里最像它的那一位",
             by_id["CAT-001"]["visual_peer"] == "CAT-002"
             and by_id["CAT-002"]["visual_peer"] == "CAT-001"
             and by_id["CAT-001"]["visual"] >= 0.8,
             f"甲↔乙 互为最像，视觉 {by_id['CAT-001']['visual']:.1%}"
             f"（同一张图重压过仍认得出）；组内最高 {g['visual_top']:.1%}")
    ev.check("F9 视觉分不掺进可疑度（判定仍然是人的事）",
             g["kind"] == "coat-area" and g["score"] == 0.9,
             f"可疑度仍是 {g['score']}（0.35 + 0.55 × 特征描述相似度 100%），"
             f"与视觉分 {g['visual_top']:.0%} 各算各的")
    ev.check("F9 纯色照片拿不到指纹，沉到最后而不是发个假 100%",
             g["visual_hashed"] == 3 and by_id["CAT-004"]["visual"] == 0.0
             and j["stats"]["hashed_groups"] == 1,
             f"丁的照片纯色无结构 → 不算指纹，4 只里 {g['visual_hashed']} 只参与排序；"
             f"stats.hashed_groups {j['stats']['hashed_groups']}")


def run_flow(client: TestClient, ev: Evidence, *, school: str, subtitle: str,
             roster_text: str, photos: list[tuple[str, bytes]], expect_cats: int,
             forms: tuple[str, ...] = ("relative", "inline")) -> str:
    r = client.post("/api/projects", json={"school": school, "subtitle": subtitle})
    ev.check("F1 建项目", r.status_code == 200, f"{school} → {r.json().get('id')}")
    pid = r.json()["id"]

    r = client.post(f"/api/projects/{pid}/roster",
                    files={"file": ("猫咪名册.csv", roster_text.encode("utf-8-sig"),
                                    "text/csv")})
    ok = r.status_code == 200
    if ok:
        j = r.json()
        detail = (f"编码 {j['encoding']}，解析出 {j['rows']} 行，"
                  f"缺列 {j['missing_columns'] or '无'}，未识别列 {j['unknown_columns'] or '无'}")
    else:
        detail = r.text[:200]
    ev.check("F1 上传名册 CSV", ok, detail)

    files = [("files", (n, b, "image/jpeg")) for n, b in photos]
    r = client.post(f"/api/projects/{pid}/photos", files=files)
    j = r.json()
    count, biggest = photo_sizes_on_disk(pid)
    ev.check("F1 上传照片", r.status_code == 200 and j["saved"] == len(photos),
             f"入库 {j['saved']} 张，跳过 {j['skipped'] or '无'}")
    ev.check("照片压缩达标（长边≤1200px、单张≤200KB）",
             j["max_side"] <= config.MAX_PHOTO_SIDE
             and biggest <= config.MAX_PHOTO_BYTES,
             f"最大长边 {j['max_side']}px，最大单张 {biggest // 1024}KB；"
             f"体积 {j['src_bytes'] // 1024}KB → {j['out_bytes'] // 1024}KB，工作区 {count} 张")

    r = client.post(f"/api/projects/{pid}/validate")
    rep = r.json()
    s = rep["summary"]
    ev.check("F2 校验通过", s["ok"] is True,
             f"{s['total_rows']} 行 / 可入图 {s['valid_rows']} 行 / "
             f"错误 {s['error_count']} 警告 {s['warning_count']} 提示 {s['info_count']}")
    by_code: dict[str, int] = {}
    for i in rep["issues"]:
        by_code[i["code"]] = by_code.get(i["code"], 0) + 1
    ev.note("F2 问题分布", ", ".join(f"{k}×{v}" for k, v in sorted(by_code.items())) or "无")
    ev.note("F2 编号范围", f"{s['id_min']} ~ {s['id_max']}，空缺 {s['id_gaps'] or '无'}，"
                          f"低置信度 {s['low_confidence'] or '无'}")
    r = client.get(f"/api/projects/{pid}/report.md")
    ev.check("F2 校验报告可下载", r.status_code == 200 and "校验报告" in r.text,
             f"{len(r.text)} 字符 Markdown")

    for form in forms:
        r = client.post(f"/api/projects/{pid}/generate", json={"form": form})
        if not ev.check(f"F3+F5 生成并打包（{form}）", r.status_code == 200,
                        "" if r.status_code == 200 else r.text[:200]):
            continue
        d = r.json()
        ev.check(f"F3 星图数据完整（{form}）",
                 d["cats"] == expect_cats and not d["missing_photos"],
                 f"CATS {d['cats']} 条（期望 {expect_cats}），照片 {d['photos_embedded']} 张，"
                 f"缺失 {len(d['missing_photos'])}")
        ev.check(f"F5 zip 命名规范（{form}）",
                 re.fullmatch(r".+-校园猫咪星图-\d{4}-\d{2}-\d{2}\.zip", d["zip_name"])
                 is not None, f"{d['zip_name']}（{d['zip_bytes'] // 1024}KB）")

        pv = client.get(f"/api/projects/{pid}/preview?form={form}")
        page = client.get(d["preview_url"])
        ev.check(f"F4 在线预览（{form}）",
                 pv.status_code == 200 and page.status_code == 200 and "CATS" in page.text,
                 f"{d['preview_url']} → HTTP {page.status_code}，{len(page.text)} 字符")

        dl = client.get(f"/api/projects/{pid}/download?form={form}")
        zf = zipfile.ZipFile(io.BytesIO(dl.content))
        cats = cats_from_html(zf.read(d["html_name"]).decode("utf-8"))
        ev.check(f"F3 注入结果可从产物反解（{form}）", len(cats) == expect_cats,
                 f"HTML 里解出 {len(cats)} 条 CATS，首条键：{','.join(list(cats[0])[:8])}…")
        verify_zip_contents(ev, zf, cats, form)

    verify_calib(client, ev, pid, expect_cats)
    verify_theme(client, ev, pid, forms)
    verify_roster_rows(client, ev, pid, expect_cats)
    verify_merge_visual(client, ev)

    r = client.post(f"/api/projects/{pid}/summary")
    md = r.json().get("markdown", "")
    ev.check("F7 归并决策摘要生成", r.status_code == 200 and "数据总览" in md,
             f"{len(md)} 字符")
    return pid


def case_demo2(client: TestClient) -> Evidence:
    ev = Evidence("验收场景 1 · 空数据包骨架 + 2 行示例数据走通 F1-F5")
    roster_text, rows, source = build_skeleton_csv()
    ev.note("数据来源", source)
    ev.note("示例行", "; ".join(f"{r['编号']} {r['昵称']}（{r['毛色']}，{r['代表照片文件']}）"
                                for r in rows))
    photos = [(r["代表照片文件"], make_photo(1600, 1200, i + 1)) for i, r in enumerate(rows)]
    pid = run_flow(client, ev, school="示例校", subtitle="跨校复制包 · 空骨架自检",
                   roster_text=roster_text, photos=photos, expect_cats=len(rows))

    def parse_census(name: str, data: bytes) -> dict:
        r = client.post("/api/census/parse", files={"file": (name, data, "text/plain")})
        return r.json() if r.status_code == 200 else {"_status": r.status_code,
                                                      "_text": r.text[:200]}

    batch = SKELETON / "普查" / "普查-batch01_模板.txt"
    if batch.exists():
        j = parse_census(batch.name, batch.read_bytes())
        ev.check("F6 普查解析接受骨架模板", "_status" not in j,
                 f"{batch.name}：解析 {j.get('count', 0)} 条（空模板，0 条属预期），"
                 f"警告 {len(j.get('warnings', []))} 条")
    real_batch = E_ROOT / "07_普查原始数据" / "普查-batch1.txt"
    if real_batch.exists():
        j = parse_census(real_batch.name, real_batch.read_bytes())
        recs = j.get("records", [])
        draft = j.get("draft_csv") or ""
        ev.check("F6 普查 batch → 名册草稿（真实 batch1）",
                 "_status" not in j and j.get("count", 0) > 0 and recs
                 and draft.startswith("编号,") and len(draft.splitlines()) - 1 == j["count"],
                 f"解析 {j.get('count', 0)} 条，警告 {len(j.get('warnings', []))} 条，"
                 f"归并建议 {len(j.get('suggestions', []))} 组，"
                 f"草稿 CSV {len(draft)} 字符 / {len(draft.splitlines()) - 1} 行；"
                 f"首条 {recs[0]['filename']} → {recs[0]['coat_main']}/{recs[0]['area']}"
                 if recs else "无记录")
    if not batch.exists() and not real_batch.exists():
        # E 盘不在（比如 CI）也不该让 F6 整个不测：用一段内置合成 batch 顶上。
        # 三行里埋了两个已知答案——同毛色同场景的两条应被提成归并建议，
        # C 画质应映射成置信度「低」。
        text = "\n".join((
            "普查 batch9（合成样例，无 E 盘时自检用）",
            "ci-001.jpg | 1 | 橘白（橘背橘头，胸腹发白） | 体型胖 粉鼻 左耳缺角 | 宿舍楼前石台 | A | 是 | 无",
            "ci-002.jpg | 1 | 橘白（橘背橘头，胸腹发白） | 体型胖 粉鼻 左耳缺角 常蹲石台 | 宿舍楼前石台 | B | 是 | 无",
            "ci-003.jpg | 2 | 三花 | 背中三花斑 尾短 | 食堂后厨走廊 | C | 否 | 有路人入镜",
        ))
        j = parse_census("合成-batch9.txt", text.encode("utf-8"))
        recs = j.get("records", [])
        draft = j.get("draft_csv") or ""
        low = [r for r in recs if r.get("confidence") == "低"]
        ev.check("F6 普查解析（合成 batch，无 E 盘环境）",
                 "_status" not in j and j.get("count", 0) == 3
                 and len(j.get("suggestions", [])) >= 1
                 and draft.startswith("编号,") and len(draft.splitlines()) - 1 == 3
                 and len(low) == 1,
                 f"解析 {j.get('count', 0)} 条，警告 {len(j.get('warnings', []))} 条，"
                 f"归并建议 {len(j.get('suggestions', []))} 组，"
                 f"C 画质 → 低置信度 {len(low)} 条，草稿 CSV {len(draft)} 字符")
    ev.note("工作区产物", json.dumps(client.get(f"/api/projects/{pid}/artifacts").json(),
                                    ensure_ascii=False)[:300])
    return ev


def case_full76(client: TestClient) -> Evidence:
    ev = Evidence("验收场景 2 · 真实名册 76 只全量回归")
    if not REAL_ROSTER.exists():
        ev.check("读取 E 盘真实名册", False, f"{REAL_ROSTER} 不存在（U 盘未挂载）")
        return ev
    roster_text = read_text(REAL_ROSTER)
    rows, missing_cols, unknown_cols, _header = csv_loader.parse_roster(roster_text)
    ev.note("名册来源", f"{REAL_ROSTER.name}，{len(rows)} 行，"
                       f"缺列 {missing_cols or '无'}，未识别列 {unknown_cols or '无'}")

    raw_names = sorted({r.photo_file.strip() for r in rows if r.photo_file.strip()})
    prefixed = [n for n in raw_names if "/" in n or "\\" in n]
    ev.note("代表照片写法", f"{len(raw_names)} 个唯一值，其中 {len(prefixed)} 个带目录前缀"
                          f"（如 {prefixed[0] if prefixed else '—'}）")
    found, missing = find_real_photos(raw_names)
    if not ev.check("在 E 盘照片库定位真实照片", not missing,
                    f"命中 {len(found)} / {len(raw_names)}"
                    + (f"，缺失 {missing[:3]}" if missing else "")):
        return ev

    photos = [(name, p.read_bytes()) for name, p in sorted(found.items())]
    ev.note("上传体积", f"{len(photos)} 张原图共 {sum(len(b) for _, b in photos) / 1048576:.1f}MB")
    run_flow(client, ev, school="中北大学", subtitle="76 只校园猫 · 全量回归",
             roster_text=roster_text, photos=photos, expect_cats=len(rows))
    return ev


def _utf8_stdio() -> None:
    """stdout 被重定向到文件/管道时 Python 会用本机编码（这台机器是 GBK），
    脚本里的 ↔ ✅ ❌ 编不出去就中途 UnicodeEncodeError 崩——自己拨到 UTF-8，不指望调用方设环境变量。
    已经是 UTF-8 就不动：测试会直接调 main()，那时 stdout 是 pytest 的捕获流。"""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="喵星图工厂验收脚本")
    ap.add_argument("--case", choices=("demo2", "full76", "all"), default="all")
    args = ap.parse_args()

    client = TestClient(app)
    cases = [("demo2", case_demo2, "验收证据-场景1-空骨架2行.md"),
             ("full76", case_full76, "验收证据-场景2-全量76只.md")]
    picked = cases if args.case == "all" else [c for c in cases if c[0] == args.case]

    exit_code = 0
    for _key, fn, name in picked:
        ev = fn(client)
        path = ev.save(name)
        print(f"\n证据已写入 {path}\n")
        exit_code |= 1 if ev.failed else 0
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
