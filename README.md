# groundgraph

**A strictly-local, deterministic code-memory graph for coding agents. Zero runtime dependencies.**

Every fact in the graph is *grounded*: an AST node, a git commit, a documentation line, or a mechanical derivation that carries its proof path. There is **no LLM anywhere in the pipeline** — generation is for answering questions, not for remembering things.

```
pip install -e .
bash demo/demo.sh          # clones pallets/flask, indexes it, asks it questions
```

Indexing flask (≈400 commits of history, full source tree) takes **~3 seconds** and produces **~12,500 facts** — on stdlib Python alone: `ast`, `sqlite3`, `re`, and git via subprocess. No embeddings, no vector store, no API keys, no telemetry, nothing leaves your machine.

## Why deterministic-first?

Most "memory for agents" systems ask a model to *summarize* code or transcripts into memories. That path is cheap to start and expensive forever: hallucinated facts, wrong-project contamination, duplicates, and a graph whose precision silently rots as it grows. We built this system after watching an LLM-distillation pipeline produce hundreds of *plausible, grounded-looking, wrong* facts — and then replacing it with deterministic extraction that produced strictly better recall material at zero model cost.

The design bet: **a smaller graph of facts you can prove beats a bigger graph of facts you can vibe.**

Three trust tiers, enforced by extractor prefix:

| tier | prefix | examples | decay |
|---|---|---|---|
| ground truth | `det:` | AST edges, git co-change, test coverage, doc rules | never (re-verified every build) |
| grounded derivation | `der:` | inverse edges, transitive closure, exception flow — each with a proof path | never (re-derivable) |
| risky/inferred | anything else | model-distilled facts, if you choose to add them | **halves every 60 days** unless re-corroborated |

Decay is applied at *query* time. Nothing is ever destroyed: supersession is soft (`valid_to` + `superseded_by`), so the graph supports time-travel and audit by construction.

## What the graph knows

**Extracted (det:)** — `defined-in` (file:line), `calls`, `imports`, `depends-on`, `inherits`, `raises`, `decorated-by`; a fail-closed `tests` relation (a test must *import and actually reference* a symbol — never a naming guess); `co-changed-with` from git history (files that repeatedly change together — real coupling static analysis cannot see, with mega-commit blast-radius bounding); and `lesson` / `because` / `cites` parsed from your engineering notes.

**Derived (der:)** — materialized inverses (`called-by`, `imported-by`, `defines`, `raised-by`); transitive `isa` class ancestry; module-level `depends-on` aggregated from cross-file symbol edges; bounded call `reaches`; `may-raise` exception propagation (*"A calls B calls C, C raises E ⇒ A may-raise E"* — with the full call path as proof); transitive module dependencies. Every derivation is cycle-safe, depth-bounded, fan-out-capped, and **logs its truncations** — no silent caps.

Python gets the deep treatment (stdlib `ast`); 20+ other languages get imports/declarations via regex. The store is one SQLite file.

## Ask it questions

```console
$ groundgraph query --db g.db --predicate called-by --subject url_for
url_for  called-by  login_required.wrapped_view  (conf 0.98, der:inverse@1)
url_for  called-by  register  (conf 0.98, der:inverse@1)
...

$ groundgraph query --db g.db --predicate tests --object flask.Flask
test_async_before_after_request  tests  flask.Flask  (conf 0.95, det:tests@1)  [code:tests/test_async.py]
test_find_best_app  tests  flask.Flask  (conf 0.95, det:tests@1)  [code:tests/test_cli.py]
...

$ groundgraph tool --db g.db query_facts '{"predicate": "may-raise", "subject": "Scaffold.__init__"}'
{"facts": ["Scaffold.__init__ may-raise RuntimeError  [derived]", ...]}
```

## Serve-time recall for agents

Two mechanisms, both battle-hardened:

**Pre-injection** — `graph_assist(task, db)` grounds the task's terms against defined symbols and prepends a relation- and lesson-aware block:

```console
$ groundgraph assist --db g.db "url_for builds the wrong external URL scheme behind an https proxy"
[graph] From the code memory graph, likely relevant:
- `Flask.url_for` — src/flask/app.py:1105 (calls RuntimeError, ValueError, _cv_app.get;
  raises RuntimeError, ValueError; co-changes-with src/flask/blueprints.py, src/flask/ctx.py, ...)
- `url_for` — src/flask/helpers.py:200 (called-by create, delete, login;
  co-changes-with src/flask/app.py, src/flask/ctx.py, ...)
Start from the most relevant file.

-- lever: injected=True grounded_symbols=2
```

**Agentic tools** — `tool_schemas()` gives you OpenAI-style schemas for `query_facts` and `explain_entity`; `run_tool()` executes them in-process, read-only. Drop them into any tool-calling agent loop.

Field-learned rules, encoded in the code:

