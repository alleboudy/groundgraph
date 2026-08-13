"""groundgraph — a strictly-local, deterministic code-memory graph for
coding agents. Zero runtime dependencies.

Every fact is grounded: an AST node, a git commit, a doc line, or a
mechanical derivation with a proof path. No LLM extraction anywhere in the
pipeline — generation is for answering, not for remembering.
"""
from groundgraph.assist import graph_assist, run_tool, tool_schemas
from groundgraph.query import FactQuery
from groundgraph.store import GraphStore
from groundgraph.types import ExtractedFact, FactRow, ProposedFact

__version__ = "0.1.0"

__all__ = [
    "ExtractedFact",
    "FactQuery",
    "FactRow",
    "GraphStore",
    "ProposedFact",
    "graph_assist",
    "run_tool",
    "tool_schemas",
    "__version__",
]
