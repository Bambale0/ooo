"""Native parsers run exclusively in the credential-free isolated service.

Host-side tests may invoke this module on locally generated synthetic fixtures.
The application must import the socket client, never this module.
"""

import asyncio
import hashlib
import json
import os
import re
import resource
import signal
import stat
from pathlib import Path

from media_probe.container_policy import inspect_edit_policy
from media_probe.measurement import (
    MAX_INPUT_BYTES,
    MAX_PIXELS,
    Measurement,
    ProbeError,
    integer,
    require,
    streams_from,
    validate_observation,
)
from media_probe.seccomp import install_decoder_filter

MAX_PROBE_OUTPUT = 8 * 1024 * 1024
MAX_METADATA_OUTPUT = 32 * 1024
MAX_STDERR = 64 * 1024
MAX_SECONDS = 45
CHUNK_SIZE = 64 * 1024
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/nonexistent"}
ENTRIES = (
    "packet=stream_index,pts,dts,duration,size,flags:"
    "packet_side_data=side_data_type,skip_samples,discard_padding:"
    "frame=media_type,stream_index,pts,duration,nb_samples,width,height:frame_side_data=:"
    "stream=index,codec_type,codec_name,profile,pix_fmt,time_base,avg_frame_rate,r_frame_rate,"
    "start_pts,duration_ts,nb_frames,sample_rate,channels,width,height:stream_disposition=attached_pic:"
    "stream_tags=:format=nb_streams,duration,start_time,format_name,size:format_tags="
)


def _limits() -> None:
    """Supplement, never replace, the container's cgroup and filesystem limits."""
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    install_decoder_filter()


async def _read_bounded(stream: asyncio.StreamReader, maximum: int) -> bytes:
    result = bytearray()
    while chunk := await stream.read(CHUNK_SIZE):
        if len(result) + len(chunk) > maximum:
            raise ProbeError("resource_limit")
        result.extend(chunk)
    return bytes(result)


async def _discard_pipe(stream: asyncio.StreamReader) -> None:
    # Only used after SIGKILL: finite buffered bytes, never accumulated in memory.
    # Reading resumes a backpressured transport so Process.wait() can finish.
    while await stream.read(CHUNK_SIZE):
        pass


