#!/bin/sh
set -eu
umask 077

: "${BACKUP_DIRECTORY:?BACKUP_DIRECTORY is required}"
: "${MYSQL_HOST:?MYSQL_HOST is required}"
: "${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
: "${MYSQL_APP_USER:?MYSQL_APP_USER is required}"
: "${MYSQL_DEFAULTS_FILE:?MYSQL_DEFAULTS_FILE is required}"
: "${POSTGRES_HOST:?POSTGRES_HOST is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${PGPASSFILE:?PGPASSFILE is required}"
: "${AGE_RECIPIENT:?AGE_RECIPIENT is required}"

"$(dirname "$0")/backup-preflight.sh"
for command_name in mysqldump pg_dump age; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$command_name is required" >&2
    exit 2
  }
done
test -f "$MYSQL_DEFAULTS_FILE"
test -f "$PGPASSFILE"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
mysql_target="$BACKUP_DIRECTORY/mysql-${MYSQL_DATABASE}-${timestamp}.sql.age"
postgres_target="$BACKUP_DIRECTORY/postgres-${POSTGRES_DB}-${timestamp}.dump.age"
checksum_target="$BACKUP_DIRECTORY/SHA256SUMS-${timestamp}"

mysqldump --defaults-extra-file="$MYSQL_DEFAULTS_FILE" \
  --host="$MYSQL_HOST" --user="$MYSQL_APP_USER" --single-transaction \
  --routines --events --triggers --set-gtid-purged=OFF "$MYSQL_DATABASE" \
  | age --recipient "$AGE_RECIPIENT" --output "$mysql_target"

PGPASSFILE="$PGPASSFILE" pg_dump --host="$POSTGRES_HOST" --username="$POSTGRES_USER" \
  --format=custom --no-owner --dbname="$POSTGRES_DB" \
  | age --recipient "$AGE_RECIPIENT" --output "$postgres_target"

test -s "$mysql_target"
test -s "$postgres_target"
BACKUP_DIRECTORY="$BACKUP_DIRECTORY" CHECKSUM_FILE="$checksum_target" \
  "$(dirname "$0")/backup-checksum.sh"
echo "encrypted backup evidence created under $BACKUP_DIRECTORY"
