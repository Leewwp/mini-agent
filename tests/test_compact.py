"""/compact(工单 13 / 设计票 04):FakeModel 驱动,覆盖成功/各失败路径/
短历史/连续压缩/_last_prompt_tokens 口径/中断/集成(REPL 命令 + 会话报告单列)。
"""

import io
import json

from mini_agent.cli import build_arg_parser, resolve_config, run_repl
from mini_agent.loop import COMPACT_SUMMARY_PREFIX
from mini_agent.providers import FakeProvider, FakeTurn
from mini_agent.repl.compact import (
    KEEP_TURNS,
    MAX_SUMMARY_TOKENS,
    compact_history,
    serialize_conversation,
)
from mini_agent.repl.orchestrator import ChatSession, repo_slug
from mini_agent.session import SessionLogger


def make_history(turns: int = 4, *, big: bool = True) -> list[dict]:
    """[system, (user, assistant)*N];大文本保证估算超 5k token。"""
    filler = ("x" * 5000 + "内容") if big else "短"
    messages: list[dict] = [{"role": "system", "content": "chat-system"}]
    for i in range(turns):
        messages.append({"role": "user", "content": f"任务{i} {filler}"})
        messages.append({"role": "assistant", "content": f"回复{i} {filler}"})
    return messages


def read_events(logger: SessionLogger) -> list[dict]:
    return [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]


def events_of(events, type_: str) -> list:
    return [e for e in events if e.get("type") == type_]


def run_compact(turns_payload, messages, tmp_path, *, instructions=""):
    provider = FakeProvider([FakeTurn(**t) for t in turns_payload])
    logger = SessionLogger(tmp_path / "runs")
    try:
        result = compact_history(
            provider=provider, logger=logger, messages=messages, instructions=instructions
        )
        events = read_events(logger)
    finally:
        logger.close()
    return result, events


GOOD_SUMMARY = "## 目标\n修复 calc.py\n## 关键上下文\ncalc.py 的 add 返回 a-b 是 bug"


# ---------------------------------------------------------------------------
# 成功路径:结构 + 事件
# ---------------------------------------------------------------------------


def test_compact_success_structure_and_events(tmp_path):
    messages = make_history(4)
    result, events = run_compact([{"text": GOOD_SUMMARY}], messages, tmp_path)
    assert result.ok, result.message
    new = result.new_messages
    # 结构定死:[system(不动), user(SUMMARY_PREFIX+摘要), 最近 2 个完整 user 轮次]
    assert new[0] == messages[0]
    assert new[1]["role"] == "user"
    assert new[1]["content"].startswith(COMPACT_SUMMARY_PREFIX)
    assert GOOD_SUMMARY in new[1]["content"]
    kept_users = [m for m in new[2:] if m["role"] == "user"]
    assert len(kept_users) == KEEP_TURNS
    assert new[2:] == messages[-4:]  # 最近 2 轮 = 最后 4 条消息原样
    # 事件:compact_request → compact_response → compact(仅成功落地)
    kinds = [e["type"] for e in events]
    assert kinds == ["compact_request", "compact_response", "compact"]
    compact_event = events[2]
    assert compact_event["trigger"] == "manual"
    assert compact_event["summary"] == GOOD_SUMMARY
    assert compact_event["replaced_messages"] == messages  # 全文可还原
    assert compact_event["kept_turns"] == KEEP_TURNS
    assert compact_event["tokens_before"] > compact_event["tokens_after"]
    assert compact_event["model"].startswith("fake")
    assert compact_event["usage"]["total_tokens"] > 0
    # context_trim 语义不变:压缩不经过自动裁剪
    assert events_of(events, "context_trim") == []


