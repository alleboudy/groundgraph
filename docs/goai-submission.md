# GOAI submission text (Agent Infra / 新智基座 track)

Copy-paste material for the registration form. Adjust lengths to the form's
field limits.

## Project name / 项目名称

groundgraph — 确定性代码记忆图谱 (Deterministic Code-Memory Graph for Coding Agents)

## One-liner / 一句话简介

**EN:** A strictly-local, zero-dependency code-memory graph where every fact
is provable — AST, git, docs, or proof-carrying derivation — giving coding
agents grounded recall without a single LLM call in the pipeline.

**中文：** 严格本地化、零依赖的代码记忆图谱：每条事实都可证明（AST / git /
文档 / 携带证明路径的推导），为编码智能体提供可靠接地的记忆能力，全流程无任何
大模型调用。

## Description / 项目描述

**EN:**

Agent memory today is mostly generated: a model summarizes code and
conversations into "memories", and the graph inherits hallucination,
cross-project contamination, and silent precision rot. groundgraph takes the
opposite bet: **a smaller graph of facts you can prove beats a bigger graph
of facts you can vibe.**

The pipeline is 100% deterministic — Python AST extraction, regex
declarations for 20+ languages, a fail-closed test-coverage relation (a test
must import AND reference a symbol), git co-change coupling with mega-commit
blast-radius bounding, and engineering-notes parsing. On top sits a grounded
derivation layer (materialized inverses, transitive class ancestry,
module-level dependencies, bounded call reach, exception-flow propagation) —
every derived fact carries its proof path, every closure is cycle-safe and
bounded, and every truncation is logged.

The graph watches itself: three trust tiers (ground-truth / derived / risky)
with query-time confidence decay on the risky tier only; a health dashboard
for duplicates, orphans, and contradictions; and a git-grounded source
freshness metric — because a graph built on a stale checkout describes code
that no longer exists, and internal metrics cannot see that.

Agents consume it two ways: pre-injection (`graph_assist` prepends grounded
file:line context with relation neighbourhoods and matching lessons — and
degrades to a loud no-op on weak matches, because a wrong file steers an
agent worse than no file) and agentic tools (`query_facts` /
`explain_entity` as OpenAI-style schemas, executed in-process, read-only,
never raising into the agent loop). Both carry built-in experiment
instrumentation: every run records whether the lever actually fired, so a
null result is attributable — a methodology we validated the hard way and
document openly, including our own measured null.

Adoption is one command in either direction: an **MCP stdio server**
(`python -m groundgraph mcp`) plugs the graph into Claude Code or any MCP
client, and a zero-dependency example agent loop serves any OpenAI-compatible
local endpoint. A **freshness daemon** (`groundgraph watch`) keeps the graph
honest against its source repos: check-then-act, fast-forward clean repos
only, never touch a dirty working tree, single-instance locked. Multi-repo
graphs get **repo-scoped grounding** plus a workspace existence-check — a
fix for a failure mode we measured in our own evaluation and shipped as a
feature.

Indexing Flask end-to-end takes ~3 seconds and yields ~12,500 facts, on
stdlib Python only. One SQLite file. Nothing leaves the machine.

**中文：**

当前的智能体记忆系统大多依赖生成：让模型把代码和对话"总结"为记忆，图谱因此
继承了幻觉、跨项目污染和随规模增长而无声衰退的精度。groundgraph 押注相反的
方向：**一个可证明的小图谱，胜过一个凭感觉的大图谱。**

流水线 100% 确定性：Python AST 提取、20+ 语言的声明/导入正则提取、失败即
关闭的测试覆盖关系（测试必须真实导入并引用目标符号）、带巨型提交爆炸半径
控制的 git 共变耦合分析、工程笔记解析。其上是接地的推导层（物化逆向边、
传递类继承、模块级依赖、有界调用可达、异常流传播）——每条推导事实携带证明
路径，所有闭包计算环安全且有界，所有截断都写入日志。

图谱自我监控：三层信任分级（基础事实/推导/风险），仅风险层按 60 天半衰期在
查询时衰减；健康仪表盘报告重复、孤儿与矛盾候选；git 底层命令驱动的源新鲜度
指标——因为建立在过时检出上的图谱描述的是已不存在的代码，而内部指标对此
无能为力。

智能体有两种接入方式：预注入（`graph_assist` 将接地的 file:line 上下文、
关系邻域与匹配的经验教训前置到任务中；弱匹配时安静退化为无操作——错误的
文件比没有文件更有害）与工具调用（`query_facts` / `explain_entity` 两个
OpenAI 风格工具，进程内只读执行，绝不向智能体循环抛出异常）。两者都内置
实验插桩：每次运行记录"杠杆是否真正触发"，使阴性结果可归因——这套方法论
我们以踩坑换来，并连同自己实测的阴性结果一起公开。

接入只需一条命令：**MCP stdio 服务器**（`python -m groundgraph mcp`）可直接
挂入 Claude Code 或任何 MCP 客户端；零依赖示例智能体循环适配任意 OpenAI 兼容
的本地推理端点。**新鲜度守护进程**（`groundgraph watch`）以"先检查后行动"的
方式保持图谱与源码同步：只快进干净的仓库、绝不触碰脏工作区、单实例锁防止并发
写入。多仓库图谱支持按仓库限定接地范围并预先剔除工作区不存在的路径——这是
我们在自己的评测中实测到的失效模式，修复后作为特性发布。

端到端索引 Flask 约 3 秒，产出约 1.25 万条事实，仅用 Python 标准库。单个
SQLite 文件。数据不出本机。

## Why this track / 为什么选择新智基座

**EN:** This is infrastructure, not an application: a reusable, observable
memory substrate any agent framework can adopt (schemas are OpenAI-style;
the store is one SQLite file). The observability story — trust tiers,
health/freshness dashboards, fired-signal instrumentation — is exactly the
"可观测" property the track calls for, applied to agent memory.

**中文：** 这是基础设施而非单一应用：任何智能体框架都能采用的可复用、可观测
记忆底座（工具为 OpenAI 风格；存储为单个 SQLite 文件）。其可观测性——信任
分级、健康/新鲜度仪表盘、杠杆触发插桩——正是赛道要求的"可观测"属性在智能体
记忆上的落地。

## Open-source / IP declaration / 开源与知识产权声明

- License: MIT. Repository: https://github.com/alleboudy/groundgraph
- Dependencies: NONE at runtime (Python stdlib only); dev-only pytest + ruff.
- All code authored by the submitter; no third-party code vendored; no
  commercial APIs or models used anywhere in the pipeline.
- Demo runs on public open-source code (pallets/flask) fetched at demo time.

## Runnable demo / 可运行演示

```
git clone https://github.com/alleboudy/groundgraph && cd groundgraph
pip install -e . && bash demo/demo.sh
```

Requires: Python >= 3.10 + git. ~3 seconds after the clone. Works fully
offline once the demo repo is cloned.
