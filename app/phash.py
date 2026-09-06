"""感知哈希（高通 dHash）· 给 F9 归并工作台做「先看谁」的视觉预排序。

只用来排序，不用来下结论：判定是不是同一只猫仍然是人的事（数据红线），哈希错了
最多让人多看两眼，不会替人改名册。

为什么在 dHash 前面加一道高通（减掉高斯模糊的自己）——这是量出来的，不是拍脑袋：
dHash 只问「左边的像素比右边暗吗」，而普查照片里背景占了大半画面，同一处石台、
同一条走廊拍的两只**不同**的猫，渐变背景会把这些位全填成 1。合成场景实测：

    | 比对                        | 纯 dHash | 高通 dHash |
    | 同图 → 半尺寸 + q70 重压    |  1.000   |   1.000    |
    | 同图 → 1/4 尺寸 + q50 重压  |  1.000   |   0.953    |
    | 橘猫 vs 三花（同背景）      |  0.844   |   0.625    |

纯 dHash 的分辨间距只有 0.16，排不出先后；高通之后是 0.33，重压副本仍稳稳认得。
64 位 = 8×8 差分，一次比对就是一个异或加 popcount，几十只猫两两比也是微秒级。

两种情况返回 None：字节解不开，以及高通之后不剩结构（见 `_dhash` 的守卫）。
两者都只让那只猫在排序里沉底，不影响候选组本身。
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

HASH_BITS = 64
MID_SIZE = 64        # 高通在这一步做：够留下主体轮廓，又不至于为一张缩略图跑大图卷积
BLUR_RADIUS = 2.0    # 实测 4.0（配 128）分辨间距略大，但重压副本会掉到 0.969，不划算
# 9×8 高通残差离 128 不足这么多，就算「这张图没有结构」。实测：纯色 0、整片过曝 0、
# 合成高频噪点 0~1，而有结构的合成场景 6~13（真实照片的边缘只会更强）。
MIN_CONTRAST = 2


def _dhash(img: Image.Image) -> int | None:
    """9×8 高通灰度 → 每行 8 个「左比右暗吗」→ 64 位；没有结构就返回 None。

    这道守卫是必需的：纯色图、整片过曝、高频噪点在高通之后什么都不剩，比较结果
    全是 False，指纹退化成 0——于是一张白图和一张黑图会报「视觉相似度 100%」，
    还被排到候选组最前面。宁可说算不出，让它在排序里沉底由人自己看。
    """
    g = img.convert("L").resize((MID_SIZE, MID_SIZE))
    hp = ImageChops.subtract(g, g.filter(ImageFilter.GaussianBlur(BLUR_RADIUS)), 1, 128)
    px = hp.resize((9, 8), Image.LANCZOS).tobytes()
    if max(128 - min(px), max(px) - 128) < MIN_CONTRAST:
        return None
    h = 0
    for y in range(8):
        row = y * 9
        for x in range(8):
            h = (h << 1) | (px[row + x] < px[row + x + 1])
    return h


def dhash_bytes(data: bytes) -> int | None:
    """解不开的字节返回 None——坏图和缺图一样，都只该让那只猫排不上序。"""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            return _dhash(img)
    except Exception:      # 用户上传什么都有可能：截断的 JPEG、改了扩展名的文本
        return None


def dhash_file(path: Path) -> int | None:
    try:
        with Image.open(path) as img:
            img.load()
            return _dhash(img)
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def visual_similarity(a: int | None, b: int | None) -> float:
    """0~1，1 表示两张图的 64 位指纹完全一致。任一方没有哈希就是 0。"""
    if a is None or b is None:
        return 0.0
    return 1 - hamming(a, b) / HASH_BITS