def test_compact_summary_prompt_contains_structure_and_conversation(tmp_path):
    """摘要 prompt:pi 六段 + <conversation> 序列化 + handoff + 聚焦指令。"""

    class RecordingProvider:
        def __init__(self):
            self.calls = []

        @property
        def model(self):
            return "fake-rec"

        def complete(self, messages, *, tools=None, on_event=None):
            self.calls.append({"messages": messages, "tools": tools})
            from mini_agent.providers import LLMResponse

            return LLMResponse(text=GOOD_SUMMARY)

    messages = make_history(3)
    logger = SessionLogger(tmp_path / "runs")
    provider = RecordingProvider()
    try:
        result = compact_history(
            provider=provider, logger=logger, messages=messages, instructions="只保留测试上下文"
        )
    finally:
        logger.close()
    assert result.ok
    call = provider.calls[0]
    assert call["tools"] is None  # 摘要调用不带工具
    system_text = call["messages"][0]["content"]
    user_text = call["messages"][1]["content"]
    assert "不要继续这段对话" in system_text
    for section in ("## 目标", "## 约束与偏好", "## 进展", "## 关键决策", "## 下一步", "## 关键上下文"):
        assert section in user_text
    assert "<conversation>" in user_text and "</conversation>" in user_text
    assert "[用户] 任务0" in user_text and "[助手] 回复0" in user_text
    assert "保留精确的文件路径、函数名" in user_text
    assert "接手继续工作" in user_text  # Codex handoff 语义
    assert "只保留测试上下文" in user_text  # 聚焦指令拼接模板尾


# ---------------------------------------------------------------------------
# 失败路径:全部不动历史、不写 compact 事件
# ---------------------------------------------------------------------------


def test_compact_short_history_skips(tmp_path):
    few = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"},
           {"role": "assistant", "content": "ok"}]
    result, events = run_compact([{"text": "x"}], few, tmp_path)
    assert not result.ok and result.skipped
    assert "很短" in result.message
    assert events == []  # 连 compact_request 都不记(未发起调用)

    small = make_history(4, big=False)  # ≥6 条但 <5k token
    result2, events2 = run_compact([{"text": "x"}], small, tmp_path)
    assert result2.skipped and events2 == []


def test_compact_empty_response_fails(tmp_path):
    messages = make_history(3)
    result, events = run_compact([{"text": ""}], messages, tmp_path)
    assert not result.ok and "为空" in result.message
    assert result.new_messages is None
    kinds = [e["type"] for e in events]
    assert kinds == ["compact_request", "compact_response"]  # 无 compact 事件
    assert events_of(events, "compact_response")[0]["text"] == ""


def test_compact_tool_calls_response_fails(tmp_path):
    messages = make_history(3)
    result, events = run_compact(
        [{"text": "我来调用工具", "tool_calls": [{"name": "list_files", "arguments": {}}]}], messages, tmp_path
    )
    assert not result.ok and "工具调用" in result.message
    assert events_of(events, "compact") == []


def test_compact_truncated_response_fails(tmp_path):
    """finish_reason=length → ProviderError → 失败不动历史。"""
    messages = make_history(3)
    result, events = run_compact([{"text": "半截", "finish_reason": "length"}], messages, tmp_path)
    assert not result.ok and "摘要调用失败" in result.message
    assert events_of(events, "compact") == []
    assert "error" in events_of(events, "compact_response")[0]


def test_compact_oversized_summary_rejected(tmp_path):
    messages = make_history(3)
    huge_summary = "摘要" + "细节" * (MAX_SUMMARY_TOKENS * 2)
    result, events = run_compact([{"text": huge_summary}], messages, tmp_path)
    assert not result.ok and "摘要超限" in result.message
    assert "聚焦指令" in result.message  # 可换聚焦指令重试的提示
    assert events_of(events, "compact") == []


def test_compact_keyboard_interrupt_leaves_history_untouched(tmp_path):
    messages = make_history(3)

    class InterruptingProvider:
        @property
        def model(self):
            return "fake-int"

        def complete(self, msgs, *, tools=None, on_event=None):
            raise KeyboardInterrupt

    logger = SessionLogger(tmp_path / "runs")
    try:
        try:
            compact_history(provider=InterruptingProvider(), logger=logger, messages=messages)
        except KeyboardInterrupt:
            pass  # REPL 的中断路径(已中断压缩:历史保持原样)
        events = read_events(logger)
    finally:
        logger.close()
    assert [e["type"] for e in events] == ["compact_request"]  # 无 response/compact
    # 历史对象未被修改(调用方仍持有原列表)
    assert len(messages) == 7


