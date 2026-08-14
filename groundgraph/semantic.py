"""Optional semantic sidecar: entity-keyed embeddings from a LOCAL endpoint.

The graph's grounding is lexical — exact and proof-carrying, but blind to a
task that shares no vocabulary with symbol names. This module adds the
complementary proposer: embed a source window around every defined symbol
(and every lesson), and at query time embed the task and rank by cosine.

Design rules:

* **The embedder proposes, the graph verifies.** A semantic hit is only a
  candidate — the assist layer still applies repo scoping, the workspace
  existence-check, and the graph neighbourhood expansion. The neural layer
  never becomes the truth source.
* **Zero required dependencies.** Embeddings come from any OpenAI-compatible
  ``/v1/embeddings`` endpoint (llama.cpp server with an embedding model,
  Ollama, LM Studio — all strictly local options) via urllib. Vectors live
  in the same SQLite file. Cosine is pure Python; ``numpy`` is used only if
  it happens to be installed.
* **Loud fallbacks.** An unreachable endpoint at QUERY time degrades to
  lexical-only grounding (logged, never a crash). At INDEX time it is a
  hard error — indexing is an explicit operator action.
* Idempotent: each vector stores a hash of the exact text embedded; an
  unchanged symbol is skipped on re-index.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import struct
import urllib.error
import urllib.request
from array import array
from pathlib import Path

logger = logging.getLogger(__name__)

try:                      # optional accelerator only — never required
    import numpy as _np
except ImportError:       # pragma: no cover - environment-dependent
    _np = None

WINDOW_BEFORE = 3         # lines of context above the definition line
WINDOW_AFTER = 27         # lines below (a typical function body head)
EMBED_BATCH = 64
_TIMEOUT = 120

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
  entity_id   INTEGER PRIMARY KEY REFERENCES entities(entity_id),
  model       TEXT NOT NULL,
  dim         INTEGER NOT NULL,
  vec         BLOB NOT NULL,     -- little-endian float32, L2-normalized
  src_hash    TEXT NOT NULL,     -- sha256 of the embedded text (idempotency)
  created_at  TEXT NOT NULL
);
"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def embed_texts(texts: list[str], *, endpoint: str, model: str,
                api_key: str = "none") -> list[list[float]]:
    """POST /embeddings for a batch of texts. Raises on failure — callers
    decide whether that is fatal (index time) or a fallback (query time)."""
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/embeddings",
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read())["data"]
    # servers may return out of order — sort by index
    data.sort(key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in data]


def _normalize(vec: list[float]) -> array:
    a = array("f", vec)
    norm = sum(x * x for x in a) ** 0.5 or 1.0
    for i in range(len(a)):
        a[i] /= norm
    return a


def _window_text(root: Path, name: str, path: str, line: int | None) -> str | None:
    f = root / path
    if not f.is_file():
        return None
    try:
        lines = f.read_text(errors="replace").splitlines()
    except OSError:
        return None
    center = (line or 1) - 1
    lo = max(0, center - WINDOW_BEFORE)
    hi = min(len(lines), center + WINDOW_AFTER)
    return f"{name} — {path}\n" + "\n".join(lines[lo:hi])


def index_entities(db_path: str, repo_root: str | Path, *, endpoint: str,
                   model: str, api_key: str = "none", repo: str | None = None,
                   now: str = "") -> dict:
    """Embed a source window for every defined symbol (of `repo`, or the
    root's basename) plus every lesson's text. Returns counts. Idempotent:
    unchanged texts are skipped via src_hash."""
    from groundgraph.assist import defined_symbols  # local import: no cycle

    root = Path(repo_root)
    repo = repo or root.name
    conn = sqlite3.connect(db_path)
    _ensure_table(conn)

    # gather (entity_id, text) pairs needing (re-)embedding
    todo: list[tuple[int, str, str]] = []          # (entity_id, text, hash)
    skipped = 0

    def _existing_hash(entity_id: int) -> str | None:
        row = conn.execute("SELECT src_hash FROM embeddings WHERE entity_id = ?",
                           (entity_id,)).fetchone()
        return row[0] if row else None

    for name, path, line in defined_symbols(conn, repo=repo):
        row = conn.execute(
            "SELECT entity_id FROM entities WHERE kind='symbol' AND name=? "
            "AND repo IS ? AND path=?", (name, repo, path)).fetchone()
        if row is None:
            continue
        text = _window_text(root, name, path, line)
        if text is None:
            continue
        h = hashlib.sha256(text.encode()).hexdigest()
        if _existing_hash(int(row[0])) == h:
            skipped += 1
            continue
        todo.append((int(row[0]), text, h))

    for topic_id, lit in conn.execute(
            "SELECT f.subject_id, f.object_lit FROM facts f "
            "JOIN entities e ON e.entity_id = f.subject_id "
            "WHERE f.valid_to IS NULL AND f.predicate = 'lesson' "
            "AND f.object_lit IS NOT NULL AND e.kind = 'lesson-topic'"):
        text = str(lit)
        h = hashlib.sha256(text.encode()).hexdigest()
        if _existing_hash(int(topic_id)) == h:
            skipped += 1
            continue
        todo.append((int(topic_id), text, h))

    written = 0
    for i in range(0, len(todo), EMBED_BATCH):
        batch = todo[i:i + EMBED_BATCH]
        vecs = embed_texts([t for _id, t, _h in batch],
                           endpoint=endpoint, model=model, api_key=api_key)
        for (entity_id, _text, h), vec in zip(batch, vecs, strict=True):
            a = _normalize(vec)
            conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(entity_id, model, dim, vec, src_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entity_id, model, len(a), a.tobytes(), h, now))
            written += 1
    conn.commit()
    conn.close()
    logger.info("semantic index: %d embedded, %d unchanged", written, skipped)
    return {"embedded": written, "unchanged": skipped}


def propose(task: str, db_path: str, *, endpoint: str, model: str,
            api_key: str = "none", top_k: int = 5, repo: str | None = None,
            ) -> list[tuple[str, str | None, int | None, float]]:
    """Semantic proposals for a task: [(name, path, line, score)], best first.
    Returns [] on ANY failure (endpoint down, no vectors, schema absent) —
    the assist layer then falls back to lexical grounding. Loud, never fatal."""
    try:
        qvec = _normalize(embed_texts([task], endpoint=endpoint, model=model,
                                      api_key=api_key)[0])
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError,
            IndexError) as e:
        logger.warning("semantic: embed endpoint unavailable (%s) — "
                       "lexical-only grounding", e)
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        sql = ("SELECT emb.entity_id, emb.dim, emb.vec, e.name, e.path "
               "FROM embeddings emb JOIN entities e ON e.entity_id = emb.entity_id")
        params: tuple = ()
        if repo is not None:
            sql += " WHERE e.repo IS ?"
            params = (repo,)
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        logger.warning("semantic: no embeddings readable (%s)", e)
        return []
    if not rows:
        return []

    q = _np.frombuffer(qvec.tobytes(), dtype=_np.float32) if _np is not None else qvec
    scored: list[tuple[float, str, str | None]] = []
    for _eid, dim, blob, name, path in rows:
        if dim != len(qvec):
            continue                       # different model/dim — skip honestly
        if _np is not None:
            v = _np.frombuffer(blob, dtype=_np.float32)
            score = float(q @ v)
        else:
            v = array("f")
            v.frombytes(blob)
            score = sum(qa * va for qa, va in zip(qvec, v, strict=False))
        scored.append((score, name, path))
    conn.close()
    scored.sort(key=lambda r: -r[0])

    out: list[tuple[str, str | None, int | None, float]] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    for score, name, path in scored[:top_k]:
        line = None
        row = conn.execute(
            "SELECT p.source_ref FROM provenance p "
            "JOIN facts f ON f.fact_id = p.fact_id "
            "JOIN entities e ON e.entity_id = f.subject_id "
            "WHERE e.name = ? AND f.predicate = 'defined-in' "
            "AND f.valid_to IS NULL LIMIT 1", (name,)).fetchone()
        if row and ":" in row[0] and row[0].rsplit(":", 1)[-1].isdigit():
            line = int(row[0].rsplit(":", 1)[-1])
        out.append((name, path, line, round(score, 4)))
    conn.close()
    return out


def _unpack(blob: bytes) -> array:        # test helper
    a = array("f")
    a.frombytes(blob)
    return a


assert struct.calcsize("f") == 4          # float32 blobs are the contract
