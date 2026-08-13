from __future__ import annotations

from groundgraph.extract_code import extract_file

SRC = '''\
import os
from json import dumps


class Base:
    pass


class Leaf(Base):
    @property
    def title(self) -> str:
        return dumps({})


def helper():
    raise ValueError("boom")


def entry():
    helper()
    return os.getcwd()
'''


def _facts():
    return extract_file("app/mod.py", SRC, "demo")


def test_defined_in_with_line_provenance() -> None:
    facts = _facts()
    defined = {f.subject_name: f.source_ref for f in facts if f.predicate == "defined-in"}
    assert defined["Leaf"] == "app/mod.py:9"
    assert defined["entry"].startswith("app/mod.py:")
    assert all(f.extractor == "det:ast@1" for f in facts)


def test_calls_inherits_raises_decorators() -> None:
    facts = _facts()
    calls = {(f.subject_name, f.object_name) for f in facts if f.predicate == "calls"}
    assert ("entry", "helper") in calls
    inherits = {(f.subject_name, f.object_name) for f in facts if f.predicate == "inherits"}
    assert ("Leaf", "Base") in inherits
    raises = {(f.subject_name, f.object_name) for f in facts if f.predicate == "raises"}
    assert ("helper", "ValueError") in raises
    decorated = {(f.subject_name, f.object_name) for f in facts if f.predicate == "decorated-by"}
    assert ("Leaf.title", "property") in decorated


def test_imports_and_module_depends() -> None:
    facts = _facts()
    imports = {f.object_name for f in facts if f.predicate == "imports"}
    assert {"os", "json"} <= imports
    depends = {(f.subject_name, f.object_name) for f in facts if f.predicate == "depends-on"}
    assert ("app.mod", "os") in depends


def test_generic_regex_language() -> None:
    swift = 'import SwiftUI\nstruct ScoreView {\n}\nfunc render() {}\n'
    facts = extract_file("ui/Score.swift", swift, "demo")
    assert any(f.predicate == "imports" and f.object_name == "SwiftUI" for f in facts)
    defined = {f.subject_name for f in facts if f.predicate == "defined-in"}
    assert {"ScoreView", "render"} <= defined


def test_syntax_error_returns_empty() -> None:
    assert extract_file("bad.py", "def (broken", "demo") == []
