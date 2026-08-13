"""Bounded exception propagation: `A calls B`, `B raises E` -> `A may-raise E`.

Through >= 1 call hop; depth-0 own-raises stay as the base `raises` facts.
Each fact is GROUNDED — its excerpt carries the full proof path
`A -> ... -> F raises E` — and its confidence decays per call hop (a raise
three calls deep is a weaker caller signal than one call deep). Truncation is
LOGGED, never silent.
"""
from __future__ import annotations

import logging

from groundgraph.derive import _live_edges
from groundgraph.types import ProposedFact

logger = logging.getLogger(__name__)

MAYRAISE_MAX_DEPTH = 3       # call hops to walk back from a raise (real stacks are shallow)
MAYRAISE_PER_SOURCE = 20     # distinct exceptions emitted per source (hub fan-out guard)
MAYRAISE_CAP = 80_000        # total runaway guard
MAYRAISE_DECAY = 0.9         # per call-hop confidence decay


def _reachable_raises(
    adj: dict[str, list[str]], raises: dict[str, set[str]],
    src: str, max_depth: int, per_source: int,
) -> tuple[list[tuple[str, int, str]], bool]:
    """PURE (no DB) reachable-raise search from one source `src`.

    BFS the call adjacency up to `max_depth` call hops; at every function
    reached that directly raises an exception E, record the derived
    `(exception, hops, proof_path)`.

    - Cycle-safe: a function is visited at most once, so A<->B terminates.
    - Deduped per exception: the FIRST reach wins (BFS => shortest chain).
    - Depth-0 skipped: `src`'s own direct raises are already `raises` facts.
    - Fan-out bounded: at most `per_source` exceptions are emitted.

    Returns `(results, truncated)`; `truncated` is True when the per_source
    cap stopped emission with reachable work pending — the caller LOGS it.
    """
    results: list[tuple[str, int, str]] = []
    raised: set[str] = set()
    seen: set[str] = {src}
    frontier: list[tuple[str, int, str]] = [(src, 0, src)]
    truncated = False
    while frontier and len(results) < per_source:
        node, depth, path = frontier.pop(0)
        if depth >= max_depth:
            continue
        for callee in adj.get(node, ()):
            if callee in seen:
                continue
            seen.add(callee)
            hops = depth + 1
            npath = f"{path} -> {callee}"
            for exc in sorted(raises.get(callee, ())):
                if exc in raised:
                    continue
                if len(results) >= per_source:
                    truncated = True
                    break
                raised.add(exc)
                results.append((exc, hops, f"{npath} raises {exc}"))
            frontier.append((callee, hops, npath))
    # Stopped at the ceiling with reachable callees still unexpanded: honest-
    # safe — never claim full coverage when we stopped early.
    if len(results) >= per_source and frontier:
        truncated = True
    return results, truncated


def derive_may_raise(
    conn, *, max_depth: int = MAYRAISE_MAX_DEPTH,
    per_source: int = MAYRAISE_PER_SOURCE, cap: int = MAYRAISE_CAP,
) -> list[ProposedFact]:
    """Walk each caller with the cycle-safe, depth- and fan-out-bounded
    `_reachable_raises` over live `calls`/`raises` facts."""
    adj: dict[str, list[str]] = {}
    repo_of: dict[str, str | None] = {}
    for caller, callee, repo in _live_edges(conn, "calls"):
        adj.setdefault(caller, []).append(callee)
        repo_of.setdefault(caller, repo)
    raises: dict[str, set[str]] = {}
    for func, exc, _repo in _live_edges(conn, "raises"):
        raises.setdefault(func, set()).add(exc)

    out: list[ProposedFact] = []
    truncated_sources = 0
    for src in adj:
        results, truncated = _reachable_raises(adj, raises, src, max_depth, per_source)
        if truncated:
            truncated_sources += 1
        for exc, hops, proof in results:
            out.append(ProposedFact(
                subject_kind="symbol", subject_name=src, subject_repo=repo_of.get(src),
                predicate="may-raise", object_kind="symbol", object_name=exc,
                object_lit=None, confidence=round(MAYRAISE_DECAY ** hops, 4),
                extractor="der:may-raise@1", origin="derived",
                excerpt=f"exception flow ({hops}-hop call path): {proof}"))
            if len(out) >= cap:
                logger.warning("derive_may_raise hit total cap %d — "
                               "remaining sources dropped", cap)
                return out
    if truncated_sources:
        logger.info("derive_may_raise: %d sources truncated at per_source=%d",
                    truncated_sources, per_source)
    return out
