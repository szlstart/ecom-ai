#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-/opt/miniconda3/envs/ecom-ai/bin/python}"
api_log="${repo_root}/artifacts/acceptance/current/quality/live-api.log"
worker_log="${repo_root}/artifacts/acceptance/current/quality/live-agent-worker.log"

export ECOM_ALLOWED_ORIGINS='http://127.0.0.1:4173'
export ECOM_PUBLIC_ORIGIN='http://127.0.0.1:4173'
export ECOM_READINESS_CHECKS_ENABLED=false
export ECOM_LIVE_E2E=1
export VITE_API_BASE_URL='http://127.0.0.1:18000/api/v1'

cleanup() {
  for pid in "${worker_pid:-}" "${api_pid:-}"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

mkdir -p "$(dirname "${api_log}")"
cd "${repo_root}"
make acceptance-scenario

(
  cd backend
  exec "${python_bin}" -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 18000
) >"${api_log}" 2>&1 &
api_pid=$!

(
  cd backend
  exec "${python_bin}" -m app.workers.agent_runtime_worker
) >"${worker_log}" 2>&1 &
worker_pid=$!

for _ in {1..60}; do
  if curl --fail --silent http://127.0.0.1:18000/health/live >/dev/null; then
    break
  fi
  if ! kill -0 "${api_pid}" 2>/dev/null; then
    echo 'Live acceptance API exited before becoming ready.' >&2
    tail -80 "${api_log}" >&2
    exit 1
  fi
  sleep 0.25
done
curl --fail --silent http://127.0.0.1:18000/health/live >/dev/null

cd frontend
pnpm test:e2e

# A green browser assertion must not hide an exception that a retry or polling
# path recovered from. Keep the connected acceptance strict about server-side
# tracebacks, implicit cartesian joins, and internal-server responses.
if rg --line-number --ignore-case \
  'cartesian product|traceback| 500 internal' \
  "${api_log}" "${worker_log}"; then
  echo 'Unexpected runtime warning/error found in live acceptance logs.' >&2
  exit 1
fi
