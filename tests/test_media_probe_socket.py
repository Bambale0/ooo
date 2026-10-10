"""Socket boundary tests use a deterministic fake decoder and synthetic bytes."""

import asyncio
import hashlib
import io
import os
import stat
import tempfile
from fractions import Fraction
from pathlib import Path

import pytest
import pytest_asyncio

from media_probe.client import measure_bytes, measure_file
from media_probe.measurement import MAX_INPUT_BYTES, Measurement, ProbeError
from media_probe.protocol import HEADER, MAGIC, decode_measurement, message_bytes, read_message
from media_probe.server import ProbeServer

DATA = b"synthetic-media-fixture" * 4000
SHA = hashlib.sha256(DATA).hexdigest()


async def fake_decoder(path, *, expected_sha256):
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256
    return Measurement(expected_sha256, Fraction(5), Fraction(5), None)


@pytest_asyncio.fixture
async def service():
    if os.environ.get("MEDIA_PROBE_RUN_UDS_TESTS") != "1":
        pytest.skip("Real AF_UNIX integration requires explicit opt-in on a permitted executor")
    with tempfile.TemporaryDirectory(prefix="probe-test-") as root:
        root = Path(root)
        server = ProbeServer(socket_path=root / "probe.sock", work_root=root / "jobs", decoder=fake_decoder)
        await server.start()
        try:
            yield server
        finally:
            await server.close()


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110 - bounded polling of a test-only completion predicate
            await asyncio.sleep(0.01)


async def test_streams_in_small_chunks_rewinds_and_cleans_up(service):
    class SmallReads(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            return super().read(size)

    file = SmallReads(DATA)
    file.seek(50)
    result = await measure_file(file, size_bytes=len(DATA), sha256=SHA, socket_path=str(service.socket_path))
    assert result.sha256 == SHA
    assert result.duration == 5
    assert file.tell() == 0 and not file.closed
    assert list(service.work_root.iterdir()) == []
    assert stat.S_IMODE(service.socket_path.stat().st_mode) == 0o660


async def test_rejects_second_job_without_spooling(service):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking(path, *, expected_sha256):
        entered.set()
        await release.wait()
        return await fake_decoder(path, expected_sha256=expected_sha256)

    service.decoder = blocking
    first = asyncio.create_task(measure_bytes(DATA, socket_path=str(service.socket_path)))
    await entered.wait()
    with pytest.raises(ProbeError, match="busy"):
        await measure_bytes(DATA, socket_path=str(service.socket_path))
    assert len(list(service.work_root.iterdir())) == 1
    release.set()
    await first
    assert list(service.work_root.iterdir()) == []


async def test_cancellation_stops_decoder_removes_file_and_frees_slot(service):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking(path, *, expected_sha256):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.decoder = blocking
    task = asyncio.create_task(measure_bytes(DATA, socket_path=str(service.socket_path)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await cancelled.wait()
    await until(lambda: service.active.done())
    assert list(service.work_root.iterdir()) == []
    service.decoder = fake_decoder
    assert (await measure_bytes(DATA, socket_path=str(service.socket_path))).duration == 5


async def test_service_deadline_covers_decoder_and_cleanup(service):
    service.timeout_seconds = 0.03

    async def blocking(path, *, expected_sha256):
        await asyncio.Event().wait()

    service.decoder = blocking
    with pytest.raises(ProbeError, match="timeout"):
        await measure_bytes(DATA, socket_path=str(service.socket_path))
    await until(lambda: service.active.done())
    assert list(service.work_root.iterdir()) == []


async def test_client_deadline_cancels_job(service):
    cancelled = asyncio.Event()

    async def blocking(path, *, expected_sha256):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.decoder = blocking
    with pytest.raises(ProbeError, match="timeout"):
        await measure_bytes(DATA, socket_path=str(service.socket_path), timeout_seconds=0.03)
    await cancelled.wait()
    await until(lambda: service.active.done())
    assert list(service.work_root.iterdir()) == []


@pytest.mark.parametrize("magic,size", [(b"http", 100), (MAGIC, MAX_INPUT_BYTES + 1), (MAGIC, 0)])
async def test_rejects_unsupported_protocol_or_oversized_header_before_spooling(service, magic, size):
    reader, writer = await asyncio.open_unix_connection(str(service.socket_path))
    writer.write(HEADER.pack(magic, size, bytes.fromhex(SHA)))
    await writer.drain()
    with pytest.raises(ProbeError, match="invalid_request"):
        await read_message(reader)
    writer.close()
    await writer.wait_closed()
    assert list(service.work_root.iterdir()) == []


async def test_hash_mismatch_does_not_decode(service):
    calls = []

    async def never(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Must reject before native parsing")

    service.decoder = never
    file = io.BytesIO(DATA)
    with pytest.raises(ProbeError, match="hash_mismatch"):
        await measure_file(file, size_bytes=len(DATA), sha256="a" * 64, socket_path=str(service.socket_path))
    assert calls == [] and file.tell() == 0
    assert list(service.work_root.iterdir()) == []


async def test_incomplete_upload_cleans_up(service):
    reader, writer = await asyncio.open_unix_connection(str(service.socket_path))
    writer.write(HEADER.pack(MAGIC, len(DATA), bytes.fromhex(SHA)))
    await writer.drain()
    assert await read_message(reader) == {"ready": True}
    writer.write(DATA[:10])
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await until(lambda: service.active.done())
    assert list(service.work_root.iterdir()) == []


async def test_response_size_is_bounded_before_body_read():
    reader = asyncio.StreamReader()
    reader.feed_data((2048).to_bytes(4, "big"))
    with pytest.raises(ProbeError, match="invalid_request"):
        await read_message(reader)


@pytest.mark.parametrize(
    "field,value", [("duration", [1, 0]), ("duration", [1, 1]), ("sha256", "a" * 64), ("content_type", "text/html")]
)
def test_client_rejects_invalid_decoder_responses(field, value):
    response = {
        "ok": True,
        "sha256": SHA,
        "duration": [5, 1],
        "video_duration": [5, 1],
        "audio_duration": None,
        "content_type": "video/mp4",
    }
    response[field] = value
    with pytest.raises(ProbeError):
        decode_measurement(response, expected_sha256=SHA)


async def test_raw_decoder_error_is_not_returned(service):
    async def fails(*args, **kwargs):
        raise RuntimeError("private input path and credentials must not be reflected")

    service.decoder = fails
    with pytest.raises(ProbeError, match="unavailable") as error:
        await measure_bytes(DATA, socket_path=str(service.socket_path))
    assert "private" not in str(error.value)
    assert list(service.work_root.iterdir()) == []


def test_service_has_no_app_or_settings_import():
    for module in ("server.py", "decoder.py", "measurement.py", "protocol.py"):
        source = (Path(__file__).parents[1] / "media_probe" / module).read_text()
        assert "from app" not in source and "import app" not in source
    assert os.name == "posix"  # AF_UNIX and the deployment contract require Linux.
    assert len(message_bytes({"ok": False, "error": "busy"})) < 100
