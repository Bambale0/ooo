import json
from pathlib import Path

import pytest

from ops.deploy import existing_host as deploy

OLD = '1' * 40
NEW = '2' * 40
OLD_IMAGE = 'sha256:' + 'a' * 64
NEW_IMAGE = 'sha256:' + 'b' * 64
TARGET = 'ghcr.io/bambale0/ooo:' + NEW
SERVICES = ('app', 'worker', 'webhook_worker', 'telegram')


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.image = OLD_IMAGE
        self.revision = OLD
        self.fail = None
        self.failed = False

    def __call__(self, args, *, env=None, timeout=60):
        args = list(args)
        self.calls.append((args, dict(env or {})))
        desired = (env or {}).get('APP_REVISION', OLD)
        if args[:2] == ['docker', 'pull']:
            if self.fail == 'pull':
                raise deploy.DeployError('pull failed')
            return ''
        if args[:3] == ['docker', 'image', 'inspect']:
            if 'org.opencontainers.image.revision' in args[4]:
                return OLD if self.fail == 'label' else NEW
            return NEW_IMAGE
        if args[:2] == ['docker', 'inspect']:
            return self.image + ' true'
        if 'config' in args:
            return json.dumps({'name': 'ooo', 'services': dict.fromkeys(SERVICES, {})})
        if 'ps' in args:
            return args[-1] + '-id\n'
        if 'run' in args:
            assert '--no-deps' in args
            assert args[-3:] == ['alembic', 'current', '--check-heads']
            if self.fail == 'schema':
                raise deploy.DeployError('migration needed')
            return ''
        if 'up' in args:
            assert '--no-deps' in args and '--no-build' in args
            assert args[-4:] == list(SERVICES)
            self.image = (env or {})['NEIRONYCH_IMAGE']
            self.revision = desired
            if self.fail == 'up' and desired == NEW and not self.failed:
                self.failed = True
                raise deploy.DeployError('partial start failed')
            return ''
        if 'exec' in args:
            if self.fail == 'health' and desired == NEW:
                raise deploy.DeployError('readiness failed')
            assert args[-1] == self.revision
            return ''
        raise AssertionError(args)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    root.mkdir()
    original = (
        b'# keep\nTOKEN=private-value-never-log\nNEIRONYCH_IMAGE=neironych:old\nAPP_REVISION='
        + OLD.encode() + b'\n'
    )
    (root / '.env').write_bytes(original)
    (root / '.env').chmod(0o600)
    (root / 'REVISION').write_text(OLD + '\n')
    (root / 'docker-compose.prod.yml').write_text('services: {}\n')
    (root / 'compose.host.yml').write_text('name: ooo\n')
    (root / 'releases' / NEW).mkdir(parents=True)
    (root / 'current').symlink_to('releases/' + OLD)
    fake = FakeDocker()
    monkeypatch.setattr(deploy, 'command', fake)
    monkeypatch.setattr(deploy.time, 'sleep', lambda value: None)
    return root, original, fake


def test_env_update_preserves_secrets_comments_and_crlf():
    content = b'# unchanged\r\nTOKEN="keep=verbatim"\r\n export APP_REVISION = "old"\r\nNEIRONYCH_IMAGE=old\r\n'
    result = deploy.updated_env(content, NEW_IMAGE, NEW)
    assert b'TOKEN="keep=verbatim"\r\n' in result
    assert result.count(b'APP_REVISION=') == 1
    assert (b'APP_REVISION=' + NEW.encode() + b'\r\n') in result
    assert b'# unchanged\r\n' in result


def test_env_duplicate_keys_rejected():
    with pytest.raises(deploy.DeployError):
        deploy.updated_env(b'APP_REVISION=a\nAPP_REVISION=b\n', NEW_IMAGE, NEW)


