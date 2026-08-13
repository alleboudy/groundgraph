"""Consolidation — the supersession write path for ProposedFacts.

Folds candidate facts into the graph: dedup identical triples (merge
provenance, raise confidence), conflict-resolve single-valued slots (newer +
at-least-as-well-supported wins -> SOFT supersede), and prune facts whose
source file no longer exists. Two hard invariants:

  * supersession is SOFT (valid_to + superseded_by), never DELETE;
  * a deterministic (det:*) fact is never retired by a lower-tier fact —
    the ground-truth backbone always outranks derived/inferred facts in the
    same slot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from groundgraph.store import GraphStore
from groundgraph.types import ProposedFact

logger = logging.getLogger(__name__)

# Predicates whose (subject, predicate) slot holds at most one current value.
SINGLE_VALUED: frozenset[str] = frozenset({"supersedes"})


@dataclass(frozen=True)
class ConsolidationStats:
    added: int = 0
    superseded: int = 0
    merged: int = 0


def _corroborated(old_conf: float, cand_conf: float) -> float:
    """Raise confidence on corroboration but never reach 1.0 (non-deterministic
    facts stay below the deterministic floor)."""
    base = max(old_conf, cand_conf)
    return round(min(0.99, base + (1.0 - base) * 0.3), 4)


def _source_kind(origin: str) -> str:
    """Map a fact's origin to its provenance source_kind."""
    if origin.startswith("doc:"):
        return "doc"
    if origin.startswith("git:"):
        return "commit"
    if origin.startswith("code:"):
        return "code_line"
    return "derived"


def _resolve_object(store: GraphStore, c: ProposedFact, now: str) -> tuple[int | None, str | None]:
    if c.object_lit is not None:
        return None, c.object_lit
    obj_repo = c.subject_repo if c.object_kind not in (None, "module") else None
    obj_path = c.object_name if c.object_kind == "file" else None
    obj_id = store.get_or_create_entity(
        kind=c.object_kind or "module", name=c.object_name or "",
        repo=obj_repo, path=obj_path, first_seen=now, last_seen=now,
    )
    return obj_id, None


def _insert(
    store: GraphStore, c: ProposedFact, subj: int,
    obj_id: int | None, obj_lit: str | None, now: str,
) -> int:
    fid = store.insert_fact(
        subject_id=subj, predicate=c.predicate, object_id=obj_id, object_lit=obj_lit,
        confidence=c.confidence, extractor=c.extractor, created_at=now, valid_from=now,
    )
    # A flip-flop (A->B->A) within one run re-asserts A; insert_fact dedups on
    # valid_from (= this run's `now`) and returns the row soft-closed when B
    # superseded A. Reopen it so the re-asserted value is current.
    store.reopen_fact(fid)
    store.insert_provenance(
        fact_id=fid, source_kind=_source_kind(c.origin),
        source_ref=c.origin, excerpt=c.excerpt, created_at=now,
    )
    return fid


def consolidate(
    store: GraphStore, candidates: list[ProposedFact], *,
    now: str, single_valued: frozenset[str] = SINGLE_VALUED,
) -> ConsolidationStats:
    added = superseded = merged = 0
    with store.batch():
        for c in candidates:
            subj = store.get_or_create_entity(
                kind=c.subject_kind, name=c.subject_name, repo=c.subject_repo,
                first_seen=now, last_seen=now,
            )
            obj_id, obj_lit = _resolve_object(store, c, now)

            existing = store.find_current_fact(
                subject_id=subj, predicate=c.predicate, object_id=obj_id, object_lit=obj_lit,
            )
            if existing is not None:
                # Identical triple -> provenance-merge + confidence bump.
                old_conf, old_extractor = store.conn.execute(
                    "SELECT confidence, extractor FROM facts WHERE fact_id = ?", (existing,)
                ).fetchone()
                store.insert_provenance(
                    fact_id=existing, source_kind=_source_kind(c.origin),
                    source_ref=c.origin, excerpt=c.excerpt, created_at=now,
                )
                # Never lower a deterministic fact via the 0.99 corroboration cap.
                if not old_extractor.startswith("det:"):
                    store.bump_confidence(existing, _corroborated(old_conf, c.confidence))
                merged += 1
                continue

            # Conflict resolution for single-valued slots.
            if c.predicate in single_valued:
                conflict = None
                conflict_conf = 0.0
                det_blocked = False
                for (
                    fid, o_id, o_lit, existing_conf, extractor
                ) in store.find_current_facts_by_subject_pred(
                    subject_id=subj, predicate=c.predicate,
                ):
                    if (o_id, o_lit) != (obj_id, obj_lit):
                        if extractor.startswith("det:"):
                            # Deterministic backbone outranks; do not retire it.
                            logger.info("keep det fact %d over non-det candidate", fid)
                            det_blocked = True
                            continue
                        conflict = fid
                        conflict_conf = existing_conf
                        break
                if conflict is not None:
                    if c.confidence >= conflict_conf:
                        new_id = _insert(store, c, subj, obj_id, obj_lit, now)
                        store.supersede_fact(conflict, valid_to=now, superseded_by=new_id)
                        superseded += 1
                        added += 1
                    else:
                        logger.info(
                            "keep fact %d (conf %.2f) over weaker candidate (conf %.2f)",
                            conflict, conflict_conf, c.confidence,
                        )
                    continue
                if det_blocked:
                    continue

            _insert(store, c, subj, obj_id, obj_lit, now)
            added += 1
    return ConsolidationStats(added=added, superseded=superseded, merged=merged)


def prune_stale_facts(
    store: GraphStore, *, existing_paths_by_repo: dict[str | None, set[str]], now: str,
) -> int:
    """Soft-archive current facts whose code_line source file no longer exists
    IN THE FACT'S OWN REPO.

    Repo-aware: the fact's own subject repo picks which repo's path set to
    check — a file deleted in repo A but present at the same rel-path in repo B
    must not shield A's stale fact. Conservative: a fact whose repo is unknown
    or unscanned is KEPT (archiving a still-valid fact is the bad direction).
    Archive == set valid_to (no winner). Never DELETE."""
    rows = store.conn.execute(
        "SELECT DISTINCT f.fact_id, subj.repo, p.source_ref FROM facts f "
        "JOIN provenance p ON p.fact_id = f.fact_id "
        "JOIN entities subj ON subj.entity_id = f.subject_id "
        "WHERE f.valid_to IS NULL AND p.source_kind = 'code_line'"
    ).fetchall()
    pruned = 0
    for fact_id, repo, source_ref in rows:
        if repo not in existing_paths_by_repo:
            continue
        path = source_ref.rsplit(":", 1)[0]  # strip ":line"
        if path not in existing_paths_by_repo[repo]:
            store.supersede_fact(int(fact_id), valid_to=now)
            pruned += 1
    logger.info("pruned %d stale facts", pruned)
    return pruned
