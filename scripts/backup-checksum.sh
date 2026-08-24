#!/bin/sh
set -eu
: "${BACKUP_DIRECTORY:?BACKUP_DIRECTORY is required}"
: "${CHECKSUM_FILE:=$BACKUP_DIRECTORY/SHA256SUMS}"
case "$BACKUP_DIRECTORY" in /|""|~|.) echo "unsafe backup directory" >&2; exit 2 ;; esac
case "$CHECKSUM_FILE" in
  "$BACKUP_DIRECTORY"/*) ;;
  *) echo "CHECKSUM_FILE must be inside BACKUP_DIRECTORY" >&2; exit 2 ;;
esac
checksum_name=$(basename "$CHECKSUM_FILE")
if command -v sha256sum >/dev/null 2>&1; then
  if [ ! -f "$CHECKSUM_FILE" ]; then
    (cd "$BACKUP_DIRECTORY" && find . -maxdepth 1 -type f ! -name 'SHA256SUMS*' \
      -exec sha256sum {} + | sort > "$checksum_name")
  fi
  (cd "$BACKUP_DIRECTORY" && sha256sum --check "$checksum_name")
elif command -v shasum >/dev/null 2>&1; then
  if [ ! -f "$CHECKSUM_FILE" ]; then
    (cd "$BACKUP_DIRECTORY" && find . -maxdepth 1 -type f ! -name 'SHA256SUMS*' \
      -exec shasum -a 256 {} + | sort > "$checksum_name")
  fi
  (cd "$BACKUP_DIRECTORY" && shasum -a 256 --check "$checksum_name")
else
  echo "sha256sum or shasum is required" >&2
  exit 2
fi
echo "backup checksum verified: $CHECKSUM_FILE"
