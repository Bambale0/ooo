#!/bin/bash
# PostgreSQL backup script for Нейроныч SaaS
# Runs inside the postgres container or as a sidecar
# Usage: ./ops/backup/backup.sh

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backup}"
DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-neironych}"
DB_NAME="${DB_NAME:-neironych}"
PGPASSWORD="${PGPASSWORD:?PGPASSWORD is required}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
ENCRYPTION_KEY="${ENCRYPTION_KEY:-}"

TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
BACKUP_FILE="${BACKUP_DIR}/neironych-${TIMESTAMP}.sql.gz"
ENCRYPTED_FILE="${BACKUP_FILE}.enc"
LATEST_LINK="${BACKUP_DIR}/latest.sql.gz.enc"

export PGPASSWORD

echo "=== Backup started: ${TIMESTAMP} ==="

# Dump and compress
pg_dump -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" \
    --format=custom --compress=9 \
    -f "${BACKUP_FILE}"

echo "Dump size: $(stat --printf=%s "${BACKUP_FILE}" 2>/dev/null || echo unknown)"

# Encrypt if key is provided
if [ -n "${ENCRYPTION_KEY}" ]; then
    openssl enc -aes-256-cbc -salt -pass "pass:${ENCRYPTION_KEY}" \
        -in "${BACKUP_FILE}" -out "${ENCRYPTED_FILE}"
    rm -f "${BACKUP_FILE}"
    ln -sf "${ENCRYPTED_FILE}" "${LATEST_LINK}"
    echo "Encrypted: ${ENCRYPTED_FILE}"
else
    ln -sf "${BACKUP_FILE}" "${LATEST_LINK}"
    echo "WARNING: backup is NOT encrypted"
fi

# WAL archive
if [ -d "${BACKUP_DIR}/wal" ]; then
    find "${BACKUP_DIR}/wal" -type f -mtime +${RETENTION_DAYS} -delete
fi

# Cleanup old backups
find "${BACKUP_DIR}" -name "neironych-*.sql.gz*" -mtime +${RETENTION_DAYS} -delete

echo "=== Backup completed ==="