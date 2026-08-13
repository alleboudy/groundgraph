"""Deterministic extractor for the `tests` relation (test symbol -> subject).

Answers "what covers symbol S?" (reverse lookup) and "what does test T
exercise?". GROUND-TRUTH MANDATE: a `tests` fact is rooted in what the test
ACTUALLY does, never a naming guess. `<test_fn> tests <subject>` is emitted
only when BOTH hold:

  1. `subject` is a FIRST-PARTY symbol the test module IMPORTS. Module-level
     `import` / `from ... import` statements say which names are external
     subjects; the stdlib, the test/mock stack, and ubiquitous 3rd-party libs
     are denied so `tests` points at first-party subjects only.
  2. the test FUNCTION actually REFERENCES that imported name — a call
     `S(...)`, a chain `S.x`, or a bare use, with the same nested-scope rule
     as the call extractor (a reference inside a nested def/class is not
     attributed).

Fail-closed: no import, or no reference -> NO fact. No LLM, no I/O.

Confidence 0.95: AST-grounded, but the test<->subject link is an authoring
convention, so just below the 1.0 floor for pure structural code facts.
Provenance (import line + reference lines) rides in `excerpt`.
"""
from __future__ import annotations

import ast
import logging
import re

from groundgraph.extract_code import _module_name
from groundgraph.types import ProposedFact

logger = logging.getLogger(__name__)

__all__ = ["extract_test_facts", "is_test_file"]

_CONF = 0.95
_EXTRACTOR = "det:tests@1"
_PREDICATE = "tests"
_MAX_REFS = 8  # cap reference line numbers shown in the excerpt

# A file is a test file iff basename `test_*.py`, basename `*_test.py`, or it
# lives under a `tests?/` directory.
_TEST_FILE_RE = re.compile(r"(?:(?:^|/)test_[^/]*\.py$)|(?:_test\.py$)|(?:(?:^|/)tests?/)")

# Top-level module names whose symbols are NOT first-party subjects: the python
# stdlib, the pytest/mock stack, and the most common 3rd-party libraries.
# Small and curated, not an exhaustive index. LIMITATION: a first-party module
# sharing one of these names would be wrongly denied.
_DENY_TOP_MODULES = frozenset({
    # pytest / test / mock stack
    "pytest", "_pytest", "unittest", "mock", "nose", "nose2", "hypothesis",
    "doctest", "coverage", "freezegun", "responses", "respx", "faker",
    "factory", "parameterized", "testfixtures", "syrupy", "snapshottest",
    "pytest_asyncio", "pytest_mock", "pytest_django", "tox",
    # python stdlib (the slice that shows up in tests)
    "__future__", "abc", "argparse", "array", "ast", "asyncio", "base64",
    "bisect", "builtins", "calendar", "collections", "configparser",
    "contextlib", "copy", "csv", "ctypes", "dataclasses", "datetime",
    "decimal", "difflib", "dis", "email", "enum", "errno", "fnmatch",
    "fractions", "functools", "gc", "getpass", "glob", "gzip", "hashlib",
    "heapq", "hmac", "html", "http", "importlib", "inspect", "io",
    "ipaddress", "itertools", "json", "keyword", "logging", "math",
    "mimetypes", "multiprocessing", "numbers", "operator", "os", "pathlib",
    "pickle", "platform", "pprint", "queue", "random", "re", "secrets",
    "select", "shlex", "shutil", "signal", "socket", "sqlite3", "ssl",
    "stat", "string", "struct", "subprocess", "sys", "tarfile", "tempfile",
    "textwrap", "threading", "time", "timeit", "tomllib", "traceback",
    "types", "typing", "unicodedata", "urllib", "uuid", "warnings",
    "weakref", "xml", "zipfile", "zlib", "zoneinfo",
    # ubiquitous 3rd-party (would otherwise leak as pseudo-subjects)
    "numpy", "pandas", "scipy", "sklearn", "torch", "tensorflow", "keras",
    "matplotlib", "seaborn", "PIL", "cv2", "requests", "httpx", "aiohttp",
    "urllib3", "yaml", "toml", "click", "rich", "tqdm", "pydantic",
    "sqlalchemy", "flask", "fastapi", "starlette", "django", "boto3",
    "botocore", "redis", "celery", "pymongo", "psycopg2", "psycopg",
    "asyncpg", "attr", "attrs", "dateutil", "pytz", "setuptools",
    "pkg_resources", "six", "dotenv", "jinja2", "markupsafe", "werkzeug",
})


def is_test_file(rel_path: str) -> bool:
    """True iff `rel_path` is a python test file."""
    return bool(_TEST_FILE_RE.search(rel_path))


