# examples

离线复现材料(FakeModel 脚本;asciinema 录屏见 [`docs/demos/`](../docs/demos/))。

- `fix-cycle.json` — 修复循环的脚本化回放:`list_files → read_file ×2 →
  write_file → run_tests → finish(success)`,针对 `fixtures/tags-repo` 的
  临时副本。无 key、无网络即可完整走通 CLI → 循环 → 围栏 → 复验 → 终报
  全部生产代码路径(`tests/test_fixtures.py` 以真实子进程断言)。

用法:

```bash
cp -r fixtures/tags-repo /tmp/tags-repo-demo
uv run mini-agent \
  --repo /tmp/tags-repo-demo \
  --task "修复 normalize_tags 对空白和空标签的处理,运行测试验证" \
  --test-cmd "pytest -q" \
  --model fake:examples/fix-cycle.json
# 终态:SUCCESS(退出码 0)
```
