"""Single-job local socket service. Production entrypoint enforces containment."""

import asyncio
import hashlib
import os
import signal
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from media_probe.decoder import decode_file
from media_probe.measurement import MAX_INPUT_BYTES, Measurement, ProbeError
from media_probe.protocol import (
    CHUNK_SIZE,
    DEFAULT_SOCKET,
    HEADER,
    MAGIC,
    MAX_SECONDS,
    encode_measurement,
    message_bytes,
)

Decoder = Callable[..., Awaitable[Measurement]]


class ProbeServer:
    def __init__(
        self,
        *,
        socket_path: Path = Path(DEFAULT_SOCKET),
        work_root: Path = Path("/tmp/media-probe"),
        decoder: Decoder = decode_file,
        timeout_seconds: float = MAX_SECONDS,
    ) -> None:
        self.socket_path = socket_path
        self.work_root = work_root
        self.decoder = decoder
        self.timeout_seconds = min(timeout_seconds, MAX_SECONDS)
        self.active: asyncio.Task | None = None
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.server = await asyncio.start_unix_server(
            self._connect, path=str(self.socket_path), backlog=8, limit=CHUNK_SIZE
        )
        os.chmod(self.socket_path, 0o660)

    def _connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Check synchronously before scheduling: simultaneous clients cannot race
        # into the decoder, and busy connections never receive or spool a body.
        if self.active is not None and not self.active.done():
            writer.write(message_bytes({"ok": False, "error": "busy"}))
            writer.close()
            return
        self.active = asyncio.create_task(self._handle(reader, writer))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                magic, size, expected = HEADER.unpack(await reader.readexactly(HEADER.size))
                if magic != MAGIC or not 16 <= size <= MAX_INPUT_BYTES:
                    raise ProbeError("invalid_request")
                writer.write(message_bytes({"ready": True}))
                await writer.drain()
                with tempfile.TemporaryDirectory(prefix="job-", dir=self.work_root) as directory:
                    path = Path(directory) / "input.mp4"
                    digest = hashlib.sha256()
                    remaining = size
                    with path.open("xb") as handle:
                        while remaining:
                            chunk = await reader.readexactly(min(CHUNK_SIZE, remaining))
                            handle.write(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                    os.chmod(path, 0o400)
                    if digest.digest() != expected:
                        raise ProbeError("hash_mismatch")
                    # EOF or unsolicited bytes after the fixed body cancel the
                    # job immediately; the protocol requires the client to wait.
                    disconnected = asyncio.create_task(reader.read(1))
                    check = asyncio.create_task(self.decoder(path, expected_sha256=digest.hexdigest()))
                    try:
                        done, _ = await asyncio.wait({disconnected, check}, return_when=asyncio.FIRST_COMPLETED)
                        if disconnected in done:
                            raise ProbeError("cancelled")
                        result = await check
                    finally:
                        for task in (disconnected, check):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(disconnected, check, return_exceptions=True)
                writer.write(message_bytes(encode_measurement(result)))
                await writer.drain()
        except TimeoutError:
            self._error(writer, "timeout")
        except ProbeError as exc:
            self._error(writer, exc.code)
        except (OSError, ValueError, asyncio.IncompleteReadError):
            self._error(writer, "invalid_request")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not expose filenames, decoder messages, inputs or environment.
            self._error(writer, "unavailable")
        finally:
            writer.close()
            try:
                async with asyncio.timeout(0.1):
                    await writer.wait_closed()
            except (TimeoutError, OSError):
                writer.transport.abort()
            except asyncio.CancelledError:
                writer.transport.abort()
                raise

    @staticmethod
    def _error(writer: asyncio.StreamWriter, code: str) -> None:
        if not writer.is_closing():
            try:
                writer.write(message_bytes({"ok": False, "error": code}))
            except (OSError, RuntimeError):
                pass

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.active is not None:
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)
        self.socket_path.unlink(missing_ok=True)


async def main() -> None:
    from media_probe.isolation import protect_broker_memory, require_isolation

    require_isolation()
    protect_broker_memory()
    server = ProbeServer()
    await server.start()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await stopped.wait()
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())
