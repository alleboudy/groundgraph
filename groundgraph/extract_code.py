"""Deterministic code-fact extractors (confidence 1.0, file:line provenance).

The backbone of the graph: exact, cheap, and re-derivable. Python facts come
from the stdlib ``ast`` module (zero dependencies); other languages get
imports / defined-in / depends-on via cheap line regexes. Cross-language
``calls`` is deliberately not emitted — a regex guess at call graphs would
break the ground-truth mandate.
"""
from __future__ import annotations

import ast
import re

from groundgraph.types import ExtractedFact

__all__ = ["CODE_EXTS", "ExtractedFact", "extract_file"]

CODE_EXTS = {
    "py", "ts", "tsx", "js", "jsx", "swift", "kt", "kts", "cpp", "cc", "cxx",
    "c", "h", "hpp", "hh", "rs", "go", "java", "rb", "php", "scala", "m", "mm",
}


def _module_name(rel_path: str) -> str:
    """`a/b/c.py` -> `a.b.c`; `a/b/__init__.py` -> `a.b`; `ui/app.ts` -> `ui.app`."""
    p = rel_path
    if "/" in p:
        head, tail = p.rsplit("/", 1)
        tail = tail.split(".")[0]
        p = f"{head}/{tail}"
    else:
        p = p.split(".")[0]
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def _callee_name(func: ast.expr) -> str | None:
    """Dotted name of a Call target: Name -> 'x'; Attribute -> 'a.b.title'."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts: list[str] = []
        cur: ast.expr = func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _direct_calls(func_node: ast.AST) -> list[tuple[str, int]]:
    """Calls lexically inside func_node but NOT inside a nested def/class."""
    out: list[tuple[str, int]] = []

    def rec(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # nested scope is visited on its own
            if isinstance(child, ast.Call):
                callee = _callee_name(child.func)
                if callee:
                    out.append((callee, child.lineno))
            rec(child)

    rec(func_node)
    return out


def _decorator_names(node: ast.AST) -> list[tuple[str, int]]:
    """Resolvable decorator names on a def/class: `@requires_auth` -> that name,
    `@router.post` -> the dotted chain, `@router.post("/x")` -> the call's func
    chain. Unresolvable expressions are skipped."""
    out: list[tuple[str, int]] = []
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = _callee_name(target)
        if name:
            out.append((name, dec.lineno))
    return out


def _direct_raises(func_node: ast.AST) -> list[tuple[str, int]]:
    """`raise X(...)` / `raise X` lexically inside func_node but NOT inside a
    nested def/class (same scoping rule as _direct_calls). Bare re-raise
    carries no symbol and is skipped."""
    out: list[tuple[str, int]] = []

    def rec(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Raise) and child.exc is not None:
                exc = child.exc.func if isinstance(child.exc, ast.Call) else child.exc
                name = _callee_name(exc)
                if name:
                    out.append((name, child.lineno))
            rec(child)

    rec(func_node)
    return out


def extract_python(rel_path: str, source: str, repo: str) -> list[ExtractedFact]:
    facts: list[ExtractedFact] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return facts
    file_module = _module_name(rel_path)

    def emit(
        sk: str, sn: str, pred: str, ok: str | None, on: str | None,
        lineno: int, *, spath: str | None = None,
    ) -> None:
        facts.append(
            ExtractedFact(
                subject_kind=sk, subject_name=sn, subject_repo=repo, subject_path=spath,
                predicate=pred, object_kind=ok, object_name=on, object_lit=None,
                extractor="det:ast@1", source_kind="code_line",
                source_ref=f"{rel_path}:{lineno}",
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                emit("file", rel_path, "imports", "module", top, node.lineno, spath=rel_path)
                emit("module", file_module, "depends-on", "module", top, node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            emit("file", rel_path, "imports", "module", top, node.lineno, spath=rel_path)
            emit("module", file_module, "depends-on", "module", top, node.lineno)

    class _V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _qual(self, name: str) -> str:
            return ".".join([*self.stack, name])

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qn = self._qual(node.name)
            emit("symbol", qn, "defined-in", "file", rel_path, node.lineno, spath=rel_path)
            # Class hierarchy — one `inherits` edge per resolvable base.
            for base in node.bases:
                base_name = _callee_name(base)
                if base_name:
                    emit("symbol", qn, "inherits", "symbol", base_name, node.lineno,
                         spath=rel_path)
            for name, lineno in _decorator_names(node):
                emit("symbol", qn, "decorated-by", "symbol", name, lineno, spath=rel_path)
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _visit_func(self, node: ast.AST) -> None:
            qn = self._qual(node.name)  # type: ignore[attr-defined]
            emit("symbol", qn, "defined-in", "file", rel_path, node.lineno, spath=rel_path)  # type: ignore[attr-defined]
            for callee, lineno in _direct_calls(node):
                emit("symbol", qn, "calls", "symbol", callee, lineno, spath=rel_path)
            for exc_name, lineno in _direct_raises(node):
                emit("symbol", qn, "raises", "symbol", exc_name, lineno, spath=rel_path)
            for name, lineno in _decorator_names(node):
                emit("symbol", qn, "decorated-by", "symbol", name, lineno, spath=rel_path)
            self.stack.append(node.name)  # type: ignore[attr-defined]
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _visit_func  # type: ignore[assignment]
        visit_AsyncFunctionDef = _visit_func  # type: ignore[assignment]

    _V().visit(tree)
    return facts


# Import-line patterns by language family. First capturing group = module.
_IMPORT_PATTERNS = [
    re.compile(r"""\bfrom\s+["']([^"']+)["']"""),                 # ts/js: import x from "mod"
    re.compile(r"""^\s*#include\s+[<"]([^>"]+)[>"]"""),           # c/c++
    re.compile(r"""^\s*import\s+["']?([\w./@-]+)["']?"""),        # swift/kotlin/java/go/ts-bare
]
# Top-level declaration with a capturable symbol name (swift/kt/ts/c++/...).
_DECL_PATTERN = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|internal\s+|open\s+|final\s+|"
    r"static\s+|override\s+|default\s+|fileprivate\s+)*"
    r"(?:func|function|class|struct|enum|protocol|extension|interface)\s+([A-Za-z_]\w*)"
)


