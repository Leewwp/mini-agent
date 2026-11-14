"""AgentLoop 核心行为(工单 03):给定 FakeModel 脚本,断言终态、
session 事件序列与文件系统副作用——全部外部可观察行为。

接缝(spec Testing Decisions):AgentLoop.run() 的 RunOutcome、
session.jsonl 事件、临时仓库副作用;不测内部调用次序。
"""

import json
from pathlib import Path

from mini_agent.executor import ToolExecutor
from mini_agent.loop import SYSTEM_PROMPT, AgentLoop
from mini_agent.providers import create_provider
from mini_agent.session import SessionLogger
from mini_agent.tools import build_default_registry


def write_script(tmp_path, payload) -> str:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def run_loop(
    tmp_path,
    repo: Path,
    turns_payload,
    *,
    task: str = "修复测试",
    test_cmd: str | None = None,
    max_steps: int = 20,
    max_context_tokens: int = 60000,
    timeout_seconds: float = 600.0,
    clock=None,
    behaviors=None,
):
    """在临时仓库上跑一次完整 AgentLoop,返回 (outcome, 事件列表, 日志路径)。

    behaviors 优先于 turns_payload:注入自定义 provider(如抛错脚本)。
    """
    if behaviors is not None:
        provider = behaviors
    else:
        provider = create_provider(f"fake:{write_script(tmp_path, turns_payload)}")
    logger = SessionLogger(tmp_path / "runs")
    registry = build_default_registry(repo, test_cmd=test_cmd, emit=logger.emit)
    executor = ToolExecutor(registry=registry, logger=logger)
    loop = AgentLoop(
        provider=provider,
        executor=executor,
        logger=logger,
        system_prompt=SYSTEM_PROMPT,
        task=task,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
        max_context_tokens=max_context_tokens,
        clock=clock,
    )
    try:
        outcome = loop.run()
        events = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    finally:
        logger.close()
    return outcome, events, logger.path


