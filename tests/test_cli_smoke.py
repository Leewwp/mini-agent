"""CLI 子进程端到端冒烟(工单 06):真实进程跑安装好的 mini-agent 入口。

与 test_cli.py 的区别:那批测试在进程内调 main(),本文件越过 Python 边界——
以真实 subprocess 启动 venv 里安装的 console script(uv run mini-agent 的
等价 venv 内入口),从进程退出码、stdout 终报与 session.jsonl 三处断言,
守住「安装即可用」与终态→退出码映射的真实生效。

- 模型走 FakeModel(--model fake:<script.json>),无 key、无网络;
- fixture 为测试内临时构造的最小仓库副本(正式 fixtures/ 属工单 05,不越界);
- 至少覆盖 SUCCESS=0 与 TESTS_FAILED=1 两个退出码(工单验收);
- 坏/好版本字节数刻意不同:规避 pyc 按 (秒级 mtime, size) 判缓存的陷阱
  (同秒同字节数改回会让子进程 pytest 读到旧坏版本,见 test_cli.py 同款注释)。
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# 坏/好版本字节数不同(见模块 docstring 的 pyc 说明)
BROKEN = "def add(a, b):\n    return a - b  # bug\n"
FIXED = "def add(a, b):\n    return a + b  # fixed\n"


def smoke_repo(root: Path) -> Path:
    """构造最小 fixture 仓库的临时副本:一个带 bug 的 calc 与其测试。"""
    repo = root / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(BROKEN, encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
    )
    return repo


def write_script(root: Path, payload: dict) -> Path:
    path = root / "script.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def run_cli(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """以真实 subprocess 运行安装好的 mini-agent 入口(等价 venv 内 `mini-agent`)。"""
    entry = Path(sys.executable).parent / "mini-agent"
    assert entry.is_file(), f"console script 未安装:{entry}(请 uv pip install -e '.[dev]')"
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINI_AGENT_")}
    return subprocess.run(
        [str(entry), *argv],
        check=False,  # 非零退出码正是被测对象(终态→退出码映射),由调用方断言
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=timeout,
    )


def read_events(session_jsonl: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in session_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def session_log_path(stdout: str) -> Path:
    """从终报行「会话日志:<path>」解析出 session.jsonl 的真实落点。"""
    match = re.search(r"会话日志:(\S+)", stdout)
    assert match, f"终报未打印会话日志路径,stdout 尾部:{stdout[-300:]!r}"
    return Path(match.group(1))


def success_turns() -> dict:
    """SUCCESS 路径脚本:读 → 修复 → 自跑测试 → finish(success)(复验自动追加)。"""
    return {
        "turns": [
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "calc.py", "content": FIXED}}]},
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {"tool_calls": [{"name": "finish", "arguments": {"result": "success", "summary": "修复 add"}}]},
        ]
    }


# 用 venv 解释器跑 pytest,不依赖 PATH 里的 python3(CI 与本地一致)
TEST_CMD = f"{sys.executable} -m pytest -q test_calc.py"


def test_subprocess_success_path_exit_zero_report_and_log(tmp_path):
    """SUCCESS=0:修复循环走通,退出码/终报/日志文件三处一致。"""
    repo = smoke_repo(tmp_path)
    script = write_script(tmp_path, success_turns())
    scratch = tmp_path / "cwd"
    scratch.mkdir()

    proc = run_cli(
        [
            "--repo", str(repo),
            "--task", "修复 calc.py 中 add 的 bug,让测试通过",
            "--model", f"fake:{script}",
            "--test-cmd", TEST_CMD,
        ],
        cwd=scratch,
    )

    assert proc.returncode == 0, f"stdout:{proc.stdout[-500:]!r} stderr:{proc.stderr[-500:]!r}"
    # 终报要素(状态/原因/步数/日志路径;diff 摘要属工单 04 的完整版终报)
    assert "终态:SUCCESS" in proc.stdout
    assert "verification_passed" in proc.stdout
    assert "退出码 0" in proc.stdout
    assert "步数:4" in proc.stdout

    # 文件系统副作用:修复真实写入了目标仓库
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED

    # 日志文件:从终报解析的路径真实存在,事件序列完整
    log = session_log_path(proc.stdout)
    assert log.is_file()
    events = read_events(log)
    assert events[0]["type"] == "session_meta"
    assert events[0]["model"].startswith("fake:")
    types = [e["type"] for e in events]
    for expected in ("llm_request", "llm_response", "tool_call", "tool_result", "final_report"):
        assert expected in types
    tool_names = {e["name"] for e in events if e["type"] == "tool_call"}
    assert {"read_file", "write_file", "run_tests", "finish"} <= tool_names
    # test_run = 模型自跑 1 次 + finish 复验 1 次(声明时现场复验语义)
    assert types.count("test_run") == 2
    final = next(e for e in events if e["type"] == "final_report")
    assert final["state"] == "SUCCESS"
    assert final["steps"] == 4
    # 日志不落 key 与请求头
    raw = log.read_text(encoding="utf-8")
    assert "api_key" not in raw and "Authorization" not in raw


def test_subprocess_tests_failed_exit_one(tmp_path):
    """TESTS_FAILED=1:模型声明失败,退出码映射真实生效。"""
    repo = smoke_repo(tmp_path)
    script = write_script(
        tmp_path,
        {"turns": [
            {"tool_calls": [{"name": "finish", "arguments": {"result": "failed", "summary": "无法修复"}}]},
        ]},
    )
    scratch = tmp_path / "cwd"
    scratch.mkdir()

    proc = run_cli(
        [
            "--repo", str(repo),
            "--task", "修复 add 的 bug",
            "--model", f"fake:{script}",
            "--test-cmd", TEST_CMD,
        ],
        cwd=scratch,
    )

    assert proc.returncode == 1, f"stdout:{proc.stdout[-500:]!r} stderr:{proc.stderr[-500:]!r}"
    assert "终态:TESTS_FAILED" in proc.stdout
    assert "model_declared_failure" in proc.stdout
    assert "退出码 1" in proc.stdout
    final = next(e for e in read_events(session_log_path(proc.stdout)) if e["type"] == "final_report")
    assert final["state"] == "TESTS_FAILED"


def test_subprocess_runs_without_any_mini_agent_env(tmp_path):
    """无 key 环境(clone 即信):剔除全部 MINI_AGENT_* 后入口仍可用。"""
    repo = smoke_repo(tmp_path)
    script = write_script(
        tmp_path,
        {"turns": [
            {"tool_calls": [{"name": "finish", "arguments": {"result": "failed", "summary": "演示"}}]},
        ]},
    )
    scratch = tmp_path / "cwd"
    scratch.mkdir()

    proc = run_cli(
        [
            "--repo", str(repo),
            "--task", "演示无 key 运行",
            "--model", f"fake:{script}",
            "--test-cmd", TEST_CMD,
        ],
        cwd=scratch,
    )
    assert proc.returncode == 1  # TESTS_FAILED(模型声明失败)而非 CONFIG_ERROR(5)
    assert "配置错误" not in proc.stdout and "配置错误" not in proc.stderr
