"""Repo-scoped grounding + workspace-existence check (the multi-repo leak
fix) and reStructuredText section support."""
from __future__ import annotations

from pathlib import Path

from groundgraph.__main__ import _write_code_facts
from groundgraph.assist import defined_symbols, graph_assist
from groundgraph.extract_lessons import split_sections
from groundgraph.query import FactQuery
from groundgraph.store import GraphStore

NOW = "2026-01-01T00:00:00+00:00"

ALPHA = "def score_color(score):\n    return 'red'\n"
BETA = "def score_color(score):\n    return 'blue'\n"


def _two_repo_db(tmp_path: Path) -> tuple[str, Path, Path]:
    """One graph built from TWO repos defining the same-named symbol at
    different paths — the exact multi-repo leak scenario."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    (alpha / "app").mkdir(parents=True)
    (beta / "ui").mkdir(parents=True)
    (alpha / "app" / "theme.py").write_text(ALPHA)
    (beta / "ui" / "colors.py").write_text(BETA)
    db = tmp_path / "g.db"
    with GraphStore.open(db) as store:
        _write_code_facts(store, alpha, "alpha", NOW)
        _write_code_facts(store, beta, "beta", NOW)
    return str(db), alpha, beta


def test_defined_symbols_repo_filter(tmp_path: Path) -> None:
    db, _a, _b = _two_repo_db(tmp_path)
    import sqlite3
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    all_paths = {p for _n, p, _l in defined_symbols(conn)}
    assert {"app/theme.py", "ui/colors.py"} <= all_paths        # unscoped mixes
    alpha_paths = {p for _n, p, _l in defined_symbols(conn, repo="alpha")}
    assert alpha_paths == {"app/theme.py"}                      # scoped filters
    conn.close()


def test_graph_assist_repo_scoping_prevents_cross_repo_paths(tmp_path: Path) -> None:
    db, _alpha, _beta = _two_repo_db(tmp_path)
    task = "the score color in the theme is wrong"
    unscoped = graph_assist(task, db)
    assert "ui/colors.py" in unscoped         # the leak, demonstrated
    scoped = graph_assist(task, db, repo="alpha")
    assert "app/theme.py" in scoped
    assert "ui/colors.py" not in scoped       # sibling repo's path excluded


def test_graph_assist_workspace_existence_check(tmp_path: Path) -> None:
    db, alpha, _beta = _two_repo_db(tmp_path)
    task = "the score color in the theme is wrong"
    # workspace = the alpha checkout: beta's ui/colors.py does not exist
    # there and must be DROPPED even without repo scoping
    out = graph_assist(task, db, workspace=alpha)
    assert "app/theme.py" in out
    assert "ui/colors.py" not in out
    # a workspace containing NEITHER path -> full no-op, task unchanged
    empty = tmp_path / "empty"
    empty.mkdir()
    assert graph_assist(task, db, workspace=empty) == task


def test_explain_entity_prefers_requested_repo(tmp_path: Path) -> None:
    db, _a, _b = _two_repo_db(tmp_path)
    import sqlite3
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    fq = FactQuery(conn)
    d_beta = fq.explain_entity("score_color", repo="beta")
    assert d_beta.entity is not None and d_beta.entity.repo == "beta"
    d_default = fq.explain_entity("score_color")
    assert d_default.entity is not None      # falls back to lowest entity_id
    d_missing = fq.explain_entity("score_color", repo="nonesuch")
    assert d_missing.entity is not None      # graceful fallback, not None
    conn.close()


RST = """\
Intro prose before any heading is ignored.

Parsing rules
=============

**Always split on whitespace before tokenising; the parser owns all
normalisation of user text.** Why: callers pass raw input.

Error handling
--------------

Raise ``ParseError`` early; see ``pkg/core.py:12`` for the boundary.
"""


def test_split_sections_handles_rst_underlines() -> None:
    secs = split_sections(RST)
    titles = [t for t, _b in secs]
    assert titles == ["Parsing rules", "Error handling"]
    assert "normalisation" in secs[0][1]
    assert "pkg/core.py:12" in secs[1][1]
    # markdown still works, and the underline is not swallowed as body
    md = split_sections("# A\n\nbody a\n\n## B\n\nbody b\n")
    assert [t for t, _ in md] == ["A", "B"]
    assert "===" not in secs[0][1]
