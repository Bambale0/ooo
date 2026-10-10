import copy
from pathlib import Path

import pytest

from media_probe.isolation import check_snapshot


@pytest.fixture
def safe_snapshot():
    return {
        "uid": 10001,
        "environment": {"PATH", "LANG", "HOME"},
        "status": "NoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0\nCapPrm:\t0\nCapBnd:\t0\n",
        "mounts": "1 0 0:1 / / ro - overlay overlay ro\n"
        "2 1 0:2 / /tmp rw,nosuid,nodev,noexec - tmpfs tmpfs rw,size=393216k\n",
        "interfaces": {"lo"},
        "memory": str(2 * 1024**3),
        "pids": "64",
        "cpu": "200000 100000",
    }


def test_required_isolation_snapshot_is_accepted(safe_snapshot):
    check_snapshot(**safe_snapshot)


@pytest.mark.parametrize(
    "field,value",
    [
        ("uid", 0),
        ("environment", {"PATH", "DATABASE_URL"}),
        ("interfaces", {"lo", "eth0"}),
        ("memory", "max"),
        ("memory", str(3 * 1024**3)),
        ("pids", "65"),
        ("cpu", "max 100000"),
        ("cpu", "300000 100000"),
        ("status", "NoNewPrivs: 0\nSeccomp: 0\n"),
        ("mounts", "1 0 0:1 / / rw - overlay overlay rw\n"),
    ],
)
def test_missing_runtime_containment_fails_closed(safe_snapshot, field, value):
    invalid = copy.deepcopy(safe_snapshot)
    invalid[field] = value
    with pytest.raises(RuntimeError):
        check_snapshot(**invalid)


def test_media_probe_overlay_does_not_inherit_application_secrets():
    root = Path(__file__).parents[1]
    text = (root / "docker-compose.media-probe.yml").read_text()
    service = text.split("  media_probe:\n", 1)[1].split("\nvolumes:", 1)[0]
    lines = [line.split("#", 1)[0] for line in service.splitlines()]
    executable = "\n".join(lines)
    for forbidden in ("env_file:", "environment:", "<<:", "ports:", "networks:", "privileged: true", "secrets:"):
        assert forbidden not in executable
    assert "network_mode: none" in executable
    assert "cap_drop: [ALL]" in executable
    assert "pids_limit: 64" in executable
    assert "mem_limit: 2g" in executable
    assert "size=384m" in executable
    dockerfile = (root / "Dockerfile.media-probe").read_text()
    assert "COPY app" not in dockerfile and "COPY . " not in dockerfile
    assert "USER 10001:10001" in dockerfile


def test_broker_privacy_guard_fails_closed(monkeypatch):
    from media_probe.isolation import protect_broker_memory

    class RejectedPrctl:
        argtypes = None
        restype = None

        def __call__(self, *args):
            return -1

    class Library:
        prctl = RejectedPrctl()

    monkeypatch.setattr("ctypes.CDLL", lambda *args, **kwargs: Library())
    with pytest.raises(RuntimeError, match="privacy protection unavailable"):
        protect_broker_memory()
