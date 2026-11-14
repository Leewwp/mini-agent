# AI 使用声明(AI Usage Disclosure)

> **TL;DR**:本项目的全部代码、测试与文档初稿均由 AI 编程助手生成;作者(Leewwp)
> 的贡献是需求定义、方向决策、逐票复核与最终把关。没有 AI,这个仓库不存在;
> 没有作者的决策与复核,它会是一个方向失控的仓库。以下如实展开。

## 1. 开发方式总览

本项目采用「spec 驱动 + 多工单 + 子 Agent 实现」的 AI agent 流程开发:

1. **规划阶段**(AI 助手提问、作者拍板):三轮结构化确认(grill)逐项敲定语言、
   模型接入、框架取舍(不用任何 Agent 框架)、工具集、沙箱口径、停止条件、
   演示形态;随后产出 spec 与 7 张带验收清单的工单。
2. **实现阶段**(每票一个独立实现者子 Agent):工单 01–07 各由一个独立的实现者
   子 Agent 在独立 worktree/分支上完成,全程 TDD(红→绿,每切片一提交),
   由 merger 子 Agent 合入集成分支。作者在每票收尾时复核验收证据。
3. **运行时验证**:工单 05 用真实模型(deepseek-flash)做了三条演示路径各两次
   的彩排(记录见 [`fixtures/REHEARSALS.md`](fixtures/REHEARSALS.md));
   其余开发全程用脚本化 FakeModel,无网络依赖。

**REPL 轮次(2026-11-14)**:同一流程追加一轮——先由 AI 产出两份调研
(压缩/记忆的开源实现对照、行内式 CLI UI 的可模仿实现,含文件/行号级引用)
与 7 张设计工单,作者逐项 grilling 拍板并经两轮独立评审修订(5 项修订 + 5 项
一句话裁决,实现期不再重议);随后按钉死顺序(轮次化循环 → UI → 装配 →
/compact → AGENTS.md 记忆 → 会话恢复 → 发布物)以 TDD 实现,每票验收证据
入工单。REPL 新增约 100 个测试(FakeModel 驱动、无 TTY、无网络),批处理
行为与既有测试零改动。

> **工单口径**:本文与 DESIGN.md 提及的「工单 01–11」是开发期内部工作区的
> 工单序列(各带验收清单与完成记录),原文未随本仓库发布;各工单的结论与
> 证据已沉淀在 [DESIGN.md](DESIGN.md)、测试套件与提交历史中。

## 2. 使用的助手与工具清单

| 助手/工具 | 承担的作用 |
|---|---|
| ZCode(AI 编程 agent CLI,由 GLM 模型驱动) | 全程主力:需求梳理与三轮结构化确认、架构调研、spec 与工单撰写、每票的 TDD 实现、代码审查、工单合并、文档定稿 |
| 其中的子 Agent 机制 | 实现者子 Agent(每票独立上下文,避免互相污染)、审查子 Agent(方案评审)、merger 子 Agent(合并) |
| tdd / implement-spec 等技能流程 | 红→绿纪律、spec 驱动的多工单拆分 |
| curl 直连 DeepSeek API(工单 01 前;REPL 轮补测) | 实测 function calling / 流式分片形状 / thinking 开关 / `finish_reason=length` 行为,结论作为 FakeModel 分片形状的事实基准;2026-11-14 补充实测「连续 user 消息」结构(压缩后历史形态)可用 |
| deepseek-flash(运行时) | **注意区分**:这是 mini-agent 这个程序运行时所调用的模型,不是开发助手;调用条件与 token 用量按项目约定记入每次终报与 `session.jsonl` |
| ruff / pytest / GitHub Actions | 质量门(均由 AI 按工单配置,作者验收) |

## 3. 作者(人)的角色:决策、复核、方向修正

以下关键决策均由作者作出,其中数项**推翻了 AI 助手的推荐**:

- **流式输出**(推荐非流式以降低复杂度;作者要现场演示观感,拍板 stream=True,
  代价是流式 tool-call 增量聚合成为 V1 最难实现的一处);
- **git 快照为主**(推荐自跟踪原文件副本以支持非 git 目录;作者选影子 git 快照,
  自跟踪降级为 stretch);
- **中文优先**(推荐英文 prompt;作者要求中文 system prompt + 中文工具描述 +
  中文界面,对齐现场演示要求;英文工具描述保留为热备);
- **独立仓库 + 严格泄露防护**(开发工作区任何无关内容绝不上传);
- **两版策略**(V1 最小闭环 + 四个扩展接缝,V2 视余量追加);
- README 英文为主 + 中文对照、MIT 协议、演示任务三级梯度(fixture 修 bug /
  算法题 / 诚实失败)等交付形态。

