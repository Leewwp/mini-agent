# tags-repo:normalize_tags 修 bug 演示仓库

这是 mini-agent 的内置 fixture 之一,复刻项目需求命令行示例中的场景:

```
mini-agent --repo ./sample-repo \
  --task "修复 normalize_tags 对空白和空标签的处理,运行测试验证" \
  --max-steps 20
```

## 场景说明

- `tags_utils/normalize.py::normalize_tags` 把逗号分隔的标签字符串切成列表;
- 已知缺陷:当前实现只做 `raw.split(",")`——标签两端的空白原样保留,
  连续逗号/纯空白段产生的空标签也不丢弃;
- `tests/test_normalize_tags.py` 是完整验收标准,其中 3 个用例
  (`test_strips_whitespace` / `test_drops_empty_tags` / `test_all_empty`)
  初始即失败,修复目标是让全部 6 个用例通过。

## 彩排命令

**务必先复制到临时目录再运行**——mini-agent 会真实写入 `--repo` 且不回滚,
fixture 仓库本身必须保持初始(失败)状态:

```bash
# 在 mini-agent 仓库根目录(需要一个带 pytest 的 Python 环境)
cp -r fixtures/tags-repo /tmp/tags-repo-demo
MINI_AGENT_API_KEY=sk-... uv run mini-agent \
  --repo /tmp/tags-repo-demo \
  --task "修复 normalize_tags 对空白和空标签的处理,运行测试验证" \
  --test-cmd "pytest -q" \
  --max-steps 20
```

无 key 时可用 FakeModel 走同一条代码路径(见 `examples/fix-cycle.json`):

```bash
cp -r fixtures/tags-repo /tmp/tags-repo-demo
uv run mini-agent \
  --repo /tmp/tags-repo-demo \
  --task "修复 normalize_tags 对空白和空标签的处理,运行测试验证" \
  --test-cmd "pytest -q" \
  --model fake:examples/fix-cycle.json
```

## 预期结果

终态 `SUCCESS`(退出码 0):模型读取 `normalize.py` 与测试,把实现修复为
「strip 两端空白 + 丢弃空标签」,全部测试通过并经声明时复验确认。
真实彩排记录(轮数/步数/token/终态)见 `../REHEARSALS.md`。
