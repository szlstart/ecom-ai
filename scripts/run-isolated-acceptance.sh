#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export ECOM_ENVIRONMENT=testing
export ECOM_MYSQL_DSN='mysql+asyncmy://ecom_app:local-app-change-me@127.0.0.1:13306/ecom_ai_test?charset=utf8mb4'
export ECOM_POSTGRES_DSN='postgresql+asyncpg://ecom_ai:local-postgres-change-me@127.0.0.1:15432/ecom_ai_ai_test'
export ECOM_REDIS_URL='redis://:local-redis-change-me@127.0.0.1:16379/15'
export ECOM_OBJECT_STORAGE_ENABLED=true
export ECOM_OBJECT_STORAGE_ENDPOINT='http://127.0.0.1:19000'
export ECOM_OBJECT_STORAGE_PUBLIC_ENDPOINT='http://127.0.0.1:19000'
export ECOM_OBJECT_STORAGE_ACCESS_KEY='local-minio-admin'
export ECOM_OBJECT_STORAGE_SECRET_KEY='local-minio-change-me'
export ECOM_OBJECT_STORAGE_BUCKET_PREFIX='test-acceptance-'
export ECOM_FILE_SCANNER_ENABLED=true
export ECOM_FILE_SCANNER_HOST='127.0.0.1'
export ECOM_FILE_SCANNER_PORT=13310
export ECOM_RUN_INTEGRATION_TESTS=1
export ECOM_RUN_FILE_INTEGRATION_TESTS=1
# Acceptance fixtures exercise the deterministic fallback gateway. Live Kimi
# compatibility and quality are evaluated by the separate provider/eval jobs.
export ECOM_AGENT_MODEL_API_URL=''
export ECOM_AGENT_MODEL_API_KEY=''
export ECOM_AGENT_MODEL_NAME=''
export ECOM_AGENT_MODEL_REQUIRED=false
export ECOM_EMBEDDING_API_URL=''
export ECOM_EMBEDDING_API_KEY=''

cd "${repo_root}"

docker compose up --detach --wait mysql postgres redis minio clamav
mysql_container="$(docker compose ps --quiet mysql)"
postgres_container="$(docker compose ps --quiet postgres)"
redis_container="$(docker compose ps --quiet redis)"

cleanup() {
  "${repo_root}/scripts/cleanup-test-object-storage.py" >/dev/null 2>&1 || true
  docker exec "${redis_container}" sh -ec 'redis-cli -a "$REDIS_PASSWORD" -n 15 FLUSHDB >/dev/null' || true
  docker exec "${postgres_container}" sh -ec 'dropdb -U "$POSTGRES_USER" --if-exists --force ecom_ai_ai_test' || true
  docker exec "${mysql_container}" sh -ec 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "DROP DATABASE IF EXISTS ecom_ai_test"' || true
}
trap cleanup EXIT INT TERM

# Drop only exact, hard-coded test namespaces. The safety validator below also
# rejects any DSN that does not end in _test before pytest can write data.
cleanup
docker exec "${mysql_container}" sh -ec 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE ecom_ai_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; GRANT ALL PRIVILEGES ON ecom_ai_test.* TO '\''$MYSQL_USER'\''@'\''%'\''; FLUSH PRIVILEGES"'
docker exec "${postgres_container}" sh -ec 'createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" ecom_ai_ai_test'

make migrate seed
make acceptance-test
