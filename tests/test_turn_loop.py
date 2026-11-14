"""AgentLoop 轮次化(工单 10 / 设计票 03):REPL 用的 turn 级 API。

全部 FakeModel 驱动,无 TTY 无网络。覆盖:REPLIED / 工具轮各终态 /
中断补回执 / turn 分组裁剪(含压缩摘要头部保护) / usage 每轮独立 /
on_tool_result(执行、skip、verifier) / 构造校验 / 批处理零改动回归。
"""

import json

from mini_agent.executor import ToolExecutor
from mini_agent.loop import (
    COMPACT_SUMMARY_PREFIX,
    SYSTEM_PROMPT,
    AgentLoop,
    TurnState,
    _history_tokens,
    trim_history,
)
from mini_agent.providers import create_provider
from mini_agent.session import SessionLogger
from mini_agent.tools import build_default_registry


def write_script(tmp_path, payload, name="script.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def make_repo(tmp_path, *, files: dict | None = None):
    repo = tmp_path / "repo"
    repo.mkdir()
    for fname, content in (files or {}).items():
        (repo / fname).write_text(content, encoding="utf-8")
    return repo


def chat_history(system_text: str = "chat-system", *pairs) -> list[dict]:
    """构造 [system, (user, assistant)*] 历史;pairs 为 (user, assistant) 文本对。"""
    msgs: list[dict] = [{"role": "system", "content": system_text}]
    for user_text, assistant_text in pairs:
        msgs.append({"role": "user", "content": user_text})
        msgs.append({"role": "assistant", "content": assistant_text})
    return msgs


def run_chat_turn(
    tmp_path,
    repo,
    turns_payload,
    initial_messages,
    *,
    script_name="script.json",
    test_cmd: str | None = None,
    max_steps: int = 20,
    max_context_tokens: int = 60000,
    timeout_seconds: float = 600.0,
    clock=None,
    user_message: str = "继续",
    extra_tool=None,
    on_tool_result=None,
):
    """按形态 A 跑一轮:initial_messages 注入历史,run_turn(user_message)。"""
    provider = create_provider(f"fake:{write_script(tmp_path, turns_payload, script_name)}")
    logger = SessionLogger(tmp_path / f"runs-{script_name}")
    registry = build_default_registry(repo, test_cmd=test_cmd, emit=logger.emit)
    if extra_tool is not None:
        registry.register(extra_tool)
    executor = ToolExecutor(registry=registry, logger=logger)
    loop = AgentLoop(
        provider=provider,
        executor=executor,
        logger=logger,
        system_prompt=SYSTEM_PROMPT,
        initial_messages=initial_messages,
        chat_mode=True,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
        max_context_tokens=max_context_tokens,
        clock=clock,
        on_tool_result=on_tool_result,
    )
    try:
        outcome = loop.run_turn(user_message)
        events = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    finally:
        logger.close()
    return outcome, events, loop


def assert_pairs_valid(messages) -> None:
    """OpenAI 协议合法性:每个 assistant tool_call 都有对应 tool 结果,无孤儿。"""
    pending: set[str] = set()
    for m in messages:
        for tc in m.get("tool_calls") or []:
            pending.add(tc["id"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in pending, f"孤儿 tool 结果:{m['tool_call_id']}"
            pending.discard(m["tool_call_id"])
    assert not pending, f"tool_call 无结果:{pending}"


def events_of(events, type_: str) -> list:
    return [e for e in events if e["type"] == type_]


# ---------------------------------------------------------------------------
# 构造校验与只读 messages
# ---------------------------------------------------------------------------


def _minimal_deps(tmp_path, repo):
    logger = SessionLogger(tmp_path / "runs-validate")
    registry = build_default_registry(repo)
    executor = ToolExecutor(registry=registry, logger=logger)
    return logger, executor


def test_constructor_rejects_task_and_initial_messages_together(tmp_path):
    repo = make_repo(tmp_path)
    logger, executor = _minimal_deps(tmp_path, repo)
    provider = create_provider(f"fake:{write_script(tmp_path, {'turns': []})}")
    try:
        import pytest

        with pytest.raises(ValueError):
            AgentLoop(
                provider=provider,
                executor=executor,
                logger=logger,
                system_prompt=SYSTEM_PROMPT,
                task="任务",
                initial_messages=chat_history(),
            )
    finally:
        logger.close()


def test_constructor_rejects_neither_task_nor_initial_messages(tmp_path):
    import pytest

    repo = make_repo(tmp_path)
    logger, executor = _minimal_deps(tmp_path, repo)
    provider = create_provider(f"fake:{write_script(tmp_path, {'turns': []})}")
    try:
        with pytest.raises(ValueError):
            AgentLoop(
                provider=provider,
                executor=executor,
                logger=logger,
                system_prompt=SYSTEM_PROMPT,
            )
    finally:
        logger.close()


def test_constructor_requires_system_head_in_initial_messages(tmp_path):
    import pytest

    repo = make_repo(tmp_path)
    logger, executor = _minimal_deps(tmp_path, repo)
    provider = create_provider(f"fake:{write_script(tmp_path, {'turns': []})}")
    try:
        with pytest.raises(ValueError):
            AgentLoop(
                provider=provider,
                executor=executor,
                logger=logger,
                system_prompt=SYSTEM_PROMPT,
                initial_messages=[{"role": "user", "content": "开头不是 system"}],
            )
    finally:
        logger.close()


def test_messages_property_returns_copy_and_system_injectable(tmp_path):
    """messages 返回拷贝;initial_messages[0] 的 system 原样进入请求(不硬编码批处理 prompt)。"""
    repo = make_repo(tmp_path, files={"README.md": "x"})
    history = chat_history("自定义chat提示词", ("你好", "你好,有什么可以帮你?"))
    outcome, events, loop = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": "好的"}]}, history, script_name="copy.json",
        user_message="继续做什么",
    )
    assert outcome.state == TurnState.REPLIED
    snapshot = loop.messages
    snapshot.append({"role": "user", "content": "篡改"})
    assert len(loop.messages) == len(snapshot) - 1  # 外部修改不影响内部历史
    first_request = events_of(events, "llm_request")[0]
    assert first_request["messages"][0] == {"role": "system", "content": "自定义chat提示词"}
    assert first_request["messages"][-1] == {"role": "user", "content": "继续做什么"}


# ---------------------------------------------------------------------------
# REPLIED:纯文本/空回复即轮末,无 stall 提醒、无终报事件
# ---------------------------------------------------------------------------


def test_text_reply_ends_turn_with_replied_and_no_terminal_events(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    history = chat_history("sys", ("你好", "你好"))
    outcome, events, _ = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": "这是文本回复。"}]}, history,
        script_name="replied.json",
    )
    assert outcome.state == TurnState.REPLIED
    assert outcome.reason == "text_reply"
    assert outcome.steps == 1
    # chat 模式轮末循环不发任何终报事件(final_report / turn_result 都归 08 编排层)
    assert events_of(events, "final_report") == []
    assert events_of(events, "turn_result") == []
    assert len(events_of(events, "llm_request")) == 1


def test_empty_reply_is_replied_not_stall(tmp_path):
    """空回复(无文本无工具调用)= REPLIED,不注入提醒、不 ERROR。"""
    repo = make_repo(tmp_path, files={"README.md": "x"})
    outcome, events, loop = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": ""}]}, chat_history("sys"),
        script_name="empty.json",
    )
    assert outcome.state == TurnState.REPLIED
    assert events_of(events, "final_report") == []
    # 空回复的 assistant 消息已入史(供 REPL 显示「(空回复)」)
    last = loop.messages[-1]
    assert last["role"] == "assistant" and not last.get("content")


def test_consecutive_text_turns_never_stall(tmp_path):
    """多轮纯文本:第 2 轮仍 REPLIED,历史中不出现 stall 提醒。"""
    repo = make_repo(tmp_path, files={"README.md": "x"})
    history = chat_history("sys")
    outcome1, _, loop1 = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": "第一轮回复"}]}, history, script_name="t1.json",
    )
    assert outcome1.state == TurnState.REPLIED
    outcome2, events2, _ = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": "第二轮回复"}]}, loop1.messages,
        script_name="t2.json",
    )
    assert outcome2.state == TurnState.REPLIED
    all_text = json.dumps(
        events_of(events2, "llm_request")[0]["messages"], ensure_ascii=False
    )
    assert "提醒" not in all_text  # 不注入 stall 提醒