def _resolve_relative(rel_path: str, module: str | None, level: int) -> str | None:
    """Resolve a relative import's base module against the file's own package.
    `from .x import y` in `a/b/test_z.py` -> base `a.b.x`; `from ..x import y`
    -> `a.x`. Returns the bare module (or None) if the level walks above the
    package root."""
    pkg_parts = _module_name(rel_path).split(".")[:-1]
    keep = len(pkg_parts) - (level - 1)
    if keep < 0:
        return module
    base = pkg_parts[:keep]
    if module:
        base = [*base, *module.split(".")]
    return ".".join(base) if base else module


def _handle_import(node: ast.Import) -> list[tuple[str, str, int, str]]:
    """`import a.b.c [as z]` -> bindings, dropping denied top modules."""
    out: list[tuple[str, str, int, str]] = []
    for alias in node.names:
        if alias.name.split(".")[0] in _DENY_TOP_MODULES:
            continue
        local = alias.asname or alias.name.split(".")[0]
        repr_ = f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
        out.append((local, alias.name, node.lineno, repr_))
    return out


def _handle_import_from(node: ast.ImportFrom, rel_path: str) -> list[tuple[str, str, int, str]]:
    """`from M import a [as z]` -> bindings. Absolute imports are dropped when
    M's top module is denied; relative imports are first-party by construction.
    Star imports carry no symbol."""
    if node.level:
        base = _resolve_relative(rel_path, node.module, node.level)
    else:
        base = node.module
        if base is None or base.split(".")[0] in _DENY_TOP_MODULES:
            return []
    dots = "." * node.level
    out: list[tuple[str, str, int, str]] = []
    for alias in node.names:
        if alias.name == "*":
            continue
        local = alias.asname or alias.name
        subject = f"{base}.{alias.name}" if base else alias.name
        repr_ = f"from {dots}{node.module or ''} import {alias.name}" + (
            f" as {alias.asname}" if alias.asname else ""
        )
        out.append((local, subject, node.lineno, repr_))
    return out


def _collect_imports(tree: ast.Module, rel_path: str) -> dict[str, tuple[str, int, str]]:
    """Map each module-level bound name -> (subject_dotted, lineno, import_repr)
    for FIRST-PARTY imports only. Later imports of the same name win. Only
    top-level statements count."""
    out: dict[str, tuple[str, int, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            bindings = _handle_import(node)
        elif isinstance(node, ast.ImportFrom):
            bindings = _handle_import_from(node, rel_path)
        else:
            continue
        for local, subject, lineno, repr_ in bindings:
            out[local] = (subject, lineno, repr_)
    return out


def _referenced_names(func_node: ast.AST) -> dict[str, list[int]]:
    """Names referenced in Load context directly inside `func_node`, NOT inside
    a nested def/class. Maps name -> sorted line numbers."""
    refs: dict[str, list[int]] = {}

    def rec(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                refs.setdefault(child.id, []).append(child.lineno)
            rec(child)

    rec(func_node)
    return {name: sorted(set(lines)) for name, lines in refs.items()}


def _iter_test_functions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every `def test_*` / `async def test_*` a runner could collect: a
    top-level function or a method on a (possibly nested) class — NOT one
    nested inside another function. Returns (qualname, node)."""
    out: list[tuple[str, ast.AST]] = []

    def walk(node: ast.AST, stack: list[str], in_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, [*stack, child.name], in_func)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not in_func and child.name.startswith("test_"):
                    out.append((".".join([*stack, child.name]), child))
                walk(child, [*stack, child.name], True)

    walk(tree, [], False)
    return out


def extract_test_facts(source: str, rel_path: str, repo: str) -> list[ProposedFact]:
    """Extract `tests` facts (test symbol -> the first-party subject it tests)
    from a python test file. PURE: stdlib `ast` only; no I/O, no model.
    Returns [] for a non-test file or a syntax error."""
    if not is_test_file(rel_path):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    imports = _collect_imports(tree, rel_path)
    if not imports:
        return []

    origin = f"code:{rel_path}"
    facts: list[ProposedFact] = []
    for qualname, func in _iter_test_functions(tree):
        seen: set[str] = set()
        for local, ref_lines in _referenced_names(func).items():
            entry = imports.get(local)
            if entry is None:
                continue
            subject, imp_line, imp_repr = entry
            if subject in seen:
                continue
            seen.add(subject)
            shown = ",".join(str(n) for n in ref_lines[:_MAX_REFS])
            more = "…" if len(ref_lines) > _MAX_REFS else ""
            excerpt = f"{imp_repr}  # import@L{imp_line}; ref@L{shown}{more}"
            facts.append(ProposedFact(
                subject_kind="symbol", subject_name=qualname, subject_repo=repo,
                predicate=_PREDICATE, object_kind="symbol", object_name=subject,
                object_lit=None, confidence=_CONF, extractor=_EXTRACTOR,
                origin=origin, excerpt=excerpt,
            ))
    return facts
