# algo-repo:算法题实现演示仓库

mini-agent 的内置 fixture 之二:算法题「从零实现一个指定函数」,演示
`run_command`(跑辅助脚本自查)+ `run_tests`(唯一计入成功判定的验证)的配合。

## 场景说明

- 任务:在 `algo/spiral.py` 中实现 `spiral_order(matrix)`——按顺时针螺旋
  顺序(右→下→左→上,逐层向内)遍历矩形矩阵,返回元素的一维列表;
- 初始状态:`raise NotImplementedError`,全部测试失败;
- **隐藏测试集**:`tests/test_spiral.py` 是完整验收标准,但任务描述只给出
  函数签名、两个 doctest 示例与约束(空矩阵、单行/单列、不得改入参、只准
  标准库)——宽/扁矩阵、全元素恰好一次、负数等边界情形不剧透,以测试为准。

## 彩排命令

**务必先复制到临时目录再运行**(agent 真实写入,不回滚;fixture 保持未实现状态):

```bash
cp -r fixtures/algo-repo /tmp/algo-repo-demo
MINI_AGENT_API_KEY=sk-... uv run mini-agent \
  --repo /tmp/algo-repo-demo \
  --task "在 algo/spiral.py 中实现 spiral_order(matrix):按顺时针螺旋顺序遍历矩阵并返回元素列表,用测试验证" \
  --test-cmd "pytest -q" \
  --max-steps 20
```

## 预期结果

终态 `SUCCESS`(退出码 0):模型实现 `spiral_order`,隐藏测试集 11 个用例
全部通过并经声明时复验确认。真实彩排记录见 `../REHEARSALS.md`。
