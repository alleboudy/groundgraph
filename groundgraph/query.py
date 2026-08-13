"""Read-only fact query over the graph store.

The single read path for agents and tools. Holds a borrowed sqlite
connection, never writes. Canonical questions:
  "what calls X"     -> query_facts(predicate="calls", object="X")
  "what covers X"    -> query_facts(predicate="tests", object="X")
  "what is X"        -> explain_entity("X")
"""
from __future__ import annotations

import logging
import sqlite3

from groundgraph.types import EntityDossier, EntityRow, FactRow

logger = logging.getLogger(__name__)


class FactQuery:
    """Read-only structured query over the fact/entity/provenance graph."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def query_facts(
        self, *, subject: str | None = None, predicate: str | None = None,
        object: str | None = None, kind: str | None = None,  # noqa: A002 — tool arg name
        repo: str | None = None, min_confidence: float = 0.5,
        as_of: str | None = None, limit: int = 50,
        subject_id: int | None = None, object_id: int | None = None,
    ) -> list[FactRow]:
        where = ["f.confidence >= ?"]
        params: list[object] = [min_confidence]
        if as_of is None:
            where.append("f.valid_to IS NULL")  # current facts only
        else:
            where.append("f.valid_from <= ? AND (f.valid_to IS NULL OR f.valid_to > ?)")
            params.extend([as_of, as_of])
        if predicate is not None:
            where.append("f.predicate = ?")
            params.append(predicate)
        if subject is not None:
            where.append("subj.name = ?")
            params.append(subject)
        if object is not None:
            # Match an entity object OR a literal object, so literal-object
            # facts are queryable by object= too.
            where.append("(obj.name = ? OR f.object_lit = ?)")
            params.extend([object, object])
        if kind is not None:
            where.append("(subj.kind = ? OR obj.kind = ?)")
            params.extend([kind, kind])
        if repo is not None:
            where.append("subj.repo = ?")
            params.append(repo)
        if subject_id is not None:
            where.append("f.subject_id = ?")
            params.append(subject_id)
        if object_id is not None:
            where.append("f.object_id = ?")
            params.append(object_id)
        sql = (
            "SELECT f.fact_id, subj.kind, subj.name, f.predicate, "
            "obj.kind, obj.name, f.object_lit, f.confidence, f.extractor, f.valid_from "
            "FROM facts f "
            "JOIN entities subj ON subj.entity_id = f.subject_id "
            "LEFT JOIN entities obj ON obj.entity_id = f.object_id "
            f"WHERE {' AND '.join(where)} "  # noqa: S608 — clauses module-controlled; values are params
            "ORDER BY f.confidence DESC, f.valid_from DESC LIMIT ?"
        )
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        out: list[FactRow] = []
        for fid, sk, sn, pred, ok, on, olit, conf, extractor, valid_from in rows:
            prov = self.conn.execute(
                "SELECT source_ref FROM provenance WHERE fact_id = ? ORDER BY prov_id",
                (fid,),
            ).fetchall()
            out.append(
                FactRow(
                    subject_kind=sk, subject_name=sn, predicate=pred,
                    object_kind=ok, object_name=on, object_lit=olit,
                    confidence=conf, extractor=extractor, valid_from=valid_from,
                    source_refs=tuple(p[0] for p in prov),
                )
            )
        return out

    def explain_entity(
        self, entity: str, *, min_confidence: float = 0.5, limit: int = 200,
        repo: str | None = None,
    ) -> EntityDossier:
        """Depth-1 neighbourhood dossier for one entity: its header plus the
        facts where it is the subject (outgoing) and the object (incoming).
        Pure deterministic graph traversal — no inference, no generation.
        `repo` prefers the same-named entity in that repo (two same-named
        entities in different repos must not swap dossiers); falls back to
        the lowest entity_id when the repo has no match."""
        row = None
        if repo is not None:
            row = self.conn.execute(
                "SELECT entity_id, kind, name, repo, path FROM entities "
                "WHERE name = ? AND repo = ? ORDER BY entity_id LIMIT 1",
                (entity, repo),
            ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT entity_id, kind, name, repo, path FROM entities WHERE name = ? "
                "ORDER BY entity_id LIMIT 1",
                (entity,),
            ).fetchone()
        if row is None:
            return EntityDossier(entity=None, outgoing=[], incoming=[])
        ent = EntityRow(entity_id=int(row[0]), kind=row[1], name=row[2],
                        repo=row[3], path=row[4])
        # Traverse by the RESOLVED entity_id, not the name — two same-named
        # entities (different repo) must not merge dossiers.
        outgoing = self.query_facts(
            subject_id=ent.entity_id, min_confidence=min_confidence, limit=limit
        )
        incoming = self.query_facts(
            object_id=ent.entity_id, min_confidence=min_confidence, limit=limit
        )
        return EntityDossier(entity=ent, outgoing=outgoing, incoming=incoming)
