from pathlib import Path


def test_deploy_uploads_versioned_runtime_configuration():
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "Package and upload verified release configuration" in workflow
    assert "docker-compose.prod.yml" in workflow
    assert "docker-compose.pitr.yml" in workflow
    assert "nginx/production.conf" in workflow
    assert "ops/backup/postgres" in workflow
    assert 'RELEASE_DIR="${RELEASES}/${TAG}"' in workflow
    assert 'SHARED="${ROOT}/shared"' in workflow


def test_deploy_starts_all_mandatory_runtime_processes():
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "--profile telegram up -d --no-build" in workflow
    for service in ("app", "worker", "webhook_worker", "telegram", "nginx"):
        assert service in workflow


def test_rollback_uses_previous_versioned_release_bundle():
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert 'previous_dir="${RELEASES}/${PREVIOUS_TAG}"' in workflow
    assert 'compose_for "${previous_dir}" --profile telegram up -d --no-build' in workflow
    assert 'ln -sfn "${RELEASE_DIR}" "${ROOT}/current"' in workflow
