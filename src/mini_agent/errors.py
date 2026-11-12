"""mini-agent 异常与退出码。

退出码(spec):0=SUCCESS 1=TESTS_FAILED 2=MAX_STEPS 3=ERROR
4=TIMEOUT 5=CONFIG_ERROR 6=UNVERIFIED。
"""

from __future__ import annotations

EXIT_SUCCESS = 0
EXIT_TESTS_FAILED = 1
EXIT_MAX_STEPS = 2
EXIT_ERROR = 3
EXIT_TIMEOUT = 4
EXIT_CONFIG_ERROR = 5
EXIT_UNVERIFIED = 6


class ConfigError(Exception):
    """配置错误(缺 API key、非法 fake: 脚本、--repo 不存在等),退出码 5。"""
