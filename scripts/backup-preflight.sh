#!/bin/sh
set -eu

for required in MYSQL_HOST MYSQL_DATABASE MYSQL_APP_USER POSTGRES_HOST POSTGRES_DB POSTGRES_USER BACKUP_DIRECTORY; do
  value=$(printenv "$required" 2>/dev/null || true)
  if [ -z "$value" ]; then
    echo "missing required environment variable: $required" >&2
    exit 2
  fi
done

case "$BACKUP_DIRECTORY" in
  /|""|~|.) echo "unsafe backup directory" >&2; exit 2 ;;
  /*) ;;
  *) echo "BACKUP_DIRECTORY must be an explicit absolute path" >&2; exit 2 ;;
esac

mkdir -p "$BACKUP_DIRECTORY"
test -w "$BACKUP_DIRECTORY"
if [ -z "${BACKUP_ENCRYPTION_KEY_ID:-}" ]; then
  echo "missing required environment variable: BACKUP_ENCRYPTION_KEY_ID" >&2
  exit 2
fi
if [ -z "${OBJECT_REPLICATION_TARGET:-}" ]; then
  echo "missing required environment variable: OBJECT_REPLICATION_TARGET" >&2
  exit 2
fi
if [ "${BACKUP_RETENTION_DAYS:-0}" -lt 7 ]; then
  echo "BACKUP_RETENTION_DAYS must be at least 7" >&2
  exit 2
fi
echo "backup preflight passed for MySQL/PostgreSQL targets with encryption and object replication"
