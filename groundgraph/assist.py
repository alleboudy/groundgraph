"""Serve-time recall: turn a task description + the graph into grounded
context for a coding agent.

Two mechanisms:

* `graph_assist(task, db_path)` — PRE-INJECTION. Ground the task's terms
  against defined symbols, expand the top candidates with their relation
  neighbourhood (called-by / calls / tests / raises / file-level co-change)
  and matching lessons, and prepend one compact block to the task.
* `tool_schemas()` / `run_tool(name, args, db_path)` — AGENTIC. Expose
  `query_facts` and `explain_entity` as OpenAI-style tools an agent can call
  mid-task, executed in-process against the store.

Hard rules, learned in field evaluation:

* A wrong file is worse than none — weak matches degrade to a loud NO-OP
  (the task returns unchanged), never a low-confidence guess.
* Every db-error path degrades gracefully: `graph_assist` returns the task
  unchanged; `run_tool` returns an explicit error string. Neither ever
  raises into an agent loop.
* Instrument whether the lever FIRED (`assist_report`): a null result you
  cannot attribute is a wasted experiment.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

from groundgraph.query import FactQuery

logger = logging.getLogger(__name__)

STOPWORDS = {"the", "and", "for", "with", "from", "that", "this", "are", "is",
             "was", "has", "have", "its", "it", "on", "in", "of", "to", "as", "at",
             "too", "not", "but", "app", "our", "please", "make", "fix", "find",
             "cause", "issue", "small", "users", "report", "reports", "product",
             "wants", "should", "show", "shows", "adjust", "accordingly"}

# British -> American folding. Applied to BOTH the task terms and the lesson
# text before matching: a task that says "color" must match a note that says
# "colour" (a real field miss — the spelling mismatch silently blanked the
# lesson lever until both sides were folded).
SPELLING_ALIASES = {"colour": "color", "behaviour": "behavior", "grey": "gray",
                    "centre": "center", "colours": "colors"}

# Symbols living in test/docs/fixture paths are EXCLUDED from grounding: they
# consume behavior, they don't define it — and long descriptive test names
# otherwise out-score the real definition every time.
NOISE_PATHS = re.compile(
    r"(^|/)(tests?|docs?|fixtures|__pycache__|examples)(/|$)|test_|_test\.")

_NEIGHBOUR_PREDS = ("calls", "called-by", "tests", "raises")


def fold_spellings(text: str) -> str:
    """Fold British spellings to American, word-wise."""
    return re.sub(r"[a-zA-Z]+",
                  lambda m: SPELLING_ALIASES.get(m.group(0).lower(), m.group(0)), text)


def task_terms(task: str) -> list[str]:
    """Lowercase alnum words (len>=3), stopwords removed, spellings folded.
    Order-preserving, de-duplicated."""
    words = re.findall(r"[a-zA-Z]{3,}", task.lower())
    seen: list[str] = []
    for raw_w in words:
        w = SPELLING_ALIASES.get(raw_w, raw_w)
        if w not in STOPWORDS and w not in seen:
            seen.append(w)
    return seen


def rank_symbols(rows: list[tuple[str, str, int | None]], terms: list[str],
                 *, top: int = 5, floor: int = 3) -> list[tuple[str, str, int | None, int]]:
    """Score (symbol, path, line) rows against task terms: 2 points for a term
    inside the symbol name, 1 inside the file basename. Test/docs/fixture
    paths excluded. Deterministic order: score desc, then name. Only rows
    scoring >= floor survive (floor 3 = a name hit plus corroboration); at
    most `top`."""
    scored = []
    for name, path, line in rows:
        if NOISE_PATHS.search(str(path).lower()):
            continue
        nl, bl = name.lower(), Path(path).name.lower()
        sc = sum((2 if t in nl else 0) + (1 if t in bl else 0) for t in terms)
        if sc >= floor:
            scored.append((name, path, line, sc))
    scored.sort(key=lambda r: (-r[3], r[0]))
    return scored[:top]


def defined_symbols(conn: sqlite3.Connection) -> list[tuple[str, str, int | None]]:
    """(name, path, line) for every symbol entity with a defined-in
    provenance. A connectable-but-schemaless db (a stale/partial build
    artifact) degrades to a loud no-op — return [], never crash."""
    try:
        raw = conn.execute(
            """SELECT DISTINCT e.name, e.path, p.source_ref
               FROM entities e
               LEFT JOIN facts f ON f.subject_id = e.entity_id AND f.predicate = 'defined-in'
               LEFT JOIN provenance p ON p.fact_id = f.fact_id
               WHERE e.kind = 'symbol' AND e.path IS NOT NULL""").fetchall()
    except sqlite3.Error as e:
        logger.warning("defined_symbols: query failed (%s) — no grounding", e)
        return []
    rows: dict[tuple[str, str], int | None] = {}
    for name, path, ref in raw:
        line = None
        if ref and ":" in ref and ref.rsplit(":", 1)[-1].isdigit():
            line = int(ref.rsplit(":", 1)[-1])
        key = (name, str(path))
        if key not in rows or (rows[key] is None and line is not None):
            rows[key] = line
    return [(n, p, ln) for (n, p), ln in rows.items()]


def _symbol_block(fq: FactQuery, name: str, path: str, line: int | None) -> str:
    """One grounded line: `name` — path:line (relations)."""
    head = f"- `{name}` — {path}" + (f":{line}" if line else "")
    dossier = fq.explain_entity(name, limit=80)
    rels: dict[str, list[str]] = {}
    for fr in dossier.outgoing:
        if fr.predicate in _NEIGHBOUR_PREDS and fr.object_name:
            rels.setdefault(fr.predicate, []).append(fr.object_name)
    for fr in dossier.incoming:               # who acts ON this symbol
        if fr.predicate == "calls" and fr.subject_name:
            rels.setdefault("called-by", []).append(fr.subject_name)
        if fr.predicate == "tests" and fr.subject_name:
            rels.setdefault("tested-by", []).append(fr.subject_name)
    # Co-change is stored on FILE entities (subject = the path) — query the
    # candidate's file, not its symbol name (a symbol never resolves to a
    # file-kind entity; querying by symbol name was a silent no-op).
    for fr in fq.query_facts(subject=path, predicate="co-changed-with", limit=3):
        if fr.object_name:
            rels.setdefault("co-changes-with", []).append(fr.object_name)
    parts = []
    for pred in ("called-by", "calls", "tests", "tested-by", "raises", "co-changes-with"):
        names = rels.get(pred)
        if names:
            parts.append(f"{pred} {', '.join(sorted(set(names))[:3])}")
    return head + (f" ({'; '.join(parts)})" if parts else "")


def _relevant_lessons(fq: FactQuery, terms: list[str], *, top: int = 2) -> list[str]:
    """Lessons and rationales whose (folded) topic/text match the task terms."""
    if not terms:
        return []
    scored: list[tuple[int, str]] = []
    for pred in ("lesson", "because"):
        for fr in fq.query_facts(predicate=pred, limit=4000):
            topic = fold_spellings((fr.subject_name or "").lower())
            text = fold_spellings((fr.object_lit or "").lower())
            sc = sum((2 if t in topic else 0) + (1 if t in text else 0) for t in terms)
            if sc >= 2:
                scored.append((sc, fr.object_lit or fr.subject_name or ""))
    scored.sort(key=lambda r: (-r[0], r[1]))
    out, seen = [], set()
    for _sc, txt in scored:
        if txt and txt not in seen:
            seen.add(txt)
            out.append(f"- {txt}")
        if len(out) >= top:
            break
    return out


def graph_assist(task: str, db_path: str, *, top: int = 5) -> str:
    """Pre-injection: ground the task, prepend a relation- and lesson-aware
    block. Returns the task UNCHANGED when nothing grounds or on any db
    error — a loud no-op, never a crash, never a guess."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.warning("graph_assist: db unavailable (%s) — prompt unchanged", e)
        return task
    try:
        terms = task_terms(task)
        ranked = rank_symbols(defined_symbols(conn), terms, top=top)
        if not ranked:
            return task
        fq = FactQuery(conn)
        blocks = [_symbol_block(fq, n, p, ln) for n, p, ln, _sc in ranked]
        lessons = _relevant_lessons(fq, terms)
    except sqlite3.Error as e:
        # A db that grounds via defined_symbols but fails a deeper query (a
        # partial/older schema) must degrade the same way: no-op, not a crash.
        logger.warning("graph_assist: query failed (%s) — prompt unchanged", e)
        return task
    finally:
        conn.close()
    out = ["[graph] From the code memory graph, likely relevant:", *blocks]
    if lessons:
        out += ["Relevant lessons/conventions:", *lessons]
    out.append("Start from the most relevant file.")
    return "\n".join(out) + "\n\n" + task


