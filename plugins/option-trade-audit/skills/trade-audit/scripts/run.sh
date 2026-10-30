#!/usr/bin/env bash
# Full audit after the order pages are ingested. Usage: bash run.sh [python]
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)"; PY="${1:-python3}"
for s in reconstruct fetch_px features features_h1 earnings_ctx; do "$PY" "$D/$s.py"; done
"$PY" "$D/analysis.py" > "${TRADE_AUDIT_DIR:-trade-audit-data}/analysis_out.txt"
"$PY" "$D/report.py"
"$PY" "$D/report.py" --sanitized -o "${TRADE_AUDIT_DIR:-trade-audit-data}/LESSONS_TABLES.md"
"$PY" "$D/priors.py"
echo "done: ${TRADE_AUDIT_DIR:-trade-audit-data}/ANALYSIS.md, LESSONS_TABLES.md, trade_priors.json"
