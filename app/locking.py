"""项目级并发锁：同一项目的写操作串行，不同项目互不干扰。

为什么需要它——实测数据（24 个并发 PUT /calib，各写一个不同编号）：

    calib.positions   期望 24 → 实际 4      丢了 20 个
    操作日志          期望 24 → 实际 2      丢了 22 条
    project.json 读到半截 12 次 → load_meta 返回 None → 存在的项目回了 404

根因是每个路由都「load_meta → 改 → save_meta 整文件覆盖」，FastAPI 又把同步
路由丢进线程池并发跑。两个请求各自读到旧值，后写的把先写的整份盖掉。

锁只加在**写**路由上。读路由不需要：文件改成原子替换之后，读的人只会看到
完整的旧版或完整的新版，不存在半截状态，也就不必跟写操作抢锁。

作用域是单个 uvicorn 进程内的线程。跨进程（--workers N）不管——本项目是
学生本地单进程使用，为不存在的部署形态引入文件锁只会带来僵锁风险。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator

from fastapi import HTTPException

from . import config

_LOCKS: dict[str, threading.Semaphore] = {}
_REGISTRY = threading.Lock()


def project_lock(pid: str) -> threading.Semaphore:
    """取（必要时建）某个项目的锁。

    用 ``Semaphore(1)`` 而不是 ``RLock``，是踩过坑的：FastAPI 把「带 yield 的
    同步依赖」的 __enter__ 和 __exit__ 分两次丢进线程池，并发一上来这两次就
    会落在**不同**的 AnyIO worker 线程上（线程名一样，身份不一样）。RLock 只
    认持有者，于是退出时抛 RuntimeError: cannot release un-acquired lock——
    更糟的是许可没还回去，这个项目从此每个写操作都 409，直到重启进程。
    信号量没有「持有者」概念，任何线程都能 release。

    代价是不可重入：guard 不能套 guard，否则第二次 acquire 会一直等到超时。
    当前只有路由挂 guard、辅助函数不挂，不存在嵌套；将来要在辅助函数里加锁，
    得先想清楚这一点。

    注册表只增不减：删项目时不摘锁。摘了的话，正持有锁的线程还没走完，
    新请求就会拿到一把全新的锁并立刻进去，等于同一个项目两把锁——
    比留着几百字节的空锁危险得多。
    """
    with _REGISTRY:
        lock = _LOCKS.get(pid)
        if lock is None:
            lock = _LOCKS[pid] = threading.Semaphore(1)
        return lock


def lock_count() -> int:
    """注册表里有多少把锁（给测试和诊断用）。"""
    with _REGISTRY:
        return len(_LOCKS)


def held_by_others(pid: str) -> bool:
    """项目锁当前是否被占着。只用于诊断，别拿它做判断——返回后状态就可能变了。

    名字里的「others」已经名不副实：信号量不记持有者，所以自己拿着锁时它也
    回 True。想问的其实是「现在忙不忙」。
    """
    lock = project_lock(pid)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def project_guard(pid: str) -> Iterator[None]:
    """FastAPI 依赖：把整个请求圈在本项目的锁里。

    拿不到锁就回 409，而不是一直挂着——用户在第二个标签页点了上传，
    第一个标签页正在生成 76 只猫的星图，这时候该给一句人话，不是转圈。

    超时值每次调用时从 config 读，不写成默认参数：默认参数在函数定义时
    就绑定了，测试没法把它调小来触发 409 分支。
    """
    lock = project_lock(pid)
    if not lock.acquire(timeout=config.PROJECT_LOCK_TIMEOUT):
        raise HTTPException(
            409, f"项目正忙：另一个操作（上传 / 校验 / 生成）还没结束，"
                 f"已等 {config.PROJECT_LOCK_TIMEOUT:.0f} 秒。请稍后重试。")
    try:
        yield
    finally:
        lock.release()
