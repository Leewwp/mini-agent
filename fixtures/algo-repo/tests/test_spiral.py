"""spiral_order 的完整验收标准(隐藏测试集)。

fixture 初始状态下全部用例因 NotImplementedError 失败。
"""

from algo.spiral import spiral_order


def test_example_3x3():
    assert spiral_order([[1, 2, 3], [4, 5, 6], [7, 8, 9]]) == [1, 2, 3, 6, 9, 8, 7, 4, 5]


def test_wide_matrix_3x4():
    assert spiral_order([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]) == [
        1, 2, 3, 4, 8, 12, 11, 10, 9, 5, 6, 7,
    ]


def test_tall_matrix_4x3():
    assert spiral_order([[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]]) == [
        1, 2, 3, 6, 9, 12, 11, 10, 7, 4, 5, 8,
    ]


def test_single_row():
    assert spiral_order([[1, 2, 3]]) == [1, 2, 3]


def test_single_column():
    assert spiral_order([[1], [2], [3]]) == [1, 2, 3]


def test_single_element():
    assert spiral_order([[42]]) == [42]


def test_empty_matrix():
    assert spiral_order([]) == []


def test_two_by_two():
    assert spiral_order([[1, 2], [3, 4]]) == [1, 2, 4, 3]


def test_returns_all_elements_exactly_once():
    matrix = [[i * 5 + j for j in range(5)] for i in range(5)]
    result = spiral_order(matrix)
    assert sorted(result) == sorted(v for row in matrix for v in row)
    assert len(result) == 25


def test_does_not_mutate_input():
    matrix = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
    snapshot = [row[:] for row in matrix]
    spiral_order(matrix)
    assert matrix == snapshot


def test_negative_and_large_values():
    matrix = [[-1, -2], [-3, -4]]
    assert spiral_order(matrix) == [-1, -2, -4, -3]
