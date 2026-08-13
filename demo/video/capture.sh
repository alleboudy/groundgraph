#!/usr/bin/env bash
# Capture the demo outputs the video renders. Run after demo/demo.sh's clone:
#   bash demo/video/capture.sh /path/to/indexed-repo-db > /tmp/gg-video-capture.txt
#   python demo/video/make_video.py --capture /tmp/gg-video-capture.txt
set -euo pipefail
DB="${1:-/tmp/groundgraph-demo.db}"
REPO="${2:-/tmp/groundgraph-demo-flask}"
PY="${PY:-python3}"

echo "### BUILD"
"$PY" -m groundgraph build --db /tmp/gg-capture-fresh.db "$REPO" 2>/dev/null | head -9
echo "### CALLED_BY"
"$PY" -m groundgraph query --db "$DB" --predicate called-by --subject url_for --limit 4 2>/dev/null
echo "### TESTS"
"$PY" -m groundgraph query --db "$DB" --predicate tests --object flask.Flask --limit 4 2>/dev/null
echo "### ASSIST"
"$PY" -m groundgraph assist --db "$DB" \
  "url_for builds the wrong external URL scheme behind an https proxy" 2>&1 | head -7
echo "### MAYRAISE"
"$PY" -m groundgraph tool --db "$DB" query_facts \
  '{"predicate": "may-raise", "subject": "Scaffold.__init__"}' 2>/dev/null
echo "### STATUS"
"$PY" -m groundgraph status --db "$DB" 2>/dev/null | head -12
