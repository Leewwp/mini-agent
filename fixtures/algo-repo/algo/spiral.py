"""矩阵螺旋遍历(算法题)。

实现 ``spiral_order(matrix)``:按顺时针螺旋顺序(右 → 下 → 左 → 上,
逐层向内)遍历二维矩阵,返回元素的一维列表。

约束:
- matrix 是矩形矩阵(list[list[int]]),元素为整数;
- 空矩阵(``[]``)返回 ``[]``;单行/单列矩阵按序返回;
- 不得修改入参矩阵;
- 只依赖 Python 标准库。

完整验收标准见 tests/test_spiral.py——该测试集是隐藏的:任务描述只给出
函数签名与少量示例,边界情形(空矩阵、单行、单列、宽/扁矩阵、入参不可变)
以测试为准。
"""

from __future__ import annotations


def spiral_order(matrix: list[list[int]]) -> list[int]:
    """按顺时针螺旋顺序返回矩阵的全部元素。

    >>> spiral_order([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
    [1, 2, 3, 6, 9, 8, 7, 4, 5]
    >>> spiral_order([])
    []
    """
    raise NotImplementedError("请实现 spiral_order")
