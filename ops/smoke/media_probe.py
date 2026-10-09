#!/usr/bin/env python3
"""Credential-free Docker/UDS gate using exclusively generated synthetic media.

Requires Linux Docker with cgroup v2. Builds locally and publishes nothing. This
is a runtime verification gate, not evidence of provider-billing equivalence.
"""

import argparse
import base64
import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Docker Official Images mirror; index and amd64/arm64 manifests were verified
# against docker-library/repo-info on 2026-10-09. Keep this immutable pin reviewed.
OFFICIAL_BASE = (
    "public.ecr.aws/docker/library/python:3.12-slim-trixie@"
    "sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
)

# Generated wholly inside a networkless container; native parsers on the host are
# never given any customer inputs. A short video and longer audio create the
# adversarial timing-table fixture after a deterministic host-side byte mutation.
GENERATOR = r"""
import base64, json, pathlib, subprocess
results = {}
for name, video_seconds, audio_seconds in [('five',5,5),('twenty',20,20),('long_audio',1,4)]:
    path = pathlib.Path('/tmp') / (name + '.mp4')
    subprocess.run(['/usr/bin/ffmpeg','-v','error','-nostdin','-f','lavfi','-i',
        f'color=c=black:s=64x64:r=25:d={video_seconds}','-f','lavfi','-t',str(audio_seconds),
        '-i','anullsrc=r=48000:cl=mono','-c:v','libx264','-preset','ultrafast',
        '-c:a','aac','-b:a','32k','-movflags','+faststart','-y',str(path)],
        check=True, capture_output=True, timeout=15)
    results[name] = base64.b64encode(path.read_bytes()).decode('ascii')
print(json.dumps(results))
"""
CLIENT = r"""
import asyncio, json, sys
from media_probe.client import measure_bytes
from media_probe.measurement import ProbeError
from media_probe.protocol import encode_measurement
async def main():
    try:
        result = await measure_bytes(sys.stdin.buffer.read(104857601))
        print(json.dumps(encode_measurement(result)))
    except ProbeError as exc:
        print(json.dumps({'ok':False, 'error':exc.code}))
asyncio.run(main())
"""


SANDBOX_ESCAPE_CHECK = r'''
import asyncio, json, sys
from media_probe.decoder import run_bounded
child = r"""
import errno, json, os, socket, threading
checks = {}
def denied(name, fn):
    try: fn()
    except OSError as exc:
        assert exc.errno == errno.EPERM, (name, exc.errno)
        checks[name] = True
    else: raise AssertionError(name + ' unexpectedly allowed')
def fork():
    child = os.fork()
    if child == 0: os._exit(0)
    os.waitpid(child, 0)
denied('socket', lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
denied('replace_socket_unlink', lambda: os.unlink('/run/media-probe/probe.sock'))
denied('replace_socket_rename', lambda: os.rename('/run/media-probe/probe.sock','/run/media-probe/replaced'))
denied('write_file', lambda: open('/tmp/unexpected-write','wb'))
denied('fork_helper', fork)
denied('signal_server', lambda: os.kill(os.getppid(), 0))
ran = []
thread = threading.Thread(target=lambda: ran.append(True))
thread.start(); thread.join(timeout=2)
assert ran == [True]
print(json.dumps(checks))
"""
print(asyncio.run(run_bounded([sys.executable,'-c',child], stdout_limit=1024)).decode())
'''

GROUP_CLEANUP_CHECK = r'''
import asyncio, os, pathlib, sys
import media_probe.decoder as decoder
# A trusted control fixture only: remove the child filter in this separate test
# interpreter to prove the process-group cleanup independently of fork denial.
decoder._limits = lambda: None
child = """
import os, time
pid = os.fork()
if pid:
    print(pid, flush=True)
    os._exit(0)
time.sleep(30)
"""
async def main():
    async with asyncio.timeout(5):
        output = await decoder.run_bounded([sys.executable,'-c',child], stdout_limit=1024)
        pid = int(output)
        # Docker --init reaps the killed orphan; no surviving helper or zombie.
        while pathlib.Path('/proc/' + str(pid)).exists():
            await asyncio.sleep(0.01)
    print('entire process group killed and reaped after leader exit')
asyncio.run(main())
'''


