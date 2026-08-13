from __future__ import annotations

import pytest

from groundgraph.extract_cochange import (
    CochangeParseError,
    _facts_from_pairs,
    cochange_pairs,
)
from groundgraph.extract_tests import extract_test_facts

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def test_tests_requires_import_and_reference() -> None:
    src = (
        "from app.theme import score_color\n"
        "import pytest\n\n"
        "def test_bands():\n"
        "    assert score_color(50) == 'red'\n\n"
        "def test_unrelated():\n"
        "    assert 1 == 1\n"
    )
    facts = extract_test_facts(src, "tests/test_theme.py", "demo")
    pairs = {(f.subject_name, f.object_name) for f in facts}
    assert ("test_bands", "app.theme.score_color") in pairs
    # no reference -> no fact (fail-closed); pytest import denied
    assert all(f.subject_name != "test_unrelated" for f in facts)
    assert all("pytest" not in (f.object_name or "") for f in facts)


def test_tests_non_test_file_and_stdlib_denied() -> None:
    src = "import os\n\ndef test_x():\n    os.getcwd()\n"
    assert extract_test_facts(src, "app/main.py", "demo") == []       # not a test file
    assert extract_test_facts(src, "tests/test_x.py", "demo") == []   # stdlib denied


def test_first_party_overrides_denylist() -> None:
    """Indexing flask's own repo: `import flask` in its tests is first-party
    and must produce facts, even though `flask` is on the deny list."""
    src = "import flask\n\ndef test_app():\n    flask.Flask('x')\n"
    assert extract_test_facts(src, "tests/test_app.py", "flask") == []  # denied by default
    facts = extract_test_facts(src, "tests/test_app.py", "flask",
                               first_party=frozenset({"flask"}))
    assert {(f.subject_name, f.object_name) for f in facts} == {("test_app", "flask")}


def test_cochange_pairs_support_and_blast_radius() -> None:
    log = "\n".join([
        SHA_A, "a.py", "b.py",
        SHA_B, "a.py", "b.py",
        SHA_C, "a.py", "b.py",
        # a mega-commit that would mint spurious pairs — ignored whole
        "d" * 40, *[f"f{i}.py" for i in range(20)],
    ])
    pairs = cochange_pairs(log, min_support=3)
    assert pairs == {("a.py", "b.py"): 3}


def test_cochange_malformed_log_fails_closed() -> None:
    with pytest.raises(CochangeParseError):
        cochange_pairs("orphan.py\n" + SHA_A)


def test_cochange_facts_symmetric_and_code_only() -> None:
    pairs = {("a.py", "b.py"): 4, ("a.py", "README.md"): 9}
    facts = _facts_from_pairs(pairs, "demo")
    edges = {(f.subject_name, f.object_name) for f in facts}
    assert ("a.py", "b.py") in edges and ("b.py", "a.py") in edges  # symmetric
    assert not any("README.md" in e for edge in edges for e in edge)  # non-code dropped
    assert all(f.confidence < 1.0 for f in facts)  # below the det-code floor