def assist_report(original: str, assisted: str) -> dict:
    """Instrumentation: did the lever FIRE? A null experiment result you
    cannot attribute (didn't help vs never fired) is uninterpretable — record
    the firing signal alongside every run."""
    injected = assisted != original
    lines = sum(1 for ln in assisted.splitlines() if ln.startswith("- `")) if injected else 0
    return {"injected": injected, "grounded_symbols": lines}


# --- agentic tools ----------------------------------------------------------

TOOL_NAMES = frozenset({"query_facts", "explain_entity"})


def tool_schemas() -> list[dict]:
    """OpenAI-style schemas for the two callable graph tools."""
    return [
        {"type": "function", "function": {"name": "query_facts",
            "description": "Query the code memory graph. Filter by subject symbol, "
                           "predicate (calls, called-by, defined-in, tests, "
                           "co-changed-with, imports, raises, lesson), or object.",
            "parameters": {"type": "object", "properties": {
                "subject": {"type": "string"}, "predicate": {"type": "string"},
                "object": {"type": "string"}, "limit": {"type": "integer"}}}}},
        {"type": "function", "function": {"name": "explain_entity",
            "description": "Depth-1 dossier for one symbol: where it is defined, "
                           "what it calls, what calls it, what tests it.",
            "parameters": {"type": "object",
                "properties": {"entity": {"type": "string"}},
                "required": ["entity"]}}},
    ]


