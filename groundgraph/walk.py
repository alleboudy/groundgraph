"""Shared repo-walking rules for every tree walker in the toolkit.

Two defenses:

* ``EXCLUDED_DIR_PARTS`` — a name list for build output, caches, vendored
  package stores, and virtualenvs.
* ``iter_repo_files`` — the walker itself, which ALSO prunes any nested git
  checkout (a subdir containing ``.git`` — a dir for clones, a file for
  worktrees). Name lists cannot win against checkouts: a nested checkout is
  duplicate repo content by construction, wherever it hides and whatever it
  is called. Indexing one silently doubles the graph.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

EXCLUDED_DIR_PARTS: frozenset[str] = frozenset({
    # nested-checkout conventions (the .git prune catches their contents)
    ".worktrees", ".clone",
    # VCS + virtualenvs + caches
    ".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", ".tox", ".cache",
    # package managers' stores — vendored copies, not project source
    "node_modules", ".pnpm-store", ".yarn", "Pods", "Carthage", "vendor",
    # build / generated output
    "build", "dist", "out", "target", "DerivedData", ".next", ".nuxt",
    ".output", ".svelte-kit", ".angular", ".parcel-cache", ".turbo",
    ".expo", ".dart_tool", ".gradle", "coverage", "bin", "obj",
    "generated", "gen", "Generated", "public", "www",
})


def iter_repo_files(root: Path) -> Iterator[Path]:
    """Every file under ``root`` that a walker should consider.

    ``os.walk`` with top-down pruning — excluded dir names are never descended
    into, and any subdirectory that is itself a git checkout (contains
    ``.git``) is pruned wholesale. Yields files in sorted order (dirs and
    names) so callers stay deterministic.
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = sorted(
            n for n in dirnames
            if n not in EXCLUDED_DIR_PARTS and not (d / n / ".git").exists()
        )
        for name in sorted(filenames):
            yield d / name
