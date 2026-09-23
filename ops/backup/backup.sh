#!/usr/bin/env bash
# Encrypted logical backup. No plaintext dump or secret passphrase in argv.
set -euo pipefail
umask 077
: "${AGE_RECIPIENT:?Set the off-server recovery public recipient}"
: "${PGDATABASE:?Set PGDATABASE}"
: "${BACKUP_DIR:?Set BACKUP_DIR}"
if [[ "${BACKUP_LOCAL_ONLY:-0}" != 1 ]]; then
  : "${BACKUP_S3_URI:?Set the off-site s3://bucket/prefix}"
  [[ "$BACKUP_S3_URI" == s3://* ]] || exit 2
fi
mkdir -p "$BACKUP_DIR"
name="neironych-$(date -u +%Y%m%dT%H%M%SZ)-${RANDOM}.dump.age"
partial=$(mktemp "$BACKUP_DIR/.backup-XXXXXX")
trap 'rm -f "$partial"' EXIT
pg_dump --format=custom --compress=9 --no-owner --no-acl | age -r "$AGE_RECIPIENT" -o "$partial"
[[ -s "$partial" ]]
mv "$partial" "$BACKUP_DIR/$name"
digest=$(sha256sum "$BACKUP_DIR/$name" | cut -d ' ' -f 1)
if [[ -n "${SUPPORT_STORAGE_DIR:-}" ]]; then
  [[ -d "$SUPPORT_STORAGE_DIR" ]] || { echo "Support storage unavailable" >&2; exit 1; }
  tar -C "$SUPPORT_STORAGE_DIR" -cf - . | age -r "$AGE_RECIPIENT" -o "$partial"
  mv "$partial" "$BACKUP_DIR/${name%.dump.age}.support.tar.age"
fi
if [[ "${BACKUP_LOCAL_ONLY:-0}" != 1 ]]; then
  target="${BACKUP_S3_URI%/}/$name"
  aws s3 cp "$BACKUP_DIR/$name" "$target" --only-show-errors --metadata "sha256=$digest"
  bucket_path=${target#s3://}
  stored=$(aws s3api head-object --bucket "${bucket_path%%/*}" --key "${bucket_path#*/}" --query Metadata.sha256 --output text)
  [[ "$stored" == "$digest" ]] || { echo 'Off-site verification failed' >&2; exit 1; }
  if [[ -n "${SUPPORT_STORAGE_DIR:-}" ]]; then
    support_name="${name%.dump.age}.support.tar.age"
    support_digest=$(sha256sum "$BACKUP_DIR/$support_name" | cut -d ' ' -f 1)
    aws s3 cp "$BACKUP_DIR/$support_name" "${BACKUP_S3_URI%/}/$support_name" --only-show-errors --metadata "sha256=$support_digest"
    support_key="${bucket_path#*/}"
    support_key="${support_key%.dump.age}.support.tar.age"
    stored=$(aws s3api head-object --bucket "${bucket_path%%/*}" --key "$support_key" --query Metadata.sha256 --output text)
    [[ "$stored" == "$support_digest" ]] || { echo 'Support off-site verification failed' >&2; exit 1; }
  fi
fi
if [[ -n "${SUPPORT_STORAGE_DIR:-}" ]]; then
  ln -sfn "${name%.dump.age}.support.tar.age" "$BACKUP_DIR/latest.support.tar.age"
fi
ln -sfn "$name" "$BACKUP_DIR/latest.dump.age"
printf 'backup_ok name=%s sha256=%s\n' "$name" "$digest"
