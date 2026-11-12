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

## 2. 使用的助手与工具清单

| 助手/工具 | 承担的作用 |
|---|---|
| ZCode(AI 编程 agent CLI,由 GLM 模型驱动) | 全程主力:需求梳理与三轮结构化确认、架构调研、spec 与工单撰写、每票的 TDD 实现、代码审查、工单合并、文档定稿 |
| 其中的子 Agent 机制 | 实现者子 Agent(每票独立上下文,避免互相污染)、审查子 Agent(方案评审)、merger 子 Agent(合并) |
| tdd / implement-spec 等技能流程 | 红→绿纪律、spec 驱动的多工单拆分 |
| curl 直连 DeepSeek API(工单 01 前) | 实测 function calling / 流式分片形状 / thinking 开关 / `finish_reason=length` 行为,结论作为 FakeModel 分片形状的事实基准 |
| deepseek-flash(运行时) | **注意区分**:这是 mini-agent 这个程序运行时所调用的模型,不是开发助手;调用条件与 token 用量按项目要求记入每次终报与 `session.jsonl` |
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

每票完成后作者复核验收证据(测试全绿、ruff 干净、彩排记录、泄露扫描),
方向性修正通过 controller 传达给实现者子 Agent。**代码层面没有一行是作者
逐行手写后提交的;作者的工作发生在决策与验收两端。**

## 4. 具体到文件:AI 生成 vs 本人改写

- **AI 生成(初稿与迭代均由 AI 完成)**:`src/mini_agent/` 全部源码、`tests/`
  全部 217 个测试、`fixtures/` 两个演示仓库及其测试、`examples/fix-cycle.json`、
  `pyproject.toml`、`.github/workflows/ci.yml`、README 双语版、DESIGN.md、
  GLOSSARY.md、LICENSE 文本(标准 MIT 模板)。
- **本人改写**:无逐行改写。作者的修改意见(如演示措辞、威胁模型口径、
  「不要写四个接缝全部由 pi-mono 验证」)以决策/复核意见形式提出,由 AI 落笔。
- **来自项目而非 AI 的内容**:项目需求本身的要求(四要素自研、五要素输出、
  诚实报告失败)与项目需求示例(normalize_tags 场景)是需求的源头,fixture 的
  期望行为以需求为准。

## 5. 借鉴来源

- **pi-mono(badlogic/pi-mono)架构调研**:无状态循环函数 + 薄状态封装、
  before/after 钩子与 `{block, reason}` 拦截、append-only 事件日志、
  「压缩即投影」、子 Agent 即工具(抽象借鉴;实现自研为进程内循环,
  pi 官方示例用的是子进程)。详见 [DESIGN.md §6/§9](DESIGN.md)。
- **Anthropic《Building effective agents》**:循环与工具边界的心智模型
  ("composable patterns" 而非框架)。
- **DeepSeek API 文档**(api-docs.deepseek.com):模型名单、参数、流式行为。

## 6. 一句话结论

按「谁写的代码」口径,这个仓库是 AI 写的;按「它为什么是这个样子」口径,
每一个样子都是作者选的。项目中如需追问任何一段实现的为什么,
[DESIGN.md](DESIGN.md) 按四要素逐节给出了答案。
