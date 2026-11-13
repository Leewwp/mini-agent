"""mini-agent CLI 入口:模式分发(--task 批处理 / --resume 恢复 / 裸命令 REPL)、
配置装配、Provider 构造与循环运行。

批处理主路径(工单 03/04,行为不变):影子 git 快照(基线)→ 完整循环 →
结束快照对账 → 五要素终报(所有终态,含 ERROR/TIMEOUT/中断,写 session 目录
report.md + diffs/ 并终端打印)。

REPL 主路径(工单 12 / 设计票 08):裸命令进入行内滚动会话;--task 存在 →
批处理(与今天完全一致);--resume <dir> → 恢复会话进 REPL;--task 与
--resume 互斥(exit 5);--repo 省略时取 git 根(失败回退 cwd)。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

from mini_agent.errors import EXIT_CONFIG_ERROR, EXIT_ERROR, ConfigError
from mini_agent.executor import ToolExecutor
from mini_agent.loop import SYSTEM_PROMPT, AgentLoop
from mini_agent.providers import (
    ProviderError,
    ReasoningDelta,
    TextDelta,
    create_provider,
)
from mini_agent.report import (
    build_report,
    read_last_test_run,
    render_terminal,
    write_report,
)
from mini_agent.sandbox import ShadowGitSnapshot
from mini_agent.session import SessionLogger
from mini_agent.tools import build_default_registry
from mini_agent.tools.descriptions import descriptions_for

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 8192
DEFAULT_MAX_STEPS = 20
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_CONTEXT_TOKENS = 60000
DEFAULT_TOOL_DESC_LANG = "zh"


@dataclass
class Config:
    """一次运行的全部配置(CLI flag > 环境变量 > 默认值)。"""

    repo: Path
    task: str | None  # 批处理任务;REPL 模式为 None
    model: str
    base_url: str
    api_key: str | None
    max_steps: int
    test_cmd: str | None
    timeout: int
    max_context_tokens: int
    thinking: bool
    tool_desc_lang: str = DEFAULT_TOOL_DESC_LANG
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    runs_dir: str | None = None  # REPL 会话根目录覆盖(--runs-dir)
    verbose: bool = False  # REPL 工具结果行完整输出(--verbose)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-agent",
        description="本地 CLI 编码 Agent:批处理自主完成编码任务,或裸命令进入交互式 REPL。",
    )
    parser.add_argument(
        "--repo", default=None,
        help="目标仓库路径(省略时取 git 根,非 git 目录用当前目录)",
    )
    parser.add_argument(
        "--task", default=None,
        help="自然语言任务描述;给出即进入批处理模式,省略进入交互式 REPL",
    )
    parser.add_argument(
        "--resume", default=None, metavar="SESSION_DIR",
        help="恢复指定会话目录并进入 REPL(与 --task 互斥;不带参数值时列出最近会话)",
    )
    parser.add_argument(
        "--runs-dir", default=None,
        help="REPL 会话根目录(默认 ~/.mini-agent/sessions;批处理不受影响)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="REPL 工具结果行显示完整输出(默认摘要行)",
    )
    parser.add_argument(
        "--max-steps", type=int, default=DEFAULT_MAX_STEPS,
        help="最大步数(一次 LLM 往返计 1 步,默认 20;REPL 中为每轮预算)",
    )
    parser.add_argument(
        "--test-cmd", default=None,
        help="预配置的测试命令(如 'pytest -q');不提供时成功将无法验证",
    )
    parser.add_argument(
        "--model", default=None,
        help="模型名(默认 deepseek-flash;fake:<script.json> 注入 FakeModel)",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
        help="整体超时秒数(默认 600;REPL 中为每轮预算)",
    )
    parser.add_argument(
        "--max-context-tokens", type=int, default=DEFAULT_MAX_CONTEXT_TOKENS,
        help="历史裁剪阈值,单位 token(默认 60000)",
    )
    parser.add_argument(
        "--thinking", action="store_true",
        help="开启模型思考模式(reasoning 内容仅展示,不参与上下文)",
    )
    return parser


def detect_git_root(start: Path | None = None) -> Path | None:
    """从 start(默认 cwd)探测 git 仓库根(``git rev-parse --show-toplevel``)。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(start if start is not None else Path.cwd()),
             "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root) if root else None


def resolve_repo(args: argparse.Namespace) -> Path:
    """--repo 显式给出永远优先;省略时 git 根,非 git 目录回退 cwd。"""
    if args.repo is not None:
        return Path(args.repo)
    return detect_git_root() or Path.cwd()