def _strip_cpp_include(mod: str) -> str:
    """`vector` from `<vector>`; basename for `a/b.h`."""
    return mod.rsplit("/", 1)[-1].split(".", maxsplit=1)[0] if "." in mod or "/" in mod else mod


def _match_import(line: str) -> str | None:
    for i, pat in enumerate(_IMPORT_PATTERNS):
        m = pat.search(line)
        if m:
            mod = m.group(1)
            return _strip_cpp_include(mod) if i == 1 else mod
    return None


def extract_generic(rel_path: str, source: str, repo: str) -> list[ExtractedFact]:
    facts: list[ExtractedFact] = []
    file_module = _module_name(rel_path)
    for lineno, line in enumerate(source.splitlines(), start=1):
        mod = _match_import(line)
        if mod:
            facts.append(
                ExtractedFact(
                    subject_kind="file", subject_name=rel_path, subject_repo=repo,
                    subject_path=rel_path, predicate="imports", object_kind="module",
                    object_name=mod, object_lit=None, extractor="det:regex@1",
                    source_kind="code_line", source_ref=f"{rel_path}:{lineno}",
                )
            )
            facts.append(
                ExtractedFact(
                    subject_kind="module", subject_name=file_module, subject_repo=repo,
                    subject_path=None, predicate="depends-on", object_kind="module",
                    object_name=mod, object_lit=None, extractor="det:regex@1",
                    source_kind="code_line", source_ref=f"{rel_path}:{lineno}",
                )
            )
        decl = _DECL_PATTERN.match(line)
        if decl:
            facts.append(
                ExtractedFact(
                    subject_kind="symbol", subject_name=decl.group(1), subject_repo=repo,
                    subject_path=rel_path, predicate="defined-in", object_kind="file",
                    object_name=rel_path, object_lit=None, extractor="det:regex@1",
                    source_kind="code_line", source_ref=f"{rel_path}:{lineno}",
                )
            )
    return facts


def extract_file(rel_path: str, source: str, repo: str) -> list[ExtractedFact]:
    """Route by extension: Python -> AST, everything else -> regex."""
    if rel_path.endswith(".py"):
        return extract_python(rel_path, source, repo)
    return extract_generic(rel_path, source, repo)
