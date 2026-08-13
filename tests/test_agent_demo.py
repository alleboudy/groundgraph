"""Agent-loop example: full round-trip against a canned in-thread
OpenAI-compatible server — the model 'calls' query_facts, the loop executes
it against the real graph, and the final answer lands."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))
import agent_demo  # noqa: E402

from .test_assist import _built_db  # noqa: E402


class _Canned(BaseHTTPRequestHandler):
    hits = 0

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).hits += 1
        if type(self).hits == 1:
            # turn 1: the "model" asks the graph who calls score_color
            msg = {"tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "query_facts",
                "arguments": json.dumps({"predicate": "called-by",
                                         "subject": "score_color"})}}]}
        else:
            # turn 2: it saw the tool result and answers with it
            tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
            assert tool_msgs and "render_row" in tool_msgs[0]["content"]
            msg = {"content": "score_color is called by render_row."}
        payload = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # silence
        pass


def test_agent_loop_round_trip(tmp_path: Path, capsys, monkeypatch) -> None:
    db = _built_db(tmp_path)
    _Canned.hits = 0
    srv = HTTPServer(("127.0.0.1", 0), _Canned)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setenv("GG_BASE_URL", f"http://127.0.0.1:{srv.server_port}/v1")
        rc = agent_demo.main(["who calls score_color?", "--db", db])
    finally:
        srv.shutdown()
    assert rc == 0
    out = capsys.readouterr().out
    assert "query_facts" in out                      # the tool call traced
    assert "render_row" in out                       # the final answer
    assert "graph consulted 1 time(s)" in out        # fired-signal
    assert "grounded answer" in out


def test_agent_loop_endpoint_error_is_clean(tmp_path: Path, capsys, monkeypatch) -> None:
    db = _built_db(tmp_path)
    monkeypatch.setenv("GG_BASE_URL", "http://127.0.0.1:9/v1")  # dead port
    rc = agent_demo.main(["anything", "--db", db])
    assert rc == 1
    assert "endpoint error" in capsys.readouterr().err