# ---------------------------------------------------------------------------
# 连续 /compact:旧摘要并入 conversation 首条
# ---------------------------------------------------------------------------


def test_serialize_conversation_marks_old_summary_first():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": COMPACT_SUMMARY_PREFIX + "\n旧摘要正文"},
        {"role": "user", "content": "新任务"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "run_tests", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "退出码 0"},
    ]
    text = serialize_conversation(messages)
    lines = text.splitlines()
    assert lines[0].startswith("[会话摘要]")
    assert "[用户] 新任务" in lines
    assert "[工具调用] run_tests({})" in text
    assert "[工具结果] 退出码 0" in text


def test_consecutive_compact_replaces_old_summary(tmp_path):
    messages = make_history(4)
    result1, _ = run_compact([{"text": "第一份摘要:修了 A"}], messages, tmp_path)
    assert result1.ok
    # 第二次压缩:历史已含旧摘要;新 conversation 应包含旧摘要且新历史只有一份摘要
    result2, _events2 = run_compact([{"text": "第二份摘要:修了 B(含 A)"}], result1.new_messages, tmp_path)
    assert result2.ok
    new = result2.new_messages
    summaries = [m for m in new if str(m.get("content", "")).startswith(COMPACT_SUMMARY_PREFIX)]
    assert len(summaries) == 1
    assert "第二份摘要" in summaries[0]["content"]
    assert new[2:] == result1.new_messages[-4:]  # 最近 2 轮原样
    # 旧摘要在第二次的 conversation 里(compact_request 不记全文,经 ChatSession 集成验证)


def test_consecutive_compact_old_summary_in_conversation(tmp_path):
    """集成验证:第二次压缩的 summary 输入包含旧摘要条目。"""

    class RecordingProvider:
        def __init__(self, text):
            self.text = text
            self.seen = []

        @property
        def model(self):
            return "fake-rec2"

        def complete(self, messages, *, tools=None, on_event=None):
            self.seen.append(messages)
            from mini_agent.providers import LLMResponse

            return LLMResponse(text=self.text)

    messages = make_history(4)
    provider = RecordingProvider("第一份摘要")
    logger = SessionLogger(tmp_path / "runs")
    try:
        first = compact_history(provider=provider, logger=logger, messages=messages)
        assert first.ok
        second = compact_history(provider=provider, logger=logger, messages=first.new_messages)
        assert second.ok
    finally:
        logger.close()
    second_input = provider.seen[1][1]["content"]
    assert "[会话摘要] " + COMPACT_SUMMARY_PREFIX in second_input  # 旧摘要并入首条
    assert "[用户] 任务" in second_input  # 保留轮次也进 conversation


# ---------------------------------------------------------------------------
# 集成:ChatSession.compact / REPL 命令 / 会话报告单列 / 不误触发自动裁剪
# ---------------------------------------------------------------------------


def _session(tmp_path, repo, script, script_name="s.json"):
    path = tmp_path / script_name
    path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    argv = [
        "--repo", str(repo), "--model", f"fake:{path}",
        "--runs-dir", str(tmp_path / "runs"),
    ]
    config = resolve_config(build_arg_parser().parse_args(argv), env={})
    from mini_agent.providers import create_provider

    output = io.StringIO()
    session = ChatSession(
        config=config, provider=create_provider(config.model), output=output,
        runs_dir=tmp_path / "runs",
    )
    return session, output


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("x", encoding="utf-8")
    return repo


def quiet_renderer(output):
    from mini_agent.repl.render import Palette, ReplRenderer, SpinnerHandle

    return ReplRenderer(output, palette=Palette(enabled=False), spinner=SpinnerHandle(None))


