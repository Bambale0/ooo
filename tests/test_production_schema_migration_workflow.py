import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(".github/workflows/production-schema-migration.yml")


def _config():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_schema_migration_is_manual_exact_head_and_production_serialized():
    text = WORKFLOW.read_text(encoding="utf-8")
    job = _config()["jobs"]["migrate"]

    assert "workflow_dispatch:" in text
    assert "git/ref/heads/main" in text
    assert "release_sha must be the exact current main revision" in text
    assert job["environment"] == "production"
    assert job["concurrency"] == {"group": "production-deploy", "cancel-in-progress": False}
    assert "StrictHostKeyChecking=yes" in text
    assert "StrictHostKeyChecking=no" not in text
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in text


def test_schema_migration_requires_verified_encrypted_backup_before_upgrade():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "secrets.PRODUCTION_BACKUP_KEY" in text
    assert "pg_dump --format=custom" in text
    assert "openssl enc -aes-256-cbc -pbkdf2 -salt" in text
    assert "sha256sum --check --status" in text
    assert "openssl enc -d -aes-256-cbc -pbkdf2" in text
    assert "-out /dev/null" in text
    assert 'exec -T postgres pg_restore --list >/dev/null' in text
    assert "pg_restore --list -" not in text
    assert "restore_status=$?" in text
    assert '[ "$restore_status" -eq 0 ]' in text
    assert text.index("pg_restore --list") < text.index("alembic upgrade head")
    assert 'test -w "$root/release-backups"' in text
    assert 'chmod 700 "$root/release-backups"' not in text
    assert "alembic current --check-heads" in text
    assert "alembic check" in text
    assert "actions/upload-artifact@" in text
    assert "retention-days: 14" in text


def test_schema_migration_changes_schema_without_switching_services():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "TELEGRAM_BOT_USERNAME" in text
    assert "docker pull" in text
    assert "alembic upgrade head" in text
    assert " up -d" not in text
    assert "docker compose down" not in text
    assert "docker restart" not in text


def test_schema_migration_credentials_are_temporary_and_cleanup_is_always_run():
    steps = _config()["jobs"]["migrate"]["steps"]
    names = [step["name"] for step in steps]
    stage = steps[names.index("Stage temporary migration credentials")]["run"]
    cleanup = steps[names.index("Remove temporary migration credentials")]

    assert "mktemp -d /dev/shm/" in stage
    assert "MIGRATION_SECRET_DIR" in stage
    assert "config.json" in stage and "backup-key" in stage
    assert cleanup["if"] == "always()"
    assert "rm -f --" in cleanup["run"]
    assert "rmdir --" in cleanup["run"]
    assert "exit 1" in cleanup["run"]


def test_every_schema_migration_shell_block_parses():
    if sys.platform == "win32":
        pytest.skip("Windows WSL bash receives CRLF over subprocess stdin")
    for step in _config()["jobs"]["migrate"]["steps"]:
        if "run" not in step:
            continue
        body = step["run"].replace("\r\n", "\n").replace("\r", "\n")
        result = subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True, check=False)
        assert result.returncode == 0, (step["name"], result.stderr)


def test_remote_schema_migration_shell_block_parses():
    if sys.platform == "win32":
        pytest.skip("Windows WSL bash receives CRLF over subprocess stdin")
    for step in _config()["jobs"]["migrate"]["steps"]:
        for body in re.findall(r"<<'SSH'\n(.*?)\nSSH", step.get("run", ""), re.S):
            normalized = body.replace("\r\n", "\n").replace("\r", "\n")
            result = subprocess.run(["bash", "-n"], input=normalized, capture_output=True, text=True, check=False)
            assert result.returncode == 0, (step["name"], result.stderr)
