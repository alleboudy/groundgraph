"""Derived facts: expand the graph by REASONING over ground-truth facts, not
by generation. Every derived fact is grounded — it traces to real base facts
via a mechanical rule, carries that derivation as provenance (a proof path in
`excerpt`), and is re-derivable, so it does not time-decay.

Derivations:
- INVERSE relations: `A calls B` -> `B called-by A` (and imports/defined-in/
  raises). The #1 agent query is "who calls/uses this?" — materializing the
  reverse edge makes it a direct lookup.
- TRANSITIVE isa: `A inherits B`, `B inherits C` -> `A isa C`.
- MODULE depends: cross-file symbol edges aggregate into module-level
  `depends-on` — an architectural abstraction the base graph only holds at
  the symbol level.
- REACHES: bounded transitive call reach with proof paths.
- MAY-RAISE (derive_exceptions.py) and TRANSITIVE MODULE DEPENDS
  (derive_tdepends.py) build on these.
"""
from __future__ import annotations

import logging

from groundgraph.types import ProposedFact

logger = logging.getLogger(__name__)

# base predicate -> inverse predicate. Only relations where the reverse is a
# genuinely useful recall direction.
INVERSE_MAP: dict[str, str] = {
    "calls": "called-by",
    "imports": "imported-by",
    "defined-in": "defines",
    "raises": "raised-by",
}
INVERSE_CONF = 0.98   # grounded mechanical inversion (just below the det: floor)
ISA_CONF = 0.95       # transitive but grounded; decays slightly per extra hop

MODULE_DEPEND_PREDS = ("calls", "imports", "depends-on")
REACH_MAX_DEPTH = 2      # only NEW 2-hop reach facts (1-hop = the base calls)
REACH_PER_SOURCE = 10    # fan-out cap per source (a hub function has thousands)
REACH_CAP = 120_000      # total runaway guard; drops are LOGGED, never silent


def _live_edges(conn, predicate: str) -> list[tuple[str, str, str | None]]:
    """(subject_name, object_name, subject_repo) for every LIVE fact of a
    predicate whose object is an entity (not a literal)."""
    conn.row_factory = None
    return list(conn.execute(
        """SELECT subj.name, obj.name, subj.repo
           FROM facts f
           JOIN entities subj ON f.subject_id = subj.entity_id
           JOIN entities obj  ON f.object_id  = obj.entity_id
           WHERE f.valid_to IS NULL AND f.predicate = ? AND f.object_id IS NOT NULL
             AND subj.name <> '' AND obj.name <> ''""", (predicate,)))


def _live_edges_paths(conn, predicate: str) -> list[tuple[str, str, str, str, str | None]]:
    """(subj_name, subj_path, obj_name, obj_path, subj_repo) for live
    entity-object facts — carries the file path so edges can aggregate to
    modules."""
    conn.row_factory = None
    return list(conn.execute(
        """SELECT s.name, COALESCE(s.path,''), o.name, COALESCE(o.path,''), s.repo
           FROM facts f
           JOIN entities s ON f.subject_id = s.entity_id
           JOIN entities o ON f.object_id  = o.entity_id
           WHERE f.valid_to IS NULL AND f.predicate = ? AND f.object_id IS NOT NULL
             AND s.name <> '' AND o.name <> ''""", (predicate,)))


def derive_inverse_facts(conn, *, cap: int = 200_000) -> list[ProposedFact]:
    """Materialize the reverse edge for each INVERSE_MAP relation. Grounded,
    1:1, deterministic. `cap` is a runaway guard, not an expected bound."""
    out: list[ProposedFact] = []
    for base, inv in INVERSE_MAP.items():
        for subj, obj, repo in _live_edges(conn, base):
            out.append(ProposedFact(
                subject_kind="symbol", subject_name=obj, subject_repo=repo,
                predicate=inv, object_kind="symbol", object_name=subj,
                object_lit=None, confidence=INVERSE_CONF,
                extractor="der:inverse@1", origin="derived",
                excerpt=f"inverse of {base}: {subj} {base} {obj}"))
            if len(out) >= cap:
                logger.warning("derive_inverse_facts hit cap %d", cap)
                return out
    return out


def _closure(pairs: list[tuple[str, str]], *, max_depth: int = 8
             ) -> set[tuple[str, str, int]]:
    """Transitive closure of a DAG-ish relation as (descendant, ancestor,
    depth) with the SHORTEST depth. Cycle-safe (a node never revisits itself),
    depth-bounded. Excludes the 1-hop base pairs — those already exist; only
    the NEW transitive facts (depth >= 2) are emitted."""
    parents: dict[str, list[str]] = {}
    for a, b in pairs:
        parents.setdefault(a, []).append(b)
    out: set[tuple[str, str, int]] = set()
    for start in parents:
        seen = {start}
        frontier = [(start, 0)]
        while frontier:
            node, d = frontier.pop()
            if d >= max_depth:
                continue
            for anc in parents.get(node, ()):
                if anc in seen:
                    continue
                seen.add(anc)
                depth = d + 1
                if depth >= 2:                       # skip the base 1-hop edge
                    out.add((start, anc, depth))
                frontier.append((anc, depth))
    return out


