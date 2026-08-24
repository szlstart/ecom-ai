#!/bin/sh
set -eu

: "${MYSQL_BACKUP_FILE:?MYSQL_BACKUP_FILE is required}"
: "${POSTGRES_BACKUP_FILE:?POSTGRES_BACKUP_FILE is required}"
: "${BACKUP_DIRECTORY:?BACKUP_DIRECTORY is required}"
: "${CHECKSUM_FILE:?CHECKSUM_FILE is required}"
: "${RESTORE_DATABASE:?RESTORE_DATABASE is required}"
: "${POSTGRES_RESTORE_DATABASE:?POSTGRES_RESTORE_DATABASE is required}"
: "${POSTGRES_RESTORE_HOST:?POSTGRES_RESTORE_HOST is required}"
: "${POSTGRES_RESTORE_USER:?POSTGRES_RESTORE_USER is required}"
: "${PGPASSFILE:?PGPASSFILE is required; do not put passwords in arguments}"
: "${MYSQL_DEFAULTS_FILE:?MYSQL_DEFAULTS_FILE is required; do not put passwords in arguments}"
: "${AGE_IDENTITY_FILE:?AGE_IDENTITY_FILE is required}"
: "${RESTORE_CONFIRMATION:?RESTORE_CONFIRMATION is required}"

if [ "${RESTORE_ENVIRONMENT:-}" != "isolated-drill" ] || \
   [ "$RESTORE_CONFIRMATION" != "restore:$RESTORE_DATABASE:$POSTGRES_RESTORE_DATABASE" ]; then
  echo "restore is allowed only in an explicitly confirmed isolated drill environment" >&2
  exit 2
fi
case "$RESTORE_DATABASE" in
  *_restore_drill) ;;
  *) echo "RESTORE_DATABASE must end with _restore_drill" >&2; exit 2 ;;
esac
case "$POSTGRES_RESTORE_DATABASE" in
  *_restore_drill) ;;
  *) echo "POSTGRES_RESTORE_DATABASE must end with _restore_drill" >&2; exit 2 ;;
esac
case "$MYSQL_BACKUP_FILE:$POSTGRES_BACKUP_FILE" in
  *.age:*.age) ;;
  *) echo "restore drill accepts encrypted .age backups only" >&2; exit 2 ;;
esac

test -f "$MYSQL_BACKUP_FILE"
test -f "$POSTGRES_BACKUP_FILE"
test -f "$MYSQL_DEFAULTS_FILE"
test -f "$AGE_IDENTITY_FILE"
test -f "$PGPASSFILE"
test -f "$CHECKSUM_FILE"
command -v age >/dev/null 2>&1 || { echo "age is required" >&2; exit 2; }
BACKUP_DIRECTORY="$BACKUP_DIRECTORY" CHECKSUM_FILE="$CHECKSUM_FILE" \
  "$(dirname "$0")/backup-checksum.sh"

restore_tmp=$(mktemp -d "${TMPDIR:-/tmp}/ecom-ai-restore.XXXXXX")
trap 'rm -rf "$restore_tmp"' EXIT HUP INT TERM
mysql_plain="$restore_tmp/mysql.sql"
postgres_plain="$restore_tmp/postgres.dump"
age --decrypt --identity "$AGE_IDENTITY_FILE" --output "$mysql_plain" "$MYSQL_BACKUP_FILE"
age --decrypt --identity "$AGE_IDENTITY_FILE" --output "$postgres_plain" "$POSTGRES_BACKUP_FILE"

mysql --defaults-extra-file="$MYSQL_DEFAULTS_FILE" --protocol=tcp \
  --host="${MYSQL_HOST:?}" "$RESTORE_DATABASE" < "$mysql_plain"
PGPASSFILE="$PGPASSFILE" pg_restore --clean --if-exists --no-owner \
  --host="$POSTGRES_RESTORE_HOST" --username="$POSTGRES_RESTORE_USER" \
  --dbname="$POSTGRES_RESTORE_DATABASE" "$postgres_plain"
echo "backup restore drill completed"
