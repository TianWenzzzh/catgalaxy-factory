"""CI 配置检查。

这套工作流的存在理由很具体：本机是 Windows + 有 E 盘总库，而 CI 是干净的
Linux/Windows runner、**没有** E 盘。两边环境差得越远，「本地绿、CI 红」或者
反过来「CI 绿但其实少测了一半」的概率就越高。所以这里盯三件事：

1. 工作流确实跑了 pytest 和 acceptance demo2（不是只跑其中一个）；
2. Windows 在矩阵里——原子写重试认 WinError 32/33、路径分隔符、GBK 控制台，
   这些坑只有 Windows runner 能踩到，而交付对象就是 Windows 用户；
3. acceptance 的 E 盘依赖是有兜底的，谁哪天加了一处硬依赖，CI 会红在这里，
   而不是红在一个看不懂的 404 上。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ACCEPTANCE = (ROOT / "scripts" / "acceptance.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wf() -> dict:
    assert WORKFLOW.exists(), "缺 .github/workflows/ci.yml"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps_of(wf: dict) -> list[dict]:
    return wf["jobs"]["test"]["steps"]


def run_commands(wf: dict) -> str:
    return "\n".join(s.get("run", "") for s in steps_of(wf))


# ---------- 工作流形状 ----------

def test_workflow_parses_and_triggers_on_push_and_pr(wf):
    # PyYAML 把裸 `on:` 解析成布尔 True（YAML 1.1 的 yes/no/on/off 遗产）
    triggers = wf.get("on", wf.get(True))
    assert triggers is not None, "工作流没有触发条件"
    assert "push" in triggers and "pull_request" in triggers


def test_ci_runs_both_pytest_and_the_acceptance_script(wf):
    """只跑 pytest 会漏掉打包产物那一层；只跑 acceptance 会漏掉单元层。"""
    cmds = run_commands(wf)
    assert re.search(r"python -m pytest", cmds), "CI 没跑 pytest"
    assert "scripts/acceptance.py --case demo2" in cmds, "CI 没跑验收脚本 demo2"


def test_ci_does_not_attempt_the_e_drive_case(wf):
    """full76 要读 E 盘总库的真实名册和 75 张原图，CI 上没有，跑了只会红。"""
    assert "full76" not in run_commands(wf)
    assert "--case all" not in run_commands(wf)


def test_windows_is_in_the_matrix(wf):
    oses = wf["jobs"]["test"]["strategy"]["matrix"]["os"]
    assert any("windows" in o for o in oses), f"矩阵里没有 Windows：{oses}"


def test_ci_forces_utf8_io(wf):
    """Windows runner 默认 GBK 控制台，验收脚本满屏中文会 UnicodeEncodeError。"""
    env = wf["jobs"]["test"].get("env", {})
    assert env.get("PYTHONIOENCODING") == "utf-8" or env.get("PYTHONUTF8") == "1"


def test_install_command_matches_declared_dev_extra(wf):
    """`pip install -e ".[dev]"` 得真有一个 dev extra，且里面有 pytest。"""
    assert re.search(r'pip install -e "\.\[dev\]"', run_commands(wf))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dev = re.search(r"dev\s*=\s*\[(.*?)\]", pyproject, re.S).group(1)
    assert "pytest" in dev, f"dev extra 里没有 pytest：{dev}"
    # 工作流要用 PyYAML 之外的东西解析？不，是本测试要 yaml，所以它得被声明
    assert "pyyaml" in dev.lower(), "本文件 import yaml，dev extra 里必须声明 pyyaml"


def test_shot_script_direct_imports_are_declared():
    """scripts/shot.py 直接 import websockets，dev extra 里必须声明。

    它现在「碰巧能用」只是 uvicorn[standard] 的传递依赖；哪天 extra 一变，
    截图脚本当场 ImportError 且原因难找。直接 import 就要声明，与上面 pyyaml 同源。
    """
    shot = (ROOT / "scripts" / "shot.py").read_text(encoding="utf-8")
    assert "import websockets" in shot, "shot.py 不再直接用 websockets 时，删掉本测试"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dev = re.search(r"dev\s*=\s*\[(.*?)\]", pyproject, re.S).group(1).lower()
    assert "websockets" in dev, "shot.py 直接 import websockets，dev extra 里必须声明"


# ---------- 验收脚本的 E 盘兜底 ----------

def test_skeleton_csv_falls_back_when_the_e_drive_is_absent(monkeypatch):
    """CI 上没有 E 盘，表头必须回退到内置 12 列，而不是抛 FileNotFoundError。"""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import acceptance as A

    monkeypatch.setattr(A, "SKELETON", ROOT / "不存在的目录" / "骨架")
    text, rows, source = A.build_skeleton_csv()
    assert text.startswith("编号,") and len(rows) >= 2
    assert "回退" in source, f"没说明数据来源是兜底的：{source}"


def test_every_e_drive_read_in_acceptance_is_guarded():
    """E 盘路径只能在 `.exists()` 之后读——硬依赖会让 CI 直接崩在 demo2。"""
    src = ACCEPTANCE
    assert "if tpl.exists()" in src, "build_skeleton_csv 的模板读取没有 exists 兜底"
    assert src.count("if batch.exists()") >= 1 and src.count("if real_batch.exists()") >= 1
    assert "if not batch.exists() and not real_batch.exists()" in src, \
        "E 盘缺失时 F6 会整段不测，必须有合成 batch 兜底"
    assert "if not REAL_ROSTER.exists()" in src, "full76 没有 E 盘缺失的早退"
    # E 盘常量不许直接接读取——必须先落到局部变量再判 exists
    assert not re.search(r"(?:E_ROOT|SKELETON|REAL_ROSTER)[^\n]*\.read_(?:text|bytes)\(", src), \
        "有 E 盘路径没经 exists 判断就直接读了"
