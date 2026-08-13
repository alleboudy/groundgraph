"""Plain-SQLite write handle for the graph store.

Rules:
* one DB file; never split.
* PRAGMA journal_mode=WAL and PRAGMA foreign_keys=ON on every connection.
* writes run inside a single transaction so partial rows never escape.
* supersession is SOFT (valid_to + superseded_by), never DELETE.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


class GraphStore:
    """Context-managed handle to the graph store."""

    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self.conn = conn
        self.db_path = db_path
        self._in_batch = False

    @classmethod
    @contextmanager
    def open(cls, db_path: Path | str) -> Iterator[GraphStore]:
        """Open (or create) the store, applying the schema on a fresh file."""
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not db_path.exists()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            if is_new:
                conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
                conn.commit()
            yield cls(conn=conn, db_path=db_path)
            conn.commit()
        finally:
            conn.close()

    # --- transactions ---------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """BEGIN/COMMIT one block; no-op when inside an outer batch()."""
        if self._in_batch:
            yield
            return
        self.conn.execute("BEGIN")
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Run many inserts inside one outer transaction (build fast-path)."""
        if self._in_batch:
            raise RuntimeError("GraphStore.batch() is not reentrant")
        self.conn.execute("BEGIN")
        self._in_batch = True
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        finally:
            self._in_batch = False

    # --- writes ---------------------------------------------------------

    def get_or_create_entity(
        self, *, kind: str, name: str, repo: str | None = None,
        path: str | None = None, meta: str | None = None,
        first_seen: str, last_seen: str,
    ) -> int:
        """Return the entity_id for (kind, name, repo), inserting if absent.

        NULL-safe: UNIQUE(kind, name, repo) does not dedup rows with repo IS
        NULL (SQLite treats NULLs as distinct), so SELECT with `repo IS ?`
        before inserting. A NULL path is backfilled when a later fact supplies
        one; a populated path is never clobbered.
        """
        row = self.conn.execute(
            "SELECT entity_id, path FROM entities WHERE kind = ? AND name = ? AND repo IS ?",
            (kind, name, repo),
        ).fetchone()
        if row is not None:
            entity_id = int(row[0])
            if row[1] is None and path is not None:
                with self._transaction():
                    self.conn.execute(
                        "UPDATE entities SET path = ?, last_seen = ? WHERE entity_id = ?",
                        (path, last_seen, entity_id),
                    )
            return entity_id
        with self._transaction():
            cur = self.conn.execute(
                "INSERT INTO entities (kind, name, repo, path, meta, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, name, repo, path, meta, first_seen, last_seen),
            )
            return int(cur.lastrowid)

    def insert_fact(
        self, *, subject_id: int, predicate: str,
        object_id: int | None = None, object_lit: str | None = None,
        confidence: float, extractor: str, created_at: str,
        valid_from: str, valid_to: str | None = None,
    ) -> int:
        """Idempotent triple insert; returns fact_id.

        Raises ValueError unless exactly one of object_id / object_lit is set
        (the Python-side mirror of the schema CHECK). NULL-safe dedup on the
        full key, since UNIQUE(...) ignores NULL object columns.
        """
        if (object_id is None) == (object_lit is None):
            raise ValueError("exactly one of object_id / object_lit must be set")
        row = self.conn.execute(
            "SELECT fact_id FROM facts WHERE subject_id = ? AND predicate = ? "
            "AND object_id IS ? AND object_lit IS ? AND valid_from = ?",
            (subject_id, predicate, object_id, object_lit, valid_from),
        ).fetchone()
        if row is not None:
            return int(row[0])
        with self._transaction():
            cur = self.conn.execute(
                "INSERT INTO facts (subject_id, predicate, object_id, object_lit, "
                "confidence, extractor, created_at, valid_from, valid_to) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (subject_id, predicate, object_id, object_lit,
                 confidence, extractor, created_at, valid_from, valid_to),
            )
            return int(cur.lastrowid)

    def insert_provenance(
        self, *, fact_id: int, source_kind: str, source_ref: str,
        excerpt: str | None = None, created_at: str,
    ) -> int:
        """Insert one evidence row; idempotent on (fact_id, source_kind,
        source_ref) so repeated builds don't pile up duplicates."""
        with self._transaction():
            existing = self.conn.execute(
                "SELECT prov_id FROM provenance "
                "WHERE fact_id = ? AND source_kind = ? AND source_ref = ? LIMIT 1",
                (fact_id, source_kind, source_ref),
            ).fetchone()
            if existing is not None:
                return int(existing[0])
            cur = self.conn.execute(
                "INSERT INTO provenance (fact_id, source_kind, source_ref, excerpt, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fact_id, source_kind, source_ref, excerpt, created_at),
            )
            return int(cur.lastrowid)

    # --- consolidation helpers -----------------------------------------

    def find_current_fact(
        self, *, subject_id: int, predicate: str,
        object_id: int | None = None, object_lit: str | None = None,
    ) -> int | None:
        """The currently-valid fact_id for an exact triple, or None.
        NULL-safe on the object columns; soft-superseded rows excluded."""
        row = self.conn.execute(
            "SELECT fact_id FROM facts WHERE subject_id = ? AND predicate = ? "
            "AND object_id IS ? AND object_lit IS ? AND valid_to IS NULL "
            "ORDER BY fact_id LIMIT 1",
            (subject_id, predicate, object_id, object_lit),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def find_current_facts_by_subject_pred(
        self, *, subject_id: int, predicate: str,
    ) -> list[tuple[int, int | None, str | None, float, str]]:
        """All current facts in a (subject, predicate) slot."""
        rows = self.conn.execute(
            "SELECT fact_id, object_id, object_lit, confidence, extractor FROM facts "
            "WHERE subject_id = ? AND predicate = ? AND valid_to IS NULL ORDER BY fact_id",
            (subject_id, predicate),
        ).fetchall()
        return [(int(r[0]), r[1], r[2], float(r[3]), r[4]) for r in rows]

    def supersede_fact(
        self, fact_id: int, *, valid_to: str, superseded_by: int | None = None,
    ) -> None:
        """SOFT close a fact: set valid_to (+ optional superseded_by). NEVER
        deletes — the old row survives for time-travel + audit.
        superseded_by=None is the archive/prune case (no winner)."""
        with self._transaction():
            self.conn.execute(
                "UPDATE facts SET valid_to = ?, superseded_by = ? WHERE fact_id = ?",
                (valid_to, superseded_by, fact_id),
            )

    def bump_confidence(self, fact_id: int, confidence: float) -> None:
        """Raise a fact's confidence (corroboration from multiple sources)."""
        with self._transaction():
            self.conn.execute(
                "UPDATE facts SET confidence = ? WHERE fact_id = ?", (confidence, fact_id)
            )

    def reopen_fact(self, fact_id: int) -> None:
        """Clear valid_to + superseded_by, making a soft-closed fact current
        again. insert_fact dedups on (subject, predicate, object, valid_from),
        so when a value flip-flops (A->B->A) within one run, re-asserting A
        returns the row soft-closed earlier in the same run; reopening it keeps
        the slot from ending with zero current rows. Idempotent."""
        with self._transaction():
            self.conn.execute(
                "UPDATE facts SET valid_to = NULL, superseded_by = NULL WHERE fact_id = ?",
                (fact_id,),
            )
