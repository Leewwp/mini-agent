"""五要素终报(工单 04):report.py 的行为测试。

公共接缝:
- ``is_test_file`` / ``read_last_test_run``(纯函数/日志读取);
- ``build_report(outcome, changes=…, test_cmd=…, last_test_run=…, session_dir=…)``
  → markdown 字符串,含五要素 + token 用量 + 模型名 + 测试文件改动提醒;
- ``write_report(session_dir, report_text, changes)`` → report.md + diffs/ 产物。
"""

import json

from mini_agent.loop import FinalState, RunOutcome
from mini_agent.report import (
    build_report,
    is_test_file,
    read_last_test_run,
    write_report,
)
from mini_agent.sandbox import ChangeSet, FileChange


def make_outcome(**overrides) -> RunOutcome:
    base = {
        "state": FinalState.SUCCESS,
        "reason": "verification_passed",
        "steps": 6,
        "prompt_tokens": 1200,
        "completion_tokens": 340,
        "total_tokens": 1540,
        "model": "deepseek-flash",
    }
    base.update(overrides)
    return RunOutcome(**base)


def make_changes(**overrides) -> ChangeSet:
    base = {
        "available": True,
        "changes": (
            FileChange("calc.py", "M", 1, 1),
            FileChange("notes.md", "A", 3, 0),
        ),
        "diff_text": "diff --git a/calc.py b/calc.py\n+fixed\n",
    }
    base.update(overrides)
    return ChangeSet(**base)


# ---------- 测试文件识别 ----------


def test_is_test_file_matches_common_conventions():
    assert is_test_file("tests/test_calc.py")
    assert is_test_file("test_calc.py")
    assert is_test_file("src/foo_test.py")
    assert is_test_file("tests/conftest.py")
    assert is_test_file("test/spec.js")
    assert not is_test_file("calc.py")
    assert not is_test_file("src/main.py")
    assert not is_test_file("README.md")


def test_is_test_file_ignores_compiled_test_artifacts():
    """pyc 编译产物不是测试文件:快照噪声不该触发 ⚠ 测试文件警报(A2)。"""
    assert not is_test_file("tests/__pycache__/test_x.cpython-312.pyc")
    assert not is_test_file("__pycache__/conftest.cpython-312.pyc")
    assert not is_test_file("src/foo.cpython-312.pyc")
    # 真实测试文件仍照常识别
    assert is_test_file("tests/test_x.py")
    assert is_test_file("conftest.py")


# ---------- 最近一次测试结果(从 append-only 日志取事实) ----------


def test_read_last_test_run_returns_latest_event(tmp_path):
    log = tmp_path / "session.jsonl"
    events = [
        {"type": "test_run", "command": "pytest -q", "returncode": 1, "timed_out": False},
        {"type": "tool_result", "content": "x"},
        {"type": "test_run", "command": "pytest -q", "returncode": 0, "timed_out": False},
    ]
    log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    last = read_last_test_run(log)
    assert last is not None
    assert last["returncode"] == 0 and last["command"] == "pytest -q"


def test_read_last_test_run_absent_returns_none(tmp_path):
    log = tmp_path / "session.jsonl"
    log.write_text(json.dumps({"type": "session_meta"}) + "\n", encoding="utf-8")
    assert read_last_test_run(log) is None


# ---------- 五要素终报(markdown) ----------


def test_build_report_contains_five_elements_tokens_and_model(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    outcome = make_outcome()
    report = build_report(
        outcome,
        changes=make_changes(),
        test_cmd="pytest -q",
        last_test_run={"command": "pytest -q", "returncode": 0, "timed_out": False},
        session_dir=session_dir,
        task="修复 add",
    )

    # 五要素逐项断言(spec「至少展示」清单)
    assert "终态:SUCCESS(原因:verification_passed)" in report  # 1 终态及原因
    assert "calc.py" in report and "notes.md" in report  # 2 修改文件与摘要
    assert "测试命令:pytest -q" in report and "退出码 0(通过)" in report  # 3 测试命令与最近一次结果
    assert "步数:6" in report  # 4 执行步数
    assert str((session_dir / "session.jsonl").resolve()) in report  # 5 日志路径(绝对路径)
    # 附带:token 用量与模型名(模型条件随结果记录)
    assert "prompt 1200" in report and "completion 340" in report and "total 1540" in report
    assert "deepseek-flash" in report
    assert "diffs/changes.diff" in report


def test_build_report_calls_out_test_file_changes(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    changes = ChangeSet(
        available=True,
        changes=(FileChange("test_calc.py", "M", 2, 0), FileChange("calc.py", "M", 1, 1)),
        diff_text="x",
    )
    report = build_report(
        make_outcome(state=FinalState.TESTS_FAILED, reason="model_declared_failure"),
        changes=changes,
        test_cmd="pytest -q",
        last_test_run={"command": "pytest -q", "returncode": 1, "timed_out": False},
        session_dir=session_dir,
    )
    assert "测试文件" in report and "test_calc.py" in report  # 单列提醒
    assert "退出码 1(失败)" in report


def test_build_report_without_test_run_states_honestly(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    report = build_report(
        make_outcome(state=FinalState.UNVERIFIED, reason="no_test_command"),
        changes=make_changes(available=False, changes=(), degraded_reason="git_unavailable"),
        test_cmd=None,
        last_test_run=None,
        session_dir=session_dir,
    )
    assert "未配置测试命令" in report
    assert "无 diff 凭证" in report  # 降级注明
    assert "git_unavailable" in report


def test_build_report_keeps_changes_on_failure_no_rollback(tmp_path):
    """失败终态:改动如实陈述,并明确不自动回滚。"""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    report = build_report(
        make_outcome(state=FinalState.TESTS_FAILED, reason="verification_failed"),
        changes=make_changes(),
        test_cmd="pytest -q",
        last_test_run={"command": "pytest -q", "returncode": 1, "timed_out": False},
        session_dir=session_dir,
    )
    assert "未自动回滚" in report and "calc.py" in report


# ---------- 产物落盘 ----------


def test_write_report_creates_report_md_and_diffs(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    report = build_report(
        make_outcome(),
        changes=make_changes(),
        test_cmd="pytest -q",
        last_test_run={"command": "pytest -q", "returncode": 0, "timed_out": False},
        session_dir=session_dir,
    )
    report_path = write_report(session_dir, report, make_changes())

    assert report_path == session_dir / "report.md"
    assert report_path.read_text(encoding="utf-8") == report
    diff = session_dir / "diffs" / "changes.diff"
    assert diff.read_text(encoding="utf-8") == make_changes().diff_text
