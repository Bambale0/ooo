#!/usr/bin/env bash
set -euo pipefail
: "${AGE_IDENTITY_FILE:?Recovery identity is required on the restore host only}"
: "${WAL_ARCHIVE_DIR:?Mount downloaded encrypted WAL here}"
wal_name=$1
output_file=$2
[[ "$wal_name" =~ ^[0-9A-F]{24}$ || "$wal_name" =~ ^[0-9A-F]{8}\.history$ ]] || exit 2
[[ -s "$WAL_ARCHIVE_DIR/$wal_name.age" ]] || exit 1
age -d -i "$AGE_IDENTITY_FILE" "$WAL_ARCHIVE_DIR/$wal_name.age" > "$output_file"
