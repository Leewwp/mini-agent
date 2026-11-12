"""标签规整工具。

normalize_tags 把用户输入的逗号分隔标签字符串整理成规范的标签列表。

已知缺陷:当前实现对「标签两端的空白」与「空标签」(连续逗号、纯空白段)
处理不当,tests/test_normalize_tags.py 中相关用例处于失败状态。
"""

from __future__ import annotations


def normalize_tags(raw: str) -> list[str]:
    """把逗号分隔的标签字符串切分为标签列表。

    期望行为以 tests/test_normalize_tags.py 为准。
    """
    return raw.split(",")
