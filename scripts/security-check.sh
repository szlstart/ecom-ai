#!/usr/bin/env bash
set -euo pipefail

security_python="${SECURITY_PYTHON:-python}"
allow_missing="${SECURITY_ALLOW_MISSING_TOOLS:-0}"
security_images="${SECURITY_IMAGES:-}"
missing=()

need_command() {
  if command -v "$1" >/dev/null 2>&1; then
    return 0
  fi
  missing+=("$1")
  return 1
}

need_module() {
  if "$security_python" -c "import $1" >/dev/null 2>&1; then
    return 0
  fi
  missing+=("python:$1")
  return 1
}

echo "[1/6] Python dependency integrity"
"$security_python" -m pip check

echo "[2/6] Python dependency vulnerability audit"
if need_module pip_audit; then
  "$security_python" -m pip_audit --require-hashes -r backend/requirements/dev.txt
fi

echo "[3/6] Python static security analysis"
if need_module bandit; then
  "$security_python" -m bandit -q -r backend/app -ll
fi

echo "[4/6] Frontend production dependency audit"
pnpm --dir frontend audit --prod --audit-level high --registry=https://registry.npmjs.org

echo "[5/6] Repository secret scan"
if need_command gitleaks; then
  gitleaks detect --source . --redact --no-banner
fi

echo "[6/6] Filesystem and container vulnerability scan"
if need_command trivy; then
  trivy fs --exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed .
  for image in $security_images; do
    trivy image --exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed "$image"
  done
fi

if ((${#missing[@]})); then
  printf 'Missing security tools: %s\n' "${missing[*]}" >&2
  if [[ "$allow_missing" != "1" ]]; then
    echo "Install them before rerunning, or explicitly set SECURITY_ALLOW_MISSING_TOOLS=1 for a non-gating local diagnostic." >&2
    exit 2
  fi
  echo "WARNING: missing tools were explicitly allowed; this run is not a release security gate." >&2
fi

echo "Security checks completed."
