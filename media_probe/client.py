"""Application-side streaming client. This module never invokes a native parser."""

import asyncio
import io
import math
import re
from typing import BinaryIO

from media_probe.measurement import MAX_INPUT_BYTES, Measurement, ProbeError
from media_probe.protocol import (
    CHUNK_SIZE,
    DEFAULT_SOCKET,
    HEADER,
    MAGIC,
    MAX_SECONDS,
    decode_measurement,
    read_message,
)


async def measure_file(
    file: BinaryIO,
    *,
    size_bytes: int,
    sha256: str,
    socket_path: str = DEFAULT_SOCKET,
    timeout_seconds: float = MAX_SECONDS,
) -> Measurement:
    """Send a seekable, read-only staged file; rewind it on success or failure.

    The caller supplies the remaining fetch/check/upload deadline, pins the exact
    subsequently uploaded bytes, and retains ownership of the input file.
    """
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or not 16 <= size_bytes <= MAX_INPUT_BYTES:
        raise ProbeError("resource_limit")
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise ProbeError("invalid_request")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_SECONDS:
        raise ProbeError("timeout")
    writer = None
    try:
        file.seek(0)
        async with asyncio.timeout(timeout_seconds):
            reader, writer = await asyncio.open_unix_connection(socket_path, limit=CHUNK_SIZE)
            writer.write(HEADER.pack(MAGIC, size_bytes, bytes.fromhex(sha256)))
            await writer.drain()
            if await read_message(reader) != {"ready": True}:
                raise ProbeError("invalid_request")
            remaining = size_bytes
            while remaining:
                data = file.read(min(CHUNK_SIZE, remaining))
                if not isinstance(data, bytes) or not data or len(data) > remaining:
                    raise ProbeError("invalid_request")
                remaining -= len(data)
                writer.write(data)
                await writer.drain()
            if file.read(1):
                raise ProbeError("invalid_request")
            return decode_measurement(await read_message(reader), expected_sha256=sha256)
    except TimeoutError as exc:
        raise ProbeError("timeout") from exc
    except (OSError, EOFError, asyncio.IncompleteReadError) as exc:
        raise ProbeError("unavailable") from exc
    finally:
        if writer is not None:
            # One request per connection; all body writes have drained and a
            # successful response has been read. Abort synchronously rather
            # than letting a stalled close extend the caller's total deadline.
            writer.close()
            writer.transport.abort()
        file.seek(0)


async def measure_bytes(
    data: bytes,
    *,
    socket_path: str = DEFAULT_SOCKET,
    timeout_seconds: float = MAX_SECONDS,
) -> Measurement:
    """Convenience for small fixtures; production callers should stream files."""
    import hashlib

    return await measure_file(
        io.BytesIO(data),
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        socket_path=socket_path,
        timeout_seconds=timeout_seconds,
    )