def _fmt_fact(fr) -> str:
    obj = fr.object_name or fr.object_lit or ""
    ref = f"  [{fr.source_refs[0]}]" if fr.source_refs else ""
    return f"{fr.subject_name} {fr.predicate} {obj}{ref}"


def run_tool(name: str, args: dict, db_path: str) -> str:
    """Execute one graph tool read-only. Returns a compact text result, or an
    explicit error string — NEVER raises into the agent loop (a malformed
    model-supplied arg must not abort a batch run)."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.warning("run_tool: db unavailable (%s) — tool degraded", e)
        return f"memory graph unavailable: {e}"
    try:
        fq = FactQuery(conn)
        if name == "query_facts":
            try:
                limit = int(args.get("limit", 12) or 12)
            except (TypeError, ValueError):
                limit = 12          # coerce a malformed limit, don't fail the call
            rows = fq.query_facts(
                subject=args.get("subject"), predicate=args.get("predicate"),
                object=args.get("object"), limit=limit)
            if not rows:
                return "no facts match that query"
            return json.dumps({"facts": [_fmt_fact(r) for r in rows[:12]]}, indent=1)
        if name == "explain_entity":
            d = fq.explain_entity(str(args.get("entity", "")))
            if d.entity is None:
                return "no facts: unknown entity"
            return json.dumps({
                "entity": d.entity.name, "path": d.entity.path,
                "outgoing": [_fmt_fact(r) for r in d.outgoing[:10]],
                "incoming": [_fmt_fact(r) for r in d.incoming[:10]]}, indent=1)
        return f"unknown tool {name}"
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.warning("run_tool: query failed (%s) — tool degraded", e)
        return f"memory query failed: {e}"
    finally:
        conn.close()
