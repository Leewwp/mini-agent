"""内置工具集:base(协议/注册表)、fs(文件四件套)、exec(命令/测试)。"""

from pathlib import Path

from mini_agent.tools.base import FunctionTool, Tool, ToolError, ToolRegistry
from mini_agent.tools.exec import make_exec_tools
from mini_agent.tools.fs import FsContext, make_fs_tools

__all__ = [
    "FunctionTool",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "FsContext",
    "build_default_registry",
]


def build_default_registry(
    repo: Path,
    *,
    test_cmd: str | None = None,
    command_timeout: float = 120.0,
    deny_patterns: tuple[str, ...] | None = None,
) -> ToolRegistry:
    """装配默认工具集(fs 四件套 + run_command/run_tests;finish 属工单 03)。

    所有工具共享同一 ``FsContext``(先读后写标记、密钥 deny-list)与同一
    仓库围栏;``deny_patterns=None`` 用默认 deny-list(.env 精确名、*.pem、
    id_rsa*),传元组覆盖。
    """
    if deny_patterns is None:
        ctx = FsContext.create(repo)
    else:
        ctx = FsContext.create(repo, deny_patterns=deny_patterns)
    registry = ToolRegistry()
    for tool in make_fs_tools(ctx):
        registry.register(tool)
    for tool in make_exec_tools(repo, test_cmd=test_cmd, timeout=command_timeout):
        registry.register(tool)
    return registry
