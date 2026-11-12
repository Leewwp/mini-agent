"""normalize_tags 的行为规约(修 bug 任务的验收标准)。

其中 test_strips_whitespace / test_drops_empty_tags / test_all_empty
在 fixture 初始状态下失败——它们就是要修的 bug。
"""

from tags_utils import normalize_tags


def test_basic_split():
    assert normalize_tags("python,go,rust") == ["python", "go", "rust"]


def test_single_tag():
    assert normalize_tags("solo") == ["solo"]


def test_strips_whitespace():
    # 标签两端的空白应去除(空格、制表符)
    assert normalize_tags(" python , go ,rust ") == ["python", "go", "rust"]
    assert normalize_tags("\tpython\t,go") == ["python", "go"]


def test_drops_empty_tags():
    # 连续逗号与纯空白段产生的空标签应被丢弃
    assert normalize_tags("a,,b") == ["a", "b"]
    assert normalize_tags("a, , b") == ["a", "b"]


def test_all_empty():
    # 全部为空标签时返回空列表
    assert normalize_tags(" , , ") == []


def test_preserves_order_and_case():
    # 不去重、不改大小写,保持出现顺序
    assert normalize_tags("Go,go,Python") == ["Go", "go", "Python"]
