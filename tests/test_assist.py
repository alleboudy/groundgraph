"""Assist-layer tests: grounding, the fold-both-sides lesson match, the
no-op discipline (nothing grounds / missing db / partial schema), and the
agentic tool executor — built through the REAL pipeline."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from groundgraph.__main__ import _write_code_facts
from groundgraph.assist import (
    assist_report,
    fold_spellings,
    graph_assist,
    rank_symbols,
    run_tool,
    task_terms,
    tool_schemas,
)
from groundgraph.consolidate import consolidate
from groundgraph.derive import derive_all
from groundgraph.extract_lessons import extract_lesson_facts
from groundgraph.store import GraphStore
from groundgraph.types import ProposedFact

NOW = "2026-01-01T00:00:00+00:00"

THEME = '''\
def score_color(score):
    if score < 55:
        return "yellow"
    return "green"


def render_row(score):
    return score_color(score)
'''

LESSON_MD = (
    "Game score colour bands are defined by explicit case ranges; "
    "edit the range boundary, not the hue constant. "
    "Why: the hue is shared by other widgets."
)


def _built_db(tmp_path: Path) -> str:
    repo = tmp_path / "demo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "theme.py").write_text(THEME)
    db = tmp_path / "g.db"
    with GraphStore.open(db) as store:
        _write_code_facts(store, repo, "demo", NOW)
        consolidate(store, derive_all(store.conn), now=NOW)
        # a lesson written with BRITISH spelling (the field trap)
        consolidate(store, extract_lesson_facts(
            "Score colour bands", LESSON_MD, "docs/notes.md"), now=NOW)
        # a co-change fact at the FILE level (how they are stored)
        consolidate(store, [ProposedFact(
            subject_kind="file", subject_name="app/theme.py", subject_repo="demo",
            predicate="co-changed-with", object_kind="file",
            object_name="app/rows.py", object_lit=None, confidence=0.7,
            extractor="det:git-cochange@1", origin="git:demo",
            excerpt="co-changed in 4 commits")], now=NOW)
    return str(db)


def test_terms_fold_and_dedup() -> None:
    t = task_terms("Adjust the score colour bands; the colour is wrong")
    assert "color" in t and "colour" not in t
    assert "adjust" not in t          # stopword
    assert t.count("color") == 1


def test_terms_keep_snake_case_identifiers_whole() -> None:
    t = task_terms("url_for builds the wrong scheme behind a proxy")
    assert "url_for" in t             # NOT split into url + for
    assert "url" in t                 # the split words still present too


def test_rank_excludes_test_paths() -> None:
    rows = [("score_color", "app/theme.py", 1),
            ("test_score_color_bands_red", "tests/test_theme.py", 9)]
    ranked = rank_symbols(rows, ["score", "color"], floor=2)
    names = [r[0] for r in ranked]
    assert "score_color" in names
    assert "test_score_color_bands_red" not in names


def test_fold_spellings_both_sides() -> None:
    assert "color" in fold_spellings("the colour bands").lower()


def test_graph_assist_grounds_neighbourhood_cochange_and_lesson(tmp_path: Path) -> None:
    db = _built_db(tmp_path)
    task = ("Scores 50 to 54 show yellow but should be red. "
            "Adjust the score color bands.")
    out = graph_assist(task, db)
    assert "score_color" in out                      # grounded the real symbol
    assert "app/theme.py:1" in out                    # path:line from provenance
    assert "called-by render_row" in out              # relation neighbourhood
    assert "co-changes-with app/rows.py" in out       # FILE-level co-change wired
    assert "colour bands" in out                      # lesson surfaced (fold matched)
    assert out.endswith(task)                         # original task preserved
    rep = assist_report(task, out)
    assert rep["injected"] is True and rep["grounded_symbols"] >= 1


def test_graph_assist_noops_when_nothing_grounds(tmp_path: Path) -> None:
    db = _built_db(tmp_path)
    task = "upgrade the webpack loader plugin"
    assert graph_assist(task, db) == task
    assert assist_report(task, task) == {"injected": False, "grounded_symbols": 0}


def test_graph_assist_noops_when_db_missing() -> None:
    task = "adjust the score color bands"
    assert graph_assist(task, "/nonexistent/nope.db") == task


def test_graph_assist_noops_on_partial_schema(tmp_path: Path) -> None:
    """A db that grounds via defined_symbols but fails a deeper query (an
    older/partial `facts` schema) must no-op, never crash the run."""
    db = tmp_path / "partial.db"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE entities(entity_id INTEGER PRIMARY KEY, kind TEXT, name TEXT,
        repo TEXT, path TEXT, meta TEXT, first_seen TEXT, last_seen TEXT);
    CREATE TABLE facts(fact_id INTEGER PRIMARY KEY, subject_id INT, predicate TEXT,
        object_id INT, object_lit TEXT, extractor TEXT, created_at TEXT,
        valid_from TEXT, valid_to TEXT);  -- NO confidence column
    CREATE TABLE provenance(prov_id INTEGER PRIMARY KEY, fact_id INT,
        source_kind TEXT, source_ref TEXT, excerpt TEXT, created_at TEXT);
    INSERT INTO entities VALUES (1,'symbol','score_color','d','app/theme.py',
        NULL,'t','t');
    INSERT INTO entities VALUES (2,'file','app/theme.py','d','app/theme.py',
        NULL,'t','t');
    INSERT INTO facts VALUES (1,1,'defined-in',2,NULL,'det:ast@1','t','t',NULL);
    INSERT INTO provenance VALUES (1,1,'code_line','app/theme.py:1',NULL,'t');
    """)
    c.commit()
    c.close()
    task = "adjust the score color bands"
    assert graph_assist(task, str(db)) == task     # no-op, no crash


def test_tool_schemas_shape() -> None:
    names = {s["function"]["name"] for s in tool_schemas()}
    assert names == {"query_facts", "explain_entity"}


def test_run_tool_query_explain_and_guards(tmp_path: Path) -> None:
    db = _built_db(tmp_path)
    q = run_tool("query_facts", {"predicate": "called-by", "subject": "score_color"}, db)
    assert "render_row" in q
    e = run_tool("explain_entity", {"entity": "score_color"}, db)
    assert "app/theme.py" in e
    none = run_tool("query_facts", {"subject": "nonesuch_xyz"}, db)
    assert "no facts" in none.lower()
    # a malformed model-supplied limit must coerce, never raise
    bad = run_tool("query_facts", {"subject": "score_color", "limit": "many"}, db)
    assert "score_color" in bad or "no facts" in bad.lower()
    # missing db -> explicit error string, no exception
    gone = run_tool("query_facts", {"subject": "x"}, "/nonexistent/nope.db")
    assert "unavailable" in gone.lower()
