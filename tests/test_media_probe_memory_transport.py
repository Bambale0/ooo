"""In-memory protocol tests; these do not prove real UDS or Docker containment."""

import asyncio
import hashlib
import io
from fractions import Fraction

import pytest
import pytest_asyncio

from media_probe.client import measure_file
from media_probe.measurement import Measurement, ProbeError
from media_probe.server import ProbeServer

DATA = b"synthetic fixture only" * 5000
SHA = hashlib.sha256(DATA).hexdigest()


class MemoryWriter:
    def __init__(self, peer):
        self.peer = peer
        self.closed = False
        self.largest_write = 0
        self.transport = self
        self.aborted = False

    def write(self, data):
        if self.closed:
            raise BrokenPipeError()
        self.largest_write = max(self.largest_write, len(data))
        self.peer.feed_data(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        if not self.closed:
            self.closed = True
            self.peer.feed_eof()

    def is_closing(self):
        return self.closed

    def abort(self):
        self.aborted = True
        self.close()

    async def wait_closed(self):
        await asyncio.sleep(0)


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110 - bounded polling of a test-only completion predicate
            await asyncio.sleep(0.001)


@pytest_asyncio.fixture
async def memory_service(tmp_path, monkeypatch):
    async def decoder(path, *, expected_sha256):
        assert path.read_bytes() == DATA
        assert path.stat().st_mode & 0o777 == 0o400
        return Measurement(expected_sha256, Fraction(5), Fraction(5), None)

    server = ProbeServer(work_root=tmp_path / "jobs", socket_path=tmp_path / "unused", decoder=decoder)
    server.work_root.mkdir(mode=0o700)
    writes = []

    async def connect(*args, **kwargs):
        server_reader, client_reader = asyncio.StreamReader(), asyncio.StreamReader()
        server_writer, client_writer = MemoryWriter(client_reader), MemoryWriter(server_reader)
        writes.append(client_writer)
        server._connect(server_reader, server_writer)
        return client_reader, client_writer

    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    try:
        yield server, writes
    finally:
        await server.close()


async def measure(**kwargs):
    return await measure_file(io.BytesIO(DATA), size_bytes=len(DATA), sha256=SHA, **kwargs)


async def test_streaming_round_trip_rewinds_and_cleans(memory_service):
    server, writers = memory_service
    file = io.BytesIO(DATA)
    file.seek(10)
    result = await measure_file(file, size_bytes=len(DATA), sha256=SHA)
    assert result.sha256 == SHA and result.duration == 5
    assert file.tell() == 0 and not file.closed
    assert max(writer.largest_write for writer in writers) <= 64 * 1024
    assert list(server.work_root.iterdir()) == []


async def test_busy_connection_does_not_spool_or_queue(memory_service):
    server, _ = memory_service
    original = server.decoder
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    server.decoder = blocking
    first = asyncio.create_task(measure())
    await entered.wait()
    with pytest.raises(ProbeError, match="busy"):
        await measure()
    assert len(list(server.work_root.iterdir())) == 1
    release.set()
    await first
    assert list(server.work_root.iterdir()) == []


async def test_client_cancellation_stops_job_and_frees_slot(memory_service):
    server, _ = memory_service
    original = server.decoder
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocking(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    server.decoder = blocking
    task = asyncio.create_task(measure())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await cancelled.wait()
    await wait_until(lambda: server.active.done())
    assert list(server.work_root.iterdir()) == []
    server.decoder = original
    assert (await measure()).duration == 5


@pytest.mark.parametrize("where", ["client", "server"])
async def test_shared_deadline_stops_decoder_and_cleans(memory_service, where):
    server, _ = memory_service
    cancelled = asyncio.Event()

    async def blocking(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    server.decoder = blocking
    options = {}
    if where == "client":
        options["timeout_seconds"] = 0.03
    else:
        server.timeout_seconds = 0.03
    with pytest.raises(ProbeError, match="timeout"):
        await measure(**options)
    await cancelled.wait()
    await wait_until(lambda: server.active.done())
    assert list(server.work_root.iterdir()) == []


async def test_bad_hash_rejected_before_decoder(memory_service):
    server, _ = memory_service

    async def never(*args, **kwargs):
        raise AssertionError("Decoder must never be invoked")

    server.decoder = never
    with pytest.raises(ProbeError, match="hash_mismatch"):
        await measure_file(io.BytesIO(DATA), size_bytes=len(DATA), sha256="a" * 64)
    assert list(server.work_root.iterdir()) == []


async def test_server_close_cancels_work_and_cleans(memory_service):
    server, _ = memory_service
    entered = asyncio.Event()

    async def blocking(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    server.decoder = blocking
    task = asyncio.create_task(measure())
    await entered.wait()
    await server.close()
    with pytest.raises(ProbeError, match="unavailable"):
        await task
    assert list(server.work_root.iterdir()) == []


async def test_client_rejects_file_with_unexpected_trailing_bytes(memory_service):
    server, _ = memory_service
    with pytest.raises(ProbeError, match="invalid_request"):
        await measure_file(io.BytesIO(DATA + b"extra"), size_bytes=len(DATA), sha256=SHA)
    await wait_until(lambda: server.active.done())
    assert list(server.work_root.iterdir()) == []


@pytest.mark.parametrize("magic,size", [(b"http", 16), (b"NMP1", 104857601), (b"NMP1", 0)])
async def test_invalid_header_rejected_without_spooling(memory_service, magic, size):
    from media_probe.protocol import HEADER, read_message

    server, _ = memory_service
    reader, writer = await asyncio.open_unix_connection("unused")
    writer.write(HEADER.pack(magic, size, bytes.fromhex(SHA)))
    await writer.drain()
    with pytest.raises(ProbeError, match="invalid_request"):
        await read_message(reader)
    writer.close()
    await writer.wait_closed()
    assert list(server.work_root.iterdir()) == []


async def test_stalled_close_cannot_extend_deadline_after_success(memory_service, monkeypatch):
    async def stalled(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(MemoryWriter, "wait_closed", stalled)
    started = asyncio.get_running_loop().time()
    async with asyncio.timeout(0.2):
        result = await measure(timeout_seconds=0.05)
    assert asyncio.get_running_loop().time() - started < 0.1
    assert result.duration == 5


async def test_external_cancellation_is_not_swallowed_by_stalled_close(memory_service, monkeypatch):
    server, _ = memory_service
    started = asyncio.Event()

    async def blocking(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    async def stalled(self):
        await asyncio.Event().wait()

    server.decoder = blocking
    monkeypatch.setattr(MemoryWriter, "wait_closed", stalled)
    task = asyncio.create_task(measure())
    await started.wait()
    task.cancel()
    cancelled_at = asyncio.get_running_loop().time()
    async with asyncio.timeout(0.2):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert asyncio.get_running_loop().time() - cancelled_at < 0.1
