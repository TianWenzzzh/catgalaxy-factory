"""路径与全局常量。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
TEMPLATE_DIR = APP_DIR / "templates"
STARMAP_TEMPLATE = TEMPLATE_DIR / "starmap.html"
STATIC_DIR = ROOT / "static"
WORKSPACE = ROOT / "workspace"

MAX_PHOTO_SIDE = 1200
MAX_PHOTO_BYTES = 200 * 1024
MAX_MAP_SIDE = 1920
DEFAULT_MAP_W = 1920                  # 底图逻辑尺寸兜底值（无真实底图时使用）
DEFAULT_MAP_H = 1239
PHOTO_CHUNK_BYTES = 1_500_000  # base64 分片大小（inline 形态）

# 上传解包预算。zip 表头里的 file_size 由上传者填写、不可信，
# 所有限额都在流式解压时实时累加判定，超了就停手，不做「先解压完再说」。
MAX_UPLOAD_BYTES = 200 * 1024 * 1024          # 单个上传文件（含 zip）体积上限
MAX_PHOTOS_PER_UPLOAD = 2000                  # 单次请求最多入库张数
MAX_PHOTOS_PER_PROJECT = 3000                 # 项目累计张数上限（×200KB ≈ 600MB 磁盘）
MAX_ZIP_ENTRIES = 20000                       # 单个 zip 条目数上限（防百万条目拖死遍历）
MAX_ZIP_INFLATED_BYTES = 1024 * 1024 * 1024   # 单次请求累计解压字节上限（防 zip 炸弹）
MAX_ZIP_DEPTH = 3                             # 嵌套 zip 递归层数（zip 套 zip 套 zip）
ZIP_READ_CHUNK = 1024 * 1024                  # 流式解压块大小

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

ROSTER_COLUMNS = [
    "编号", "昵称", "军衔", "工位", "毛色", "特征描述",
    "代表照片文件", "照片数量", "出没区域", "关联照片编号", "置信度", "备注",
]

CONFIDENCE_LEVELS = ("高", "中", "低")


def project_dir(pid: str) -> Path:
    return WORKSPACE / pid


def ensure_dirs(pid: str) -> dict:
    """创建并返回项目的目录结构。"""
    d = project_dir(pid)
    photos = d / "assets" / "photos"
    out = d / "out"
    dist = d / "dist"
    data = d / "data"
    for p in (photos, out, dist, data):
        p.mkdir(parents=True, exist_ok=True)
    return {"root": d, "photos": photos, "out": out, "dist": dist, "data": data}
