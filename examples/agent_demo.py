"""A minimal tool-calling agent over the graph — zero dependencies.

Points any OpenAI-compatible endpoint (llama.cpp server, Ollama, vLLM,
LM Studio — all strictly local options) at the groundgraph tools and lets
the model answer questions BY QUERYING THE GRAPH instead of guessing:

    export GG_BASE_URL=http://127.0.0.1:8080/v1     # your local server
    export GG_MODEL=your-served-model-name          # whatever your server serves
    export GG_API_KEY=none                          # if the server needs one
    python examples/agent_demo.py --db /tmp/gg-flask.db \
        "What can Scaffold.__init__ raise, and which tests cover Flask?"

The loop offers `query_facts` + `explain_entity` (from
groundgraph.assist.tool_schemas), executes calls in-process against the
store (read-only), and prints the full trace — including how many times the
model actually consulted the graph (the fired-signal discipline: an answer
you cannot attribute to retrieval is indistinguishable from a guess).

Uses urllib from the stdlib — no client library required.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from groundgraph.assist import TOOL_NAMES, run_tool, tool_schemas

MAX_TURNS = 8


def chat(base_url: str, api_key: str, model: str, messages: list[dict],
         tools: list[dict]) -> dict:
    """One /chat/completions call via urllib. Returns the assistant message."""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model, "messages": messages, "tools": tools,
                         "temperature": 0.0, "stream": False}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())["choices"][0]["message"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question")
    ap.add_argument("--db", default="graph.db")
    args = ap.parse_args(argv)

    base_url = os.environ.get("GG_BASE_URL", "http://127.0.0.1:8080/v1")
    api_key = os.environ.get("GG_API_KEY", "none")
    model = os.environ.get("GG_MODEL", "default")

    messages: list[dict] = [
        {"role": "system", "content":
            "You answer questions about a codebase using the code memory "
            "graph tools. Query the graph before answering; cite the facts "
            "you retrieved. If the graph has no answer, say so."},
        {"role": "user", "content": args.question},
    ]
    tools = tool_schemas()
    graph_calls = 0

    for turn in range(MAX_TURNS):
        try:
            msg = chat(base_url, api_key, model, messages, tools)
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
            print(f"endpoint error: {e}\n(is a server running at {base_url}?)",
                  file=sys.stderr)
            return 1
        calls = msg.get("tool_calls") or []
        if not calls:
            print(f"\n=== answer (turn {turn}) ===\n{msg.get('content', '')}")
            break
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": calls})
        for tc in calls:
            fn = tc["function"]
            name = fn["name"]
            try:
                fn_args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                fn_args = {}
            if name in TOOL_NAMES:
                graph_calls += 1
                result = run_tool(name, fn_args, args.db)
            else:
                result = f"unknown tool {name}"
            print(f"[turn {turn}] {name}({json.dumps(fn_args)})"
                  f" -> {result[:120]}...")
            messages.append({"role": "tool", "content": result,
                             "tool_call_id": tc.get("id", "call_0")})
    else:
        print("\n(hit the turn cap without a final answer)")

    # The fired-signal: did the answer actually come from the graph?
    print(f"\n-- graph consulted {graph_calls} time(s) "
          f"{'(grounded answer)' if graph_calls else '(UNGROUNDED — treat as a guess)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
