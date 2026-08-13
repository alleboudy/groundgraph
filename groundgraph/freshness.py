"""Build-freshness metric: the graph is only as current as its source repos.

The failure this prevents: a graph silently built on a checkout hundreds of
commits behind origin describes code that no longer exists — and internal
health metrics are blind to it (they measure precision rot, not source lag).

GROUND-TRUTH-ONLY numbers: every count comes from git plumbing
(`git rev-list --count`, `git rev-parse`, `git status --porcelain`) — never
an estimate. When git cannot answer (no upstream, not a repo, git missing)
the value is reported as "unknown" (`None`, `-1`, `"unknown"`), never a
guessed 0.
"""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Local git plumbing is fast; a ceiling guards against a hung git process
# (e.g. a stale index.lock) blocking the dashboard.
_GIT_TIMEOUT_S = 10


def _git(repo_path: str, *args: str) -> str | None:
    """Run `git -C <repo_path> <args>`; stripped stdout on success, None on
    any failure. A SUCCESS with empty output returns "" — distinct from None —
    so a clean `status --porcelain` is not mistaken for a failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git %s failed in %s: %s", " ".join(args), repo_path, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def repo_freshness(repo_path: str) -> dict:
    """Git-derived freshness for one repo. Never raises.

    Returns {repo, branch, behind, ahead, dirty, head}:
    - behind/ahead: commit counts vs the upstream, or None when unknown.
    - dirty: `git status --porcelain` line count (0 == clean), or -1 when the
      path is not a git repo (dirty is unknowable, not zero).
    - branch/head: current branch and short sha, or "unknown".
    """
    name = os.path.basename(os.path.abspath(repo_path)) or repo_path
    freshness: dict = {
        "repo": name, "branch": "unknown", "behind": None,
        "ahead": None, "dirty": -1, "head": "unknown",
    }
    if _git(repo_path, "rev-parse", "--is-inside-work-tree") != "true":
        logger.warning("not a git work tree (or git unavailable): %s", repo_path)
        return freshness

    branch = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        freshness["branch"] = branch
    head = _git(repo_path, "rev-parse", "--short", "HEAD")
    if head:
        freshness["head"] = head
    porcelain = _git(repo_path, "status", "--porcelain")
    if porcelain is not None:  # "" (clean) is a real answer -> 0, not unknown
        freshness["dirty"] = len(porcelain.splitlines())
    # No upstream -> both rev-list calls fail -> None (honest unknown), never 0.
    freshness["behind"] = _to_int(_git(repo_path, "rev-list", "--count", "HEAD..@{u}"))
    freshness["ahead"] = _to_int(_git(repo_path, "rev-list", "--count", "@{u}..HEAD"))
    return freshness


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def graph_freshness(repo_paths: list[str], *,
                    newest_fact_ts: str | None = None) -> dict:
    """Aggregate source-freshness across the graph's source repos.

    Reads git only (no DB, no network). Returns per-repo freshness plus a
    `warnings` list, shaped like `graph_health`'s dict so both drop onto the
    same status surface. `stalest_behind` is the max known `behind`, or None
    when git could not answer for any repo (never a guessed 0).
    """
    per_repo: list[dict] = []
    warnings: list[str] = []
    known_behind: list[int] = []
    for path in repo_paths:
        fresh = repo_freshness(path)
        per_repo.append(fresh)
        behind = fresh["behind"]
        if isinstance(behind, int):
            known_behind.append(behind)
            if behind > 0:
                warnings.append(f"{fresh['repo']} is {behind} commits behind origin")
        if fresh["dirty"] > 0:
            warnings.append(f"{fresh['repo']} has uncommitted changes")
        if behind is None and fresh["dirty"] == -1:
            warnings.append(f"{fresh['repo']} freshness unknown — not a git repo?")

    graph_age_days: float | None = None
    if newest_fact_ts is not None:
        parsed = _parse_ts(newest_fact_ts)
        if parsed is not None:
            now = datetime.now(tz=parsed.tzinfo or timezone.utc)
            graph_age_days = round(max(0.0, (now - parsed).total_seconds() / 86400.0), 2)

    return {
        "repos": per_repo,
        "stalest_behind": max(known_behind) if known_behind else None,
        "newest_fact_ts": newest_fact_ts,
        "graph_age_days": graph_age_days,
        "warnings": warnings,
    }
