#!/bin/sh
set -eu
SERVICE="${SERVICE:-backend}"
BACKEND_SERVICES="api file-worker batch-worker order-timeout-worker payment-reconcile-worker logistics-sync-worker admin-approval-worker realtime-outbox-worker agent-runtime-worker knowledge-indexer ai-memory-cleanup-worker"
CANARY_URL="${CANARY_URL:?CANARY_URL is required}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:?ROLLBACK_IMAGE is required}"
CURRENT_IMAGE="${CURRENT_IMAGE:?CURRENT_IMAGE is required}"
COMPOSE_FILES="${COMPOSE_FILES:--f compose.yaml -f compose.production.yaml}"
STOP_THRESHOLD="${STOP_THRESHOLD:-3}"
failures=0
case "$ROLLBACK_IMAGE" in *@sha256:????????????????????????????????????????????????????????????????) ;; *) echo "ROLLBACK_IMAGE must be digest pinned" >&2; exit 2 ;; esac
case "$CURRENT_IMAGE" in *@sha256:????????????????????????????????????????????????????????????????) ;; *) echo "CURRENT_IMAGE must be digest pinned" >&2; exit 2 ;; esac
case "$SERVICE" in
  backend|api|file-worker|batch-worker|order-timeout-worker|payment-reconcile-worker|logistics-sync-worker|admin-approval-worker|realtime-outbox-worker|agent-runtime-worker|knowledge-indexer|ai-memory-cleanup-worker|frontend) ;;
  *) echo "unsupported canary service: $SERVICE" >&2; exit 2 ;;
esac
if [ -z "${HEALTH_PATH:-}" ]; then
  if [ "$SERVICE" = "frontend" ]; then HEALTH_PATH=/healthz; else HEALTH_PATH=/health/live; fi
fi
services_to_check="$SERVICE"
if [ "$SERVICE" = "backend" ]; then services_to_check="$BACKEND_SERVICES"; fi
for runtime_service in $services_to_check; do
  container_id=$(docker compose $COMPOSE_FILES ps -q "$runtime_service")
  test -n "$container_id" || { echo "$runtime_service is not running" >&2; exit 2; }
  running_image=$(docker inspect --format '{{.Config.Image}}' "$container_id")
  if [ "$running_image" != "$CURRENT_IMAGE" ]; then
    echo "$runtime_service does not match CURRENT_IMAGE" >&2
    exit 2
  fi
done
for _ in 1 2 3 4 5; do
  if ! curl --fail --silent --show-error "$CANARY_URL$HEALTH_PATH" >/dev/null; then
    failures=$((failures + 1))
  fi
done
if [ "$failures" -ge "$STOP_THRESHOLD" ]; then
  # COMPOSE_FILES is operator-controlled static release configuration, not user input.
  if [ "$SERVICE" = "frontend" ]; then
    ECOM_FRONTEND_IMAGE="$ROLLBACK_IMAGE" docker compose $COMPOSE_FILES up -d --no-deps --no-build --force-recreate "$SERVICE"
  elif [ "$SERVICE" = "backend" ]; then
    # The API and every asynchronous consumer share one event/schema contract and
    # therefore roll back as a single compatibility unit.
    ECOM_API_IMAGE="$ROLLBACK_IMAGE" docker compose $COMPOSE_FILES up -d --no-deps --no-build --force-recreate $BACKEND_SERVICES
  else
    ECOM_API_IMAGE="$ROLLBACK_IMAGE" docker compose $COMPOSE_FILES up -d --no-deps --no-build --force-recreate "$SERVICE"
  fi
  curl --fail --silent --show-error --retry 10 --retry-delay 2 "$CANARY_URL$HEALTH_PATH" >/dev/null
  for runtime_service in $services_to_check; do
    container_id=$(docker compose $COMPOSE_FILES ps -q "$runtime_service")
    running_image=$(docker inspect --format '{{.Config.Image}}' "$container_id")
    test "$running_image" = "$ROLLBACK_IMAGE" || {
      echo "rollback health passed but $runtime_service has an unexpected image" >&2
      exit 4
    }
  done
  echo "canary failed; rolled $SERVICE back to $ROLLBACK_IMAGE" >&2
  exit 3
fi
echo "canary passed for $SERVICE"
