"""Semantic sidecar tests against a deterministic in-thread embeddings
endpoint: bag-of-words hashed into 64 dims, so cosine reflects real
vocabulary overlap between the query and each symbol's source window."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from groundgraph.assist import graph_assist_ex
from groundgraph.semantic import index_entities, propose

from .test_assist import _built_db

DIM = 64


def _bow(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in re.findall(r"[a-z_]{3,}", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM
        v[h] += 1.0
    return v


class _Embed(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = [{"index": i, "embedding": _bow(t)}
                for i, t in enumerate(body["input"])]
        payload = json.dumps({"data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _server():
    srv = HTTPServer(("127.0.0.1", 0), _Embed)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1"


def _indexed(tmp_path: Path) -> tuple[str, str, HTTPServer, Path]:
    db = _built_db(tmp_path)                      # demo repo: app/theme.py
    srv, url = _server()
    counts = index_entities(db, tmp_path / "demo", endpoint=url, model="bow")
    assert counts["embedded"] >= 2                # score_color, render_row (+lesson)
    return db, url, srv, tmp_path / "demo"


def test_index_is_idempotent(tmp_path: Path) -> None:
    db, url, srv, _root = _indexed(tmp_path)
    try:
        again = index_entities(db, tmp_path / "demo", endpoint=url, model="bow")
        assert again["embedded"] == 0 and again["unchanged"] >= 2
    finally:
        srv.shutdown()


def test_propose_ranks_by_window_vocabulary(tmp_path: Path) -> None:
    db, url, srv, _root = _indexed(tmp_path)
    try:
        # "yellow" appears only inside score_color's BODY — not in any symbol name
        hits = propose("the widget turns yellow sometimes", db,
                       endpoint=url, model="bow", top_k=2)
    finally:
        srv.shutdown()
    assert hits and hits[0][0] == "score_color"
    assert hits[0][1] == "app/theme.py" and hits[0][2] == 1   # path:line resolved


def test_hybrid_grounds_task_with_no_name_vocabulary(tmp_path: Path) -> None:
    """THE complementarity claim: lexical grounding fails (no shared words
    with symbol names), the semantic proposer grounds it, the graph renders
    the same verified neighbourhood block."""
    db, url, srv, _root = _indexed(tmp_path)
    task = "the widget turns yellow sometimes"
    try:
        out, rep = graph_assist_ex(task, db, semantic_endpoint=url,
                                   semantic_model="bow")
    finally:
        srv.shutdown()
    assert rep["injected"] is True
    assert rep["lexical_hits"] == 0 and rep["semantic_hits"] >= 1
    assert "score_color" in out and "app/theme.py:1" in out
    # and without the endpoint, the same task is a clean lexical no-op
    out2, rep2 = graph_assist_ex(task, db)
    assert out2 == task and rep2["injected"] is False


def test_hybrid_endpoint_down_degrades_to_lexical(tmp_path: Path) -> None:
    db, _url, srv, _root = _indexed(tmp_path)
    srv.shutdown()
    # a lexical-groundable task still injects, with semantic_hits=0, no crash
    out, rep = graph_assist_ex("adjust the score color bands in the theme", db,
                               semantic_endpoint="http://127.0.0.1:9/v1")
    assert rep["injected"] is True and rep["semantic_hits"] == 0
    assert rep["lexical_hits"] >= 1 and "score_color" in out


def test_workspace_check_applies_to_semantic_proposals(tmp_path: Path) -> None:
    db, url, srv, _root = _indexed(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    try:
        out, rep = graph_assist_ex("the widget turns yellow sometimes", db,
                                   semantic_endpoint=url, semantic_model="bow",
                                   workspace=empty)
    finally:
        srv.shutdown()
    # the proposal's path does not exist in the workspace -> dropped -> no-op
    assert rep["injected"] is False and out == "the widget turns yellow sometimes"