def derive_transitive_isa(conn) -> list[ProposedFact]:
    """Class ancestry: transitive closure of `inherits` -> `isa` facts
    (depth>=2). Confidence decays a touch with depth (a great-grandparent is a
    weaker signal than a parent) but stays grounded."""
    edges = _live_edges(conn, "inherits")
    repo_of = {a: r for a, _b, r in edges}
    pairs = [(a, b) for a, b, _r in edges]
    out: list[ProposedFact] = []
    for desc, anc, depth in _closure(pairs):
        out.append(ProposedFact(
            subject_kind="symbol", subject_name=desc, subject_repo=repo_of.get(desc),
            predicate="isa", object_kind="symbol", object_name=anc, object_lit=None,
            confidence=round(ISA_CONF * (0.9 ** (depth - 2)), 4),
            extractor="der:isa@1", origin="derived",
            excerpt=f"transitive inherits ({depth} hops): {desc} isa {anc}"))
    return out


def derive_module_depends(conn) -> list[ProposedFact]:
    """Aggregate cross-FILE symbol edges (calls/imports/depends-on) into
    MODULE-level `depends-on` facts. Grounded (counts real edges), bounded
    (module pairs << symbol pairs). The support count rides in the proof so a
    1-edge link and a 50-edge link are distinguishable."""
    support: dict[tuple[str, str], tuple[int, str | None]] = {}
    for pred in MODULE_DEPEND_PREDS:
        for _sn, sp, _on, op, repo in _live_edges_paths(conn, pred):
            if not sp or not op or sp == op:
                continue                          # same file / unknown path
            key = (sp, op)
            cnt, r = support.get(key, (0, repo))
            support[key] = (cnt + 1, r or repo)
    out: list[ProposedFact] = []
    for (sp, op), (cnt, repo) in support.items():
        out.append(ProposedFact(
            subject_kind="module", subject_name=sp, subject_repo=repo,
            predicate="depends-on", object_kind="module", object_name=op,
            object_lit=None, confidence=0.9, extractor="der:module@1",
            origin="derived",
            excerpt=f"aggregated from {cnt} cross-file symbol edge(s): {sp} -> {op}"))
    return out


def derive_reaches(conn, *, max_depth: int = REACH_MAX_DEPTH,
                   per_source: int = REACH_PER_SOURCE, cap: int = REACH_CAP
                   ) -> list[ProposedFact]:
    """Bounded transitive call REACH: `A calls B calls C` -> `A reaches C`
    (depth>=2 only). BFS per source, cycle-safe, capped both per-source and in
    total. Each fact carries its proof path; confidence decays per hop.
    Truncation is LOGGED (no silent caps)."""
    adj: dict[str, list[str]] = {}
    repo_of: dict[str, str | None] = {}
    for sn, _sp, on, _op, repo in _live_edges_paths(conn, "calls"):
        adj.setdefault(sn, []).append(on)
        repo_of.setdefault(sn, repo)
    out: list[ProposedFact] = []
    truncated_sources = 0
    for src in adj:
        emitted = 0
        seen = {src}
        frontier = [(src, 0, src)]      # (node, depth, path)
        while frontier and emitted < per_source:
            node, d, path = frontier.pop(0)
            if d >= max_depth:
                continue
            for nxt in adj.get(node, ()):
                if nxt in seen:
                    continue
                seen.add(nxt)
                depth = d + 1
                npath = f"{path} -> {nxt}"
                if depth >= 2 and emitted < per_source:
                    out.append(ProposedFact(
                        subject_kind="symbol", subject_name=src,
                        subject_repo=repo_of.get(src),
                        predicate="reaches", object_kind="symbol", object_name=nxt,
                        object_lit=None, confidence=round(0.9 ** depth, 4),
                        extractor="der:reach@1", origin="derived",
                        excerpt=f"call path ({depth} hops): {npath}"))
                    emitted += 1
                    if len(out) >= cap:
                        logger.warning("derive_reaches hit total cap %d — "
                                       "remaining sources dropped", cap)
                        return out
                frontier.append((nxt, depth, npath))
        if emitted >= per_source and len(adj.get(src, [])) > per_source:
            truncated_sources += 1
    if truncated_sources:
        logger.info("derive_reaches: %d sources truncated at per_source=%d",
                    truncated_sources, per_source)
    return out


def derive_all(conn, *, reaches: bool = True, modules: bool = True,
               may_raise: bool = True) -> list[ProposedFact]:
    """Phase-1 derived facts, all readable from the base graph in one pass.
    NOTE: transitive-module-depends is NOT here — it reads the module-depends
    facts this pass PRODUCES, so it must run AFTER these are consolidated
    (see derive_tdepends.derive_transitive_module_depends)."""
    facts = derive_inverse_facts(conn) + derive_transitive_isa(conn)
    if modules:
        facts += derive_module_depends(conn)
    if reaches:
        facts += derive_reaches(conn)
    if may_raise:
        from groundgraph.derive_exceptions import derive_may_raise
        facts += derive_may_raise(conn)
    logger.info("derive_all: %d derived facts", len(facts))
    return facts
