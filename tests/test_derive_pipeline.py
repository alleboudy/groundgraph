"""Derivation tests run through the REAL pipeline (code write -> derive ->
consolidate), not hand-rolled schemas — so they also cover the store."""
from __future__ import annotations

from pathlib import Path

from groundgraph.__main__ import _write_code_facts
from groundgraph.consolidate import consolidate
from groundgraph.derive import _closure, derive_all
from groundgraph.derive_tdepends import derive_transitive_module_depends
from groundgraph.query import FactQuery
from groundgraph.store import GraphStore

SRC = '''\
class Base:
    pass


class Mid(Base):
    pass


class Leaf(Mid):
    pass


def c():
    raise KeyError("k")


def b():
    c()


def a():
    b()
'''

NOW = "2026-01-01T00:00:00+00:00"


def _built_store(tmp_path: Path):
    repo = tmp_path / "demo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text(SRC)
    # direct-name import: the callee's lexical name matches the defined
    # symbol, so the cross-file edge carries paths on both endpoints
    (repo / "pkg" / "io.py").write_text("from pkg.core import a\n\ndef load():\n    a()\n")
    return repo


def test_closure_is_cycle_safe_and_depth_bounded() -> None:
    # a 2-cycle terminates and yields NO self-ancestry (never A isa A)
    assert _closure([("A", "B"), ("B", "A")]) == set()
    tri = _closure([("A", "B"), ("B", "C"), ("C", "A")])
    assert ("A", "C", 2) in tri
    assert all(x != y for x, y, _ in tri)
    chain = [(f"n{i}", f"n{i + 1}") for i in range(20)]
    deep = _closure(chain, max_depth=3)
    assert deep and all(d <= 3 for _a, _b, d in deep)


def test_pipeline_derives_all_relations(tmp_path: Path) -> None:
    repo = _built_store(tmp_path)
    db = tmp_path / "g.db"
    with GraphStore.open(db) as store:
        _write_code_facts(store, repo, "demo", NOW)
        consolidate(store, derive_all(store.conn), now=NOW)
        consolidate(store, derive_transitive_module_depends(store.conn), now=NOW)
        fq = FactQuery(store.conn)

        # inverse: b called-by a
        called_by = {(r.subject_name, r.object_name)
                     for r in fq.query_facts(predicate="called-by", limit=100)}
        assert ("b", "a") in called_by

        # transitive isa: Leaf isa Base (2 hops), 1-hop not re-emitted
        isa = {(r.subject_name, r.object_name)
               for r in fq.query_facts(predicate="isa", limit=100)}
        assert ("Leaf", "Base") in isa
        assert ("Leaf", "Mid") not in isa

        # reaches: a -> b -> c means a reaches c with a proof path
        reach = [r for r in fq.query_facts(predicate="reaches", limit=100)
                 if r.subject_name == "a" and r.object_name == "c"]
        assert reach

        # may-raise: a may-raise KeyError through the 2-hop call chain
        may = {(r.subject_name, r.object_name)
               for r in fq.query_facts(predicate="may-raise", limit=100)}
        assert ("a", "KeyError") in may
        assert ("b", "KeyError") in may

        # module depends, both levels: the AST import fact (module names) and
        # the derived file-path aggregation from the cross-file call edge
        dep = {(r.subject_name, r.object_name)
               for r in fq.query_facts(predicate="depends-on", limit=200)}
        assert ("pkg.io", "pkg") in dep               # det: import (top module)
        assert ("pkg/io.py", "pkg/core.py") in dep    # der: cross-file symbol edge


def test_build_is_idempotent(tmp_path: Path) -> None:
    repo = _built_store(tmp_path)
    db = tmp_path / "g.db"
    with GraphStore.open(db) as store:
        first = _write_code_facts(store, repo, "demo", NOW)
        again = _write_code_facts(store, repo, "demo", "2026-01-02T00:00:00+00:00")
    assert first > 0
    assert again == 0  # unchanged repo -> nothing new written
