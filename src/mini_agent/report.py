"""五要素终报(工单 04,spec「可观测」「停止条件」)。

spec「至少展示」清单 → 报告字段:
1. 终态及原因(六终态枚举 + reason);
2. 修改文件与 diff 摘要(影子 git 快照对账,产物存 session 目录 ``diffs/``);
3. 测试命令与最近一次结果(从 append-only session.jsonl 的最后一条
   ``test_run`` 事件取——日志是事实来源,不另立状态);
4. 执行步数;
5. 日志路径(绝对路径)。
另附 token 用量与模型名(模型条件随结果记录);修改了测试文件单列提醒;
所有终态(含 ERROR/TIMEOUT/中断)都出同一份报告;失败不自动回滚,改动如实陈述。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mini_agent.loop import RunOutcome
from mini_agent.sandbox import ChangeSet, FileChange

STATUS_LABELS = {"A": "新增", "M": "修改", "D": "删除"}
DIFF_SUMMARY_LINES = 120  # report.md 内嵌 diff 摘要的行数上限(全文在 diffs/)
TEST_FILE_ALERT = (
    "本次运行修改了测试文件:{files} —— 请人工复核测试改动是否合理"
    '(防止"改测试让自己通过")'
)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def is_test_file(relpath: str) -> bool:
    """常见测试文件约定:test_*.py、*_test.py、conftest.py、tests/ 或 test/ 目录下。

    ``__pycache__`` 内的 .pyc 编译产物不算测试文件——那只是 pytest 副产物
    噪声,不该触发"改测试作弊"警报。
    """
    path = relpath.lower()
    if path.endswith(".pyc") or "__pycache__" in path.split("/"):
        return False
    name = path.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    return any(part in ("tests", "test") for part in path.split("/")[:-1])


def read_last_test_run(session_jsonl: Path) -> dict | None:
    """读 append-only 日志,返回最后一条 ``test_run`` 事件;无则 None。"""
    last: dict | None = None
    try:
        with Path(session_jsonl).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "test_run":
                    last = event
    except OSError:
        return None
    return last


def _test_result_text(test_cmd: str | None, last_run: dict | None) -> str:
    if not test_cmd:
        return "未配置测试命令(--test-cmd)"
    if last_run is None:
        return f"{test_cmd};最近一次结果:本次运行未执行"
    if last_run.get("timed_out"):
        return f"{test_cmd};最近一次结果:超时被强制终止"
    returncode = last_run.get("returncode")
    verdict = "通过" if returncode == 0 else "失败"
    return f"{test_cmd};最近一次结果:退出码 {returncode}({verdict})"


def _token_text(outcome: RunOutcome) -> str:
    suffix = "(估算值)" if outcome.total_tokens and outcome.prompt_tokens is None else ""
    return (
        f"token 用量{suffix}:"
        f"prompt {outcome.prompt_tokens if outcome.prompt_tokens is not None else '-'} / "
        f"completion {outcome.completion_tokens} / total {outcome.total_tokens}"
    )


def _format_change(change) -> str:
    ins = "-" if change.insertions is None else f"+{change.insertions}"
    dels = "-" if change.deletions is None else f"-{change.deletions}"
    return f"{change.path}({status_label(change.status)},{ins}/{dels})"


def test_file_changes(changes: ChangeSet) -> list[str]:
    """修改清单中的测试文件(单列提醒,防"改测试让自己通过")。"""
    return [c.path for c in changes.changes if is_test_file(c.path)]


# ---------------------------------------------------------------------------
# markdown 终报(report.md 全文)
# ---------------------------------------------------------------------------


def build_report(
    outcome: RunOutcome,
    *,
    changes: ChangeSet,
    test_cmd: str | None,
    last_test_run: dict | None,
    session_dir: Path,
    task: str = "",
) -> str:
    """生成五要素 markdown 终报(所有终态共用一个版式)。"""
    session_dir = Path(session_dir)
    log_path = (session_dir / "session.jsonl").resolve()
    report_path = (session_dir / "report.md").resolve()
    lines: list[str] = ["# mini-agent 运行终报", ""]
    lines.append(f"- 终态:{outcome.state.value}(原因:{outcome.reason})")
    if task:
        lines.append(f"- 任务:{task}")
    lines.append(f"- 执行步数:{outcome.steps}")
    lines.append(f"- 测试命令:{_test_result_text(test_cmd, last_test_run)}")
    lines.append(f"- 模型:{outcome.model}")
    lines.append(f"- {_token_text(outcome)}")
    lines.append(f"- 会话日志:{log_path}")
    lines.append(f"- 终报文件:{report_path}")
    if changes.available:
        lines.append("- 改动凭证:影子 git 快照,完整 diff 见 diffs/changes.diff")
    else:
        reason = changes.degraded_reason or "snapshot_failed"
        lines.append(f"- 改动凭证:无 diff 凭证(原因:{reason}),已降级为文件清单")
    if outcome.summary:
        lines.append(f"- 模型声明:{outcome.summary}")
    if outcome.detail:
        lines.append(f"- 详情:{outcome.detail}")

    lines += ["", f"## 修改文件({len(changes.changes)})"]
    if not changes.changes:
        lines.append("(本次运行没有文件改动)")
    else:
        lines += ["| 文件 | 状态 | +行 | -行 |", "|---|---|---|---|"]
        for c in changes.changes:
            ins = "-" if c.insertions is None else str(c.insertions)
            dels = "-" if c.deletions is None else str(c.deletions)
            lines.append(f"| {c.path} | {status_label(c.status)} | {ins} | {dels} |")
    test_files = test_file_changes(changes)
    if test_files:
        lines += ["", f"> ⚠ {TEST_FILE_ALERT.format(files='、'.join(test_files))}"]

    if changes.available and changes.diff_text:
        lines += ["", "## diff 摘要", "", "```diff"]
        diff_lines = changes.diff_text.splitlines()
        head = diff_lines[:DIFF_SUMMARY_LINES]
        lines += head
        if len(diff_lines) > len(head):
            lines.append(f"…(仅展示前 {DIFF_SUMMARY_LINES} 行,完整 diff 见 diffs/changes.diff)")
        lines += ["```"]

    lines += ["", "## 基线与凭证说明"]
    if outcome.state.value != "SUCCESS":
        lines.append("- 改动已如实保留,未自动回滚(失败也是合法结果)。")
    for note in changes.notes:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def write_report(session_dir: Path, report_text: str, changes: ChangeSet) -> Path:
    """把终报与 diff 产物写入 session 目录:report.md + diffs/changes.diff。"""
    session_dir = Path(session_dir)
    report_path = session_dir / "report.md"
    report_path.write_text(report_text, encoding="utf-8")
    if changes.diff_text:
        diffs_dir = session_dir / "diffs"
        diffs_dir.mkdir(parents=True, exist_ok=True)
        (diffs_dir / "changes.diff").write_text(changes.diff_text, encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# 终端终报(所有终态都打印;保持工单 03 的行格式兼容)
# ---------------------------------------------------------------------------


def render_terminal(
    outcome: RunOutcome,
    *,
    changes: ChangeSet,
    test_cmd: str | None,
    last_test_run: dict | None,
    session_dir: Path,
) -> str:
    """终端版五要素终报(与 report.md 同源,紧凑排版)。"""
    session_dir = Path(session_dir)
    lines = [
        "================ 终报 ================",
        f"终态:{outcome.state.value}(原因:{outcome.reason}),退出码 {outcome.exit_code}",
    ]
    if outcome.summary:
        lines.append(f"模型声明:{outcome.summary}")
    if outcome.detail:
        lines.append(f"详情:{outcome.detail}")
    lines.append(f"步数:{outcome.steps};{_token_text(outcome)};模型:{outcome.model}")
    lines.append(f"测试命令:{_test_result_text(test_cmd, last_test_run)}")
    lines.append(f"修改文件({len(changes.changes)}):")
    if not changes.changes:
        lines.append("  (本次运行没有文件改动)")
    for c in changes.changes:
        marker = "  ⚠ 测试文件 " if is_test_file(c.path) else "  "
        lines.append(f"{marker}{_format_change(c)}")
    if changes.available:
        lines.append("改动凭证:diffs/changes.diff(影子 git 快照)")
    else:
        reason = changes.degraded_reason or "snapshot_failed"
        lines.append(f"改动凭证:无 diff 凭证(原因:{reason}),文件清单见 report.md")
    lines.append(f"会话日志:{(session_dir / 'session.jsonl').resolve()}")
    lines.append(f"终报全文:{(session_dir / 'report.md').resolve()}")
    lines.append("======================================")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 会话级终报(REPL,工单 12 / 设计票 08):轮次/总步数/token 三项(压缩摘要
# 单列)/模型/测试/跨段改动(影子 git 快照,合并 /new 分段)/日志路径
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSummary:
    """REPL 会话汇总(会话终报输入;turns=0 的空会话也出报告)。"""

    state: str  # "正常退出" / "异常退出"
    turns: int
    steps: int
    prompt_tokens: int | None
    completion_tokens: int
    total_tokens: int
    compact_tokens: int  # 其中压缩摘要调用的 token(0 = 本段未压缩)
    model: str
    resumed_from: str | None = None


def merge_changesets(sets: list[ChangeSet]) -> ChangeSet:
    """合并多段快照(/new 分段):同路径后段覆盖;diff 全文顺序拼接。"""
    if not sets:
        return ChangeSet(available=False, changes=(), degraded_reason="no_snapshot")
    merged: dict[str, FileChange] = {}
    for cs in sets:
        for change in cs.changes:
            merged[change.path] = change  # 后段覆盖同路径(物理状态以最新为准)
    available = any(cs.available for cs in sets)
    diff_text = "\n".join(cs.diff_text for cs in sets if cs.diff_text)
    degraded = None if available else (sets[-1].degraded_reason or "snapshot_failed")
    notes: list[str] = []
    if len(sets) > 1:
        notes.append(f"会话经历 {len(sets)} 段(/new 轮换);改动清单为各段合并,同路径以最新为准")
    for cs in sets:
        notes.extend(cs.notes)
    return ChangeSet(
        available=available,
        changes=tuple(merged.values()),
        diff_text=diff_text,
        degraded_reason=degraded,
        baseline_dirty=any(cs.baseline_dirty for cs in sets),
        baseline_untracked=sum(cs.baseline_untracked for cs in sets),
        notes=tuple(notes),
    )


def _session_token_text(summary: SessionSummary) -> str:
    compact_note = f",其中压缩摘要 {summary.compact_tokens} tok" if summary.compact_tokens else ""
    prompt = summary.prompt_tokens if summary.prompt_tokens is not None else "-"
    return (
        f"token 用量:prompt {prompt} / completion {summary.completion_tokens}"
        f" / total {summary.total_tokens}{compact_note}"
    )


def build_session_report(
    summary: SessionSummary,
    *,
    changes: ChangeSet,
    test_cmd: str | None,
    last_test_run: dict | None,
    session_dir: Path,
    repo: Path | None = None,
) -> str:
    """生成会话级 markdown 终报(/exit 与异常路径共用一个版式)。"""
    session_dir = Path(session_dir)
    log_path = (session_dir / "session.jsonl").resolve()
    report_path = (session_dir / "report.md").resolve()
    lines: list[str] = ["# mini-agent 会话终报", ""]
    lines.append(f"- 会话状态:{summary.state}")
    if summary.resumed_from:
        lines.append(f"- 恢复自:{summary.resumed_from}")
    lines.append(f"- 轮次:{summary.turns};总步数:{summary.steps}")
    lines.append(f"- 模型:{summary.model}")
    lines.append(f"- {_session_token_text(summary)}")
    lines.append(f"- 测试命令:{_test_result_text(test_cmd, last_test_run)}")
    if repo is not None:
        lines.append(f"- 仓库:{repo}")
    lines.append(f"- 会话日志:{log_path}")
    lines.append(f"- 终报文件:{report_path}")
    if changes.available:
        lines.append("- 改动凭证:影子 git 快照,完整 diff 见 diffs/changes.diff")
    else:
        reason = changes.degraded_reason or "snapshot_failed"
        lines.append(f"- 改动凭证:无 diff 凭证(原因:{reason}),已降级为文件清单")

    lines += ["", f"## 修改文件({len(changes.changes)})"]
    if not changes.changes:
        lines.append("(本次会话没有文件改动)")
    else:
        lines += ["| 文件 | 状态 | +行 | -行 |", "|---|---|---|---|"]
        for c in changes.changes:
            ins = "-" if c.insertions is None else str(c.insertions)
            dels = "-" if c.deletions is None else str(c.deletions)
            lines.append(f"| {c.path} | {status_label(c.status)} | {ins} | {dels} |")
    test_files = [c.path for c in changes.changes if is_test_file(c.path)]
    if test_files:
        lines += ["", f"> ⚠ {TEST_FILE_ALERT.format(files='、'.join(test_files))}"]

    if changes.available and changes.diff_text:
        lines += ["", "## diff 摘要", "", "```diff"]
        diff_lines = changes.diff_text.splitlines()
        head = diff_lines[:DIFF_SUMMARY_LINES]
        lines += head
        if len(diff_lines) > len(head):
            lines.append(f"…(仅展示前 {DIFF_SUMMARY_LINES} 行,完整 diff 见 diffs/changes.diff)")
        lines += ["```"]
    for note in changes.notes:
        lines += ["", f"> 注:{note}"]
    lines.append("")
    return "\n".join(lines)


def render_session_terminal(
    summary: SessionSummary,
    *,
    changes: ChangeSet,
    test_cmd: str | None,
    last_test_run: dict | None,
    session_dir: Path,
) -> str:
    """终端版会话终报(与 report.md 同源,紧凑排版)。"""
    session_dir = Path(session_dir)
    lines = [
        "================ 会话终报 ================",
        f"会话状态:{summary.state};轮次 {summary.turns};总步数 {summary.steps};模型 {summary.model}",
        _session_token_text(summary),
        f"测试命令:{_test_result_text(test_cmd, last_test_run)}",
        f"修改文件({len(changes.changes)}):",
    ]
    if not changes.changes:
        lines.append("  (本次会话没有文件改动)")
    for c in changes.changes:
        marker = "  ⚠ 测试文件 " if is_test_file(c.path) else "  "
        lines.append(f"{marker}{_format_change(c)}")
    if changes.available:
        lines.append("改动凭证:diffs/changes.diff(影子 git 快照)")
    else:
        reason = changes.degraded_reason or "snapshot_failed"
        lines.append(f"改动凭证:无 diff 凭证(原因:{reason}),文件清单见 report.md")
    lines.append(f"会话日志:{(session_dir / 'session.jsonl').resolve()}")
    lines.append(f"终报全文:{(session_dir / 'report.md').resolve()}")
    lines.append("=========================================")
    return "\n".join(lines)
