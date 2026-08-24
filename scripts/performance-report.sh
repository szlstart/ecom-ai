#!/bin/sh
set -eu

: "${BASE_URL:=http://127.0.0.1:8000}"
: "${SCENARIO:=load}"
: "${REPORT_DIR:=artifacts/performance}"

case "$SCENARIO" in
  load|stress|spike|soak) ;;
  *) echo "SCENARIO must be one of: load, stress, spike, soak" >&2; exit 2 ;;
esac

command -v k6 >/dev/null 2>&1 || {
  echo "k6 is required; no synthetic report will be produced" >&2
  exit 2
}

case "$REPORT_DIR" in
  /*) ;;
  *) REPORT_DIR="$(pwd)/$REPORT_DIR" ;;
esac
case "$REPORT_DIR" in
  /|"$HOME") echo "REPORT_DIR must be a workspace artifact directory" >&2; exit 2 ;;
esac

mkdir -p "$REPORT_DIR"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
summary_file="$REPORT_DIR/${SCENARIO}-${timestamp}.json"

BASE_URL="$BASE_URL" SCENARIO="$SCENARIO" \
  k6 run --summary-export "$summary_file" "$(dirname "$0")/performance-scenarios.js"

test -s "$summary_file" || {
  echo "k6 did not produce a summary: $summary_file" >&2
  exit 1
}

echo "performance evidence: $summary_file"