# ---------------------------------------------------------------------------
# 工具轮:终态映射与「无 final_report」
# ---------------------------------------------------------------------------

GOOD_FILES = {
    "calc.py": "def add(a, b):\n    return a + b\n",
    "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
}


def test_tool_turn_success_maps_without_final_report(tmp_path):
    repo = make_repo(tmp_path, files=GOOD_FILES)
    script = {
        "turns": [
            {"tool_calls": [{"name": "run_tests", "arguments": {}}]},
            {
                "tool_calls": [
                    {"name": "finish", "arguments": {"result": "success", "summary": "测试通过"}}
                ]
            },
        ]
    }
    outcome, events, _ = run_chat_turn(
        tmp_path, repo, script, chat_history("sys"), script_name="success.json",
        test_cmd="python3 -m pytest -q test_calc.py",
    )
    assert outcome.state == TurnState.SUCCESS
    assert outcome.reason == "verification_passed"
    assert outcome.summary == "测试通过"
    assert outcome.steps == 2
    assert events_of(events, "final_report") == []
    assert events_of(events, "turn_result") == []


def test_tool_turn_max_steps_and_timeout(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script = {"turns": [{"tool_calls": [{"name": "list_files", "arguments": {}}]}]}
    outcome, _, _ = run_chat_turn(
        tmp_path, repo, script, chat_history("sys"), script_name="maxsteps.json", max_steps=1,
    )
    assert outcome.state == TurnState.MAX_STEPS
    assert outcome.reason == "steps_exhausted"
    assert outcome.steps == 1

    class FastFakeClock:
        def __init__(self, step_seconds=10.0):
            self.step_seconds = step_seconds
            self.elapsed = 0.0

        def __call__(self):
            self.elapsed += self.step_seconds
            return self.elapsed

    outcome2, _, _ = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": "x"}]}, chat_history("sys"),
        script_name="timeout.json", timeout_seconds=5.0, clock=FastFakeClock(10.0),
    )
    assert outcome2.state == TurnState.TIMEOUT
    assert outcome2.reason == "deadline_exceeded"


