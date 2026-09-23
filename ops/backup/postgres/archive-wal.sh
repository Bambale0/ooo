#!/usr/bin/env bash
# PostgreSQL retries nonzero archive_command exits; never delete unarchived WAL.
set -euo pipefail
umask 077
: "${AGE_RECIPIENT:?required}"
: "${WAL_ARCHIVE_DIR:=/var/lib/postgresql/archive}"
source_file=$1
wal_name=$2
[[ "$wal_name" =~ ^[0-9A-F]{24}(\.[0-9A-F]{8}\.backup)?$ || "$wal_name" =~ ^[0-9A-F]{8}\.history$ ]] || exit 2
mkdir -p "$WAL_ARCHIVE_DIR"
cluster_id=$(LC_ALL=C pg_controldata "$PGDATA" | awk -F ': *' '$1 == "Database system identifier" { print $2 }')
[[ "$cluster_id" =~ ^[0-9]+$ ]] || exit 2
if [[ -f "$WAL_ARCHIVE_DIR/.cluster-id" ]]; then
  [[ "$(cat "$WAL_ARCHIVE_DIR/.cluster-id")" == "$cluster_id" ]] || { echo 'Wrong WAL archive cluster' >&2; exit 2; }
else
  printf '%s\n' "$cluster_id" > "$WAL_ARCHIVE_DIR/.cluster-id"
fi
archive="$WAL_ARCHIVE_DIR/$wal_name.age"
# Retry reuses the original encrypted bytes, so remote checksum is stable.
if [[ ! -s "$archive" ]]; then
  temporary=$(mktemp "$WAL_ARCHIVE_DIR/.wal-XXXXXX")
  trap 'rm -f "$temporary"' EXIT
  age -r "$AGE_RECIPIENT" -o "$temporary" "$source_file"
  mv "$temporary" "$archive"
fi
if [[ "${BACKUP_LOCAL_ONLY:-0}" != 1 ]]; then
  : "${WAL_S3_URI:?Set a unique s3://bucket/cluster-id/wal prefix}"
  [[ "$WAL_S3_URI" == s3://* ]] || exit 2
  digest=$(sha256sum "$archive" | cut -d ' ' -f 1)
  target="${WAL_S3_URI%/}/$cluster_id/$wal_name.age"
  aws s3 cp "$archive" "$target" --only-show-errors --metadata "sha256=$digest"
  bucket_path=${target#s3://}
  stored=$(aws s3api head-object --bucket "${bucket_path%%/*}" --key "${bucket_path#*/}" --query Metadata.sha256 --output text)
  [[ "$stored" == "$digest" ]] || exit 1
fi
