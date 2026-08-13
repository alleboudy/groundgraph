"""Anti-rot instrumentation for the graph.

A graph that grows in COUNT while rotting in PRECISION is a regression. Two
pure primitives make rot measurable and reversible-by-default:

- `effective_confidence`: inferred (non det:/der:) facts DECAY with age
  unless re-corroborated; deterministic and derived facts do not (they are
  rebuilt/re-derived on every build). Applied at QUERY time — nothing is
  destroyed; a stale fact just falls below the recall floor until reaffirmed.
- `graph_health`: the precision/staleness dashboard — tier ratio, duplicate/
  orphan/contradiction counts, decay-dormant count — with warn flags so
  precision regression is caught, not discovered.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

DECAY_HALF_LIFE_DAYS = 60.0
RECALL_FLOOR = 0.5
# A "contradiction" only means something where a subject should have ONE
# value. Multi-valued relations (calls/imports/raises/cites/lesson...)
# legitimately have many objects per subject and must NOT be flagged — in a
# real deployment a naive detector raised 12,000+ false alarms on them.
# Keep this set conservative.
SINGLE_VALUED_PREDICATES = frozenset({
    "decided", "prefers", "corrected", "supersedes",
    "convention-applies-to", "defined-in",
})


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def effective_confidence(base: float, created_at: str | None, now: str,
                         extractor: str | None, *,
                         half_life_days: float = DECAY_HALF_LIFE_DAYS) -> float:
    """PURE: age-decayed confidence. Deterministic (det:) and derived (der:)
    facts do NOT decay — they are re-verified on every build. Anything else
    halves its confidence every `half_life_days` of age. Missing timestamps
    return `base` unchanged."""
    if not extractor or extractor.startswith(("det:", "der:")):
        return base
    c = _parse_ts(created_at)
    n = _parse_ts(now)
    if c is None or n is None:
        return base
    age_days = max(0.0, (n - c).total_seconds() / 86400.0)
    return round(base * (0.5 ** (age_days / half_life_days)), 4)


def graph_health(conn: sqlite3.Connection, *, now: str,
                 recall_floor: float = RECALL_FLOOR) -> dict:
    """The status dashboard. Reads only; returns a metrics dict with a
    `warnings` list. Live facts = valid_to IS NULL."""
    conn.row_factory = sqlite3.Row
    live = "valid_to IS NULL"
    total = conn.execute(f"SELECT COUNT(*) FROM facts WHERE {live}").fetchone()[0]
    superseded = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE valid_to IS NOT NULL").fetchone()[0]
    det = conn.execute(
        f"SELECT COUNT(*) FROM facts WHERE {live} AND extractor LIKE 'det:%'").fetchone()[0]
    der = conn.execute(
        f"SELECT COUNT(*) FROM facts WHERE {live} AND extractor LIKE 'der:%'").fetchone()[0]
    # "inferred" = the RISKY tier only, the one that decays and needs a
    # precision guard. det: (ground truth) and der: (grounded re-derivable)
    # are excluded from the precision-risk math.
    inferred = conn.execute(
        f"SELECT COUNT(*) FROM facts WHERE {live} "
        "AND extractor NOT LIKE 'det:%' AND extractor NOT LIKE 'der:%'").fetchone()[0]
    ages = conn.execute(
        f"SELECT MIN(created_at), MAX(created_at) FROM facts WHERE {live}").fetchone()
    sv = ",".join("?" * len(SINGLE_VALUED_PREDICATES))
    contradictions = conn.execute(f"""
        SELECT COUNT(*) FROM (
          SELECT subject_id, predicate
          FROM facts WHERE {live} AND predicate IN ({sv})
          GROUP BY subject_id, predicate
          HAVING COUNT(DISTINCT COALESCE(object_id, -1) || '|' || COALESCE(object_lit,'')) > 1
        )""", tuple(SINGLE_VALUED_PREDICATES)).fetchone()[0]
    dups = conn.execute(f"""
        SELECT COALESCE(SUM(c - 1), 0) FROM (
          SELECT COUNT(*) c FROM facts WHERE {live}
          GROUP BY subject_id, predicate, COALESCE(object_id,-1), COALESCE(object_lit,'')
          HAVING c > 1
        )""").fetchone()[0]
    orphans = conn.execute(f"""
        SELECT COUNT(*) FROM facts f JOIN entities e ON f.subject_id = e.entity_id
        WHERE f.{live} AND (e.name IS NULL OR TRIM(e.name) = '')""").fetchone()[0]
    dormant = 0
    for r in conn.execute(
            f"SELECT confidence, created_at, extractor FROM facts WHERE {live} "
            "AND extractor NOT LIKE 'det:%' AND extractor NOT LIKE 'der:%'"):
        if effective_confidence(r[0], r[1], now, r[2]) < recall_floor:
            dormant += 1

    warnings: list[str] = []
    if total and inferred / total > 0.4:
        warnings.append(f"risky-inferred:total {inferred}/{total} > 0.4 — precision risk")
    if total and dups / max(total, 1) > 0.05:
        warnings.append(f"{dups} duplicate live facts (>5%) — consolidation lagging")
    if orphans:
        warnings.append(f"{orphans} orphan/empty-subject facts — junk nodes")
    if contradictions:
        warnings.append(f"{contradictions} contradiction candidates — review/supersede")
    if inferred and dormant / max(inferred, 1) > 0.3:
        warnings.append(f"{dormant}/{inferred} inferred facts decay-dormant (>30%) — "
                        "stale, re-corroborate or rebuild")

    return {
        "live_facts": total, "superseded": superseded,
        "deterministic": det, "derived": der, "inferred": inferred,
        "oldest": ages[0], "newest": ages[1],
        "duplicates": dups, "orphans": orphans,
        "contradiction_candidates": contradictions,
        "decay_dormant_inferred": dormant,
        "warnings": warnings,
    }
