from pathlib import Path


def test_production_deploy_requires_explicit_repository_gate():
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "vars.PRODUCTION_DEPLOY_ENABLED == 'true'" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow


def test_operations_document_deploy_arming_prerequisites():
    operations = Path("docs/OPERATIONS.md").read_text(encoding="utf-8")

    assert "## Production deploy arming gate" in operations
    assert "PRODUCTION_DEPLOY_ENABLED=true" in operations
    assert "DEPLOY_SSH_KEY" in operations
    assert "DEPLOY_KNOWN_HOSTS" in operations
    assert "DEPLOY_HOST" in operations
