# GOAI Submission Cover Sheet / 参赛作品说明

**Track / 赛道:** 新智基座 (Agent Infra)

## Title / 作品名称

**groundgraph — Deterministic Code-Memory Graph for Coding Agents**
（groundgraph — 面向编码智能体的确定性代码记忆图谱）

## One-liner / 一句话简介

**EN:** A strictly-local, zero-dependency code-memory graph where every fact
is provable — AST, git, docs, or proof-carrying derivation — giving coding
agents grounded recall with no LLM call anywhere in the memory pipeline.

**中文：** 严格本地化、零依赖的代码记忆图谱：每条事实都可证明（AST / git /
文档 / 携带证明路径的推导），为编码智能体提供可靠接地的记忆能力，记忆流水线
全程无任何大模型调用。

## Links / 链接

| what | url |
|---|---|
| Repository / 代码仓库 | https://github.com/alleboudy/groundgraph |
| Demo video / 演示视频 (66s) | https://github.com/alleboudy/groundgraph/releases/download/v0.3.0/groundgraph-demo.mp4 |
| Latest release / 最新版本 (v0.3.0, wheel + video) | https://github.com/alleboudy/groundgraph/releases/tag/v0.3.0 |
| Honest evaluation methodology / 诚实评测方法论 | `docs/honest-eval.md` |
| Enterprise blueprint / 企业落地蓝图 | https://github.com/alleboudy/codescribe/blob/main/docs/22-memory-system-blueprint.md |

## Run the demo / 运行演示 (3 commands, ~1 minute)

```bash
git clone https://github.com/alleboudy/groundgraph && cd groundgraph
pip install -e .
bash demo/demo.sh     # clones Flask, indexes ~12,500 facts in ~3s, asks it questions
```

Requires Python ≥ 3.10 + git. Fully offline after the demo clone. MCP
integration is one command: `claude mcp add groundgraph -- python -m
groundgraph mcp --db graph.db` (works with any MCP client, including IDE
agents).

## What to look at, per criterion / 评审维度对照

- **技术创新性 (Innovation):** deterministic-first memory (no LLM
  extraction — the opposite of GraphRAG-style pipelines); proof-carrying
  derivations; three trust tiers with query-time decay; "the embedder
  proposes, the graph verifies" hybrid (v0.3.0) — semantic recall without
  surrendering the ground-truth mandate; built-in fired-signal experiment
  instrumentation.
- **开源贡献度 (Open-source value):** MIT; zero runtime dependencies (the
  cheapest supply chain to audit is the one that does not exist); 46 tests;
  CI; reusable as library, CLI, MCP server, or agent tools.
- **方案完整性 (Completeness):** extractors (AST + 20 languages, tests,
  git co-change, lessons) → derivation layer → anti-rot + freshness
  dashboards → watch daemon → serve-time recall (pre-injection + agentic +
  MCP) → optional semantic sidecar with a local-endpoint embedder.
- **Demo 可运行性 (Runnable demo):** one script, ~1 minute, real public
  codebase (Flask), every README number reproducible.
- **成长潜力 (Growth):** the enterprise blueprint extends the same core to
  Perforce / Bugzilla / Polarion / OneDrive sources and a jargon-tuned
  embedding model, with milestones an agent can implement.
- **Honesty / 诚实声明:** we publish our measurement methodology AND our
  null result (recall grounds tasks; it does not by itself lift a small
  model's end-to-end fix rate — the wall is generation). The instrumentation
  that makes that null attributable ships in the product.

## Open-source & IP declaration / 开源与知识产权声明

- License: MIT. All code authored by the submitter. No vendored third-party
  code. No commercial APIs or models anywhere in the pipeline.
- Runtime dependencies: none (Python stdlib only). Dev-only: pytest, ruff.
- The demo indexes public open-source code (pallets/flask) fetched at demo
  time.

## Submitter / 参赛者

Ahmad Alleboudy — https://github.com/alleboudy