# ---------------------------------------------------------------------------
# 中断修复:Ctrl+C 打断工具轮 → 补齐缺失 tool 结果
# ---------------------------------------------------------------------------


class InterruptingTool:
    """第 2 次调用抛 KeyboardInterrupt(模拟 Ctrl+C 打断工具执行)。"""

    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "interrupting"

    @property
    def description(self):
        return "测试用:第二次调用抛 KeyboardInterrupt"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    def execute(self, args):
        self.calls += 1
        if self.calls >= 2:
            raise KeyboardInterrupt
        return "第一次执行成功"


def test_interrupt_during_tools_fills_missing_receipts(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    tool = InterruptingTool()
    script = {
        "turns": [
            {
                "tool_calls": [
                    {"name": "interrupting", "arguments": {}},
                    {"name": "interrupting", "arguments": {}},
                ]
            }
        ]
    }
    outcome, events, loop = run_chat_turn(
        tmp_path, repo, script, chat_history("sys"), script_name="interrupt.json",
        extra_tool=tool, user_message="跑两个工具",
    )
    assert outcome.state == TurnState.INTERRUPTED
    assert outcome.reason == "user_interrupted"

    messages = loop.messages
    assert_pairs_valid(messages)
    # 第二个调用的中断回执内容
    assistant = next(m for m in messages if m.get("tool_calls"))
    second_id = assistant["tool_calls"][1]["id"]
    replies = [m for m in messages if m.get("role") == "tool"]
    assert any(m["tool_call_id"] == second_id and "中断" in m["content"] for m in replies)

    # 中断轮不发终报事件
    assert events_of(events, "final_report") == []
    assert events_of(events, "turn_result") == []

    # 下一轮(以修复后的历史构造)请求合法且正常 REPLIED
    outcome2, events2, _ = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": "继续完成"}]}, loop.messages,
        script_name="after-interrupt.json",
    )
    assert outcome2.state == TurnState.REPLIED
    assert_pairs_valid(events_of(events2, "llm_request")[0]["messages"])


