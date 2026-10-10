"""Synthetic child checks only; real socket attempts are reserved for Docker CI."""

import json
import sys

import pytest

from media_probe.decoder import run_bounded
from media_probe.seccomp import DENIED, install_decoder_filter

CHECK_FILES_AND_PROCESSES = r"""
import errno, json, os, threading
result = {}
def denied(name, function):
    try:
        function()
    except OSError as exc:
        assert exc.errno == errno.EPERM, (name, exc.errno)
        result[name] = True
    else:
        raise AssertionError(name + ' unexpectedly allowed')
def fork():
    child = os.fork()
    if child == 0: os._exit(0)
    os.waitpid(child, 0)
denied('create', lambda: open('/tmp/probe-must-not-create', 'wb'))
denied('unlink', lambda: os.unlink('/tmp/probe-must-not-unlink'))
denied('rename', lambda: os.rename('/tmp/a', '/tmp/b'))
denied('chmod', lambda: os.chmod('/tmp', 0o700))
denied('fork', fork)
denied('setpgid', lambda: os.setpgid(0, 0))
denied('signal_other_process', lambda: os.kill(os.getppid(), 0))
with open('/etc/os-release', 'rb') as file: assert file.read(10)
ran = []
thread = threading.Thread(target=lambda: ran.append(True))
thread.start(); thread.join(timeout=2)
assert ran == [True]
result['read_only_open_and_threads'] = True
print(json.dumps(result))
"""


async def test_native_child_cannot_write_mutate_fork_or_signal_others():
    result = json.loads(await run_bounded([sys.executable, "-c", CHECK_FILES_AND_PROCESSES], stdout_limit=1024))
    assert set(result) == {
        "create",
        "unlink",
        "rename",
        "chmod",
        "fork",
        "setpgid",
        "signal_other_process",
        "read_only_open_and_threads",
    }
    assert all(result.values())


def test_missing_libseccomp_fails_closed(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("Library unavailable")

    monkeypatch.setattr("media_probe.seccomp.ctypes.CDLL", unavailable)
    with pytest.raises(OSError):
        install_decoder_filter()


def test_policy_explicitly_denies_cross_job_escape_mechanisms():
    assert {
        "socket",
        "socketpair",
        "connect",
        "bind",
        "unlink",
        "rename",
        "ptrace",
        "process_vm_writev",
        "pidfd_getfd",
        "fork",
        "vfork",
        "setsid",
        "setpgid",
        "io_uring_setup",
        "ioctl",
    } <= set(DENIED)