BROKER_PRIVACY_CHECK = r'''
import asyncio, pathlib, sys
from media_probe.decoder import run_bounded
broker = []
for proc in pathlib.Path('/proc').iterdir():
    if not proc.name.isdecimal(): continue
    try: command = (proc / 'cmdline').read_bytes().split(b'\0')
    except OSError: continue
    if len(command) >= 3 and command[1:3] == [b'-m', b'media_probe.server']:
        broker.append(int(proc.name))
assert len(broker) == 1, broker
child = """
import errno, os
pid = %d
for target in ('/proc/%%d/mem' %% pid, '/proc/%%d/fd/0' %% pid):
    try:
        fd = os.open(target, os.O_RDONLY)
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EPERM), (target, exc.errno)
    else:
        os.close(fd)
        raise AssertionError('Broker private data is readable')
try: os.listdir('/proc/%%d/fd' %% pid)
except OSError as exc: assert exc.errno in (errno.EACCES, errno.EPERM)
else: raise AssertionError('Broker descriptors are visible')
print('broker memory and descriptors protected')
""" % broker[0]
print(asyncio.run(run_bounded([sys.executable,'-c',child], stdout_limit=1024)).decode())
'''


RESOURCE_RECOVERY_CHECK = r"""
import asyncio, pathlib, sys
import media_probe.decoder as decoder
from media_probe.measurement import ProbeError
async def expect(code, script, limit=1024):
    try:
        await decoder.run_bounded([sys.executable,'-c',script], stdout_limit=limit)
    except ProbeError as exc:
        assert exc.code == code, exc.code
    else: raise AssertionError('Resource failure was accepted')
async def main():
    await expect('resource_limit', "print('x' * 1000000)", 64)
    await expect('decode_failed', 'bytearray(2 * 1024**3)')
    decoder.MAX_SECONDS = 0.1  # This isolated test interpreter only.
    await expect('timeout', 'while True: pass')
    decoder.MAX_SECONDS = 45
    processes = []
    create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    async def record(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        started.set()
        return process
    asyncio.create_subprocess_exec = record
    task = asyncio.create_task(decoder.run_bounded([sys.executable,'-c','import time;time.sleep(30)'],stdout_limit=64))
    await started.wait()
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    else: raise AssertionError('External cancellation was swallowed')
    assert processes[0].returncode is not None
    assert not pathlib.Path('/proc/' + str(processes[0].pid)).exists()
    print('output/memory/CPU deadline/cancellation limits verified')
asyncio.run(main())
"""

SOCKET_CANCELLATION_CHECK = r"""
import asyncio, pathlib, sys
from media_probe.client import measure_bytes
from media_probe.measurement import ProbeError

def native_children():
    found = []
    for proc in pathlib.Path('/proc').iterdir():
        if not proc.name.isdecimal(): continue
        try: command = (proc / 'cmdline').read_bytes().split(b'\0')[0]
        except OSError: continue
        if command in (b'/usr/bin/ffprobe',b'/usr/bin/ffmpeg'): found.append(proc)
    return found

async def clean():
    async with asyncio.timeout(3):
        while native_children() or list(pathlib.Path('/tmp/media-probe').iterdir()):
            await asyncio.sleep(0.005)

async def main():
    data = sys.stdin.buffer.read(104857601)
    task = asyncio.create_task(measure_bytes(data))
    async with asyncio.timeout(3):
        while not native_children():
            assert not task.done(), 'Decode completed before cancellation boundary was observed'
            await asyncio.sleep(0.002)
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    else: raise AssertionError('Live UDS cancellation was swallowed')
    await clean()
    await asyncio.sleep(0.15)
    try: await measure_bytes(data, timeout_seconds=0.001)
    except ProbeError as exc: assert exc.code == 'timeout'
    else: raise AssertionError('Tiny client deadline was ignored')
    await clean()
    # Let the closing connection release its admission slot before retrying.
    await asyncio.sleep(0.15)
    assert (await measure_bytes(data)).duration >= 20
    print('live UDS cancellation/deadline cleanup and successful retry verified')
asyncio.run(main())
"""


def run(args, *, data=None, timeout=60, check=True):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout, cwd=ROOT)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({args[0]}): {result.stderr[-4096:].decode(errors='replace')}")
    return result


def hardened():
    return [
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--memory-swap",
        "2g",
        "--pids-limit",
        "64",
        "--ipc",
        "private",
        "--user",
        "10001:10001",
        "--init",
        "--ulimit",
        "core=0",
        "--ulimit",
        "nofile=128:128",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=384m,uid=10001,gid=10001,mode=0700",
    ]