# ---------------------------------------------------------------------------
# turn 分组裁剪:纯函数 + 循环集成 + 压缩摘要头部保护
# ---------------------------------------------------------------------------


def test_trim_turn_grouping_removes_old_user_turns():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(20):
        msgs.append({"role": "user", "content": f"用户消息{i} " + "x" * 300})
        msgs.append({"role": "assistant", "content": f"回复{i} " + "y" * 300})
    trimmed, removed = trim_history(
        msgs, max_prompt_tokens=1500, keep_last_groups=2, grouping="turn"
    )
    assert len(trimmed) < len(msgs)
    assert _history_tokens(trimmed) <= 1500
    assert trimmed[0]["role"] == "system"  # system 不裁
    assert trimmed[-4:] == msgs[-4:]  # 最近 2 个 user 轮次完整保留
    summaries = [
        m for m in trimmed if m.get("role") == "assistant" and "轮对话" in (m.get("content") or "")
    ]
    assert len(summaries) == 1
    # 被移除的是旧轮次原文
    assert all(m in msgs for m in removed)
    assert sum(1 for m in removed if m.get("role") == "user") >= 10


def test_trim_turn_grouping_default_is_tool_for_batch():
    """默认 grouping='tool':纯文本轮(无交互组)不会被裁——批处理行为不变。"""
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(10):
        msgs.append({"role": "user", "content": f"u{i} " + "x" * 300})
        msgs.append({"role": "assistant", "content": f"a{i} " + "y" * 300})
    trimmed, removed = trim_history(msgs, max_prompt_tokens=1500)
    assert trimmed == msgs and removed == []


def test_trim_turn_grouping_protects_compact_summary_head():
    """压缩后历史 [system, user(SUMMARY_PREFIX+摘要), 轮次…] 的头部受保护。"""
    summary_head = {
        "role": "user",
        "content": COMPACT_SUMMARY_PREFIX + "\n目标:修复 bug\n关键上下文:calc.py",
    }
    turns = []
    for i in range(6):
        turns.append({"role": "user", "content": f"任务{i} " + "u" * 200})
        turns.append({"role": "assistant", "content": f"回复{i} " + "a" * 200})
    msgs = [{"role": "system", "content": "sys"}, summary_head, *turns]
    trimmed, _removed = trim_history(
        msgs, max_prompt_tokens=800, keep_last_groups=2, grouping="turn"
    )
    assert trimmed[0]["role"] == "system"
    assert trimmed[1] is summary_head or trimmed[1]["content"] == summary_head["content"]
    assert trimmed[1]["content"].startswith(COMPACT_SUMMARY_PREFIX)
    assert _history_tokens(trimmed) <= 800
    # 摘要绝不会被机械摘要替换
    assert not any(
        m.get("role") == "assistant" and "轮对话" in (m.get("content") or "") for m in trimmed[:2]
    )


def _interaction_group(tag: str, size: int) -> list[dict]:
    call_id = f"call_{tag}"
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": f"{tag}.py"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "r" * size},
    ]


