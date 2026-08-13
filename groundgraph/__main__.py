"""groundgraph CLI.

    groundgraph build  --db graph.db PATH [PATH ...] [--docs GLOB] [--no-derive]
    groundgraph status --db graph.db [--repos PATH ...]
    groundgraph query  --db graph.db [--subject S] [--predicate P] [--object O]
    groundgraph explain --db graph.db SYMBOL
    groundgraph assist --db graph.db "task text"
    groundgraph tool   --db graph.db NAME JSON_ARGS

`build` runs the full deterministic pipeline: code facts (AST/regex) ->
tests -> git co-change -> lesson docs -> derived layer (inverse, isa,
module-depends, reaches, may-raise) -> transitive module depends. Everything
grounded; no model anywhere.
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from groundgraph.assist import assist_report, graph_assist, run_tool
from groundgraph.consolidate import consolidate, prune_stale_facts
from groundgraph.derive import derive_all
from groundgraph.derive_tdepends import derive_transitive_module_depends
from groundgraph.extract_cochange import extract_cochange_facts
from groundgraph.extract_code import CODE_EXTS, extract_file
from groundgraph.extract_lessons import extract_doc_facts
from groundgraph.extract_tests import extract_test_facts, is_test_file
from groundgraph.freshness import graph_freshness
from groundgraph.health import graph_health
from groundgraph.query import FactQuery
from groundgraph.store import GraphStore
from groundgraph.walk import iter_repo_files

logger = logging.getLogger("groundgraph")

DETERMINISTIC_CONFIDENCE = 1.0


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _write_code_facts(store: GraphStore, repo_root: Path, repo: str, now: str) -> int:
    """Extract + write deterministic code facts for one repo. Idempotent: an
    unchanged repo re-run writes nothing new (dedup on the current triple)."""
    written = 0
    paths: set[str] = set()
    with store.batch():
        for path in iter_repo_files(repo_root):
            ext = path.suffix.lstrip(".").lower()
            if ext not in CODE_EXTS:
                continue
            rel = str(path.relative_to(repo_root))
            paths.add(rel)
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for ef in extract_file(rel, source, repo):
                subj_id = store.get_or_create_entity(
                    kind=ef.subject_kind, name=ef.subject_name,
                    repo=ef.subject_repo, path=ef.subject_path,
                    first_seen=now, last_seen=now,
                )
                object_id = None
                object_lit = ef.object_lit
                if ef.object_name is not None:
                    obj_repo = ef.subject_repo if ef.object_kind != "module" else None
                    obj_path = ef.object_name if ef.object_kind == "file" else None
                    object_id = store.get_or_create_entity(
                        kind=ef.object_kind, name=ef.object_name,
                        repo=obj_repo, path=obj_path,
                        first_seen=now, last_seen=now,
                    )
                fact_id = store.find_current_fact(
                    subject_id=subj_id, predicate=ef.predicate,
                    object_id=object_id, object_lit=object_lit,
                )
                if fact_id is None:
                    fact_id = store.insert_fact(
                        subject_id=subj_id, predicate=ef.predicate,
                        object_id=object_id, object_lit=object_lit,
                        confidence=DETERMINISTIC_CONFIDENCE,
                        extractor=ef.extractor, created_at=now, valid_from=now,
                    )
                    written += 1
                store.insert_provenance(
                    fact_id=fact_id, source_kind=ef.source_kind,
                    source_ref=ef.source_ref, created_at=now,
                )
    # prune facts whose source file no longer exists in this repo
    prune_stale_facts(store, existing_paths_by_repo={repo: paths}, now=now)
    return written


def _first_party_packages(repo_root: Path) -> frozenset[str]:
    """Top-level package names that belong to this repo: any `<pkg>/__init__.py`
    directly at the root or under a conventional `src/` layout. These override
    the tests-extractor deny list, so indexing a famous library's own repo
    does not deny its own package."""
    tops: set[str] = set()
    for base in (repo_root, repo_root / "src"):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and (child / "__init__.py").is_file():
                tops.add(child.name)
    return frozenset(tops)


def _collect_test_facts(repo_root: Path, repo: str) -> list:
    first_party = _first_party_packages(repo_root)
    if first_party:
        logger.info("tests: first-party packages for %s: %s",
                    repo, ", ".join(sorted(first_party)))
    out = []
    for path in iter_repo_files(repo_root):
        rel = str(path.relative_to(repo_root))
        if not rel.endswith(".py") or not is_test_file(rel):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.extend(extract_test_facts(source, rel, repo, first_party=first_party))
    return out


def cmd_build(args: argparse.Namespace) -> int:
    now = _now()
    total = {"code": 0, "tests": 0, "cochange": 0, "lessons": 0,
             "derived": 0, "tdepends": 0}
    with GraphStore.open(args.db) as store:
        for repo_path in args.paths:
            root = Path(repo_path).resolve()
            if not root.is_dir():
                print(f"FATAL: not a directory: {root}", file=sys.stderr)
                return 1
            repo = root.name
            logger.info("build: code facts for %s", repo)
            total["code"] += _write_code_facts(store, root, repo, now)
            logger.info("build: tests relation for %s", repo)
            stats = consolidate(store, _collect_test_facts(root, repo), now=now)
            total["tests"] += stats.added
            logger.info("build: co-change relation for %s", repo)
            stats = consolidate(store, extract_cochange_facts(str(root), repo), now=now)
            total["cochange"] += stats.added
        for pattern in args.docs or []:
            for doc in sorted(globmod.glob(pattern, recursive=True)):
                stats = consolidate(store, extract_doc_facts(doc), now=now)
                total["lessons"] += stats.added
        if not args.no_derive:
            logger.info("build: derived layer (phase 1)")
            stats = consolidate(store, derive_all(store.conn), now=now)
            total["derived"] = stats.added
            # Phase 2 reads the module facts phase 1 just consolidated.
            logger.info("build: transitive module depends (phase 2)")
            stats = consolidate(
                store, derive_transitive_module_depends(store.conn), now=now)
            total["tdepends"] = stats.added
        health = graph_health(store.conn, now=now)
    print(json.dumps({"written": total, "health": health}, indent=1))
    return 0


def _ro(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def cmd_status(args: argparse.Namespace) -> int:
    conn = _ro(args.db)
    try:
        health = graph_health(conn, now=_now())
    finally:
        conn.close()
    if args.repos:
        health["source_freshness"] = graph_freshness(
            args.repos, newest_fact_ts=health.get("newest"))
    print(json.dumps(health, indent=1))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    conn = _ro(args.db)
    try:
        rows = FactQuery(conn).query_facts(
            subject=args.subject, predicate=args.predicate,
            object=args.object, limit=args.limit)
    finally:
        conn.close()
    for r in rows:
        obj = r.object_name or r.object_lit or ""
        ref = f"  [{r.source_refs[0]}]" if r.source_refs else ""
        print(f"{r.subject_name}  {r.predicate}  {obj}  "
              f"(conf {r.confidence}, {r.extractor}){ref}")
    print(f"-- {len(rows)} fact(s)")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    conn = _ro(args.db)
    try:
        d = FactQuery(conn).explain_entity(args.symbol)
    finally:
        conn.close()
    if d.entity is None:
        print(f"unknown entity: {args.symbol}")
        return 1
    print(f"{d.entity.kind} `{d.entity.name}`"
          + (f" — {d.entity.path}" if d.entity.path else ""))
    for label, facts in (("outgoing", d.outgoing), ("incoming", d.incoming)):
        print(f"  {label}:")
        for r in facts[:15]:
            obj = r.object_name or r.object_lit or ""
            print(f"    {r.subject_name} {r.predicate} {obj}  (conf {r.confidence})")
    return 0


def cmd_assist(args: argparse.Namespace) -> int:
    out = graph_assist(args.task, args.db, repo=args.repo, workspace=args.workspace)
    print(out)
    report = assist_report(args.task, out)
    print(f"\n-- lever: injected={report['injected']} "
          f"grounded_symbols={report['grounded_symbols']}", file=sys.stderr)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from groundgraph.mcp import serve
    # MCP servers must keep stdout pure JSON-RPC; logs go to stderr only.
    return serve(args.db)


def cmd_tool(args: argparse.Namespace) -> int:
    try:
        tool_args = json.loads(args.json_args)
    except json.JSONDecodeError as e:
        print(f"bad JSON args: {e}", file=sys.stderr)
        return 2
    print(run_tool(args.name, tool_args, args.db))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="groundgraph", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="run the full deterministic pipeline")
    p.add_argument("--db", default="graph.db")
    p.add_argument("paths", nargs="+", help="repo root(s) to index")
    p.add_argument("--docs", action="append", default=None,
                   help="glob(s) of lesson/notes markdown to ingest")
    p.add_argument("--no-derive", action="store_true")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("status", help="health + anti-rot dashboard")
    p.add_argument("--db", default="graph.db")
    p.add_argument("--repos", nargs="*", default=None,
                   help="source repo paths for the freshness metric")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("query", help="query facts")
    p.add_argument("--db", default="graph.db")
    p.add_argument("--subject")
    p.add_argument("--predicate")
    p.add_argument("--object")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_query)

    p = sub.add_parser("explain", help="depth-1 dossier for one symbol")
    p.add_argument("--db", default="graph.db")
    p.add_argument("symbol")
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("assist", help="pre-injection block for a task")
    p.add_argument("--db", default="graph.db")
    p.add_argument("task")
    p.add_argument("--repo", default=None,
                   help="scope grounding to one repo (multi-repo graphs)")
    p.add_argument("--workspace", default=None,
                   help="drop refs whose path is absent from this directory")
    p.set_defaults(fn=cmd_assist)

    p = sub.add_parser("tool", help="run an agent tool (query_facts/explain_entity)")
    p.add_argument("--db", default="graph.db")
    p.add_argument("name")
    p.add_argument("json_args")
    p.set_defaults(fn=cmd_tool)

    p = sub.add_parser("mcp", help="MCP stdio server (query_facts/explain_entity/assist)")
    p.add_argument("--db", default="graph.db")
    p.set_defaults(fn=cmd_mcp)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
