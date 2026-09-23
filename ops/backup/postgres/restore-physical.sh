#!/usr/bin/env bash
# Explicitly prepare an EMPTY recovery volume; never operate on a running PGDATA.
set -euo pipefail
umask 077
: "${PGDATA:?Set an isolated recovery directory}"
: "${AGE_IDENTITY_FILE:?required}"
: "${BASE_BACKUP_FILE:?required}"
: "${RECOVERY_TARGET_TIME:?Set a UTC timestamp after the base backup finished}"
[[ "$RECOVERY_TARGET_TIME" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?\ UTC$ ]] || exit 2
[[ "${RESTORE_ISOLATED_CONFIRM:-}" == "empty-recovery-volume" ]] || exit 2
mkdir -p "$PGDATA"
[[ -z "$(ls -A "$PGDATA")" ]] || { echo 'Recovery destination must be empty' >&2; exit 2; }
age -d -i "$AGE_IDENTITY_FILE" "$BASE_BACKUP_FILE" | tar -xz -C "$PGDATA"
cat >> "$PGDATA/postgresql.auto.conf" <<EOF
restore_command = '/usr/local/bin/restore-wal.sh %f %p'
recovery_target_time = '$RECOVERY_TARGET_TIME'
recovery_target_action = 'promote'
EOF
touch "$PGDATA/recovery.signal"
chmod 700 "$PGDATA"
