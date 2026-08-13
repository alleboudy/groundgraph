"""Transitive module dependencies: `depends-on` closure at the module level.

`transitively-depends-on` facts (depth>=2 only; the 1-hop base already lives
in the graph). Reuses the cycle-safe, depth-bounded closure from derive.py.
Runs AFTER derive_module_depends' facts are consolidated: with no module
`depends-on` facts yet it returns [] cleanly.
"""
from __future__ import annotations

import logging

from groundgraph.derive import _closure
from groundgraph.types import ProposedFact

logger = logging.getLogger(__name__)

TDEPENDS_DECAY = 0.9   # per-hop confidence decay


def _live_module_depends(conn) -> list[tuple[str, str, str | None]]:
    """(subject, object, repo) for every LIVE MODULE-level `depends-on` fact.
    Both endpoints must be kind='module'; empty names are skipped (they would
    seed junk closure nodes)."""
    conn.row_factory = None
    return list(conn.execute(
        """SELECT s.name, o.name, s.repo
           FROM facts f
           JOIN entities s ON f.subject_id = s.entity_id
           JOIN entities o ON f.object_id  = o.entity_id
           WHERE f.valid_to IS NULL AND f.predicate = 'depends-on'
             AND s.kind = 'module' AND o.kind = 'module'
             AND s.name <> '' AND o.name <> ''"""))


def derive_transitive_module_depends(
    conn, *, max_depth: int = 5, cap: int = 60_000,
) -> list[ProposedFact]:
    """Bounded transitive closure of module `depends-on`. Each fact carries
    its hop count as proof; confidence decays per hop. Truncation is LOGGED."""
    edges = _live_module_depends(conn)
    if not edges:
        return []
    repo_of = {a: r for a, _b, r in edges}
    pairs = [(a, b) for a, b, _r in edges]
    out: list[ProposedFact] = []
    for desc, anc, depth in _closure(pairs, max_depth=max_depth):
        out.append(ProposedFact(
            subject_kind="module", subject_name=desc, subject_repo=repo_of.get(desc),
            predicate="transitively-depends-on", object_kind="module", object_name=anc,
            object_lit=None, confidence=round(TDEPENDS_DECAY ** depth, 4),
            extractor="der:tdepends@1", origin="derived",
            excerpt=f"transitive depends ({depth} hops): {desc} -> {anc}"))
        if len(out) >= cap:
            logger.warning("derive_transitive_module_depends hit cap %d — "
                           "remaining transitive pairs dropped", cap)
            return out
    return out
