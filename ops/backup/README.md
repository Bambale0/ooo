# Encrypted backups, support files and point-in-time recovery

There are two complementary paths. Logical backup verifies application records and
attachments; physical base backup + WAL supports recovery to a chosen timestamp.
Both use age authenticated encryption. No private recovery identity is needed on
the primary/backup host. Provision the identity on the isolated recovery runner only
for the drill, from an independent recovery secret store.

## Production WAL

Copy `backup.env.example` to the deployment's `.backup.env` (mode 600), configure
independent Russian S3/MinIO storage and a public AGE_RECIPIENT. Use both
`docker-compose.prod.yml` and `docker-compose.pitr.yml`. Build the custom PostgreSQL
16 image and pin NEIRONYCH_POSTGRES_IMAGE to the tested image. Existing data remains
in `postgres_data`; encrypted WAL is in `wal_archive`. Verify the upgrade against a
restored copy before changing an existing deployment's PostgreSQL image.

`archive-wal.sh` encrypts a completed WAL file before upload. A nonzero encryption,
upload or verification exit makes PostgreSQL retry. `archive_timeout=60s` bounds the
segment-switch delay when WAL is being generated. The S3 path includes PostgreSQL's
system identifier; the local archive refuses another cluster's files. S3 metadata
checks precede success. Do not reuse a remote namespace for unrelated clusters.
No script deletes WAL or remote backups automatically.

Create a physical base backup daily, inside the primary container:

```sh
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml exec -T \
  --user postgres postgres /usr/local/bin/physical-backup.sh
```

The script connects through the local socket using POSTGRES_USER, streams
`pg_basebackup -Ft -Xfetch -z` directly into age and uploads the encrypted archive.
It supports this repository's single data directory (no extra tablespaces).
Keep WAL covering every retained base backup and the desired recovery window.
Storage lifecycle/retention must be chosen together; deleting arbitrary WAL breaks
recovery. Monitor `pg_stat_archiver`, disk space and backup exit status. Verify
`archive_mode=on`, a successful forced `pg_switch_wal()`, and increasing
`archived_count` before accepting production traffic.

## Logical backup and support attachments

Build `docker build -t neironych-backup ops/backup`. Supply a protected env file:
PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE, BACKUP_DIR, AGE_RECIPIENT, BACKUP_S3_URI;
S3 credentials and AWS_ENDPOINT_URL as needed. Run with only the required backup
mounts/network. The backup user must be able to read the support volume; match its
numeric owner UID or use the controlled backup runner's read permissions.

Set SUPPORT_STORAGE_DIR to a read-only mount of `support_data`. `backup.sh` streams
an encrypted custom-format pg_dump and a paired encrypted support tar archive.
File objects are immutable; extra unreferenced objects in the archive are harmless.
Both uploads are verified before updating local `latest` links. Neither generated
AI outputs nor user-provided test references are copied into support storage.

For a logical restore drill, use the `restore-test.sh` entrypoint with
AGE_IDENTITY_FILE, BACKUP_FILE and a **new** PGDATABASE=`restore_test_<name>`.
Add SUPPORT_BACKUP_FILE and an absent RESTORE_SUPPORT_DIR ending in `restore_test_*`.
Existing destinations are refused. The script checks both ledgers, attachment
existence/sizes and a 900-second runtime limit. To run the service after recovery,
mount recovered support files at their original configured path.

CI creates a synthetic partner, payment, generation, API key, balanced ledgers and
support attachment before backup. A local populated restore on 2026-09-23 recovered
all of these with zero ledger mismatches in **2 seconds**.

## Physical point-in-time recovery

On an isolated recovery host, download the selected encrypted base backup and all
required encrypted WAL from its **system identifier** prefix. Mount the latter as
WAL_ARCHIVE_DIR. Provide the recovery identity as AGE_IDENTITY_FILE, readable only
by the recovery process. Never mount it on the primary server.

Run `restore-physical.sh` from the PostgreSQL image with:

- PGDATA: an empty, isolated recovery volume;
- BASE_BACKUP_FILE and AGE_IDENTITY_FILE: read-only archive/key mounts;
- RECOVERY_TARGET_TIME: `YYYY-MM-DD HH:MM:SS.ffffff UTC`, after that base backup ended;
- RESTORE_ISOLATED_CONFIRM=`empty-recovery-volume`.

Then start PostgreSQL 16 using that restored volume, the same key/WAL mounts and
`archive_mode=off`. `restore_command` decrypts each WAL segment on demand. PostgreSQL
promotes at the requested timestamp; missing WAL or an unreachable target must be
investigated before this server is used. Verify `pg_is_in_recovery()`, expected
business records and both balance ledgers. The rest of the application's secrets,
especially PROVIDER_CREDENTIALS_MASTER_KEY, must be restored from the separate
secret recovery process.

Repeatable local drill:

```sh
docker build -t ooo-pitr-check ops/backup/postgres
python ops/smoke/pitr.py
```

It creates disposable, network-isolated containers and volumes, writes three
transactions, restores the base backup, replays the second transaction from encrypted
WAL and excludes the third by timestamp. It also verifies promotion and cleans up.
The measured run on 2026-09-23 took **5.97 seconds**. It is a small local fixture,
not proof of production-sized RTO or remote-storage availability.

Schedule daily logical/physical backups and a weekly isolated restore on the
operator's scheduler; alert on any nonzero exit and missing expected archives.
CI runs both local recovery paths on every PR. BACKUP_LOCAL_ONLY=1 bypasses remote
storage only for isolated tests. Production requires a successful off-site drill,
the selected Russian storage contour and an independently held recovery key.

Underlying recovery behavior follows the official PostgreSQL 16
[pg_basebackup documentation](https://www.postgresql.org/docs/16/app-pgbasebackup.html).
