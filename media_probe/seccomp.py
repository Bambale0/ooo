"""Per-decoder confinement through libseccomp's audited, architecture-aware API.

This is additive to Docker's default profile. No syscall numbers are maintained
here. Unknown required syscall names or unavailable libseccomp fail closed.
"""

import ctypes
import errno
import fcntl
import os
import platform

# Stable public libseccomp API constants, not architecture-specific syscall IDs.
SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = 0x00050000
SCMP_CMP_NE = 1
SCMP_CMP_MASKED_EQ = 7


class ArgCompare(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


DENIED = (
    "socket socketpair connect bind listen accept accept4 sendto sendmsg sendmmsg "
    "recvfrom recvmsg recvmmsg shutdown getsockname getpeername getsockopt setsockopt socketcall "
    "fork vfork setsid setpgid ptrace process_vm_readv process_vm_writev pidfd_open pidfd_getfd "
    "pidfd_send_signal kill tkill rt_sigqueueinfo rt_tgsigqueueinfo kcmp process_madvise process_mrelease "
    "setpriority sched_setscheduler sched_setparam sched_setattr sched_setaffinity "
    "creat unlink unlinkat rename renameat renameat2 link linkat symlink symlinkat "
    "mkdir mkdirat rmdir chmod fchmod fchmodat fchmodat2 chown fchown lchown fchownat "
    "truncate ftruncate utime utimes futimesat utimensat mknod mknodat "
    "setxattr lsetxattr fsetxattr removexattr lremovexattr fremovexattr "
    "mount umount2 pivot_root chroot fsopen fsconfig fsmount fspick open_tree move_mount mount_setattr "
    "quotactl quotactl_fd open_by_handle_at name_to_handle_at "
    "io_uring_setup io_uring_enter io_uring_register ioctl bpf perf_event_open "
    "unshare setns keyctl add_key request_key userfaultfd fanotify_init fanotify_mark "
    "shmget shmat shmctl shmdt semget semop semtimedop semctl msgget msgsnd msgrcv msgctl "
    "mq_open mq_unlink mq_timedsend mq_timedreceive mq_notify mq_getsetattr"
).split()


def install_decoder_filter() -> None:
    # clone's argument ordering is platform-specific; these are the explicitly
    # reviewed deployment architectures. Other architectures must fail closed.
    if platform.machine() not in {"x86_64", "aarch64"}:
        raise RuntimeError("Unsupported decoder sandbox architecture")
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(ArgCompare),
    ]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    context = lib.seccomp_init(SCMP_ACT_ALLOW)
    if not context:
        raise RuntimeError("Decoder sandbox unavailable")

    def deny(name, *comparisons, error=errno.EPERM):
        syscall = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
        if syscall == -1:
            raise RuntimeError("Required decoder sandbox syscall is unknown")
        if syscall < -1:
            # libseccomp pseudo-syscall: no native syscall exists on this ABI.
            return
        array = (ArgCompare * len(comparisons))(*comparisons)
        if lib.seccomp_rule_add_array(context, SCMP_ACT_ERRNO | error, syscall, len(array), array) != 0:
            raise RuntimeError("Decoder sandbox rule rejected")

    try:
        for name in DENIED:
            deny(name)
        # clone3 has an indirect argument struct, so deny and let glibc's normal
        # pthread implementation fall back to the inspectable clone syscall.
        deny("clone3", error=errno.ENOSYS)
        deny("openat2", error=errno.ENOSYS)
        for name, flag_arg in (("open", 1), ("openat", 2)):
            for mode in (os.O_WRONLY, os.O_RDWR):
                deny(name, ArgCompare(flag_arg, SCMP_CMP_MASKED_EQ, os.O_ACCMODE, mode))
            for flag in (os.O_CREAT, os.O_TRUNC, os.O_APPEND, os.O_TMPFILE):
                deny(name, ArgCompare(flag_arg, SCMP_CMP_MASKED_EQ, flag, flag))
        # Only threads sharing VM, signal handlers and thread group are allowed.
        for bit in (0x00000100, 0x00000800, 0x00010000):  # CLONE_VM, SIGHAND, THREAD
            deny("clone", ArgCompare(0, SCMP_CMP_MASKED_EQ, bit, 0))
        # No exit signal, PID fd, ptrace, vfork, parent change or new namespace.
        forbidden_clone = 0xFF | 0x1000 | 0x2000 | 0x4000 | 0x8000 | 0x20000 | 0x800000 | 0x7E000000
        for bit in (1 << index for index in range(32)):
            if forbidden_clone & bit:
                deny("clone", ArgCompare(0, SCMP_CMP_MASKED_EQ, bit, bit))
        # pthread cancellation/abort may signal own threads, never another TGID.
        deny("tgkill", ArgCompare(0, SCMP_CMP_NE, os.getpid(), 0))
        # Prevent asynchronous-file signal ownership from targeting another process.
        for command in (fcntl.F_SETOWN, fcntl.F_SETSIG, 15):  # F_SETOWN_EX is Linux UAPI 15.
            deny("fcntl", ArgCompare(1, SCMP_CMP_MASKED_EQ, 0xFFFFFFFF, command))
        deny("prlimit64", ArgCompare(0, SCMP_CMP_NE, 0, 0))
        # libseccomp sets no_new_privs before loading its unprivileged filter.
        if lib.seccomp_load(context) != 0:
            raise RuntimeError("Decoder sandbox could not be installed")
    finally:
        lib.seccomp_release(context)
