#!/bin/sh
set -eu

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
REQUESTS="${REQUESTS:-100}"
CONCURRENCY="${CONCURRENCY:-10}"

case "$REQUESTS:$CONCURRENCY" in
  *[!0-9:]*|0:*|*:0) echo "REQUESTS and CONCURRENCY must be positive integers" >&2; exit 2 ;;
esac

command -v curl >/dev/null
i=0
while [ "$i" -lt "$REQUESTS" ]; do
  curl --fail --silent --show-error "$BASE_URL/health/live" >/dev/null &
  i=$((i + 1))
  if [ $((i % CONCURRENCY)) -eq 0 ]; then
    wait
  fi
done
wait
echo "load smoke completed: requests=$REQUESTS concurrency=$CONCURRENCY"
