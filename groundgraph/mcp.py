"""MCP (Model Context Protocol) stdio server — stdlib only.

Exposes the graph to any MCP client (Claude Code, IDE agents, etc.) as three
tools: `query_facts`, `explain_entity`, and `assist`. Newline-delimited
JSON-RPC 2.0 over stdio, per the MCP transport spec. Read-only: the server
opens the store `mode=ro` per call and can never write.

Register (Claude Code example):

    claude mcp add groundgraph -- python -m groundgraph mcp --db /path/graph.db

Design rules carried over from the assist layer: every failure returns a
tool-result error string (isError), never a crash; a malformed request gets
a JSON-RPC error, and the loop always continues.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from groundgraph.assist import graph_assist_ex, run_tool

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"

# MCP tool declarations (inputSchema is plain JSON Schema — the MCP shape,
# distinct from the OpenAI function-call shape in assist.tool_schemas).
TOOLS: list[dict] = [
    {
        "name": "query_facts",
        "description": ("Query the code memory graph. Filter by subject symbol, "
                        "predicate (calls, called-by, defined-in, tests, "
                        "co-changed-with, imports, raises, may-raise, lesson), "
                        "object, or repo."),
        "inputSchema": {"type": "object", "properties": {
            "subject": {"type": "string"}, "predicate": {"type": "string"},
            "object": {"type": "string"}, "repo": {"type": "string"},
            "limit": {"type": "integer"}}},
    },
    {
        "name": "explain_entity",
        "description": ("Depth-1 dossier for one symbol: where it is defined, "
                        "what it calls, what calls it, what tests it."),
        "inputSchema": {"type": "object", "properties": {
            "entity": {"type": "string"}, "repo": {"type": "string"}},
            "required": ["entity"]},
    },
    {
        "name": "assist",
        "description": ("Ground a task description against the graph: returns "
                        "the task prefixed with relevant symbols (file:line), "
                        "their relations, and matching lessons — or unchanged "
                        "when nothing grounds (loud no-op). Optional repo scope "
                        "and workspace existence-check."),
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string"}, "repo": {"type": "string"},
            "workspace": {"type": "string"}},
            "required": ["task"]},
    },
]


def _tool_result(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, args: dict, db_path: str,
              semantic_endpoint: str | None = None) -> dict:
    """Execute one MCP tool call. Never raises — errors become isError
    results the model can read and react to."""
    try:
        if name in ("query_facts", "explain_entity"):
            return _tool_result(run_tool(name, args, db_path))
        if name == "assist":
            task = str(args.get("task", ""))
            out, rep = graph_assist_ex(
                task, db_path, repo=args.get("repo"),
                workspace=args.get("workspace"),
                semantic_endpoint=semantic_endpoint)
            return _tool_result(json.dumps({"prompt": out, **rep}, indent=1))
        return _tool_result(f"unknown tool {name}", is_error=True)
    except Exception as e:  # noqa: BLE001 — the loop must survive anything
        logger.warning("mcp call_tool(%s) failed: %s", name, e)
        return _tool_result(f"tool failed: {e}", is_error=True)


def handle_request(req: dict, db_path: str,
                   semantic_endpoint: str | None = None) -> dict | None:
    """One JSON-RPC request -> response dict, or None for notifications."""
    method = req.get("method", "")
    rid = req.get("id")
    if rid is None:                       # notification (initialized, etc.)
        return None
    if method == "initialize":
        client_ver = (req.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION)
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "groundgraph", "version": "0.3.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        result = call_tool(str(params.get("name", "")),
                           params.get("arguments") or {}, db_path,
                           semantic_endpoint)
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(db_path: str, *, stdin=None, stdout=None,
          semantic_endpoint: str | None = None) -> int:
    """The stdio loop: one JSON-RPC message per line. A malformed line gets
    a parse error; the loop continues until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp: dict[str, Any] | None = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle_request(req, db_path, semantic_endpoint)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    return 0