def make_repo(tmp_path, *, files: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in (files or {}).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# 用例组 ② 之一:MAX_STEPS 耗尽
# ---------------------------------------------------------------------------


def test_max_steps_exhaustion_returns_max_steps_outcome(tmp_path):
    """模型每轮都调工具但从不 finish:步数耗尽 → MAX_STEPS / 退出码 2。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    turns = {"turns": [{"tool_calls": [{"name": "list_files", "arguments": {}}]}] * 5}
    outcome, events, _ = run_loop(tmp_path, repo, turns, max_steps=3)

    assert outcome.state == "MAX_STEPS"
    assert outcome.reason == "steps_exhausted"
    assert outcome.exit_code == 2
    assert outcome.steps == 3  # 一次 LLM 往返计 1 步

    # 每步都有 llm_request / llm_response,工具每步真实执行
    kinds = [e["type"] for e in events]
    assert kinds.count("llm_request") == 3
    assert kinds.count("llm_response") == 3
    assert kinds.count("tool_result") == 3

    # 首个请求含中文系统提示词与任务,且带工具定义
    first_request = events[0]
    assert first_request["messages"][0]["role"] == "system"
    assert "run_tests" in SYSTEM_PROMPT and "finish" in SYSTEM_PROMPT  # 中文提示词就位
    assert first_request["messages"][1] == {"role": "user", "content": "修复测试"}
    assert first_request["tools"] and any(
        t["function"]["name"] == "list_files" for t in first_request["tools"]
    )


# ---------------------------------------------------------------------------
# 用例组 ④ 修复闭环 + 声明时复验通过 → SUCCESS
# ---------------------------------------------------------------------------

BROKEN_CODE = "def add(a, b):\n    return a - b  # bug\n"
FIXED_CODE = "def add(a, b):\n    return a + b\n"


def broken_test_repo(tmp_path) -> Path:
    """带失败测试的临时仓库(等价验收第 1 条的真实闭环场景)。"""
    return make_repo(
        tmp_path,
        files={
            "calc.py": BROKEN_CODE,
            "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        },
    )


def test_full_fix_cycle_reaches_success(tmp_path):
    """写坏→run_tests 失败→修复→再测通过→finish(success)→现场复验→SUCCESS(退出码 0)。

    FakeModel 脚本等价替代真实模型(政策:真实彩排属工单 05)。
    """
    repo = broken_test_repo(tmp_path)
    turns = {
        "turns": [
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "calc.py", "content": BROKEN_CODE}}]},
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "calc.py", "content": FIXED_CODE}}]},
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"result": "success", "summary": "已修复 add 函数"},
                    }
                ]
            },
        ]
    }
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, task="修复 add", test_cmd="python3 -m pytest -q test_calc.py"
    )

    assert outcome.state == "SUCCESS"
    assert outcome.exit_code == 0
    assert outcome.reason == "verification_passed"
    assert outcome.summary == "已修复 add 函数"
    assert outcome.steps == 6
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED_CODE

    # 失败输出确实回填进下一轮 llm_request 上下文(修复循环的机制本体)
    step4_request = next(e for e in events if e["type"] == "llm_request" and e["step"] == 4)
    history_text = json.dumps(step4_request["messages"], ensure_ascii=False)
    assert "assert add(1, 2) == 3" in history_text  # 失败断言出现在模型上下文
    assert "1 failed" in history_text or "failed" in history_text

    # run_tests 每次执行(模型两次 + 复验一次)都落 test_run 事件,含完整 stdout
    test_runs = [e for e in events if e["type"] == "test_run"]
    assert len(test_runs) == 3
    assert all("stdout" in e and "stderr" in e for e in test_runs)
    assert test_runs[-1]["returncode"] == 0  # 复验现场重跑且通过

    # 复验的 tool_call 也进日志(经 verifier 重跑,不硬编码工具名)
    verify_calls = [
        e for e in events if e["type"] == "tool_call" and e.get("step") == 6 and e["name"] == "run_tests"
    ]
    assert len(verify_calls) == 1

    assert events[-1]["type"] == "final_report" and events[-1]["state"] == "SUCCESS"


# ---------------------------------------------------------------------------
# 用例组 ③:SUCCESS 判定(声明时复验四情形)+ 终态映射
# ---------------------------------------------------------------------------

GOOD_TEST_REPO_FILES = {
    "calc.py": "def add(a, b):\n    return a + b\n",
    "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
}


def finish_call(result: str, summary: str = "") -> dict:
    return {"name": "finish", "arguments": {"result": result, "summary": summary}}


def llm_events(events, type_: str) -> list:
    return [e for e in events if e["type"] == type_]


def test_tests_pass_but_model_never_finishes_is_not_success(tmp_path):
    """测试通过但模型不 finish → MAX_STEPS,绝不 SUCCESS。"""
    repo = make_repo(tmp_path, files=GOOD_TEST_REPO_FILES)
    turns = {
        "turns": [
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
        ]
    }
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, test_cmd="python3 -m pytest -q test_calc.py", max_steps=2
    )
    assert outcome.state == "MAX_STEPS"  # 非 SUCCESS
    assert outcome.exit_code == 2
    assert llm_events(events, "final_report")[0]["state"] == "MAX_STEPS"


def test_finish_success_reverification_failure_is_rejected_then_exhausts(tmp_path):
    """finish(success) 复验失败 → 拒绝并回填输出 → 继续;步数耗尽 → TESTS_FAILED(退出码 1)。"""
    repo = broken_test_repo(tmp_path)
    turns = {
        "turns": [
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {"tool_calls": [finish_call("success", "应当被拒绝") ]},
            {"tool_calls": [finish_call("success", "再试一次") ]},
        ]
    }
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, test_cmd="python3 -m pytest -q test_calc.py", max_steps=3
    )
    assert outcome.state == "TESTS_FAILED"
    assert outcome.reason == "verification_failed"
    assert outcome.exit_code == 1

    # 拒绝消息回填历史:复验输出出现在第 3 步的模型上下文里
    step3_request = next(e for e in events if e["type"] == "llm_request" and e["step"] == 3)
    history_text = json.dumps(step3_request["messages"], ensure_ascii=False)
    assert "已拒绝 finish(success)" in history_text
    assert "现场复验未通过" in history_text

    # 复验每次 finish 都现场重跑:模型 1 次 + 两次 finish 各 1 次 = 3 次 test_run
    assert len(llm_events(events, "test_run")) == 3


def test_stale_pass_then_breaking_file_then_finish_success_is_not_success(tmp_path):
    """测试通过→再写坏文件→finish(success)→复验现场重跑发现失败→非 SUCCESS。

    这是声明时复验防"过期结果"的核心用例:历史里有通过的记录也不采信。
    """
    repo = make_repo(tmp_path, files=GOOD_TEST_REPO_FILES)
    turns = {
        "turns": [
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},  # 通过
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "calc.py", "content": BROKEN_CODE}}]},  # 写坏
            {"tool_calls": [finish_call("success", "测试是过的(历史)") ]},
        ]
    }
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, test_cmd="python3 -m pytest -q test_calc.py", max_steps=4
    )
    assert outcome.state != "SUCCESS"
    assert outcome.state == "TESTS_FAILED" and outcome.reason == "verification_failed"
    # 复验发生在写坏之后,且是现场重跑(第 4 步的 verifier 调用)
    verify = [
        e for e in events
        if e["type"] == "tool_call" and e["name"] == "run_tests" and e.get("step") == 4
    ]
    assert len(verify) == 1
    assert (repo / "calc.py").read_text(encoding="utf-8") == BROKEN_CODE  # 写坏保留(不自动回滚)


def test_finish_success_without_running_tests_is_not_success(tmp_path):
    """从没跑过测试就 finish(success) → 现场复验仍是唯一判定:失败 → 非 SUCCESS。"""
    repo = broken_test_repo(tmp_path)
    turns = {"turns": [{"tool_calls": [finish_call("success", "我好了")]}]}
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, test_cmd="python3 -m pytest -q test_calc.py", max_steps=1
    )
    assert outcome.state == "TESTS_FAILED"  # 非 SUCCESS
    # run_tests 只被复验触发(模型从未调用)
    runs = llm_events(events, "test_run")
    assert len(runs) == 1 and runs[0]["returncode"] != 0


def test_finish_failed_maps_to_tests_failed(tmp_path):
    """finish(failed) → TESTS_FAILED(model_declared_failure),退出码 1。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    turns = {"turns": [{"tool_calls": [finish_call("failed", "任务无法完成")]}]}
    outcome, _, _ = run_loop(tmp_path, repo, turns, test_cmd="python3 -m pytest -q")
    assert outcome.state == "TESTS_FAILED"
    assert outcome.reason == "model_declared_failure"
    assert outcome.exit_code == 1
    assert outcome.summary == "任务无法完成"


def test_finish_success_without_test_cmd_is_unverified(tmp_path):
    """未配置 --test-cmd 时 finish(success) → UNVERIFIED(no_test_command),退出码 6。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    turns = {"turns": [{"tool_calls": [finish_call("success", "完成")]}]}
    outcome, _, _ = run_loop(tmp_path, repo, turns, test_cmd=None)
    assert outcome.state == "UNVERIFIED"
    assert outcome.reason == "no_test_command"
    assert outcome.exit_code == 6


def test_missing_test_command_is_error_not_tests_failed(tmp_path):
    """测试命令不存在/不可执行 → ERROR(test_command_not_found),退出码 3,不是 TESTS_FAILED。"""
    repo = make_repo(tmp_path, files=GOOD_TEST_REPO_FILES)
    turns = {"turns": [{"tool_calls": [finish_call("success", "完成")]}]}
    outcome, _, _ = run_loop(
        tmp_path, repo, turns, test_cmd="definitely-not-a-test-runner-xyz"
    )
    assert outcome.state == "ERROR"
    assert outcome.reason == "test_command_not_found"
    assert outcome.exit_code == 3


def test_run_command_success_does_not_count_as_verification(tmp_path):
    """run_command 执行成功 ≠ 验证:finish(success) 复验只认 run_tests。"""
    repo = broken_test_repo(tmp_path)
    turns = {
        "turns": [
            # allowlist 内的命令成功退出,但测试实际是失败的
            {"tool_calls": [{"name": "run_command", "arguments": {"command": "python3 -c \"print('looks fine')\""}}]},
            {"tool_calls": [finish_call("success", "命令跑通了") ]},
        ]
    }
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, test_cmd="python3 -m pytest -q test_calc.py", max_steps=2
    )
    assert outcome.state == "TESTS_FAILED"  # run_command 的成功救不了失败的测试
    assert outcome.reason == "verification_failed"
    # run_command 的执行没有产生 test_run 事件(不计入验证链)
    assert all(e["type"] != "test_run" or "pytest" in e["command"] for e in events)


def test_rejected_finish_gives_remaining_calls_not_executed_receipts(tmp_path):
    """finish(success) 被拒后本轮剩余调用也必须有结果——下一轮请求才合法。"""
    repo = broken_test_repo(tmp_path)
    turns = {
        "turns": [
            {
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "calc.py"}},
                    finish_call("success", "测试是过的"),
                    {"name": "write_file", "arguments": {"path": "never.txt", "content": "x"}},
                ]
            },
            {"tool_calls": [finish_call("failed", "结束")]},
        ]
    }
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, test_cmd="python3 -m pytest -q test_calc.py", max_steps=2
    )
    assert outcome.state == "TESTS_FAILED" and outcome.reason == "model_declared_failure"
    assert not (repo / "never.txt").exists()  # finish 之后的调用未执行

    # 本轮 3 个 tool_call id 全部有结果(write_file 为「未执行」回执;
    # 复验的 run_tests 也发生在 step 1,故 tool_result 共 4 条)
    step1_results = [e for e in events if e["type"] == "tool_result" and e.get("step") == 1]
    assert len(step1_results) == 4
    assert any("未执行" in e["content"] and e["name"] == "write_file" for e in step1_results)

    # 第 2 步请求的消息历史配对合法(每个 tool_call id 都有 tool 结果)
    step2_request = next(e for e in events if e["type"] == "llm_request" and e["step"] == 2)
    pending = set()
    for m in step2_request["messages"]:
        for tc in m.get("tool_calls") or []:
            pending.add(tc["id"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in pending
            pending.discard(m["tool_call_id"])
    assert not pending

    # 回归(A1):跳过原因必须到达模型——「未执行」回执不能是空字符串
    assistant = next(m for m in step2_request["messages"] if m.get("tool_calls"))
    skip_call = next(c for c in assistant["tool_calls"] if c["function"]["name"] == "write_file")
    skip_reply = next(
        m for m in step2_request["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == skip_call["id"]
    )
    assert "未执行" in skip_reply["content"] and "finish 被拒绝后跳过" in skip_reply["content"]


# ---------------------------------------------------------------------------
# 用例组 ⑦:单轮工具调用超过 8 个
# ---------------------------------------------------------------------------


def test_more_than_eight_tool_calls_in_one_turn_all_get_results(tmp_path):
    """单轮 10 个调用:前 8 个按序执行,后 2 个补「未执行」结果——每个 tool_call id 都有结果。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    calls = [
        {"name": "write_file", "arguments": {"path": f"f{i}.txt", "content": str(i)}}
        for i in range(10)
    ]
    turns = {"turns": [{"tool_calls": calls}, {"tool_calls": [finish_call("failed", "演示")]}]}
    outcome, events, _ = run_loop(tmp_path, repo, turns, max_steps=2)

    assert outcome.state == "TESTS_FAILED"  # 轮次本身正常走完
    step1_results = [
        e for e in events if e["type"] == "tool_result" and e.get("step") == 1
    ]
    assert len(step1_results) == 10  # 没有调用被无声丢弃
    skipped = [e for e in step1_results if "未执行" in e["content"]]
    assert len(skipped) == 2 and all("单轮上限" in e["content"] for e in skipped)
    assert all(e["is_error"] for e in skipped)
    # 前 8 个真实执行:文件落盘
    for i in range(8):
        assert (repo / f"f{i}.txt").read_text(encoding="utf-8") == str(i)
    assert not (repo / "f8.txt").exists() and not (repo / "f9.txt").exists()
    # 下一轮请求的消息历史里,10 个 tool_call id 全部有配对 tool 结果
    step2_request = next(e for e in events if e["type"] == "llm_request" and e["step"] == 2)
    assistant = next(m for m in step2_request["messages"] if m.get("tool_calls"))
    assert len(assistant["tool_calls"]) == 10
    tool_replies = [m for m in step2_request["messages"] if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_replies} == {
        c["id"] for c in assistant["tool_calls"]
    }

    # 回归(A1):超上限的回执原因必须到达模型,不能是空字符串
    skipped_ids = {c["id"] for c in assistant["tool_calls"][8:]}
    skipped_replies = [m for m in tool_replies if m["tool_call_id"] in skipped_ids]
    assert len(skipped_replies) == 2
    assert all("未执行" in m["content"] and "单轮上限" in m["content"] for m in skipped_replies)


# ---------------------------------------------------------------------------
# 用例组 ②:model_stalled(连续 2 轮空转)
# ---------------------------------------------------------------------------


def test_model_stalling_two_rounds_ends_with_error(tmp_path):
    """连续 2 轮不调用工具也不 finish:第 1 轮注入中文提醒,第 2 轮 ERROR(model_stalled)。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    turns = {"turns": [{"text": "我在想想……"}, {"text": "我还在想……"}]}
    outcome, events, _ = run_loop(tmp_path, repo, turns, max_steps=10)

    assert outcome.state == "ERROR"
    assert outcome.reason == "model_stalled"
    assert outcome.exit_code == 3
    assert outcome.steps == 2  # 不空转到 MAX_STEPS

    # 第 1 轮后注入中文提醒 user 消息,出现在第 2 步请求上下文里
    step2_request = next(e for e in events if e["type"] == "llm_request" and e["step"] == 2)
    reminders = [
        m for m in step2_request["messages"] if m["role"] == "user" and "提醒" in m["content"]
    ]
    assert len(reminders) == 1 and "未调用任何工具" in reminders[0]["content"]

    # 只发生 2 次 LLM 往返(第 2 次后立即终止,无第 3 次请求)
    assert len(llm_events(events, "llm_request")) == 2


def test_single_idle_round_then_tool_use_resets_stall_counter(tmp_path):
    """空转 1 轮后恢复调用工具:计数清零,不误杀。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    turns = {
        "turns": [
            {"text": "我先看看仓库。"},
            {"tool_calls": [{"name": "list_files", "arguments": {}}]},
            {"text": "再想想。"},
            {"tool_calls": [{"name": "list_files", "arguments": {}}]},
            {"tool_calls": [finish_call("failed", "结束") ]},
        ]
    }
    outcome, events, _ = run_loop(tmp_path, repo, turns, max_steps=10)
    assert outcome.state == "TESTS_FAILED"
    assert len(llm_events(events, "llm_request")) == 5


# ---------------------------------------------------------------------------
# 用例组 ②:整体超时 TIMEOUT 与 API 错误 ERROR(可注入时钟,禁止真实等待)
# ---------------------------------------------------------------------------


class FastFakeClock:
    """每次读时钟推进 fixed 秒:毫秒级真实耗时内走完任意长的虚拟时间。"""

    def __init__(self, step_seconds: float = 10.0) -> None:
        self.step_seconds = step_seconds
        self.elapsed = 0.0

    def __call__(self) -> float:
        self.elapsed += self.step_seconds
        return self.elapsed


def test_deadline_exceeded_returns_timeout_without_real_waiting(tmp_path):
    """整体超时 → TIMEOUT(dealine_exceeded),退出码 4;时钟全注入,无真实等待。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    turns = {"turns": [{"tool_calls": [{"name": "list_files", "arguments": {}}]}] * 5}
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, max_steps=10, timeout_seconds=25.0, clock=FastFakeClock(10.0)
    )
    assert outcome.state == "TIMEOUT"
    assert outcome.reason == "deadline_exceeded"
    assert outcome.exit_code == 4
    assert 1 <= outcome.steps < 10  # 在若干步后触发,而非耗尽步数
    assert llm_events(events, "final_report")[0]["state"] == "TIMEOUT"


class FailingProvider:
    """按脚本抛 ProviderError 的伪 Provider(重试语义属 01 的 provider 单测)。"""

    def __init__(self, error) -> None:
        self.error = error
        self.calls = 0

    @property
    def model(self) -> str:
        return "fake-failing"

    def complete(self, messages, *, tools=None, on_event=None):
        self.calls += 1
        raise self.error


def test_provider_retry_exhausted_maps_to_error_terminal(tmp_path):
    """429/5xx/网络错误重试 2 次后仍失败(provider 已重试)→ 循环 ERROR,退出码 3。"""
    from mini_agent.providers import ProviderRetryExhausted

    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    provider = FailingProvider(ProviderRetryExhausted("模型服务持续不可用,已重试 2 次后仍失败"))
    outcome, events, _ = run_loop(tmp_path, repo, None, behaviors=provider)
    assert outcome.state == "ERROR"
    assert outcome.reason == "provider_error"
    assert outcome.exit_code == 3
    assert provider.calls == 1
    assert llm_events(events, "final_report")[0]["state"] == "ERROR"


def test_provider_request_error_maps_to_error_terminal(tmp_path):
    """400/401 等请求错误不重试,直接 ERROR(循环级映射)。"""
    from mini_agent.providers import ProviderRequestError

    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    provider = FailingProvider(ProviderRequestError("模型请求被拒绝(HTTP 401)", 401, "检查 key"))
    outcome, _, _ = run_loop(tmp_path, repo, None, behaviors=provider)
    assert outcome.state == "ERROR" and outcome.exit_code == 3


# ---------------------------------------------------------------------------
# reasoning_delta 专门脚本单独测(默认脚本不产生;CI 默认路径 = 演示路径)
# ---------------------------------------------------------------------------


def test_reasoning_script_reasoning_shown_but_never_enters_context(tmp_path):
    """开启 reasoning 的专门脚本:闭环照常,reasoning 只展示、绝不进消息历史。"""
    repo = make_repo(tmp_path, files=GOOD_TEST_REPO_FILES)
    turns = {
        "turns": [
            {
                "reasoning": "先运行测试看看现状。",
                "text": "我来运行测试。",
                "tool_calls": [{"name": "run_tests", "arguments": {}}],
            },
            {
                "reasoning": "测试通过,可以声明完成。",
                "tool_calls": [
                    {"name": "finish", "arguments": {"result": "success", "summary": "测试通过"}}
                ],
            },
        ]
    }
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, test_cmd="python3 -m pytest -q test_calc.py"
    )
    assert outcome.state == "SUCCESS"
    # reasoning 在 llm_response 事件里可观测(展示用)
    responses = llm_events(events, "llm_response")
    assert responses[0]["reasoning"].startswith("先运行测试")
    # 但绝不进入后续请求的消息历史(组装消息只放 text)
    step2_request = next(e for e in events if e["type"] == "llm_request" and e["step"] == 2)
    assert "先运行测试" not in json.dumps(step2_request["messages"], ensure_ascii=False)
    assistant_messages = [
        m for m in step2_request["messages"] if m.get("role") == "assistant"
    ]
    assert any(m.get("content") == "我来运行测试。" for m in assistant_messages)


# ---------------------------------------------------------------------------
# 步数预算提醒(工单 04):只防「过度准备耗尽预算」,一次性,仅批处理
# ---------------------------------------------------------------------------


def budget_reminders_in(event) -> int:
    return sum(
        1
        for m in event["messages"]
        if m.get("role") == "user" and "预算提醒" in (m.get("content") or "")
    )


def test_budget_reminder_injected_once_at_75_percent_when_read_only(tmp_path):
    """20 步全只读:第 16 次 llm_request 起历史里恰好一条提醒,前 15 次没有。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    turns = {"turns": [{"tool_calls": [{"name": "list_files", "arguments": {}}]}] * 20}
    outcome, events, _ = run_loop(tmp_path, repo, turns, max_steps=20)

    assert outcome.state == "MAX_STEPS"  # 提醒不改终态语义
    requests = llm_events(events, "llm_request")
    assert len(requests) == 20
    assert [budget_reminders_in(r) for r in requests[:15]] == [0] * 15
    assert [budget_reminders_in(r) for r in requests[15:]] == [1] * 5  # 只提醒一次
    reminder = next(
        m for m in requests[15]["messages"] if "预算提醒" in (m.get("content") or "")
    )
    assert "15/20" in reminder["content"]


def test_budget_reminder_skipped_when_write_happened_before_threshold(tmp_path):
    """75% 前出现过 write_file:全程不提醒(已进入产出阶段)。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    write = {
        "tool_calls": [{"name": "write_file", "arguments": {"path": "out.txt", "content": "x"}}]
    }
    read = {"tool_calls": [{"name": "list_files", "arguments": {}}]}
    turns = {"turns": [read, write] + [read] * 18}
    outcome, events, _ = run_loop(tmp_path, repo, turns, max_steps=20)

    assert outcome.state == "MAX_STEPS"
    requests = llm_events(events, "llm_request")
    assert all(budget_reminders_in(r) == 0 for r in requests)


def test_budget_reminder_skipped_after_finish_attempt(tmp_path):
    """finish 声明被拒后继续只读:已有结束声明,不再提醒。"""
    repo = broken_test_repo(tmp_path)
    turns = {
        "turns": [
            {"tool_calls": [finish_call("success", "复验会失败")]}
        ]
        + [{"tool_calls": [{"name": "list_files", "arguments": {}}]}] * 19
    }
    outcome, events, _ = run_loop(
        tmp_path, repo, turns, test_cmd="python3 -m pytest -q test_calc.py", max_steps=20
    )
    assert outcome.state == "TESTS_FAILED"  # 复验被拒后步数耗尽(既有语义)
    requests = llm_events(events, "llm_request")
    assert all(budget_reminders_in(r) == 0 for r in requests)


# ---------------------------------------------------------------------------
# chat 预算提醒(REPL):75% 经 on_activity 打展示行,不注入历史
# ---------------------------------------------------------------------------


def run_chat_turn(tmp_path, repo, turns_payload, *, max_steps, activity):
    """单轮 chat AgentLoop(镜像 REPL 每轮新实例的装配);on_activity 收集展示行。"""
    provider = create_provider(f"fake:{write_script(tmp_path, turns_payload)}")
    logger = SessionLogger(tmp_path / "runs")
    registry = build_default_registry(repo, emit=logger.emit)
    executor = ToolExecutor(registry=registry, logger=logger)
    loop = AgentLoop(
        provider=provider,
        executor=executor,
        logger=logger,
        system_prompt=SYSTEM_PROMPT,
        task=None,
        chat_mode=True,
        initial_messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        max_steps=max_steps,
        on_activity=activity.append,
    )
    try:
        outcome = loop.run_turn("读一下仓库")
        events = [
            json.loads(line)
            for line in logger.path.read_text(encoding="utf-8").splitlines()
        ]
    finally:
        logger.close()
    return outcome, events


def test_chat_budget_reminder_is_display_line_not_history(tmp_path):
    """chat 75%:提醒经 on_activity 打一行(含「继续」指引),user 消息零注入。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    read = {"tool_calls": [{"name": "list_files", "arguments": {}}]}
    activity: list[str] = []
    outcome, events = run_chat_turn(
        tmp_path, repo,
        {"turns": [read] * 7 + [{"text": "读完了。"}]},
        max_steps=8, activity=activity,
    )
    assert outcome.state == "REPLIED"  # 提醒不改轮末语义
    reminders = [line for line in activity if "预算提醒" in line]
    assert len(reminders) == 1  # 一次性
    assert "6/8" in reminders[0] and "继续" in reminders[0]
    requests = llm_events(events, "llm_request")
    assert all(budget_reminders_in(r) == 0 for r in requests)  # 历史零注入


def test_chat_budget_reminder_fires_even_after_write(tmp_path):
    """chat:已写入仍提醒(与批处理不同——预算知情,不是催促产出)。"""
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    write = {
        "tool_calls": [
            {"name": "write_file", "arguments": {"path": "out.txt", "content": "x"}}
        ]
    }
    read = {"tool_calls": [{"name": "list_files", "arguments": {}}]}
    activity: list[str] = []
    outcome, _ = run_chat_turn(
        tmp_path, repo,
        {"turns": [write] + [read] * 6 + [{"text": "好了。"}]},
        max_steps=8, activity=activity,
    )
    assert outcome.state == "REPLIED"
    assert any("预算提醒" in line for line in activity)
