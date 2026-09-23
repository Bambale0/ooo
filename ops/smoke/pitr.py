#!/usr/bin/env python3
"""Real encrypted WAL/PITR drill in network-isolated disposable PostgreSQL 16."""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4


def run(*args, input=None):
    return subprocess.run(args, input=input, check=True, capture_output=True, text=True).stdout.strip()


def drill():
    image = os.environ.get("PITR_TEST_IMAGE", "ooo-pitr-check")
    prefix = "ooo-pitr-" + uuid4().hex[:8]
    primary, recovery = prefix + "-primary", prefix + "-recovery"
    archive, data, restored = prefix + "-archive", prefix + "-data", prefix + "-restored"
    containers, volumes = [], []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ooo-pitr-") as temporary:
        directory = Path(temporary)
        identity = directory / "recovery.key"
        identity.write_text(run("docker", "run", "--rm", "--entrypoint", "age-keygen", image))
        identity.chmod(0o600)
        recipient = run(
            "docker", "run", "--rm", "-v", f"{identity}:/key:ro", "--entrypoint", "age-keygen", image, "-y", "/key"
        )
        postgres_uid = int(run("docker", "run", "--rm", "--entrypoint", "id", image, "-u", "postgres"))
        # Docker's postgres user must read the synthetic recovery identity.
        run(
            "docker", "run", "--rm", "-v", f"{identity}:/key", "--entrypoint", "chown", image, str(postgres_uid), "/key"
        )

        def sql(container, statement):
            return run(
                "docker", "exec", container, "psql", "-U", "postgres", "-At", "-v", "ON_ERROR_STOP=1", "-c", statement
            )

        def ready(container):
            for _ in range(60):
                try:
                    return sql(container, "SELECT 1")
                except subprocess.CalledProcessError:
                    time.sleep(0.5)
            raise RuntimeError("PostgreSQL did not become ready")

        try:
            for volume in (archive, data, restored):
                run("docker", "volume", "create", volume)
                volumes.append(volume)
            containers.append(primary)
            run(
                "docker",
                "run",
                "-d",
                "--name",
                primary,
                "--network",
                "none",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "BACKUP_LOCAL_ONLY=1",
                "-e",
                f"AGE_RECIPIENT={recipient}",
                "-v",
                f"{data}:/var/lib/postgresql/data",
                "-v",
                f"{archive}:/var/lib/postgresql/archive",
                image,
                "postgres",
                "-c",
                "archive_mode=on",
                "-c",
                "archive_command=/usr/local/bin/archive-wal.sh %p %f",
                "-c",
                "wal_keep_size=512MB",
            )
            ready(primary)
            sql(primary, "CREATE TABLE pitr_probe (id integer primary key); INSERT INTO pitr_probe VALUES (1)")
            run(
                "docker",
                "exec",
                "--user",
                "postgres",
                "-e",
                "BACKUP_DIR=/var/lib/postgresql/archive/base",  # pragma: allowlist secret
                primary,
                "/usr/local/bin/physical-backup.sh",
            )
            sql(primary, "INSERT INTO pitr_probe VALUES (2)")
            target = sql(
                primary, "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US') || ' UTC'"
            )
            sql(primary, "SELECT pg_sleep(0.1); INSERT INTO pitr_probe VALUES (3)")
            wal = sql(primary, "SELECT pg_walfile_name(pg_current_wal_lsn())")
            sql(primary, "SELECT pg_switch_wal()")
            for _ in range(60):
                try:
                    run("docker", "exec", primary, "test", "-s", f"/var/lib/postgresql/archive/{wal}.age")
                    break
                except subprocess.CalledProcessError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("Encrypted WAL archive did not arrive")
            run(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/usr/local/bin/restore-physical.sh",
                "-e",
                "PGDATA=/restore",
                "-e",
                "AGE_IDENTITY_FILE=/key",
                "-e",
                "BASE_BACKUP_FILE=/archive/base/latest.base.tar.gz.age",  # pragma: allowlist secret
                "-e",
                f"RECOVERY_TARGET_TIME={target}",
                "-e",
                "RESTORE_ISOLATED_CONFIRM=empty-recovery-volume",  # pragma: allowlist secret
                "-v",
                f"{restored}:/restore",
                "-v",
                f"{archive}:/archive:ro",
                "-v",
                f"{identity}:/key:ro",
                image,
            )
            containers.append(recovery)
            run(
                "docker",
                "run",
                "-d",
                "--name",
                recovery,
                "--network",
                "none",
                "-e",
                "AGE_IDENTITY_FILE=/key",
                "-e",
                "WAL_ARCHIVE_DIR=/archive",
                "-v",
                f"{restored}:/var/lib/postgresql/data",
                "-v",
                f"{archive}:/archive:ro",
                "-v",
                f"{identity}:/key:ro",
                image,
                "postgres",
                "-c",
                "archive_mode=off",
            )
            ready(recovery)
            result = sql(recovery, "SELECT string_agg(id::text, ',' ORDER BY id) FROM pitr_probe")
            assert result == "1,2", f"Wrong recovery boundary: {result}"
            assert sql(recovery, "SELECT pg_is_in_recovery()") == "f"
            elapsed = round(time.monotonic() - started, 2)
            assert elapsed <= 900
            print(
                json.dumps(
                    {
                        "pitr": "PASS",
                        "baseline_restored": True,
                        "after_backup_transaction_replayed": True,
                        "after_target_transaction_excluded": True,
                        "encrypted_wal": True,
                        "seconds": elapsed,
                    }
                )
            )
        except Exception:
            for container in containers:
                logs = subprocess.run(["docker", "logs", "--tail", "30", container], capture_output=True, text=True)
                print(logs.stdout, logs.stderr)
            raise
        finally:
            for container in containers:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            for volume in volumes:
                subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


if __name__ == "__main__":
    drill()
