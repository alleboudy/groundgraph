"""End-to-end CLI test: build a tiny repo (with lesson docs), then query,
explain, assist, status through the real argparse entrypoints."""
from __future__ import annotations

from pathlib import Path

from groundgraph.__main__ import main


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text(
        "def parse(text):\n    return text.split()\n\n"
        "def run(text):\n    return parse(text)\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text(
        "# Parsing rules\n\n**Always split on whitespace before tokenising; "
        "the parser owns normalisation.** Why: callers pass raw user text.\n")
    return repo


def test_build_query_explain_assist_status(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)
    db = str(tmp_path / "g.db")

    rc = main(["build", "--db", db, str(repo),
               "--docs", str(repo / "docs" / "*.md")])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"written"' in out and '"health"' in out

    rc = main(["query", "--db", db, "--predicate", "called-by",
               "--subject", "parse"])
    assert rc == 0
    assert "run" in capsys.readouterr().out

    rc = main(["explain", "--db", db, "parse"])
    assert rc == 0
    assert "pkg/core.py" in capsys.readouterr().out

    # "parse" hits the symbol name (2) and "core" the file basename (1) —
    # meeting the anti-misgrounding floor of 3, as designed
    rc = main(["assist", "--db", db,
               "the parse function in core keeps raw whitespace"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "parse" in captured.out            # grounded
    assert "injected=True" in captured.err    # instrumentation fired

    rc = main(["status", "--db", db])
    assert rc == 0
    assert '"live_facts"' in capsys.readouterr().out