def resolve_config(args: argparse.Namespace, env: dict | None = None) -> Config:
    """装配配置:校验 --repo、解析模型三件套(MINI_AGENT_API_KEY / _BASE_URL / _MODEL)。"""
    env = env if env is not None else os.environ
    repo = resolve_repo(args)
    if not repo.is_dir():
        raise ConfigError(f"--repo 路径不存在或不是目录:{args.repo}")
    if args.max_steps < 1:
        raise ConfigError(f"--max-steps 必须不小于 1,收到:{args.max_steps}")
    if args.timeout < 1:
        raise ConfigError(f"--timeout 必须不小于 1 秒,收到:{args.timeout}")
    if args.max_context_tokens < 1:
        raise ConfigError(f"--max-context-tokens 必须为正数,收到:{args.max_context_tokens}")

    model = args.model or env.get("MINI_AGENT_MODEL") or DEFAULT_MODEL
    base_url = env.get("MINI_AGENT_BASE_URL") or DEFAULT_BASE_URL
    api_key = env.get("MINI_AGENT_API_KEY") or None
    tool_desc_lang = env.get("MINI_AGENT_TOOL_DESC_LANG") or DEFAULT_TOOL_DESC_LANG
    descriptions_for(tool_desc_lang)  # 启动期校验(未知语言立即报配置错误)
    if not model.startswith("fake:") and not api_key:
        raise ConfigError(
            "未配置 API key:请设置环境变量 MINI_AGENT_API_KEY"
            "(模型服务地址可用 MINI_AGENT_BASE_URL 覆盖,模型名可用 MINI_AGENT_MODEL 覆盖);"
            "或使用 --model fake:<script.json> 在无 key 环境复现"
        )
    return Config(
        repo=repo,
        task=args.task,
        model=model,
        base_url=base_url,
        api_key=api_key,
        max_steps=args.max_steps,
        test_cmd=args.test_cmd,
        timeout=args.timeout,
        max_context_tokens=args.max_context_tokens,
        thinking=args.thinking,
        tool_desc_lang=tool_desc_lang,
        runs_dir=getattr(args, "runs_dir", None),
        verbose=getattr(args, "verbose", False),
    )


def _stream_handler() -> callable:
    """归一化事件 → 终端:正文逐字上屏(stdout),思考增量走 stderr 仅作展示。"""

    def on_event(event) -> None:
        if isinstance(event, TextDelta):
            sys.stdout.write(event.content)
            sys.stdout.flush()
        elif isinstance(event, ReasoningDelta):
            sys.stderr.write(event.content)
            sys.stderr.flush()

    return on_event


def run(config: Config, provider) -> int:
    """完整循环:快照 → 循环 → 快照对账 → 五要素终报;返回进程退出码。

    所有终态(含 ERROR/TIMEOUT/中断/意外异常)都走同一出口:结束快照、
    写 report.md + diffs/、终端打印终报;失败不自动回滚,改动如实保留。
    """
    with SessionLogger.create(repo=config.repo) as logger:
        display_base_url = "fake" if config.model.startswith("fake:") else config.base_url
        logger.emit(
            "session_meta",
            model=config.model,
            base_url=display_base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            thinking=config.thinking,
            tool_desc_lang=config.tool_desc_lang,
            repo=str(config.repo),
            task=config.task,
            max_steps=config.max_steps,
            test_cmd=config.test_cmd,
            timeout_seconds=config.timeout,
        )
        snapshot = ShadowGitSnapshot.start(config.repo)  # 基线(失败自动降级,不中断)
        registry = build_default_registry(
            config.repo,
            test_cmd=config.test_cmd,
            emit=logger.emit,  # run_tests 的 test_run 事件
            # 英文描述热备:MINI_AGENT_TOOL_DESC_LANG=en 时只换文案不换行为
            tool_descriptions=(
                descriptions_for(config.tool_desc_lang)
                if config.tool_desc_lang != DEFAULT_TOOL_DESC_LANG
                else None
            ),
        )
        executor = ToolExecutor(registry=registry, logger=logger)
        loop = AgentLoop(
            provider=provider,
            executor=executor,
            logger=logger,
            system_prompt=SYSTEM_PROMPT,
            task=config.task,
            max_steps=config.max_steps,
            timeout_seconds=float(config.timeout),
            max_context_tokens=config.max_context_tokens,
            on_activity=lambda line: print(line, flush=True),  # 每步工具活动行
            stream=_stream_handler(),
        )
        try:
            outcome = loop.run()
        except ProviderError as exc:  # 防御:循环应内部消化一切 Provider 错误
            outcome = loop.emergency_error_outcome(str(exc))
        except KeyboardInterrupt:
            outcome = loop.interrupted_outcome()
        except Exception as exc:  # noqa: BLE001 —— 防御:意外内部错误也要有交代(traceback 走 stderr)
            traceback.print_exc()
            outcome = loop.emergency_error_outcome(f"内部错误:{type(exc).__name__}: {exc}")
        changes = snapshot.finish()  # 结束快照 + diff 对账(失败自动降级)
        last_test_run = read_last_test_run(logger.path)  # 事实来源:append-only 日志
        report_text = build_report(
            outcome,
            changes=changes,
            test_cmd=config.test_cmd,
            last_test_run=last_test_run,
            session_dir=logger.session_dir,
            task=config.task,
        )
        write_report(logger.session_dir, report_text, changes)
        print()
        print(
            render_terminal(
                outcome,
                changes=changes,
                test_cmd=config.test_cmd,
                last_test_run=last_test_run,
                session_dir=logger.session_dir,
            )
        )
        return outcome.exit_code