def test_turn_trim_extreme_overload_falls_back_to_tool_groups_keeps_summary():
    """保底 2 轮也超限 → 只对保留轮内部按 tool 组降载;语义摘要仍在头部不被替换。"""
    summary_head = {
        "role": "user",
        "content": COMPACT_SUMMARY_PREFIX + "\n语义摘要正文",
    }
    turn_groups = []
    for t in range(2):  # 只有 2 轮 = 保底,turn 分组一个也裁不掉
        turn_groups.append({"role": "user", "content": f"任务{t} " + "u" * 200})
        for g in range(4):  # 每轮 4 个交互组
            turn_groups.extend(_interaction_group(f"t{t}g{g}", 2000))
    msgs = [{"role": "system", "content": "sys"}, summary_head, *turn_groups]
    trimmed, removed = trim_history(
        msgs, max_prompt_tokens=3000, keep_last_groups=2, grouping="turn"
    )
    # 头部原样:system + 语义摘要 user 消息
    assert trimmed[0]["role"] == "system"
    assert trimmed[1]["content"].startswith(COMPACT_SUMMARY_PREFIX)
    assert_pairs_valid(trimmed)
    # 机械降载发生了(被移除的 tool 消息),但摘要未被替换
    assert any(m.get("role") == "tool" for m in removed)
    assert trimmed[1] is not None and COMPACT_SUMMARY_PREFIX in trimmed[1]["content"]
    # 机械摘要(若有)出现在摘要头部之后,且不是 user 角色
    mechanical = [m for m in trimmed if "工具交互" in (m.get("content") or "")]
    assert all(m.get("role") == "assistant" for m in mechanical)


def test_chat_loop_trims_with_turn_grouping_and_logs_event(tmp_path):
    """chat 模式超限历史跑一轮:自动裁剪用 turn 分组,context_trim 记录 grouping=turn。"""
    repo = make_repo(tmp_path, files={"README.md": "x"})
    pairs = [(f"用户{i} " + "x" * 800, f"回复{i} " + "y" * 800) for i in range(8)]
    history = chat_history("sys", *pairs)
    outcome, events, _ = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": "好"}]}, history,
        script_name="trim-integration.json", max_context_tokens=1500,
    )
    assert outcome.state == TurnState.REPLIED
    trims = events_of(events, "context_trim")
    assert len(trims) == 1
    assert trims[0]["grouping"] == "turn"
    assert trims[0]["removed_turns"] >= 5
    request = events_of(events, "llm_request")[0]
    assert_pairs_valid(request["messages"])
    assert request["messages"][0]["role"] == "system"
    assert len(request["messages"]) < len(history) + 2  # 确实裁了


# ---------------------------------------------------------------------------
# usage 独立 + on_tool_result
# ---------------------------------------------------------------------------


def test_turn_usage_independent_and_accumulable(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    outcome1, _, loop1 = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": "回复一"}]}, chat_history("sys"),
        script_name="u1.json", user_message="第一问",
    )
    outcome2, _, _ = run_chat_turn(
        tmp_path, repo, {"turns": [{"text": "回复二"}]}, loop1.messages,
        script_name="u2.json", user_message="第二问",
    )
    for outcome in (outcome1, outcome2):
        assert outcome.prompt_tokens is not None and outcome.prompt_tokens > 0
        assert outcome.completion_tokens > 0
        assert outcome.total_tokens >= outcome.prompt_tokens + 0
        assert outcome.model.startswith("fake:")
    # 第二轮的 prompt 明显大于第一轮(历史更长);两轮各自独立、可由会话侧累加
    assert outcome2.prompt_tokens > outcome1.prompt_tokens
    total = outcome1.total_tokens + outcome2.total_tokens
    assert total > 0


def test_on_tool_result_fires_for_exec_skip_and_verifier(tmp_path):
    repo = make_repo(tmp_path, files=GOOD_FILES)
    calls = [
        {"name": "write_file", "arguments": {"path": f"f{i}.txt", "content": str(i)}}
        for i in range(9)  # 8 执行 + 1 超上限 skip
    ]
    script = {
        "turns": [
            {"tool_calls": calls},
            {
                "tool_calls": [
                    {"name": "finish", "arguments": {"result": "success", "summary": "完成"}}
                ]
            },
        ]
    }
    seen: list = []
    outcome, _events, _ = run_chat_turn(
        tmp_path, repo, script, chat_history("sys"), script_name="callback.json",
        test_cmd="python3 -m pytest -q test_calc.py", on_tool_result=seen.append,
    )
    assert outcome.state == TurnState.SUCCESS
    names = [r.name for r in seen]
    # 8 次执行 + 1 次 skip + finish 本身 + verifier 重跑
    assert names.count("write_file") == 9
    assert names.count("finish") == 1
    assert names.count("run_tests") == 1  # verifier
    skipped = [r for r in seen if r.name == "write_file" and "未执行" in r.content]
    assert len(skipped) == 1 and skipped[0].is_error
    # verifier 的结果也回调
    verifier = next(r for r in seen if r.name == "run_tests")
    assert verifier.verification_passed is True


