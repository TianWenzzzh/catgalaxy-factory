"""pytest 根配置：把项目根加入 sys.path，并提供公共夹具。"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, store  # noqa: E402

# 真实数据总库的根：STARMAP_HOME 优先，缺省这台 Windows 机的 E 盘。
# Linux 上满血回归：export STARMAP_HOME=<总库挂载点>（07 普查/08 照片/09 底图在其下）；
# 不设也没事，依赖总库的用例会按路径不存在自动跳过。
E_ROOT = Path(os.environ.get("STARMAP_HOME") or r"E:\猫咪星图_总库")


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    """把运行时工作区指向临时目录，避免污染真实 workspace/。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", ws)
    monkeypatch.setattr(store, "WORKSPACE", ws)
    return ws


def make_jpeg(width: int, height: int, color=(200, 140, 60), quality: int = 95) -> bytes:
    """造一张测试用 JPEG（带噪点，便于检验压缩真实生效）。"""
    img = Image.new("RGB", (width, height), color)
    px = img.load()
    for y in range(0, height, 3):
        for x in range(0, width, 3):
            px[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 5) % 256)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


@pytest.fixture
def photo_factory():
    return make_jpeg


CSV_HEADER = ("编号,昵称,军衔,工位,毛色,特征描述,代表照片文件,照片数量,"
              "出没区域,关联照片编号,置信度,备注")


def make_csv(rows: list[list[str]], header: str = CSV_HEADER) -> str:
    lines = [header]
    for r in rows:
        lines.append(",".join(f'"{c}"' if ("," in str(c) or '"' in str(c)) else str(c) for c in r))
    return "\n".join(lines) + "\n"


@pytest.fixture
def csv_factory():
    return make_csv


@pytest.fixture
def two_row_csv():
    """与 12_跨校复制包 骨架对齐的 2 行示例数据。"""
    return make_csv([
        ["CAT-001", "墩墩", "喵校长", "总揽全校猫务", "全橘虎斑",
         "体型胖 粉鼻 侧躺露肚", "demo-001.jpg", "1", "宿舍楼前石台",
         "batch1:DEMO", "高", "示例数据"],
        ["CAT-002", "格子", "中士", "宿舍内务员", "橘白",
         "橘头橘背白胸腹 常钻桌布下", "demo-002.jpg", "3", "教学楼走廊",
         "batch1:DEMO", "中", "同场景连拍确认"],
    ])