def run_repl(
    config: Config,
    provider,
    *,
    resume_messages: list[dict] | None = None,
    resumed_from: str | None = None,
    stdin=None,
    stdout=None,
) -> int:
    """交互式 REPL 主路径(工单 12 / 设计票 08 装配)。

    - TTY:prompt_toolkit 输入框 + spinner + patch_stdout;非 TTY(测试/管道):
      纯文本输入、无 spinner、无 patch_stdout(渲染层自动降级);
    - 退出码:/exit(含 Ctrl+D/双击 Ctrl+C)→ 0;未捕获异常 → 3 并出会话级报告。
    """
    from mini_agent.repl.app import ReplApp
    from mini_agent.repl.orchestrator import ChatSession
    from mini_agent.repl.prompt import make_plain_input

    input_stream = stdin if stdin is not None else sys.stdin
    output = stdout if stdout is not None else sys.stdout
    session = ChatSession(
        config=config,
        provider=provider,
        output=output,
        runs_dir=Path(config.runs_dir) if config.runs_dir else None,
        resume_messages=resume_messages,
        resumed_from=resumed_from,
    )
    interactive = bool(input_stream.isatty() and output.isatty())
    if interactive:
        from mini_agent.repl.prompt import PROMPT_TEXT, build_prompt_session
        from mini_agent.repl.waiting import WaitingSpinner

        prompt_session = build_prompt_session()

        def input_fn() -> str:
            return prompt_session.prompt(PROMPT_TEXT)

        def spinner_factory(text: str):
            return WaitingSpinner(text)

        patch_stdout_flag = True
    else:
        input_fn = make_plain_input(input_stream)
        spinner_factory = None
        patch_stdout_flag = False
    banner = (
        f"mini-agent 交互会话 · 模型 {config.model} · 仓库 {config.repo.name}\n"
        "直接输入消息开始对话;/help 查看命令;Ctrl+D 或双击 Ctrl+C 退出并生成会话终报"
    )
    app = ReplApp(
        run_turn_fn=session.turn,
        output=output,
        input_fn=input_fn,
        spinner_factory=spinner_factory,
        verbose=config.verbose,
        show_reasoning=config.thinking,
        on_new=session.new_session,
        on_compact=session.compact,  # /compact(工单 13):spinner/中断反馈由 ReplApp 管
        on_exit=session.finish,
        banner=banner,
        patch_stdout=patch_stdout_flag,
    )
    try:
        return app.run()
    except Exception as exc:  # noqa: BLE001 —— 防御:异常退出也出会话级报告
        traceback.print_exc()
        session.finish_error(exc)
        return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # 模式分发(设计票 08 Answer 1):--task → 批处理;--resume → 恢复;裸命令 → REPL
    if args.task is not None and args.resume is not None:
        print(
            "[配置错误] --task(批处理)与 --resume(恢复会话)不能同时使用", file=sys.stderr
        )
        return EXIT_CONFIG_ERROR
    try:
        config = resolve_config(args)
        provider = create_provider(
            config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            thinking=config.thinking,
        )
        resume_messages = None
        resumed_from = None
        if args.resume is not None:
            from mini_agent.resume import load_resume_messages

            resume_messages = load_resume_messages(Path(args.resume))
            resumed_from = str(Path(args.resume).resolve())
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.task is not None:
        return run(config, provider)  # 批处理:行为与今天完全一致
    return run_repl(
        config, provider, resume_messages=resume_messages, resumed_from=resumed_from
    )


if __name__ == "__main__":
    sys.exit(main())
