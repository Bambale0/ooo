import re
import subprocess
from pathlib import Path

import yaml


def test_host_deploy_workflow_is_trusted_and_keeps_arming_gate():
    text = Path('.github/workflows/deploy.yml').read_text(encoding='utf-8')
    config = yaml.safe_load(text)
    job = config['jobs']['deploy']
    guard = job['if']
    assert "vars.PRODUCTION_DEPLOY_ENABLED == 'true'" in guard
    assert "workflow_run.event == 'push'" in guard
    assert "workflow_run.head_branch == 'main'" in guard
    assert 'workflow_run.head_repository.full_name == github.repository' in guard
    assert job['environment'] == 'production'
    assert job['concurrency']['cancel-in-progress'] is False
    assert 'compose.host.yml' in text and 'ops/deploy/existing_host.py' in text
    assert 'StrictHostKeyChecking=yes' in text
    assert 'StrictHostKeyChecking=no' not in text
    assert text.count('git/ref/heads/main') == 2


def test_every_deploy_shell_block_parses():
    config = yaml.safe_load(Path('.github/workflows/deploy.yml').read_text(encoding='utf-8'))
    for step in config['jobs']['deploy']['steps']:
        if 'run' in step:
            result = subprocess.run(['bash', '-n'], input=step['run'], capture_output=True, text=True, check=False)
            assert result.returncode == 0, (step['name'], result.stderr)


def test_remote_deploy_shell_blocks_parse():
    config = yaml.safe_load(Path('.github/workflows/deploy.yml').read_text(encoding='utf-8'))
    for step in config['jobs']['deploy']['steps']:
        for body in re.findall(r"<<'SSH'\n(.*?)\nSSH", step.get('run', ''), re.S):
            result = subprocess.run(['bash', '-n'], input=body, capture_output=True, text=True, check=False)
            assert result.returncode == 0, (step['name'], result.stderr)


def test_release_archive_is_private_and_verified_before_extraction():
    text = Path('.github/workflows/deploy.yml').read_text(encoding='utf-8')
    assert 'umask 077; mktemp -d' in text
    assert "stat -c '%a'" in text
    assert 'sha256sum --check --status' in text
    assert text.index('sha256sum --check --status') < text.index('tar -xzf')
    assert 'BUNDLE="${DEPLOY_BUNDLE_DIR}/release.tar.gz"' in text
