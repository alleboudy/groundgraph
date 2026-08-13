"""Event-driven freshness daemon: check-then-act, not busy-rebuild.

The failure this prevents: a graph silently built on a checkout hundreds of
commits behind origin describes code that no longer exists. One pass:

  CHECK  git fetch each source repo; measure commits behind upstream.
  DECIDE nothing behind and no --force -> "graph fresh, nothing to do".
  ACT    fast-forward the CLEAN behind repos only (a dirty working tree is
         NEVER touched — no stash, no reset; the operator's uncommitted work
         is sacred and the skip is logged loudly), then rebuild.

A single-instance lock (fcntl, stdlib) makes overlapping passes skip instead
of stacking writes on the same database: a rebuild can outlast a short
scheduler interval. Builds are idempotent, so a pass over an unchanged graph
writes nothing.
"""
from __future__ import annotations

import fcntl
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from groundgraph.freshness import repo_freshness

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepoPlan:
    repo: str
    behind: int | None
    dirty: int
    action: str   # "pull" | "skip-dirty" | "skip-fresh" | "skip-unknown"


def plan_repo(fresh: dict) -> RepoPlan:
    """PURE decision for one repo's freshness dict (from repo_freshness):

    - behind unknown (no upstream / not a repo)  -> skip-unknown
    - behind == 0                                -> skip-fresh
    - behind > 0 and dirty                       -> skip-dirty (sacred rule)
    - behind > 0 and clean                       -> pull
    """
    behind, dirty = fresh["behind"], fresh["dirty"]
    if not isinstance(behind, int):
        action = "skip-unknown"
    elif behind == 0:
        action = "skip-fresh"
    elif dirty != 0:
        action = "skip-dirty"
    else:
        action = "pull"
    return RepoPlan(repo=fresh["repo"], behind=behind if isinstance(behind, int) else None,
                    dirty=dirty, action=action)


def _git(repo_path: str, *args: str) -> bool:
    try:
        return subprocess.run(["git", "-C", repo_path, *args],
                              capture_output=True, text=True, timeout=120,
                              check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_pass(db: str, repo_paths: list[str], *, docs: list[str] | None,
             force: bool = False, fetch: bool = True) -> dict:
    """One check-then-act pass. Returns a summary dict. Never raises on a
    single repo's failure — one bad repo never aborts the pass."""
    plans: list[RepoPlan] = []
    pulled: list[str] = []
    for path in repo_paths:
        if fetch and not _git(path, "fetch", "-q"):
            logger.warning("watch: git fetch failed for %s (network?)", path)
        plan = plan_repo(repo_freshness(path))
        plans.append(plan)
        if plan.action == "pull":
            if _git(path, "pull", "--ff-only", "-q"):
                pulled.append(plan.repo)
                logger.info("watch: fast-forwarded %s (%s behind)",
                            plan.repo, plan.behind)
            else:
                logger.warning("watch: ff-only pull failed for %s — left as-is",
                               plan.repo)
        elif plan.action == "skip-dirty":
            logger.warning("watch: %s is %s behind but DIRTY (%d path(s)) — "
                           "not pulling, operator work preserved",
                           plan.repo, plan.behind, plan.dirty)

    rebuild = force or bool(pulled)
    summary = {"plans": [p.__dict__ for p in plans], "pulled": pulled,
               "rebuilt": False}
    if not rebuild:
        logger.info("watch: graph fresh, nothing to do "
                    "(0 pulls across %d repo(s))", len(repo_paths))
        return summary
    # Rebuild via the same pipeline as `build` (idempotent).
    from groundgraph.__main__ import run_full_build
    totals = run_full_build(db, repo_paths, docs=docs, no_derive=False)
    summary["rebuilt"] = True
    summary["written"] = totals
    return summary


def watch(db: str, repo_paths: list[str], *, docs: list[str] | None = None,
          interval: float | None = None, force: bool = False,
          lock_path: str | None = None) -> int:
    """Single pass (interval=None) or loop. The fcntl lock makes a
    concurrent invocation skip cleanly (exit 0, logged) instead of stacking
    writes on the same db file."""
    lock_file = Path(lock_path or (str(db) + ".lock"))
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_file.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.warning("watch: another pass holds %s — skipping (not an error)",
                       lock_file)
        fh.close()
        return 0
    try:
        while True:
            run_pass(db, repo_paths, docs=docs, force=force)
            if interval is None:
                return 0
            logger.info("watch: sleeping %.0fs", interval)
            time.sleep(interval)
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