def test_after_compact_next_turn_not_prematurely_trimmed(tmp_path):
    """压缩后下一轮不误触发自动裁剪(每轮新实例天然无 stale _last_prompt_tokens)。"""
    repo = make_repo(tmp_path)
    filler = "x" * 3500  # 每条消息约 875 token;4 轮 ≈ 7000(>5k 可压缩,<8k 不裁剪)
    summary_text = "## 目标\n继续修复\n## 关键上下文\ncalc.py add 有 bug"
    script = {
        "turns": [
            {"text": f"回复{i} {filler}"} for i in range(4)
        ] + [{"text": summary_text}, {"text": "压缩后的回复"}]
    }
    path = tmp_path / "notrim.json"
    path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    argv = ["--repo", str(repo), "--model", f"fake:{path}", "--runs-dir", str(tmp_path / "runs")]
    config = resolve_config(build_arg_parser().parse_args(argv), env={})
    import dataclasses

    config = dataclasses.replace(config, max_context_tokens=8000)
    from mini_agent.providers import create_provider

    output = io.StringIO()
    session = ChatSession(
        config=config, provider=create_provider(config.model),
        output=output, runs_dir=tmp_path / "runs",
    )
    renderer = quiet_renderer(output)
    for i in range(4):
        session.turn(f"问题{i} {filler}", renderer)
    events_mid = read_events_for(session)
    assert count_type(events_mid, "context_trim") == 0  # 压缩前未触阈值

    session.compact("")
    assert count_type(read_events_for(session), "compact") == 1

    session.turn("压缩后新问题", renderer)
    events_final = read_events_for(session)
    assert count_type(events_final, "context_trim") == 0  # 压缩后不误触发
    session.finish()


def test_fresh_turn_loop_has_no_stale_last_prompt_tokens(tmp_path):
    """架构保证:每轮新实例,上一轮的 _last_prompt_tokens 不进入下一轮。"""
    from mini_agent.executor import ToolExecutor
    from mini_agent.loop import SYSTEM_PROMPT, AgentLoop
    from mini_agent.providers import create_provider
    from mini_agent.tools import build_default_registry

    repo = make_repo(tmp_path)
    path = tmp_path / "stale.json"
    path.write_text(json.dumps({"turns": [{"text": "r1"}, {"text": "r2"}]}, ensure_ascii=False), encoding="utf-8")
    logger = SessionLogger(tmp_path / "runs-stale")
    registry = build_default_registry(repo)
    loop1 = AgentLoop(
        provider=create_provider(f"fake:{path}"), executor=ToolExecutor(registry=registry, logger=logger),
        logger=logger, system_prompt=SYSTEM_PROMPT,
        initial_messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        chat_mode=True,
    )
    loop1.run_turn("第一问")
    assert loop1._last_prompt_tokens is not None
    loop2 = AgentLoop(
        provider=create_provider(f"fake:{path}"), executor=ToolExecutor(registry=registry, logger=logger),
        logger=logger, system_prompt=SYSTEM_PROMPT, initial_messages=loop1.messages, chat_mode=True,
    )
    assert loop2._last_prompt_tokens is None  # 新实例初始状态干净
    logger.close()


def read_events_for(session):
    path = session.session_dir / "session.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def count_type(events, type_):
    return sum(1 for e in events if e.get("type") == type_)


def test_compact_via_repl_command_reports_and_sums_usage(tmp_path):
    repo = make_repo(tmp_path)
    filler = "x" * 3000
    script = {
        "turns": [
            {"text": f"回复{i} {filler}"} for i in range(4)
        ] + [{"text": "## 目标\n修复测试\n## 关键上下文\ntest_calc.py 断言 add(1,2)==3"}, {"text": "压缩后回复"}]
    }
    path = tmp_path / "repl.json"
    path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    argv = [
        "--repo", str(repo), "--model", f"fake:{path}",
        "--runs-dir", str(tmp_path / "runs"),
    ]
    config = resolve_config(build_arg_parser().parse_args(argv), env={})
    from mini_agent.providers import create_provider

    output = io.StringIO()
    stdin_lines = [f"问题{i} {filler}" for i in range(1, 5)] + ["/compact", "继续", "/exit"]
    code = run_repl(
        config, create_provider(config.model),
        stdin=io.StringIO("\n".join(stdin_lines) + "\n"),
        stdout=output,
    )
    assert code == 0
    text = output.getvalue()
    assert "已压缩:" in text
    assert "其中压缩摘要" in text  # /compact 完成提示单列
    report = next((tmp_path / "runs" / repo_slug(repo)).iterdir()) / "report.md"
    content = report.read_text(encoding="utf-8")
    assert "其中压缩摘要" in content and "tok" in content  # 会话终报单列
