"""路径、全局常量与原子写文件的底座。"""
import errno
import os
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
TEMPLATE_DIR = APP_DIR / "templates"
STARMAP_TEMPLATE = TEMPLATE_DIR / "starmap.html"
STATIC_DIR = ROOT / "static"
WORKSPACE = ROOT / "workspace"

MAX_PHOTO_SIDE = 1200
MAX_PHOTO_BYTES = 200 * 1024
MAX_MAP_SIDE = 1920

# F11 校徽。页面上只占 34px 见方，存 256px 已经留了 7 倍余量；原图上限卡 4MB，
# 免得一个 3000dpi 的印刷稿在 Pillow 打开之前就把内存吃掉。
# LOGO_EXTS 故意不含 .svg——SVG 能带 <script>，而产物是要挂到公众号上的。
MAX_LOGO_SIDE = 256
MAX_LOGO_UPLOAD_BYTES = 4 * 1024 * 1024
LOGO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

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

# 项目锁的等待上限。必须长于最慢的一次操作（76 张照片压缩 + 生成约 1 分钟），
# 否则用户在第二个标签页点一下就会被误判成「项目正忙」。真卡死了才该报 409。
PROJECT_LOCK_TIMEOUT = 180.0

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


def tmp_sibling(dest: Path) -> Path:
    """目标文件同目录下的唯一临时名。

    同目录是硬要求：os.replace 只在同一个卷内才是原子的，跨卷会退化成
    「复制 + 删除」，读的人照样能撞见半截文件。名字带 pid 和随机串，
    这样两个并发写者不会互相踩掉对方的临时文件。
    """
    return dest.with_name(f".{dest.name}.tmp-{os.getpid()}-{uuid4().hex}")


# Windows 的替换重试参数。写死在模块级是为了测试能调小——
# 真要验证重试分支，总不能让测试等 1.2 秒。
REPLACE_ATTEMPTS = 60
REPLACE_SLEEP = 0.02

# 共享冲突：文件被别人开着。winerror 32/33 是 Windows 的 sharing violation，
# 在 Python 里也会以 PermissionError(EACCES) 冒出来。
_RETRY_ERRNOS = {errno.EACCES, errno.EPERM}
_RETRY_WINERRORS = {5, 32, 33}


def atomic_replace(tmp: Path, dest: Path) -> None:
    """os.replace 的重试版。

    Windows 上目标文件只要被别的句柄开着——并发的读请求、杀毒软件扫描、
    搜索索引——替换就会被拒（WinError 5 / 32）。POSIX 没这问题，rename
    覆盖一个正被读的文件是合法的。实测 4 个线程狂读 project.json 时，
    写入方稳定撞上，所以这不是理论风险。

    读者只持有句柄几微秒，短睡 + 有限次重试就能错开；重试耗尽仍失败
    说明是真问题（文件只读、权限不对），原样抛出去让人看见。
    """
    last: OSError | None = None
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, dest)
            return
        except OSError as exc:
            retryable = (exc.errno in _RETRY_ERRNOS
                         or getattr(exc, "winerror", None) in _RETRY_WINERRORS)
            if not retryable:
                raise
            last = exc
            time.sleep(REPLACE_SLEEP)
    raise last  # type: ignore[misc]


def atomic_write_bytes(dest: Path, data: bytes) -> None:
    """先写临时文件、再原子替换——读的人永远只会看到完整的旧版或新版。

    实测过不加这层会怎样：并发读 project.json 有 12/800 次读到半截，
    load_meta 返回 None，于是一个明明存在的项目对客户端回了 404。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_sibling(dest)
    try:
        tmp.write_bytes(data)
        atomic_replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(dest: Path, text: str, encoding: str = "utf-8",
                      newline: str | None = None) -> None:
    """atomic_write_bytes 的文本版，newline 语义与 Path.write_text 一致。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_sibling(dest)
    try:
        with open(tmp, "w", encoding=encoding, newline=newline) as fh:
            fh.write(text)
        atomic_replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# 读侧重试参数。同样放模块级，方便测试调小。
READ_ATTEMPTS = 40
READ_SLEEP = 0.005


def _retry_on_sharing(read):
    """撞上 Windows 共享冲突就重试，别的异常原样抛。

    实测（4 线程狂读 + 1 线程原子替换 project.json，6.2 秒）：
        读成功 92613 次 · 读到半截 0 次 · PermissionError(errno 13) 573 次
    半截是 0，说明原子替换确实生效了；那 573 次是写方 os.replace 的一瞬间
    读方 open 被拒。这不是文件损坏，重试几毫秒就能读到完整的旧版或新版。

    关键在于别把它当损坏处理：load_meta 从前一律 except → None，
    于是一个好好存在的项目会凭空从下拉框里消失，甚至对客户端回 404。
    """
    last: OSError | None = None
    for _ in range(READ_ATTEMPTS):
        try:
            return read()
        except OSError as exc:
            retryable = (exc.errno in _RETRY_ERRNOS
                         or getattr(exc, "winerror", None) in _RETRY_WINERRORS)
            if not retryable:
                raise          # FileNotFoundError(ENOENT) 走这里，交给调用方判断
            last = exc
            time.sleep(READ_SLEEP)
    raise last  # type: ignore[misc]


def retry_read_bytes(dest: Path) -> bytes:
    """读二进制，撞上共享冲突自动重试。文件不存在则抛 FileNotFoundError。"""
    dest = Path(dest)
    return _retry_on_sharing(dest.read_bytes)


def retry_read_text(dest: Path, encoding: str = "utf-8") -> str:
    """读文本，撞上共享冲突自动重试。文件不存在则抛 FileNotFoundError。"""
    dest = Path(dest)
    return _retry_on_sharing(lambda: dest.read_text(encoding=encoding))