def test_on_tool_result_also_fires_for_skipped_after_rejected_finish(tmp_path):
    """finish(success) 复验被拒后,本轮剩余调用补「未执行」回执并回调。"""
    broken = {
        "calc.py": "def add(a, b):\n    return a - b\n",
        "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    }
    repo = make_repo(tmp_path, files=broken)
    script = {
        "turns": [
            {
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "calc.py"}},
                    {"name": "finish", "arguments": {"result": "success", "summary": "好了"}},
                    {"name": "write_file", "arguments": {"path": "never.txt", "content": "x"}},
                ]
            },
            {"text": "我来继续修复"},
        ]
    }
    seen: list = []
    outcome, _events, _ = run_chat_turn(
        tmp_path, repo, script, chat_history("sys"), script_name="rejected.json",
        test_cmd="python3 -m pytest -q test_calc.py", on_tool_result=seen.append,
    )
    assert outcome.state == TurnState.REPLIED  # 被拒后模型继续,最终文本回复
    skipped = [r for r in seen if r.name == "write_file" and "未执行" in r.content]
    assert len(skipped) == 1
    assert not (repo / "never.txt").exists()
    # 复验 run_tests 与 finish 本身也都回调
    assert any(r.name == "run_tests" for r in seen)
    assert any(r.name == "finish" for r in seen)


# ---------------------------------------------------------------------------
# 批处理零改动:run() 冒烟(完整回归由现有 222 测试保证)
# ---------------------------------------------------------------------------


def test_batch_run_smoke_unchanged(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "x"})
    script_payload = {
        "turns": [
            {
                "tool_calls": [
                    {"name": "finish", "arguments": {"result": "failed", "summary": "结束"}}
                ]
            }
        ]
    }
    provider = create_provider(f"fake:{write_script(tmp_path, script_payload, 'batch.json')}")
    logger = SessionLogger(tmp_path / "runs-batch")
    registry = build_default_registry(repo)
    executor = ToolExecutor(registry=registry, logger=logger)
    loop = AgentLoop(
        provider=provider,
        executor=executor,
        logger=logger,
        system_prompt=SYSTEM_PROMPT,
        task="批处理任务",
    )
    try:
        outcome = loop.run()
        events = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    finally:
        logger.close()
    assert outcome.state.value == "TESTS_FAILED"
    assert outcome.exit_code == 1
    assert events[-1]["type"] == "final_report"  # 批处理照常发终报


# ---------------------------------------------------------------------------
# 步数预算提醒(工单 04):chat 模式不注入——user 消息是轮次边界,
# 注入会干扰 turn 分组裁剪与 /compact 切点
# ---------------------------------------------------------------------------


def test_run_turn_never_injects_budget_reminder(tmp_path):
    repo = make_repo(tmp_path, files={"README.md": "# demo\n"})
    turns = {"turns": [{"tool_calls": [{"name": "list_files", "arguments": {}}]}] * 12}
    outcome, events, _ = run_chat_turn(
        tmp_path,
        repo,
        turns,
        chat_history(),
        max_steps=12,  # 阈值 75% = 9 步:批处理下早已提醒,chat 必须零注入
    )
    assert outcome.state == TurnState.MAX_STEPS
    for request in events_of(events, "llm_request"):
        assert all(
            "预算提醒" not in (m.get("content") or "") for m in request["messages"]
        )