def test_host_release_preserves_topology_and_persists_revision(fixture, capsys):
    root, original, fake = fixture
    deploy.HostRelease(root, NEW, TARGET, attempts=2).execute()
    assert (root / 'REVISION').read_text().strip() == NEW
    assert (root / 'current').readlink() == Path('releases') / NEW
    assert (root / '.env').read_bytes() == deploy.updated_env(original, NEW_IMAGE, NEW)
    assert (root / '.env').stat().st_mode & 0o777 == 0o600
    assert fake.image == NEW_IMAGE
    compose_calls = [args for args, _ in fake.calls if args[:2] == ['docker', 'compose']]
    assert all(str(root / 'compose.host.yml') in args for args in compose_calls)
    assert all('--project-directory' in args for args in compose_calls)
    assert all('nginx' not in args and 'postgres' not in args for args in compose_calls)
    assert not any('upgrade' in args or 'down' in args for args in compose_calls)
    assert list((root / 'release-backups').glob('*/.env'))[0].read_bytes() == original
    assert 'private-value-never-log' not in capsys.readouterr().out


@pytest.mark.parametrize('stage', ['pull', 'label', 'schema', 'up', 'health'])
def test_failed_release_never_publishes_new_revision(fixture, stage):
    root, original, fake = fixture
    fake.fail = stage
    with pytest.raises(deploy.DeployError):
        deploy.HostRelease(root, NEW, TARGET, attempts=2).execute()
    assert (root / 'REVISION').read_text().strip() == OLD
    assert (root / '.env').read_bytes() == original
    assert (root / 'current').readlink() == Path('releases') / OLD
    assert fake.image == OLD_IMAGE
    if stage in {'up', 'health'}:
        rollbacks = [env for args, env in fake.calls if 'up' in args and env['APP_REVISION'] == OLD]
        assert rollbacks and rollbacks[-1]['NEIRONYCH_IMAGE'] == OLD_IMAGE


def test_missing_host_override_fails_without_docker_side_effects(fixture):
    root, _, fake = fixture
    (root / 'compose.host.yml').unlink()
    with pytest.raises(deploy.DeployError):
        deploy.HostRelease(root, NEW, TARGET).execute()
    assert not fake.calls


def test_idempotent_release_does_not_restart_services(fixture):
    root, _, fake = fixture
    deploy.HostRelease(root, NEW, TARGET).execute()
    fake.calls.clear()
    deploy.HostRelease(root, NEW, TARGET).execute()
    assert not any('up' in args or 'pull' in args or 'run' in args for args, _ in fake.calls)


def test_missing_runtime_process_blocks_release(fixture):
    root, _, fake = fixture
    original = fake.__call__

    def missing(args, **kwargs):
        if 'ps' in args and args[-1] == 'telegram':
            return ''
        return original(args, **kwargs)

    deploy.command = missing
    with pytest.raises(deploy.DeployError):
        deploy.HostRelease(root, NEW, TARGET).execute()
    assert not any('pull' in args or 'up' in args for args, _ in fake.calls)


def test_bad_revision_rejected_before_any_docker_call(fixture):
    root, _, fake = fixture
    with pytest.raises(deploy.DeployError):
        deploy.HostRelease(root, '../escape', TARGET).execute()
    assert not fake.calls


def test_multiple_pollers_are_rejected_before_pull(fixture, monkeypatch):
    root, _, fake = fixture
    original = fake.__call__

    def scaled(args, **kwargs):
        if 'config' in args:
            config = json.loads(original(args, **kwargs))
            config['services']['telegram']['scale'] = 2
            return json.dumps(config)
        return original(args, **kwargs)

    monkeypatch.setattr(deploy, 'command', scaled)
    with pytest.raises(deploy.DeployError):
        deploy.HostRelease(root, NEW, TARGET).execute()
    assert not any('up' in args or 'pull' in args for args, _ in fake.calls)


def test_persistence_failure_rolls_back_partial_release(fixture, monkeypatch):
    root, original, fake = fixture
    write = deploy.atomic_write
    failed = False

    def fail_once(path, content):
        nonlocal failed
        if path.name == 'REVISION' and content.strip() == NEW.encode() and not failed:
            failed = True
            raise OSError('disk error')
        return write(path, content)

    monkeypatch.setattr(deploy, 'atomic_write', fail_once)
    with pytest.raises(OSError):
        deploy.HostRelease(root, NEW, TARGET).execute()
    assert fake.image == OLD_IMAGE
    assert (root / '.env').read_bytes() == original
    assert (root / 'REVISION').read_text().strip() == OLD
    assert (root / 'current').readlink() == Path('releases') / OLD
