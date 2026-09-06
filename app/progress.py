"""长任务进度：给前端一个可轮询的出口，别让人对着一个 Toast 干等。

76 张照片的压缩要几十秒。这期间请求没回来、界面一动不动，用户的合理推断是
「卡死了」，然后去刷新页面——而刷新正好打断上传，几十秒白等。所以进度必须
是「服务端写、前端轮询」，光靠浏览器只能知道字节传完了、不知道压缩到哪了。

进度落在 workspace/{pid}/progress.json，走 atomic_write_text，因此
GET /progress 是读路由、不挂项目锁也安全（理由见 locking.py）。

进度只是参考，权威信号永远是那个 HTTP 请求回来了没有：服务端要是崩了，
progress.json 会永远停在 running。所以每条记录都带 updated_at，前端发现
好几秒没推进就改口说「可能已中断」，而不是继续假装在走。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from .config import atomic_write_text, project_dir, retry_read_text

# 落盘节流间隔（秒）。前端轮询本来就有 300ms 间隔，每张照片都写一次纯属浪费。
FLUSH_INTERVAL = 0.15

# 超过这个秒数没推进，前端就该怀疑任务已经死了。取值要大于最慢的一步：
# 单张照片的解码+缩放+编码实测最坏约 1.5 秒，打 zip 的十几秒是整段无进展的，
# 所以给到 20 秒——既不会在正常压缩中误报，也不至于让人对着死进度条等一分钟。
STALE_AFTER = 20.0

IDLE: dict = {"task": "", "state": "idle", "done": 0, "total": None,
              "fraction": None, "note": "", "message": "", "run_id": "",
              "started_at": 0.0, "updated_at": 0.0, "elapsed": 0.0}


def progress_path(pid: str) -> Path:
    return project_dir(pid) / "progress.json"


def read(pid: str) -> dict:
    """读进度。没有记录（这个项目从没跑过长任务）就回 IDLE。"""
    try:
        text = retry_read_text(progress_path(pid))
    except FileNotFoundError:
        return dict(IDLE)
    try:
        rec = json.loads(text)
    except Exception:
        return dict(IDLE)          # 记录写坏了，当成没进度，别让前端崩
    rec.setdefault("state", "idle")
    rec.setdefault("run_id", "")
    return rec


def snapshot_for_client(pid: str) -> dict:
    """给轮询接口用的记录：额外带上服务端当前时间与「多久算卡死」的阈值。

    带 server_now 是为了让前端不必去比自己的本地时钟——学生机器的时间可能不准，
    拿 Date.now() 减 updated_at 会算出负数或者离谱地大的「无进展秒数」，
    于是一个好好在跑的任务被报成已中断。
    """
    rec = read(pid)
    rec["server_now"] = time.time()
    rec["stale_after"] = STALE_AFTER
    return rec


class Reporter:
    """一次长任务的进度记录器。

    用 ``with`` 把整段任务包住：异常路径也会被标成 failed。少了这一层，
    一个 413 回滚或一次 Pillow 抛错就会把 progress.json 永远留在 running，
    下一个打开页面的人会看到一条走不完的进度条。
    """

    def __init__(self, pid: str, task: str, total: Optional[int] = None):
        self.pid = pid
        self.task = task
        self.total = total
        self.done = 0
        self.note = ""
        self.message = ""
        self.state = "running"
        self.started = time.time()
        self.updated = self.started
        self._last_flush = 0.0
        # 前端是在自己那个请求还没回来时就开始轮询的，磁盘上的记录有可能是
        # 上一轮留下的（服务端崩了就永远停在 running）。给它一个 run_id，
        # 客户端才能分清「这次的任务」和「上次的尸体」。
        self.run_id = uuid4().hex[:12]

    # ---------- 推进 ----------

    def bump(self, note: str = "") -> None:
        """完成一项。note 会显示在进度条上，比如正在压的那个文件名。"""
        self.done += 1
        if note:
            self.note = note
        self._flush(throttle=True)

    def grow_total(self, n: int) -> None:
        """总量比预想的多——zip 展开之前不知道里面有多少张。

        累加而不是覆盖：嵌套 zip 会一层层报上来。
        """
        if n > 0:
            self.total = (self.total or 0) + n
            self._flush(throttle=True)

    def drop_total(self, n: int = 1) -> None:
        """从预期总量里扣掉几项。

        上传一个 zip 时初始 total 按「文件个数」算，zip 自己占了一格，但它不是
        一张照片、永远不会被 bump。不扣掉的话 76 张的包会显示成 76/77，
        进度条差一格走到头。
        """
        if self.total:
            self.total = max(0, self.total - n)
            self._flush(throttle=True)

    def stage(self, note: str) -> None:
        """阶段型任务（生成星图那种没有「第几张」概念的）用它推进。"""
        self.bump(note)

    @property
    def fraction(self) -> Optional[float]:
        """0~1 的完成度。总量未知时返回 None，前端据此画不确定态条纹。"""
        if not self.total or self.total <= 0:
            return None
        return min(1.0, self.done / self.total)

    def as_callback(self) -> Callable[[str], None]:
        """给 image_proc.ZipBudget.on_item 用的签名适配器。"""
        return self.bump

    def snapshot(self) -> dict:
        return {"task": self.task, "state": self.state, "done": self.done,
                "total": self.total, "fraction": self.fraction, "note": self.note,
                "message": self.message, "run_id": self.run_id,
                "started_at": round(self.started, 3),
                "updated_at": round(self.updated, 3),
                "elapsed": round(self.updated - self.started, 2)}

    # ---------- 落盘 ----------

    def _flush(self, *, throttle: bool) -> None:
        now = time.time()
        # 只在 running 时节流。done/failed 是终态，必须立刻可见——
        # 否则前端轮询到最后一次可能还停在 97%，然后请求回来了，看着像跳变。
        if throttle and self.state == "running" and now - self._last_flush < FLUSH_INTERVAL:
            return
        self.updated = now
        self._last_flush = now
        atomic_write_text(progress_path(self.pid),
                          json.dumps(self.snapshot(), ensure_ascii=False))

    def finish(self, message: str = "") -> None:
        self.state = "done"
        self.message = message
        if self.total:
            self.done = self.total      # 跳过几个的情况别让条子停在 96%
        self._flush(throttle=False)

    def fail(self, message: str) -> None:
        self.state = "failed"
        self.message = message
        self._flush(throttle=False)

    # ---------- with ----------

    def __enter__(self) -> "Reporter":
        self._flush(throttle=False)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            if self.state == "running":
                self.finish()
        elif self.state == "running":
            self.fail(f"{exc_type.__name__}: {exc}")
        # 返回 None：不吞异常，HTTPException 该回什么状态码还回什么
