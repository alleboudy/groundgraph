"""Deterministic co-change extractor: files that repeatedly change together.

A real coupling signal static analysis cannot see — "what tends to break
together." Two files with no import, call, or inheritance edge between them
can still be tightly coupled if every fix to one forces a fix to the other;
only the commit history reveals it. Every fact is grounded in actual
`git log` co-occurrence counts. The relation is `co-changed-with`.

Two-part design:

* `cochange_pairs` — PURE. Parses `git log --name-only --pretty=format:%H`
  text into per-commit file sets and counts unordered file-pair
  co-occurrences. It bounds the blast radius of large commits (a 50-file
  refactor must not mint C(50,2)=1225 spurious pairs) and returns only pairs
  meeting a support threshold.
* `extract_cochange_facts` — runs git, calls cochange_pairs, keeps CODE-file
  pairs only, and emits ProposedFacts.

Fail-closed: a malformed log (a file path before any commit header) raises
`CochangeParseError` rather than guessing. A git failure logs a warning and
yields no facts.
"""
from __future__ import annotations

import itertools
import logging
import re
import subprocess
from collections.abc import Iterator

from groundgraph.types import ProposedFact

logger = logging.getLogger(__name__)

__all__ = ["CODE_EXTS", "CochangeParseError", "cochange_pairs", "extract_cochange_facts"]

# Deliberate CODE-only allowlist, narrower than extract_code.CODE_EXTS: a
# temporal coupling between two source files is meaningful; co-change over
# lockfiles/generated/docs is mostly mechanical churn.
CODE_EXTS: frozenset[str] = frozenset({
    "py", "ts", "tsx", "swift", "rs", "js", "go", "kt",
})

_PREDICATE = "co-changed-with"
_EXTRACTOR = "det:git-cochange@1"

# Confidence rises with support but caps BELOW the deterministic-CODE floor:
# a temporal coupling is real, git-grounded evidence, but weaker than an AST
# edge. The cap bites at support >= 8 (0.5 + 0.05*8 = 0.9).
_CONF_BASE = 0.5
_CONF_STEP = 0.05
_CONF_CAP = 0.9

_MAX_FACTS = 50_000        # hard cap; a LOGGED warning fires if hit
_GIT_TIMEOUT = 120.0
_MIN_COMMIT_FILES = 2
_HASH_RE = re.compile(r"[0-9a-f]{40}")


class CochangeParseError(ValueError):
    """git-log text is not in `--name-only --pretty=format:%H` shape (a file
    path appears before any commit header). Fail-closed: refuse to mint pairs
    from an unrecognized format rather than guessing commit boundaries."""


def _iter_commit_filesets(git_log_text: str) -> Iterator[set[str]]:
    """Yield the set of file paths touched by each commit.

    A full 40-hex SHA line starts a new commit; any other non-blank line is a
    path in the current commit; blank lines are skipped. Raises
    CochangeParseError if a path precedes the first commit header.
    """
    files: set[str] | None = None
    for raw in git_log_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _HASH_RE.fullmatch(line):
            if files is not None:
                yield files
            files = set()
        elif files is None:
            raise CochangeParseError(f"file line before any commit header: {line!r}")
        else:
            files.add(line)
    if files is not None:
        yield files


def cochange_pairs(
    git_log_text: str, *, max_files_per_commit: int = 12, min_support: int = 3,
) -> dict[tuple[str, str], int]:
    """Count unordered file-pair co-occurrences across commits. PURE.

    Returns ``{(fileA, fileB): count}`` with fileA < fileB (canonical
    unordered key), keeping only pairs with count >= ``min_support``.

    Commits touching MORE than ``max_files_per_commit`` files are IGNORED
    whole — the core noise-control lever. A bulk rename / reformat /
    dependency bump co-changes everything mechanically, and pair count grows
    as C(n, 2); bounding the per-commit blast radius keeps a mega-commit from
    fabricating coupling that isn't there.
    """
    counts: dict[tuple[str, str], int] = {}
    for files in _iter_commit_filesets(git_log_text):
        if len(files) < _MIN_COMMIT_FILES or len(files) > max_files_per_commit:
            continue
        for pair in itertools.combinations(sorted(files), 2):
            counts[pair] = counts.get(pair, 0) + 1
    return {pair: c for pair, c in counts.items() if c >= min_support}


def _is_code_file(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in CODE_EXTS


def _confidence(support: int) -> float:
    """min(0.9, 0.5 + 0.05*support): more co-changes -> higher confidence,
    never reaching the deterministic-CODE floor."""
    return min(_CONF_CAP, _CONF_BASE + _CONF_STEP * support)


def _facts_from_pairs(pairs: dict[tuple[str, str], int], repo: str) -> list[ProposedFact]:
    """Pure pairs -> ProposedFacts: CODE-filter, order by support, emit
    SYMMETRIC facts, honor the cap.

    SYMMETRIC by choice: co-change is undirected, but the graph is traversed
    subject-anchored. Storing only one canonical (a<b) row would make "what
    co-changes with X?" miss X's partners whenever X sorts to the larger
    side. Emitting BOTH directions makes the undirected edge answerable from
    either endpoint by a plain subject lookup.
    """
    code_pairs = {
        pair: c for pair, c in pairs.items()
        if _is_code_file(pair[0]) and _is_code_file(pair[1])
    }
    # Deterministic order: strongest coupling first, then lexical — if the cap
    # ever bites, it stably keeps the highest-support pairs.
    ordered = sorted(code_pairs.items(), key=lambda kv: (-kv[1], kv[0]))

    facts: list[ProposedFact] = []
    origin = f"git:{repo}"
    capped = False
    for (file_a, file_b), support in ordered:
        if len(facts) + 2 > _MAX_FACTS:   # keep a pair's two directions atomic
            capped = True
            break
        conf = _confidence(support)
        excerpt = f"co-changed in {support} commits"
        for subject, obj in ((file_a, file_b), (file_b, file_a)):
            facts.append(ProposedFact(
                subject_kind="file", subject_name=subject, subject_repo=repo,
                predicate=_PREDICATE, object_kind="file", object_name=obj,
                object_lit=None, confidence=conf, extractor=_EXTRACTOR,
                origin=origin, excerpt=excerpt,
            ))
    if capped:
        logger.warning(
            "cochange: hit the %d-fact cap for %s; emitting a truncated "
            "(highest-support-first) set from %d code pairs — NOT silent",
            _MAX_FACTS, repo, len(code_pairs),
        )
    return facts


def extract_cochange_facts(
    repo_root: str, repo: str, *, max_commits: int = 4000,
) -> list[ProposedFact]:
    """Extract `co-changed-with` ProposedFacts from a repo's git history.

    Fail-closed: a git failure (binary missing, non-zero exit, timeout, bad
    path) logs a warning and returns [] — no facts, not a crash.
    """
    cmd = ["git", "-C", repo_root, "log", "--name-only",
           "--pretty=format:%H", "-n", str(max_commits)]
    try:
        proc = subprocess.run(
            cmd, check=True, capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("cochange: `git log` failed for %s (%s): %s", repo, repo_root, e)
        return []

    pairs = cochange_pairs(proc.stdout)
    facts = _facts_from_pairs(pairs, repo)
    logger.info(
        "cochange: %s -> %d facts from %d raw pairs (>= min_support)",
        repo, len(facts), len(pairs),
    )
    return facts
