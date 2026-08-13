#!/usr/bin/env bash
# groundgraph demo — index a real repo and ask it questions.
#
#   bash demo/demo.sh                 # clones pallets/flask (shallow) and indexes it
#   bash demo/demo.sh /path/to/repo   # indexes your repo instead
#
# Everything runs locally: stdlib Python + git. No network after the clone,
# no model, no telemetry.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-python3}"
DB="${DB:-/tmp/groundgraph-demo.db}"

REPO="${1:-}"
if [ -z "$REPO" ]; then
  REPO=/tmp/groundgraph-demo-flask
  if [ ! -d "$REPO" ]; then
    echo "== cloning pallets/flask (shallow) =="
    git clone -q --depth 400 https://github.com/pallets/flask.git "$REPO"
  fi
fi

cd "$HERE"
rm -f "$DB" "$DB-wal" "$DB-shm"

echo "== build: full deterministic pipeline (code -> tests -> co-change -> derived) =="
time "$PY" -m groundgraph build --db "$DB" "$REPO" 2>/dev/null

echo ""
echo "== who calls url_for? (derived inverse edge) =="
"$PY" -m groundgraph query --db "$DB" --predicate called-by --subject url_for --limit 5 2>/dev/null || true

echo ""
echo "== what tests cover flask.Flask? (fail-closed tests relation) =="
"$PY" -m groundgraph query --db "$DB" --predicate tests --object flask.Flask --limit 5 2>/dev/null || true

echo ""
echo "== explain one symbol (depth-1 dossier) =="
"$PY" -m groundgraph explain --db "$DB" url_for 2>/dev/null || true

echo ""
echo "== serve-time recall for an agent task (with fired-signal instrumentation) =="
"$PY" -m groundgraph assist --db "$DB" \
  "url_for builds the wrong external URL scheme behind an https proxy" || true

echo ""
echo "== agentic tool call (what an agent sees) =="
"$PY" -m groundgraph tool --db "$DB" query_facts \
  '{"predicate": "may-raise", "subject": "Scaffold.__init__"}' 2>/dev/null || true

echo ""
echo "== health + anti-rot dashboard =="
"$PY" -m groundgraph status --db "$DB" --repos "$REPO" 2>/dev/null || true