REPL 轮的关键作者决策(均经逐项拍板,详见设计工单):REPL 成为主推使用方式
而批处理零改动(核心场景);轮次化选「每轮新建实例」形态;`/compact`
摘要「user 角色 + system 后最前」的混合方案;记忆只做 AGENTS.md 且 REPL-only;
实现顺序钉死并设 8h/12h 砍单线(最终均提前达成,未触发降级);英文 DESIGN
正式不做;`/clear` 做 `/new` 别名;双击 Ctrl+C 窗口 2 秒。

每票完成后作者复核验收证据(测试全绿、ruff 干净、彩排记录、泄露扫描),
方向性修正通过 controller 传达给实现者子 Agent。**代码层面没有一行是作者
逐行手写后提交的;作者的工作发生在决策与验收两端。**

## 4. 具体到文件:AI 生成 vs 本人改写

- **AI 生成(初稿与迭代均由 AI 完成)**:`src/mini_agent/` 全部源码(含 REPL 轮
  的 `repl/`、`memory.py`、`resume.py` 及 `loop.py` 轮次化改造)、`tests/`
  全部测试(首轮交付 222 个;REPL 轮后共 323 个;可靠性加固轮后共 336 个;
  /connect 会话内提供 API Key 轮后 342 个;REPL 步数预算轮后共 345 个——
  本节修正:早期文档写作「217」时点早于工具描述英文热备等收尾票,以提交历史
  为准,首轮基线为 222,REPL 实现新增 101,加固轮新增 13)、`fixtures/` 两个演示仓库及其测试、
  `examples/`、`pyproject.toml`、`.github/workflows/ci.yml`、README 双语版、
  DESIGN.md、GLOSSARY.md、AI-USAGE.md、NOTICE、LICENSE 文本(标准 MIT 模板)。
- **本人改写**:无逐行改写。作者的修改意见(如演示措辞、威胁模型口径、
  「不要写四个接缝全部由 pi-mono 验证」)以决策/复核意见形式提出,由 AI 落笔。
- **来自项目需求而非 AI 的内容**:项目目标(四要素自研、五要素输出、
  诚实报告失败)与示例场景(normalize_tags 修 bug)是需求的源头,fixture 的
  期望行为以需求为准。
- **第三方代码搬运(合规声明见 [NOTICE](NOTICE))**:`repl/waiting.py` 整文件
  搬运自 Aider-AI/aider 的 `waiting.py`(Apache-2.0,已按 §4(b) 在文件头与
  NOTICE 声明修改:去 rich 化等);`repl/prompt.py` 的 PromptSession 骨架结构性
  参考gptme(MIT,已重写);其余(压缩/记忆/恢复机制)为调研 pi-mono /
  Codex CLI / DeepSeek Harness 后的自研实现,无代码拷贝。

## 5. 借鉴来源

- **pi-mono(badlogic/pi-mono)架构调研**:无状态循环函数 + 薄状态封装、
  before/after 钩子与 `{block, reason}` 拦截、append-only 事件日志、
  「压缩即投影」、子 Agent 即工具(抽象借鉴;实现自研为进程内循环,
  pi 官方示例用的是子进程)。REPL 轮另借鉴其压缩摘要六段模板与
  `<conversation>` 序列化、AGENTS.md 候选名与 32KiB 上限。详见
  [DESIGN.md §6/§9/§11](DESIGN.md)。
- **OpenAI Codex CLI(codex-rs)**:压缩摘要放 user 角色、handoff 摘要语义;
  其「摘要误存 tool_calls」缺陷(issue #55508)是本项目响应校验的教训。
- **DeepSeek Harness(deepseek-ai/deepseek-harness)**:崩溃恢复补占位思路。
- **aider(Apache-2.0)**:REPL 顺序式骨架与 spinner(整文件搬运,见 NOTICE)。
- **gptme(MIT)**:prompt_toolkit 输入框骨架与 patch_stdout 用法(结构性参考)。
- **prompt_toolkit(BSD-3)**:REPL 唯一新增运行依赖。Claude Code 仅为闭源观感
  参考,无代码级借鉴。
- **Anthropic《Building effective agents》**:循环与工具边界的心智模型
  ("composable patterns" 而非框架)。
- **DeepSeek API 文档**(api-docs.deepseek.com):模型名单、参数、流式行为。

## 6. 一句话结论

按「谁写的代码」口径,这个仓库是 AI 写的;按「它为什么是这个样子」口径,
每一个样子都是作者选的。如需追问任何一段实现的为什么,
[DESIGN.md](DESIGN.md) 按四要素逐节给出了答案。