async def run_bounded(argv: list[str], *, stdout_limit: int, stderr_metadata: bool = False) -> bytes:
    """No shell, bounded pipe readers, scrubbed environment and reliable reaping."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=SAFE_ENV,
        close_fds=True,
        start_new_session=True,
        preexec_fn=_limits,
        limit=CHUNK_SIZE,
    )
    assert process.stdout is not None and process.stderr is not None

    def kill_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def reap_group() -> int:
        # Process.wait() can itself await pipe closure on asyncio transports;
        # poll the OS-reaped returncode so an inherited pipe cannot hide exit.
        while process.returncode is None:  # noqa: ASYNC110 - no public pre-pipe-EOF exit event
            await asyncio.sleep(0.01)
        returncode = process.returncode
        # Do this even after a successful leader exit, before waiting for pipe
        # EOF: a stray helper could otherwise keep pipes/the job alive forever.
        kill_group()
        return returncode

    pending = [
        asyncio.create_task(_read_bounded(process.stdout, stdout_limit)),
        asyncio.create_task(_read_bounded(process.stderr, 1024 * 1024 if stderr_metadata else MAX_STDERR)),
        asyncio.create_task(reap_group()),
    ]
    try:
        async with asyncio.timeout(MAX_SECONDS):
            output, errors, returncode = await asyncio.gather(*pending)
        if returncode != 0 or (errors and not stderr_metadata):
            raise ProbeError("decode_failed")
        return errors if stderr_metadata else output
    except TimeoutError as exc:
        raise ProbeError("timeout") from exc
    finally:
        async def cleanup() -> None:
            kill_group()
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # Cancelled capped readers may leave asyncio's pipe transport paused.
            # Reap AND drain both streams after killing the whole process group.
            # communicate() would accumulate output and break the memory bound.
            await asyncio.gather(_discard_pipe(process.stdout), _discard_pipe(process.stderr), process.wait())

        cleaning = asyncio.create_task(cleanup(), name="media-probe-process-cleanup")
        interrupted = False
        while not cleaning.done():
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                # A second cancellation must not detach cleanup or strand pipes.
                interrupted = True
        await cleaning
        if interrupted:
            raise asyncio.CancelledError


def input_options(*, audio: bool = True) -> list[str]:
    # All options are constants. There is no URL, protocol, path or FFmpeg option
    # field in the client protocol. External MOV references remain disabled.
    return [
        "-v",
        "error",
        "-threads",
        "1",
        "-err_detect",
        "explode",
        "-max_alloc",
        str(64 * 1024 * 1024),
        "-max_pixels",
        str(MAX_PIXELS),
        *(["-max_samples", "1048576"] if audio else []),
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        "mov",
        "-codec_whitelist",
        "h264,aac",
        "-enable_drefs",
        "0",
        "-use_absolute_path",
        "0",
        "-f",
        "mov",
    ]


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def sniff_content_type(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(16)
    if len(header) != 16 or header[4:8] != b"ftyp":
        raise ProbeError("unsupported_media")
    length = int.from_bytes(header[:4])
    if not 16 <= length <= 4096 or length % 4:
        raise ProbeError("unsupported_media")
    brand = header[8:12]
    if brand not in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"qt  "}:
        raise ProbeError("unsupported_media")
    return "video/quicktime" if brand == b"qt  " else "video/mp4"


def _document(data: bytes) -> dict:
    try:
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ProbeError()
        return result
    except (ValueError, RecursionError) as exc:
        raise ProbeError() from exc


async def decode_file(path: Path, *, expected_sha256: str) -> Measurement:
    """Return an unchanged-file local estimate only after two complete decodes.

    FFprobe decodes every packet/frame with edit lists disabled and emits bounded
    timing/sample evidence. FFmpeg independently completes an error-fatal decode
    to a null sink. No encoded media is produced, saved, replaced or uploaded.
    """
    before = path.lstat()  # noqa: ASYNC240 - bounded local private tmpfs, single-job service
    if not stat.S_ISREG(before.st_mode) or not 16 <= before.st_size <= MAX_INPUT_BYTES:
        raise ProbeError("resource_limit")
    content_type = sniff_content_type(path)
    edits = inspect_edit_policy(path)
    if sha256_file(path) != expected_sha256:
        raise ProbeError("hash_mismatch")
    try:
        async with asyncio.timeout(MAX_SECONDS):
            metadata = _document(
                await run_bounded(
                    [
                        "/usr/bin/ffprobe",
                        *input_options(),
                        "-show_streams",
                        "-show_format",
                        "-show_entries",
                        ENTRIES[ENTRIES.index("stream=index") :],
                        "-of",
                        "json",
                        "-i",
                        str(path),
                    ],
                    stdout_limit=MAX_METADATA_OUTPUT,
                )
            )
            streams_from(metadata, size_bytes=before.st_size)
            decoded = _document(
                await run_bounded(
                    [
                        "/usr/bin/ffprobe",
                        *input_options(),
                        "-ignore_editlist",
                        "1",
                        "-show_streams",
                        "-show_format",
                        "-show_packets",
                        "-show_frames",
                        "-show_entries",
                        ENTRIES,
                        "-of",
                        "json",
                        "-i",
                        str(path),
                    ],
                    stdout_limit=MAX_PROBE_OUTPUT,
                )
            )
            require(len(edits) == len(decoded["streams"]), "unsupported_media")
            for edit, raw_stream, normal_stream in zip(edits, decoded["streams"], metadata["streams"], strict=True):
                if raw_stream["codec_type"] == "video":
                    require((edit or 0) == integer(raw_stream["start_pts"]))
                else:
                    require((edit or 0) == integer(raw_stream["duration_ts"]) - integer(normal_stream["duration_ts"]))
            result = validate_observation(metadata, decoded, sha256=expected_sha256, size_bytes=before.st_size)
            audio_log = await run_bounded(
                [
                    "/usr/bin/ffmpeg",
                    "-hide_banner",
                    "-nostdin",
                    "-nostats",
                    "-copyts",
                    "-xerror",
                    *input_options(audio=result.audio_duration is not None),
                    "-loglevel",
                    "repeat+level+info",
                    "-ignore_editlist",
                    "1",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-threads",
                    "1",
                    "-filter_threads",
                    "1",
                    "-filter_complex_threads",
                    "1",
                    "-map_metadata",
                    "-1",
                    "-af",
                    "ashowinfo",
                    "-c:v",
                    "wrapped_avframe",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "null",
                    "-",
                ],
                stdout_limit=1024,
                stderr_metadata=True,
            )
            verify_audio_decode_log(audio_log, decoded)
    except TimeoutError as exc:
        raise ProbeError("timeout") from exc
    after = path.lstat()  # noqa: ASYNC240 - bounded local private tmpfs, single-job service
    if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
        raise ProbeError("hash_mismatch")
    if sha256_file(path) != expected_sha256:
        raise ProbeError("hash_mismatch")
    return Measurement(result.sha256, result.duration, result.video_duration, result.audio_duration, content_type)


def verify_audio_decode_log(log: bytes, decoded: dict) -> None:
    """Cross-check actual decoded sample rate, not merely the MP4 declaration.

    FFprobe frame JSON omits AVFrame.sample_rate. The identity ashowinfo filter
    exposes it during the independent full decode; rate/channel changes or a
    filter reset fail closed. Logs are memory-bounded and are never returned.
    """
    require(not re.search(rb"\[(?:warning|error|fatal|panic)\]", log), "decode_failed")
    pattern = re.compile(
        rb"n:([0-9]+) pts:(-?[0-9]+) pts_time:[^ ]+ fmt:fltp "
        rb"channels:([0-9]+) chlayout:[^ ]+ rate:([0-9]+) nb_samples:([0-9]+) ",
    )
    matches = pattern.findall(log)
    audio = [stream for stream in decoded["streams"] if stream["codec_type"] == "audio"]
    if not audio:
        require(not matches)
        return
    stream = audio[0]
    frames = [
        row
        for row in decoded["packets_and_frames"]
        if row["type"] == "frame" and row["stream_index"] == stream["index"]
    ]
    require(len(matches) == len(frames))
    for index, (match, frame) in enumerate(zip(matches, frames, strict=True)):
        n, pts, channels, rate, samples = (int(value) for value in match)
        require(n == index and pts == integer(frame["pts"]))
        require(channels == integer(stream["channels"]) and rate == integer(stream["sample_rate"]))
        require(samples == integer(frame["nb_samples"]))