- **A wrong file is worse than none.** Weak matches degrade to a *loud no-op* — the task returns unchanged. The injector never emits a low-confidence guess, because a bad grounding actively steers an agent into the weeds.
- **Nothing raises into the agent loop.** Every db-error path (missing file, partial schema, malformed model-supplied arguments) degrades to a no-op or an explicit error string. A corrupt graph must never abort a batch run.
- **Instrument whether the lever fired.** `assist_report()` returns `{injected, grounded_symbols}` per task. A null experiment result you cannot attribute — *"retrieval didn't help"* vs. *"retrieval never ran"* — is a wasted experiment. See [docs/honest-eval.md](docs/honest-eval.md).
- **Both sides of a spelling fold.** A task that says `color` matches an engineering note that says `colour`. (A real field miss: the mismatch silently blanked the lesson lever until both sides were folded.)

## Anti-rot: the graph watches itself

A graph that grows in count while rotting in precision is a regression, not progress.

- `groundgraph status` — live/superseded counts, tier ratios, exact-duplicate count, orphan nodes, contradiction candidates (restricted to single-valued predicates — multi-valued relations like `calls` legitimately have many objects; a naive detector once raised 12,000+ false alarms), and decay-dormant counts, each with warning flags.
- **Source freshness** — the graph is only as current as the checkouts it was built from. `status --repos <path>` reports how many commits each source repo is behind its upstream, from git plumbing only, with honest `unknown` (never a guessed 0) when git can't answer. This exists because we once found a production graph silently built on a checkout **297 commits behind origin** — and no internal metric could see it.
- **Stale-fact pruning** — facts whose source file vanished from *their own repo* are soft-archived on every build.
- Builds are idempotent: re-running over an unchanged repo writes nothing.

## What this is *not*

Honesty section — measured, not hoped:

- It is **not** a semantic search engine. Grounding is lexical + structural. If your task shares no vocabulary with the code, it grounds nothing (by design — see rule 1 above). Pair it with a local embedding index if you need symptom-to-code recall.
- **Recall ≠ capability.** In our own instrumented evaluation, serve-time recall reliably *grounded* tasks (correct file:line every time) but did **not** lift a small local model's end-to-end fix rate — the binding constraint was the model's edit generation, not retrieval. We publish that method (and that null) rather than implying a win: [docs/honest-eval.md](docs/honest-eval.md).
- Cross-language `calls` is not extracted (a regex call-graph would violate the ground-truth mandate). Non-Python languages get imports + declarations only.
- The `tests` deny-list is a curated heuristic; repos whose package shares a name with a famous library are handled via first-party detection, but exotic layouts may need the `first_party=` override.

## Library use

```python
from groundgraph import GraphStore, FactQuery, graph_assist, tool_schemas, run_tool

with GraphStore.open("g.db") as store:
    ...  # build via `python -m groundgraph build`, or write facts yourself

import sqlite3
conn = sqlite3.connect("file:g.db?mode=ro", uri=True)
fq = FactQuery(conn)
fq.query_facts(predicate="called-by", subject="url_for")
fq.explain_entity("Flask")           # depth-1 dossier

prompt = graph_assist(task, "g.db")  # pre-injection, loud no-op on weak match
```

## 中文简介

**groundgraph** 是一个严格本地化、零运行时依赖的确定性代码记忆图谱，为编码智能体（coding agents）提供可验证的记忆基础设施。

核心理念：**图谱中的每一条事实都可以被证明** —— 它要么来自 AST 节点、git 提交记录、文档条目，要么来自携带完整证明路径的机械推导。整条流水线中**没有任何大模型参与**：生成能力用于回答问题，而不是用于记忆。

- **三层信任分级**：`det:`（AST/git/文档提取的基础事实，每次构建重新验证，永不衰减）、`der:`（逆向边、传递闭包、异常传播等推导事实，携带证明路径，可重新推导）、其余为风险层（按 60 天半衰期在查询时衰减，从不物理删除）。
- **防腐监控**：健康仪表盘报告重复、孤儿节点、矛盾候选与衰减休眠事实；源新鲜度指标用 git 底层命令如实报告图谱构建源落后上游多少提交。
- **智能体接入**：既支持把接地上下文预注入任务提示词（弱匹配时安静退化为无操作，绝不输出低置信度的猜测），也提供 `query_facts` / `explain_entity` 两个 OpenAI 风格工具供智能体在任务中调用；并内置"杠杆是否触发"的实验插桩，保证阴性结果可解释。
- **实测性能**：索引 Flask 全仓库约 3 秒，产出约 1.25 万条事实，仅用 Python 标准库。

诚实声明：在我们自己的插桩评测中，检索增强稳定地提供了正确的定位（file:line），但**没有**提升小模型端到端修复率 —— 瓶颈在生成而非检索。我们选择公开这套测量方法与该阴性结果，而不是暗示一个不存在的胜利。详见 [docs/honest-eval.md](docs/honest-eval.md)。

## License

MIT. Built for local-first agent stacks — your code and your graph never leave your machine.
