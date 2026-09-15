"""入口脚本自带 UTF-8 兜底：stdout 被重定向到文件/管道时，不许被本机 GBK 崩掉。

实测踩过（2026-09-07）：`python scripts/acceptance.py --case all > log 2>&1` 在本机把
stdout 退回 GBK，打到 `↔`（U+2194）时 UnicodeEncodeError 崩在 `Evidence.check` 的 print 里
——141 条验收只跑了 60 多条就死了；更阴的是复合命令最后那个 `echo "EXIT=$?"` 成功了，
后台任务照报 exit 0，崩溃被吞掉。CI 是靠 workflow 里的 `PYTHONUTF8=1` 侥幸躲过的，
本地重定向/管道没人兜。

三个入口脚本都含 GBK 编不出的字符（实测：acceptance `↔✅❌` / cleanup `⚠` / shot `↔`），
所以三个都要自己兜——不靠调用方设环境变量，也不在 import 期动手（那会污染 pytest 的捕获流）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# 每个脚本里 GBK 编不出的字符，逐个 encode('gbk') 数出来的，不是猜的
NON_GBK = {
    "acceptance": "↔✅❌",
    "cleanup": "⚠",
    "shot": "↔",
}


def _child_env() -> dict[str, str]:
    """把子进程钉死在 GBK 输出上，并摘掉任何可能替我们兜底的环境变量。"""
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    env["PYTHONIOENCODING"] = "gbk"
    return env


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        env=_child_env(),
        cwd=str(ROOT),
        timeout=120,
    )


def test_control_gbk_stdout_really_crashes_on_these_characters():
    """反向对照：不打兜底就确实崩。没有这条，下面的「通过」可能是白通过的。"""
    proc = _run("print('↔✅❌⚠')")
    assert proc.returncode != 0, "GBK 管道下打印这些字符本该崩，没崩说明对照失效"
    assert b"UnicodeEncodeError" in proc.stderr


def test_importing_a_script_does_not_touch_stdout_encoding():
    """兜底不能在 import 期生效：那会改掉 pytest 的捕获流，也会让下面的用例假通过。"""
    for name in NON_GBK:
        proc = _run(
            f"import sys; sys.path.insert(0, r'{SCRIPTS}'); sys.path.insert(0, r'{ROOT}');"
            f"import {name}; print(sys.stdout.encoding)"
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert proc.stdout.decode().strip().lower().replace("-", "") == "gbk", (
            f"{name}.py 在 import 期就把 stdout 改掉了"
        )


def test_guard_lets_each_script_print_its_own_non_gbk_characters():
    """打过兜底之后，同一批字符在 GBK 管道下也能原样出去（UTF-8 字节）。"""
    for name, chars in NON_GBK.items():
        proc = _run(
            f"import sys; sys.path.insert(0, r'{SCRIPTS}'); sys.path.insert(0, r'{ROOT}');"
            f"import {name} as m; m._utf8_stdio(); print({chars!r})"
        )
        assert proc.returncode == 0, f"{name}: {proc.stderr.decode('utf-8', 'replace')}"
        assert b"UnicodeEncodeError" not in proc.stderr
        assert proc.stdout.decode("utf-8").strip() == chars


def test_guard_is_called_at_the_top_of_every_entry_point():
    """能力不接到入口上等于没有：三个脚本的 main() 里都得真调一次。"""
    for name in NON_GBK:
        src = (SCRIPTS / f"{name}.py").read_text(encoding="utf-8")
        assert "def _utf8_stdio(" in src, f"{name}.py 没有兜底函数"
        start = src.index("def main(")
        body = src[start:]
        assert "_utf8_stdio()" in body.split("\ndef ", 1)[0], (
            f"{name}.py 的 main() 里没调 _utf8_stdio()——重定向照样崩"
        )
