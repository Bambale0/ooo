"""Versioned bytes-only Unix socket contract. No URLs, paths, codecs or options."""

import asyncio
import json
import re
import struct
from fractions import Fraction

from media_probe.measurement import MAX_DURATION, Measurement, ProbeError, integer

MAGIC = b"NMP1"
HEADER = struct.Struct("!4sI32s")
LENGTH = struct.Struct("!I")
MAX_RESPONSE_BYTES = 1024
CHUNK_SIZE = 64 * 1024
DEFAULT_SOCKET = "/run/media-probe/probe.sock"
MAX_SECONDS = 45


def message_bytes(message: dict) -> bytes:
    payload = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ProbeError("resource_limit")
    return LENGTH.pack(len(payload)) + payload


async def read_message(reader: asyncio.StreamReader) -> dict:
    size = LENGTH.unpack(await reader.readexactly(LENGTH.size))[0]
    if not 1 <= size <= MAX_RESPONSE_BYTES:
        raise ProbeError("invalid_request")
    try:
        result = json.loads(await reader.readexactly(size))
    except (ValueError, RecursionError) as exc:
        raise ProbeError("invalid_request") from exc
    if not isinstance(result, dict):
        raise ProbeError("invalid_request")
    if result.get("ok") is False:
        raise ProbeError(result.get("error", "invalid_media"))
    return result


def _fraction(value: object) -> Fraction:
    if not isinstance(value, list) or len(value) != 2:
        raise ProbeError("invalid_request")
    result = Fraction(integer(value[0], minimum=1), integer(value[1], minimum=1, maximum=10**9))
    if not 0 < result <= MAX_DURATION:
        raise ProbeError("invalid_request")
    return result


def encode_measurement(result: Measurement) -> dict:
    def fraction(value: Fraction | None) -> list[int] | None:
        return [value.numerator, value.denominator] if value is not None else None

    return {
        "ok": True,
        "sha256": result.sha256,
        "duration": fraction(result.duration),
        "video_duration": fraction(result.video_duration),
        "audio_duration": fraction(result.audio_duration),
        "content_type": result.content_type,
    }


def decode_measurement(message: dict, *, expected_sha256: str) -> Measurement:
    if set(message) != {"ok", "sha256", "duration", "video_duration", "audio_duration", "content_type"}:
        raise ProbeError("invalid_request")
    if message["ok"] is not True or message["content_type"] not in {"video/mp4", "video/quicktime"}:
        raise ProbeError("invalid_request")
    digest = message["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or digest != expected_sha256:
        raise ProbeError("hash_mismatch")
    duration = _fraction(message["duration"])
    video = _fraction(message["video_duration"])
    audio = None if message["audio_duration"] is None else _fraction(message["audio_duration"])
    if duration < max(video, audio or 0):
        raise ProbeError("invalid_request")
    return Measurement(digest, duration, video, audio, message["content_type"])