def child_boxes(data, start, end, wanted):
    result = []
    while start < end:
        size = int.from_bytes(data[start : start + 4], "big")
        if not 8 <= size <= end - start:
            raise AssertionError("Invalid generated fixture structure")
        if data[start + 4 : start + 8] == wanted:
            result.append((start + 8, start + size))
        start += size
    return result


def compress_audio_timing(original):
    """Make all declared timing 1 s while retaining 4 s of encoded AAC frames."""
    data = bytearray(original)
    moov, end = child_boxes(data, 0, len(data), b"moov")[0]

    def divide(offset):
        value = int.from_bytes(data[offset : offset + 4], "big")
        data[offset : offset + 4] = (value // 4).to_bytes(4, "big")

    mvhd, _ = child_boxes(data, moov, end, b"mvhd")[0]
    divide(mvhd + 16)
    for trak, trak_end in child_boxes(data, moov, end, b"trak"):
        mdia, mdia_end = child_boxes(data, trak, trak_end, b"mdia")[0]
        hdlr, _ = child_boxes(data, mdia, mdia_end, b"hdlr")[0]
        if data[hdlr + 8 : hdlr + 12] != b"soun":
            continue
        tkhd, _ = child_boxes(data, trak, trak_end, b"tkhd")[0]
        divide(tkhd + 20)
        mdhd, _ = child_boxes(data, mdia, mdia_end, b"mdhd")[0]
        divide(mdhd + 16)
        edts, edts_end = child_boxes(data, trak, trak_end, b"edts")[0]
        elst, _ = child_boxes(data, edts, edts_end, b"elst")[0]
        divide(elst + 8)
        divide(elst + 12)
        minf, minf_end = child_boxes(data, mdia, mdia_end, b"minf")[0]
        stbl, stbl_end = child_boxes(data, minf, minf_end, b"stbl")[0]
        stts, _ = child_boxes(data, stbl, stbl_end, b"stts")[0]
        count = int.from_bytes(data[stts + 4 : stts + 8], "big")
        for entry in range(count):
            divide(stts + 12 + entry * 8)
    return bytes(data)


def inspect_hardening(container, volume):
    inspected = json.loads(run(["docker", "inspect", container]).stdout)[0]
    host, config = inspected["HostConfig"], inspected["Config"]
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] and not host["Privileged"]
    assert host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert not any("seccomp" in option for option in host["SecurityOpt"]), "Must retain Docker default seccomp"
    assert host["Memory"] == host["MemorySwap"] == 2 * 1024**3
    assert host["NanoCpus"] == 2_000_000_000 and host["PidsLimit"] == 64
    assert not host.get("PortBindings") and not host.get("Binds") and not host.get("Devices")
    assert not host.get("PidMode") and host["IpcMode"] == "private"
    assert config["User"] == "10001:10001" and host["Init"] is True
    mounts = [entry for entry in inspected["Mounts"] if entry["Type"] != "tmpfs"]
    assert all(entry["Destination"] == "/tmp" for entry in inspected["Mounts"] if entry["Type"] == "tmpfs")
    assert len(mounts) == 1 and mounts[0]["Name"] == volume
    assert mounts[0]["Destination"] == "/run/media-probe" and mounts[0]["Type"] == "volume"
    # This also checks actual no-new-privileges/capabilities/seccomp, interfaces,
    # root mount and tmpfs, and cgroup v2 values from inside the running container.
    run(
        [
            "docker",
            "exec",
            container,
            "python",
            "-c",
            "from media_probe.isolation import require_isolation; require_isolation()",
        ]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Existing locally built checker image; omit to resolve and build")
    args = parser.parse_args()
    suffix = uuid.uuid4().hex[:12]
    image = args.image or f"neironych-media-probe-ci:{suffix}"
    checker, unsafe, volume = f"probe-check-{suffix}", f"probe-unsafe-{suffix}", f"probe-ipc-{suffix}"
    if not args.image:
        metadata = run(["docker", "buildx", "imagetools", "inspect", OFFICIAL_BASE], timeout=120).stdout
        match = re.search(rb"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", metadata, re.MULTILINE)
        if not match or match.group(1).decode("ascii") != OFFICIAL_BASE.rsplit("@", 1)[1]:
            raise RuntimeError("Official Python manifest digest did not match the reviewed pin")
        base = OFFICIAL_BASE
        print(f"Resolved official immutable Python base: {base}", flush=True)
        run(
            [
                "docker",
                "build",
                "--pull",
                "--build-arg",
                f"MEDIA_PROBE_BASE={base}",
                "-f",
                "Dockerfile.media-probe",
                "-t",
                image,
                ".",
            ],
            timeout=480,
        )
    try:
        run(
            [
                "docker",
                "volume",
                "create",
                "--driver",
                "local",
                "--opt",
                "type=tmpfs",
                "--opt",
                "device=tmpfs",
                "--opt",
                "o=size=1m,uid=10001,gid=10001,mode=0770,noexec,nosuid,nodev",
                volume,
            ]
        )
        mount = ["--mount", f"type=volume,source={volume},target=/run/media-probe"]
        run(["docker", "run", "-d", "--name", checker, *hardened(), *mount, image])
        deadline = time.monotonic() + 15
        while True:
            state = json.loads(run(["docker", "inspect", checker]).stdout)[0]["State"]
            if not state["Running"]:
                log_result = run(["docker", "logs", checker])
                logs = (log_result.stdout + log_result.stderr)[-4096:]
                raise RuntimeError(f"Checker failed to start: {logs.decode(errors='replace')}")
            ready = run(
                [
                    "docker",
                    "exec",
                    checker,
                    "python",
                    "-c",
                    "from pathlib import Path; assert Path('/run/media-probe/probe.sock').is_socket()",
                ],
                check=False,
            )
            if not ready.returncode:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Checker socket did not become ready")
            time.sleep(0.2)
        inspect_hardening(checker, volume)
        run(["docker", "exec", checker, "python", "-c", BROKER_PRIVACY_CHECK], timeout=10)
        run(["docker", "exec", checker, "python", "-c", SANDBOX_ESCAPE_CHECK], timeout=10)
        run(["docker", "exec", checker, "python", "-c", GROUP_CLEANUP_CHECK], timeout=10)
        print("Verified native child cannot replace socket or fork; orphan cleanup passed", flush=True)
        generated = run(
            ["docker", "run", "--rm", *hardened(), "--entrypoint", "python", image, "-c", GENERATOR], timeout=60
        ).stdout
        fixtures = {name: base64.b64decode(data, validate=True) for name, data in json.loads(generated).items()}
        run(["docker", "exec", checker, "python", "-c", RESOURCE_RECOVERY_CHECK], timeout=15)
        run(
            ["docker", "exec", "-i", checker, "python", "-c", SOCKET_CANCELLATION_CHECK],
            data=fixtures["twenty"],
            timeout=15,
        )

        def measure(data):
            response = run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-i",
                    *hardened(),
                    "--mount",
                    f"type=volume,source={volume},target=/run/media-probe,readonly",
                    "--entrypoint",
                    "python",
                    image,
                    "-c",
                    CLIENT,
                ],
                data=data,
                timeout=55,
            ).stdout
            assert len(response) <= 1024
            return json.loads(response)

        for name, seconds in (("five", 5), ("twenty", 20)):
            result = measure(fixtures[name])
            assert result["ok"] is True, result
            assert result["sha256"] == hashlib.sha256(fixtures[name]).hexdigest()
            numerator, denominator = result["duration"]
            assert seconds * denominator <= numerator < (seconds + 1) * denominator
            assert result["audio_duration"] is not None
            print(f"Verified real UDS + full decode: {seconds}s synthetic H.264/AAC", flush=True)
        result = measure(compress_audio_timing(fixtures["long_audio"]))
        assert result == {"ok": False, "error": "inconsistent_timing"}, result
        assert measure(fixtures["five"][:-100])["ok"] is False
        assert measure(b"#EXTM3U\nhttps://169.254.169.254/\n")["ok"] is False
        # A subsequent valid job proves rejection did not poison the single slot.
        assert measure(fixtures["five"])["ok"] is True
        run(
            [
                "docker",
                "exec",
                checker,
                "python",
                "-c",
                "from pathlib import Path; assert not list(Path('/tmp/media-probe').iterdir())",
            ]
        )
        unsafe_options = hardened()
        unsafe_options[unsafe_options.index("--user") + 1] = "0:0"
        failure = run(["docker", "run", "--name", unsafe, *unsafe_options, *mount, image], timeout=10, check=False)
        assert failure.returncode != 0 and b"Required media-probe isolation is missing" in failure.stderr
        print("Verified malformed media rejection, cleanup, and unsafe-startup refusal", flush=True)
    finally:
        for name in (checker, unsafe):
            run(["docker", "rm", "-f", name], check=False)
        run(["docker", "volume", "rm", volume], check=False)
        if not args.image:
            run(["docker", "image", "rm", image], check=False)


if __name__ == "__main__":
    main()
