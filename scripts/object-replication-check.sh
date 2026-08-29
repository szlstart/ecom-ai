#!/bin/sh
set -eu

: "${PRIMARY_ENDPOINT:?PRIMARY_ENDPOINT is required}"
: "${PRIMARY_BUCKET:?PRIMARY_BUCKET is required}"
: "${REPLICA_ENDPOINT:?REPLICA_ENDPOINT is required}"
: "${REPLICA_BUCKET:?REPLICA_BUCKET is required}"
: "${OBJECT_KEY:?OBJECT_KEY is required}"
: "${EXPECTED_SHA256:?EXPECTED_SHA256 is required}"

command -v aws >/dev/null 2>&1 || { echo "aws CLI is required" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 2; }
case "$EXPECTED_SHA256" in
  ????????????????????????????????????????????????????????????????) ;;
  *) echo "EXPECTED_SHA256 must contain 64 hexadecimal characters" >&2; exit 2 ;;
esac
case "$EXPECTED_SHA256" in *[!0-9a-fA-F]*) echo "EXPECTED_SHA256 is invalid" >&2; exit 2 ;; esac

primary_json=$(aws --endpoint-url "$PRIMARY_ENDPOINT" s3api head-object \
  --bucket "$PRIMARY_BUCKET" --key "$OBJECT_KEY" --output json)
replica_json=$(aws --endpoint-url "$REPLICA_ENDPOINT" s3api head-object \
  --bucket "$REPLICA_BUCKET" --key "$OBJECT_KEY" --output json)

primary_checksum=$(printf '%s' "$primary_json" | jq -r '.Metadata.sha256 // empty')
replica_checksum=$(printf '%s' "$replica_json" | jq -r '.Metadata.sha256 // empty')
primary_version=$(printf '%s' "$primary_json" | jq -r '.VersionId // empty')
replica_version=$(printf '%s' "$replica_json" | jq -r '.VersionId // empty')

if [ "$primary_checksum" != "$EXPECTED_SHA256" ] || [ "$replica_checksum" != "$EXPECTED_SHA256" ]; then
  echo "object replication checksum mismatch" >&2
  exit 1
fi
if [ -z "$primary_version" ] || [ -z "$replica_version" ]; then
  echo "object versioning evidence is missing" >&2
  exit 1
fi
echo "object replication verified with matching checksums and non-empty version IDs"
