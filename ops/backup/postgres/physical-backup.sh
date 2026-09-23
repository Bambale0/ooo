#!/usr/bin/env bash
# Run inside the database container as postgres, with a local replication connection.
set -euo pipefail
umask 077
: "${AGE_RECIPIENT:?required}"
: "${BACKUP_DIR:?required}"
export PGHOST=/var/run/postgresql PGUSER="${POSTGRES_USER:-postgres}"
mkdir -p "$BACKUP_DIR"
name="base-$(date -u +%Y%m%dT%H%M%SZ)-${RANDOM}.tar.gz.age"
temporary=$(mktemp "$BACKUP_DIR/.base-XXXXXX")
trap 'rm -f "$temporary"' EXIT
# stdout mode excludes extra tablespaces and streams no plaintext dump to disk.
pg_basebackup -D - -Ft -Xfetch -z --checkpoint=fast --no-password | age -r "$AGE_RECIPIENT" -o "$temporary"
mv "$temporary" "$BACKUP_DIR/$name"
if [[ "${BACKUP_LOCAL_ONLY:-0}" != 1 ]]; then
  : "${BACKUP_S3_URI:?required}"
  [[ "$BACKUP_S3_URI" == s3://* ]] || exit 2
  digest=$(sha256sum "$BACKUP_DIR/$name" | cut -d ' ' -f 1)
  target="${BACKUP_S3_URI%/}/$name"
  aws s3 cp "$BACKUP_DIR/$name" "$target" --only-show-errors --metadata "sha256=$digest"
  bucket_path=${target#s3://}
  stored=$(aws s3api head-object --bucket "${bucket_path%%/*}" --key "${bucket_path#*/}" --query Metadata.sha256 --output text)
  [[ "$stored" == "$digest" ]] || exit 1
fi
ln -sfn "$name" "$BACKUP_DIR/latest.base.tar.gz.age"
printf 'physical_backup_ok name=%s\n' "$name"
