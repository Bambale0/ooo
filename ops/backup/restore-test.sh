#!/usr/bin/env bash
# PGDATABASE must be a NEW, isolated database. Existing destinations are refused.
set -euo pipefail
umask 077
: "${AGE_IDENTITY_FILE:?Path to the off-server recovery identity}"
: "${PGDATABASE:?Name of the new isolated restore database}"
: "${BACKUP_FILE:?Path to the encrypted archive}"
[[ "$PGDATABASE" == restore_test_* ]] || { echo 'Use a restore_test_* destination' >&2; exit 2; }
start=$(date +%s)
createdb "$PGDATABASE"
# pipefail catches wrong keys, tampering and partial input; no unencrypted dump.
age -d -i "$AGE_IDENTITY_FILE" "$BACKUP_FILE" | pg_restore --exit-on-error --no-owner --no-acl --dbname "$PGDATABASE"
psql --set=ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL'
SELECT 'migration=' || version_num FROM alembic_version;
SELECT 'partners=' || count(*) FROM partners;
SELECT 'generations=' || count(*) FROM generations;
SELECT 'payments=' || count(*) FROM payment_invoices;
SELECT 'api_keys=' || count(*) FROM api_keys;
SELECT 'unbalanced_partners=' || count(*) FROM (
  SELECT p.id FROM partners p JOIN ledger_entries l ON l.partner_id=p.id
  GROUP BY p.id,p.balance_rub HAVING p.balance_rub <> sum(l.amount_rub)
) AS broken;
DO $$ BEGIN
  IF EXISTS (
    SELECT p.id FROM partners p JOIN ledger_entries l ON l.partner_id=p.id
    GROUP BY p.id,p.balance_rub HAVING p.balance_rub <> sum(l.amount_rub)
  ) THEN RAISE EXCEPTION 'Retail ledger mismatch after restore'; END IF;
  IF EXISTS (
    SELECT p.id FROM partners p JOIN coverage_ledger_entries l ON l.partner_id=p.id
    GROUP BY p.id,p.cost_coverage_rub HAVING p.cost_coverage_rub <> sum(l.amount_rub)
  ) THEN RAISE EXCEPTION 'Coverage ledger mismatch after restore'; END IF;
END $$;
SQL
if [[ -n "${SUPPORT_BACKUP_FILE:-}" ]]; then
  : "${RESTORE_SUPPORT_DIR:?Set an empty restore_test_* directory for attachments}"
  [[ "${RESTORE_SUPPORT_DIR##*/}" == restore_test_* ]] || exit 2
  [[ ! -e "$RESTORE_SUPPORT_DIR" ]] || { echo 'Support destination already exists' >&2; exit 2; }
  mkdir -p "$RESTORE_SUPPORT_DIR"
  age -d -i "$AGE_IDENTITY_FILE" "$SUPPORT_BACKUP_FILE" | tar -xf - -C "$RESTORE_SUPPORT_DIR"
  # Verify every database attachment has the expected immutable object and size.
  while IFS=$'\t' read -r object_path object_size; do
    [[ -n "$object_path" ]] || continue
    restored="$RESTORE_SUPPORT_DIR/${object_path##*/}"
    [[ -f "$restored" && "$(wc -c < "$restored" | tr -d ' ')" == "$object_size" ]] || {
      echo 'Missing or truncated support attachment' >&2; exit 1;
    }
  done < <(psql --set=ON_ERROR_STOP=1 --tuples-only --no-align -F $'\t' -c 'SELECT storage_path,file_size_bytes FROM support_attachments')
fi
elapsed=$(( $(date +%s) - start ))
[[ "$elapsed" -le 900 ]] || { echo "restore_slow seconds=$elapsed" >&2; exit 1; }
printf 'restore_ok seconds=%s database=%s\n' "$elapsed" "$PGDATABASE"
