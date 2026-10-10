"""Fail closed before listening if required Linux containment is missing.

Deployment must additionally verify Docker's default seccomp, image digest and
absence of secret/host mounts. These checks are defense-in-depth, not a sandbox.
"""

import os
from pathlib import Path

ALLOWED_ENV = frozenset(
    {
        "PATH",
        "HOSTNAME",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        # Public metadata inherited from the official Python image; not application secrets.
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "GPG_KEY",
    }
)


def _require(condition: bool) -> None:
    if not condition:
        raise RuntimeError("Required media-probe isolation is missing")


def check_snapshot(
    *, uid: int, environment: set[str], status: str, mounts: str, interfaces: set[str], memory: str, pids: str, cpu: str
) -> None:
    """Pure checking logic so unsafe configurations can be tested without Docker."""
    _require(uid > 0 and environment <= ALLOWED_ENV)
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    _require(fields.get("NoNewPrivs", "").strip() == "1")
    _require(fields.get("Seccomp", "").strip() == "2")
    _require(all(int(fields.get(field, "1").strip(), 16) == 0 for field in ("CapEff", "CapPrm", "CapBnd")))
    _require(interfaces <= {"lo"})
    mounted = {}
    for line in mounts.splitlines():
        before, after = line.split(" - ", 1)
        parts = before.split()
        mounted[parts[4]] = (set(parts[5].split(",")), after.split()[0], set(after.split()[2].split(",")))
    _require("/" in mounted and "ro" in mounted["/"][0])
    _require("/tmp" in mounted)
    options, fs_type, super_options = mounted["/tmp"]
    _require(fs_type == "tmpfs" and {"nosuid", "nodev", "noexec"} <= options)
    sizes = [option[5:] for option in super_options if option.startswith("size=")]
    _require(len(sizes) == 1)
    size_text = sizes[0]
    multiplier = {"k": 1024, "m": 1024**2, "g": 1024**3}
    size = int(size_text[:-1]) * multiplier[size_text[-1]] if size_text[-1] in multiplier else int(size_text)
    _require(100 * 1024**2 <= size <= 384 * 1024**2)
    _require(memory.strip().isdigit() and 0 < int(memory) <= 2 * 1024**3)
    _require(pids.strip().isdigit() and 0 < int(pids) <= 64)
    quota, period = cpu.split()
    _require(quota.isdigit() and period.isdigit() and 0 < int(quota) <= 2 * int(period))


def require_isolation() -> None:
    try:
        cgroup = Path("/sys/fs/cgroup")
        check_snapshot(
            uid=os.geteuid(),
            environment=set(os.environ),
            status=Path("/proc/self/status").read_text(),
            mounts=Path("/proc/self/mountinfo").read_text(),
            interfaces={path.name for path in Path("/sys/class/net").iterdir()},
            memory=(cgroup / "memory.max").read_text(),
            pids=(cgroup / "pids.max").read_text(),
            cpu=(cgroup / "cpu.max").read_text(),
        )
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise RuntimeError("Required media-probe isolation is missing") from exc


def protect_broker_memory() -> None:
    """Deny same-UID /proc memory/fd access before accepting any input bytes."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    # Documented Linux prctl API constants: PR_SET_DUMPABLE and PR_GET_DUMPABLE.
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
        raise RuntimeError("Media-probe broker privacy protection unavailable")
