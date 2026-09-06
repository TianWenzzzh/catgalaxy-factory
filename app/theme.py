"""F11 · 星图主题化：配色、字体、页脚署名。

产物是要拷给别人、双击就能打开的，所以有两条硬约束：

1. **不能引用任何外部资源。** 字体只能用系统里本来就有的，不打包 webfont——
   一个中文字体子集几 MB 起步，版权也说不清。所以这里给的是「字体栈」：
   列几个常见字体名，机器上有哪个用哪个，都没有就退到系统默认。
   同一份产物在不同机器上长相会有细微差别，这是取舍，不是 bug。

2. **主题值会被拼进 <style> 和 HTML，所以只接受严格白名单。** 颜色必须是
   #rgb / #rrggbb，字体只能是预设 key，署名一律 HTML 转义。放行自由文本
   等于给产物开一个注入口子——而产物是要挂到学校公众号上的。

预设全是深色底。不是审美偏好：星点是亮色，canvas 上画的是发光的小圆点，
换成浅色底就是一片看不见星星的白。要做浅色版得连星点配色和辉光一起重画，
不在这次范围里。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# #rgb / #rrggbb / #rrggbbaa。多一个字符都不行——CSS 值里能塞 } 和 ; 就能越狱。
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

# 用户能改的颜色键 → 模板里的 CSS 变量名
COLOR_VARS = {
    "bg": "--bg",
    "ink": "--ink",
    "dim": "--dim",
    "accent": "--gold",
    "accent_soft": "--gold-soft",
    "teal": "--teal",
    "mag": "--mag",
    "stats_ink": "--stats-ink",
}

# 字体栈。key 是白名单，value 才是真正写进 CSS 的东西——用户永远碰不到 value，
# 所以这里可以放心写引号和逗号。
FONTS: dict[str, str] = {
    "system": '"PingFang SC","Microsoft YaHei","Hiragino Sans GB",system-ui,sans-serif',
    "song": '"Songti SC","SimSun","Noto Serif CJK SC",serif',
    "kai": '"Kaiti SC","KaiTi","STKaiti",serif',
    "hei": '"SimHei","Heiti SC","Microsoft YaHei",sans-serif',
    "rounded": '"Yuanti SC","YouYuan","Microsoft YaHei Rounded",sans-serif',
}

FONT_LABELS = {
    "system": "系统黑体（默认）",
    "song": "宋体 · 校史馆气质",
    "kai": "楷体 · 手写温度",
    "hei": "黑体 · 粗重醒目",
    "rounded": "圆体 · 亲和",
}

COLOR_LABELS = {
    "bg": "夜空底色",
    "ink": "正文主色",
    "dim": "次要文字",
    "accent": "强调色（星星选中环）",
    "accent_soft": "标题色",
    "teal": "连线色",
    "mag": "图例辅色",
    "stats_ink": "统计行文字",
}

MAX_SIGNATURE = 120


@dataclass
class Preset:
    key: str
    label: str
    blurb: str
    colors: dict[str, str]
    line: str                 # 描边，带透明度，只能由预设给（用户改不了 rgba）
    panel: str                # 面板底
    glow: str = ""            # 标题辉光，留空则从 accent 自动算


PRESETS: dict[str, Preset] = {p.key: p for p in (
    Preset("midnight", "子夜金（默认）", "深蓝夜空 + 暖金星星，最耐看的一套",
           dict(bg="#070c1c", ink="#e8efff", dim="#7e92c4", accent="#ffd76a",
                accent_soft="#ffe9a8", teal="#7de8d8", mag="#c48cff",
                stats_ink="#9fb2e0"),
           "rgba(140,170,255,.16)", "rgba(10,18,40,.86)"),
    Preset("aurora", "极光青", "偏冷的青绿，投在教室大屏上更清爽",
           dict(bg="#04121a", ink="#e6fbff", dim="#79a9bb", accent="#6ef2c3",
                accent_soft="#b6ffe4", teal="#63d7ff", mag="#a78bfa",
                stats_ink="#8fc6d6"),
           "rgba(120,220,230,.16)", "rgba(6,24,34,.86)"),
    Preset("dawn", "晨曦橘", "暖橘调，黄昏喂猫那批照片最配",
           dict(bg="#1a0d09", ink="#fff1e6", dim="#c49a86", accent="#ff9d5c",
                accent_soft="#ffc79a", teal="#ffd166", mag="#ef7d9f",
                stats_ink="#d8ab93"),
           "rgba(255,180,140,.16)", "rgba(34,18,12,.86)"),
    Preset("sakura", "夜樱粉", "樱花季的粉，配浅色毛的猫",
           dict(bg="#160a14", ink="#ffeef6", dim="#b98aa8", accent="#ff9ec7",
                accent_soft="#ffc9de", teal="#9be7d4", mag="#c9a0ff",
                stats_ink="#cd9fbb"),
           "rgba(255,160,210,.16)", "rgba(30,14,28,.86)"),
    Preset("ink", "墨青书院", "低饱和墨色，配校徽和书法字最稳",
           dict(bg="#0b1013", ink="#eef3f2", dim="#8fa3a0", accent="#d9c27e",
                accent_soft="#eee0b4", teal="#86b8ae", mag="#9d8ec4",
                stats_ink="#a3b4b1"),
           "rgba(150,190,185,.16)", "rgba(14,22,26,.86)"),
)}

DEFAULT_PRESET = "midnight"


@dataclass
class Theme:
    """一个项目的主题配置。没配过就用 DEFAULT_PRESET，产物长相与旧版一致。"""

    preset: str = DEFAULT_PRESET
    colors: dict[str, str] = field(default_factory=dict)   # 覆盖预设的部分颜色
    title_font: str = "system"
    body_font: str = "system"
    footer_signature: str = ""
    updated_at: str = ""

    # ---------- 校验 ----------

    def normalized(self) -> "Theme":
        """把非法值挡在门外，返回一个干净的副本。

        预设名不认识就退回默认——用户的 theme.json 可能是手改的，也可能是
        旧版本留下的；直接抛错会让一个明明能生成的项目卡在 500。
        """
        preset = self.preset if self.preset in PRESETS else DEFAULT_PRESET
        colors = {k: v.strip() for k, v in self.colors.items()
                  if k in COLOR_VARS and isinstance(v, str) and _HEX.match(v.strip())}
        sig = re.sub(r"\s+", " ", (self.footer_signature or "")).strip()[:MAX_SIGNATURE]
        return Theme(
            preset=preset,
            colors=colors,
            title_font=self.title_font if self.title_font in FONTS else "system",
            body_font=self.body_font if self.body_font in FONTS else "system",
            footer_signature=sig,
            updated_at=self.updated_at,
        )

    def rejected(self) -> list[str]:
        """哪些用户输入被丢掉了，用来回给前端说人话。"""
        out = []
        if self.preset not in PRESETS:
            out.append(f"配色预设「{self.preset}」不存在，已退回默认")
        for k, v in self.colors.items():
            if k not in COLOR_VARS:
                out.append(f"颜色项「{k}」不支持，已忽略")
            elif not isinstance(v, str) or not _HEX.match(v.strip()):
                out.append(f"颜色值「{v}」不是合法的 #RRGGBB，已忽略")
        if self.title_font not in FONTS:
            out.append(f"标题字体「{self.title_font}」不在白名单，已退回系统黑体")
        if self.body_font not in FONTS:
            out.append(f"正文字体「{self.body_font}」不在白名单，已退回系统黑体")
        if len((self.footer_signature or "").strip()) > MAX_SIGNATURE:
            out.append(f"页脚署名超过 {MAX_SIGNATURE} 字，已截断")
        return out

    # ---------- 生效值 ----------

    def effective_colors(self) -> dict[str, str]:
        base = dict(PRESETS[self.preset].colors)
        base.update(self.colors)
        return base

    def glow_rgba(self) -> str:
        """标题辉光：从 accent 算出同色的半透明 rgba。

        不让用户直接填 rgba，是因为 rgba() 里能塞任意字符（包括 `)` 和 `;`），
        那就不是颜色而是 CSS 注入了。给我一个合法的 hex，我来算。
        """
        p = PRESETS[self.preset]
        if p.glow:
            return p.glow
        return _hex_to_rgba(self.effective_colors().get("accent", "#ffd76a"), .34)

    def scrim(self) -> tuple[str, str]:
        """顶栏渐变遮罩 (顶部, 中部)。

        模板里原本写死 rgba(5,9,22,.94)——那是深蓝。换成晨曦橘预设之后，暖色
        星空顶上压一条深蓝遮罩会脏。所以从 bg 派生：暗化系数 0.78 与模板 canvas
        的天空顶色一致，默认预设下算出来正好是原来那两个值。
        """
        rgb = _hex_rgb(self.effective_colors().get("bg", "#070c1c")) or (7, 12, 28)
        dark = _shade(rgb, .78)
        return _rgba(dark, .94), _rgba(dark, .55)


def _hex_rgb(hex_color: str) -> tuple[int, int, int] | None:
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 8:                 # #rrggbbaa：alpha 由我们自己算，丢掉它
        h = h[:6]
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return None
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _shade(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return (max(0, min(255, round(rgb[0] * factor))),
            max(0, min(255, round(rgb[1] * factor))),
            max(0, min(255, round(rgb[2] * factor))))


def _rgba(rgb: tuple[int, int, int], alpha: float) -> str:
    return f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{alpha})"


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    rgb = _hex_rgb(hex_color)
    if rgb is None:
        return f"rgba(255,215,106,{alpha})"      # 兜底：默认金色
    return _rgba(rgb, alpha)


def css_block(theme: Theme | None) -> str:
    """生成一段覆盖用的 <style>。没有自定义（默认预设 + 默认字体 + 无覆盖）
    就返回空串——产物里不该凭空多一段和模板重复的 CSS。"""
    t = (theme or Theme()).normalized()
    p = PRESETS[t.preset]
    is_default = (t.preset == DEFAULT_PRESET and not t.colors
                  and t.title_font == "system" and t.body_font == "system")
    if is_default:
        return ""

    colors = t.effective_colors()
    decls = [f"{COLOR_VARS[k]}:{v}" for k, v in colors.items() if k in COLOR_VARS]
    decls.append(f"--line:{p.line}")
    decls.append(f"--panel:{p.panel}")
    decls.append(f"--title-glow:{t.glow_rgba()}")
    top, mid = t.scrim()
    decls.append(f"--scrim-top:{top}")
    decls.append(f"--scrim-mid:{mid}")

    css = "/* 主题：由喵星图工厂按项目配置注入 */\n:root{" + ";".join(decls) + "}\n"
    if t.body_font != "system":
        css += f'html,body{{font-family:{FONTS[t.body_font]}}}\n'
    if t.title_font != "system":
        css += f"h1,.sub{{font-family:{FONTS[t.title_font]}}}\n"
    return f"<style>\n{css}</style>"


def preset_menu() -> list[dict]:
    """给前端下拉框用的预设清单，带上色块，免得只能看名字猜。"""
    return [{"key": p.key, "label": p.label, "blurb": p.blurb,
             "swatch": [p.colors["bg"], p.colors["accent"], p.colors["teal"],
                        p.colors["mag"]]}
            for p in PRESETS.values()]


def font_menu() -> list[dict]:
    return [{"key": k, "label": FONT_LABELS[k]} for k in FONTS]


def color_menu() -> list[dict]:
    return [{"key": k, "label": COLOR_LABELS.get(k, k), "css": v}
            for k, v in COLOR_VARS.items()]
