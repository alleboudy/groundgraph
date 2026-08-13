"""MCP server tests: the handler logic directly, plus a REAL subprocess
speaking newline-delimited JSON-RPC over stdio (what an MCP client does)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from groundgraph.mcp import handle_request

from .test_assist import _built_db  # the real built fixture graph


def test_initialize_tools_list_and_unknown_method(tmp_path: Path) -> None:
    db = _built_db(tmp_path)
    init = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18"}}, db)
    assert init["result"]["serverInfo"]["name"] == "groundgraph"
    assert init["result"]["protocolVersion"] == "2025-06-18"   # echoes client
    tools = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, db)
    names = {t["name"] for t in tools["result"]["tools"]}
    assert names == {"query_facts", "explain_entity", "assist"}
    err = handle_request({"jsonrpc": "2.0", "id": 3, "method": "nope/nope"}, db)
    assert err["error"]["code"] == -32601
    # notifications produce no response
    assert handle_request({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}, db) is None


def test_tools_call_query_and_assist(tmp_path: Path) -> None:
    db = _built_db(tmp_path)
    q = handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "query_facts",
                                   "arguments": {"predicate": "called-by",
                                                 "subject": "score_color"}}}, db)
    assert not q["result"]["isError"]
    assert "render_row" in q["result"]["content"][0]["text"]
    a = handle_request({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                        "params": {"name": "assist",
                                   "arguments": {"task": "adjust the score "
                                                 "color bands in the theme"}}}, db)
    body = json.loads(a["result"]["content"][0]["text"])
    assert body["injected"] is True and body["grounded_symbols"] >= 1
    bad = handle_request({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                          "params": {"name": "nonesuch", "arguments": {}}}, db)
    assert bad["result"]["isError"] is True    # tool error, not protocol error


def test_stdio_subprocess_end_to_end(tmp_path: Path) -> None:
    """Spawn the real server and speak the wire protocol."""
    db = _built_db(tmp_path)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "explain_entity",
                    "arguments": {"entity": "score_color"}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "groundgraph", "mcp", "--db", db],
        input="\n".join(json.dumps(m) for m in msgs) + "\n",
        capture_output=True, text=True, timeout=30, check=True,
    )
    responses = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
    by_id = {r["id"]: r for r in responses}
    assert by_id[1]["result"]["serverInfo"]["name"] == "groundgraph"
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == \
        {"query_facts", "explain_entity", "assist"}
    assert "app/theme.py" in by_id[3]["result"]["content"][0]["text"]
    assert len(responses) == 3           # the notification got no response
