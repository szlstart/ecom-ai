#!/bin/sh
set -eu

: "${BASE_URL:=http://127.0.0.1:8000}"
: "${SCENARIO:=load}"
: "${REPORT_DIR:=artifacts/performance}"
: "${WORKLOAD_PROFILE:=public-catalog}"
: "${DATASET_LABEL:=unspecified}"

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
manifest_file="$REPORT_DIR/${SCENARIO}-${timestamp}.manifest.json"

BASE_URL="$BASE_URL" SCENARIO="$SCENARIO" WORKLOAD_PROFILE="$WORKLOAD_PROFILE" \
  k6 run --summary-export "$summary_file" "$(dirname "$0")/performance-scenarios.js"

test -s "$summary_file" || {
  echo "k6 did not produce a summary: $summary_file" >&2
  exit 1
}

build_sha=$(git rev-parse HEAD 2>/dev/null || printf unknown)
runtime_sha=$(curl --fail --silent --show-error "$BASE_URL/health/live" 2>/dev/null \
  | sed -n 's/.*"build_sha":"\([^"]*\)".*/\1/p')
runtime_sha=${runtime_sha:-unknown}
machine=$(uname -sm)
k6_version=$(k6 version | head -n 1)
has_user_token=false; test -n "${USER_TOKEN:-${AUTH_TOKEN:-}}" && has_user_token=true
has_merchant_token=false; test -n "${MERCHANT_TOKEN:-}" && has_merchant_token=true
has_admin_token=false; test -n "${ADMIN_TOKEN:-}" && has_admin_token=true
cat > "$manifest_file" <<EOF
{
  "generated_at": "$timestamp",
  "commit_sha": "$build_sha",
  "runtime_build_sha": "$runtime_sha",
  "scenario": "$SCENARIO",
  "workload_profile": "$WORKLOAD_PROFILE",
  "dataset_label": "$DATASET_LABEL",
  "base_url": "$BASE_URL",
  "machine": "$machine",
  "k6_version": "$k6_version",
  "credentials_present": {
    "user": $has_user_token,
    "merchant": $has_merchant_token,
    "admin": $has_admin_token
  },
  "resource_ids_present": {
    "product": $(test -n "${PRODUCT_ID:-}" && printf true || printf false),
    "store": $(test -n "${STORE_ID:-}" && printf true || printf false),
    "order": $(test -n "${ORDER_ID:-}" && printf true || printf false),
    "conversation": $(test -n "${CONVERSATION_ID:-}" && printf true || printf false)
  }
}
EOF

echo "performance evidence: $summary_file"
echo "performance manifest: $manifest_file"
