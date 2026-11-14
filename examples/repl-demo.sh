#!/usr/bin/env bash
# REPL 演示/录屏脚本(工单 16 / 设计票 09)。
#
# 用法:
#   ./examples/repl-demo.sh <mini-agent 仓库路径>            # 交互彩排(直接进 REPL)
#   ./examples/repl-demo.sh <mini-agent 仓库路径> --record   # 用 expect 驱动 + asciinema 录制
#                                                             # (产物 docs/demos/repl-session.cast)
#
# 演示路径:多轮对话(读仓库/修 bug/贴代码)→ /compact(真实语义压缩)
# → 压缩后继续对话 → /exit(会话终报)。全程真实模型(deepseek-flash)。
#
# 注意:
# - API key 经环境变量 MINI_AGENT_API_KEY 注入,绝不出现在命令行/录屏/日志;
# - fixture 一律先 cp 到 /tmp 副本再操作,绝不动仓库内 fixture;
# - 录屏用 --runs-dir 指到 /tmp,画面不出现用户主目录路径;
# - expect 驱动只用整块 send(不用 send_slow):send_slow 会把多字节 UTF-8
#   中文从字节中间切开,prompt_toolkit 的输入解码被破坏后整条会话失联
#   (实测结论,见工单 16 Comments)。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${2:-interactive}"
DEMO_REPO=/tmp/mini-agent-repl-demo
DEMO_RUNS=/tmp/mini-agent-repl-demo-runs

# 1) fixture 副本 + 加长 README(读入后上下文足够,/compact 才会真实压缩)
rm -rf "$DEMO_REPO" "$DEMO_RUNS"
cp -r "$ROOT/fixtures/tags-repo" "$DEMO_REPO"
cat >> "$DEMO_REPO/README.md" <<'EOF'

## 使用示例

```python
from tags_utils import normalize_tags

normalize_tags("python,go,rust")        # ["python", "go", "rust"]
normalize_tags(" python , go ,,rust ")  # ["python", "go", "rust"]
normalize_tags(" , , ")                 # []
```

## 行为规约(与 tests/test_normalize_tags.py 一一对应)

1. 逗号是唯一分隔符;2. 无逗号时整串为一个标签;3. 去两端空白;
4. 空标签一律丢弃;5. 全空返回 [];6. 不去重、不改大小写、保序。
EOF

export PATH="$ROOT/.venv/bin:$PATH"

if [[ "$MODE" == "interactive" ]]; then
  echo "== REPL 交互彩排:repo=$DEMO_REPO(建议依次演示:读仓库→修 bug→贴代码→/compact→总结→/exit)=="
  exec mini-agent --repo "$DEMO_REPO" --test-cmd "pytest -q" --runs-dir "$DEMO_RUNS"
fi

# 2) --record:expect 驱动(整块 send;等待轮末状态行)+ asciinema 录制
DRIVER=$(mktemp /tmp/mini-agent-repl-drive.XXXXXX.exp)
cat > "$DRIVER" <<'EXPECT_SCRIPT'
#!/usr/bin/expect -f
# 用法: expect <本文件> <repo-dir> <runs-dir>
set repo [lindex $argv 0]
set runs [lindex $argv 1]
set timeout -1
set stty_init "cols 100 rows 40"

proc await_turn_end {label} {
    expect {
        -re "✓ (已回复|完成)" {}
        -re "✗ (测试未通过|MAX_STEPS|本轮超时|本轮出错)" {}
        -re "⚠ 未配置" {}
        -re "● 本轮已中断" {}
        timeout { puts "\n\[drive] TIMEOUT at $label"; exit 1 }
    }
    expect -re "❯"
}

spawn mini-agent --repo $repo --test-cmd "pytest -q" --runs-dir $runs
expect -re "❯"
after 700

send "看看仓库结构,通读 normalize_tags 的实现和全部测试,跑一次基线,然后告诉我问题出在哪。\r"
await_turn_end "turn1"
after 900

send "好,请修复它并跑测试验证,完成后声明结束。\r"
await_turn_end "turn2"
after 900

send "把修复后的 normalize.py 全文和 tests/test_normalize_tags.py 全文都贴出来,解释改动,并说明每个用例为什么现在能通过。\r"
await_turn_end "turn3"
after 900

send "/compact\r"
expect {
    -re "(已压缩|很短|压缩失败|已中断压缩)" {}
    timeout { puts "\n\[drive] TIMEOUT at compact"; exit 1 }
}
expect -re "❯"
after 900

send "用一句话总结这个会话做了什么。\r"
await_turn_end "turn4"
after 700

send "/exit\r"
expect {
    -re "会话终报" {}
    timeout { puts "\n\[drive] TIMEOUT at exit"; exit 1 }
}
expect EOF
EXPECT_SCRIPT

echo "== 录制:asciinema → docs/demos/repl-session.cast =="
cd "$ROOT"
asciinema rec --overwrite --idle-time-limit 1.5 \
  -c "expect $DRIVER $DEMO_REPO $DEMO_RUNS" \
  docs/demos/repl-session.cast
rm -f "$DRIVER"
echo "== 完成:asciinema play docs/demos/repl-session.cast =="
