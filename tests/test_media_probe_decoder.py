"""Only generated synthetic media may reach native parsers in host-side tests."""

import asyncio
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from media_probe.decoder import decode_file, run_bounded
from media_probe.measurement import ProbeError


@pytest.fixture(scope="module")
def synthetic_videos(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Native FFmpeg unavailable; synthetic integration checks not run")
    root = tmp_path_factory.mktemp("synthetic_probe_media")
    paths = {}
    for seconds in (1, 4, 5, 20):
        path = root / f"{seconds}.mp4"
        subprocess.run(  # noqa: ASYNC221 - bounded synthetic fixture setup, no concurrent service
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s=64x64:r=25:d={seconds}",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                str(seconds),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-b:a",
                "32k",
                "-movflags",
                "+faststart",
                "-y",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        paths[seconds] = path
    return paths


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("seconds", [5, 20])
async def test_full_decode_synthetic_h264_aac(seconds, synthetic_videos):
    path = synthetic_videos[seconds]
    before = digest(path)
    result = await decode_file(path, expected_sha256=before)
    assert result is not None
    assert seconds <= result.duration < seconds + 1
    assert result.audio_duration > seconds
    assert result.sha256 == before == digest(path)


async def test_rejects_truncated_generated_media(synthetic_videos, tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(synthetic_videos[5].read_bytes()[:-100])
    with pytest.raises(ProbeError):
        await decode_file(path, expected_sha256=digest(path))


async def test_rejects_non_mp4_without_invoking_native_decoder(tmp_path, monkeypatch):
    path = tmp_path / "playlist.mp4"
    path.write_bytes(b"#EXTM3U\nhttps://169.254.169.254/\n")
    calls = []
    monkeypatch.setattr("media_probe.decoder.run_bounded", lambda *a, **k: calls.append(a))
    with pytest.raises(ProbeError):
        await decode_file(path, expected_sha256=digest(path))
    assert calls == []


async def test_subprocess_output_is_bounded():
    with pytest.raises(ProbeError, match="resource_limit"):
        await run_bounded(["/usr/bin/python3", "-c", "print('x'*100000)"], stdout_limit=100)


async def test_subprocess_timeout_kills_and_reaps_child(monkeypatch):
    real_create = asyncio.create_subprocess_exec
    children = []
    started = asyncio.Event()

    async def record(*args, **kwargs):
        process = await real_create(*args, **kwargs)
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    task = asyncio.create_task(
        run_bounded(
            [
                "/usr/bin/python3",
                "-c",
                "import time; time.sleep(30)",
            ],
            stdout_limit=100,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].returncode is not None
    assert not Path(f"/proc/{children[0].pid}").exists()  # noqa: ASYNC240 - local verification only


def boxes(data, start, end):
    """Fixture-only MP4 box walker, not part of measurement or production code."""
    while start < end:
        size = int.from_bytes(data[start : start + 4], "big")
        assert 8 <= size <= end - start
        yield data[start + 4 : start + 8], start + 8, start + size
        start += size
    assert start == end


def children(data, payload, end, kind):
    return [(p, e) for k, p, e in boxes(data, payload, end) if k == kind]


async def test_real_mp4_one_second_metadata_hides_four_seconds_decoded_aac(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("Native synthetic media fixture generation unavailable")
    path = tmp_path / "compressed-audio-timing.mp4"
    subprocess.run(  # noqa: ASYNC221 - bounded synthetic fixture setup, no concurrent service
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=25:d=1",
            "-f",
            "lavfi",
            "-t",
            "4",
            "-i",
            "anullsrc=r=48000:cl=mono",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-movflags",
            "+faststart",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    data = bytearray(path.read_bytes())
    moov, end = children(data, 0, len(data), b"moov")[0]

    def divide_u32(offset):
        number = int.from_bytes(data[offset : offset + 4], "big")
        data[offset : offset + 4] = (number // 4).to_bytes(4, "big")

    mvhd, _ = children(data, moov, end, b"mvhd")[0]
    divide_u32(mvhd + 16)
    for trak, trak_end in children(data, moov, end, b"trak"):
        mdia, mdia_end = children(data, trak, trak_end, b"mdia")[0]
        hdlr, _ = children(data, mdia, mdia_end, b"hdlr")[0]
        if data[hdlr + 8 : hdlr + 12] != b"soun":
            continue
        tkhd, _ = children(data, trak, trak_end, b"tkhd")[0]
        divide_u32(tkhd + 20)
        mdhd, _ = children(data, mdia, mdia_end, b"mdhd")[0]
        divide_u32(mdhd + 16)
        edts, edts_end = children(data, trak, trak_end, b"edts")[0]
        elst, _ = children(data, edts, edts_end, b"elst")[0]
        divide_u32(elst + 8)
        # Scale the priming edit too, so all displayed metadata appears coherent.
        divide_u32(elst + 12)
        minf, minf_end = children(data, mdia, mdia_end, b"minf")[0]
        stbl, stbl_end = children(data, minf, minf_end, b"stbl")[0]
        stts, _ = children(data, stbl, stbl_end, b"stts")[0]
        count = int.from_bytes(data[stts + 4 : stts + 8], "big")
        for entry in range(count):
            divide_u32(stts + 12 + entry * 8)
    path.write_bytes(data)
    # Independently establish the regression, instead of only asserting reject.
    import json

    from media_probe.decoder import ENTRIES, input_options

    declared = json.loads(
        await run_bounded(
            [
                "/usr/bin/ffprobe",
                *input_options(),
                "-show_format",
                "-show_streams",
                "-show_entries",
                ENTRIES[ENTRIES.index("stream=index") :],
                "-of",
                "json",
                "-i",
                str(path),
            ],
            stdout_limit=32768,
        )
    )
    raw = json.loads(
        await run_bounded(
            [
                "/usr/bin/ffprobe",
                *input_options(),
                "-ignore_editlist",
                "1",
                "-show_frames",
                "-show_entries",
                "frame=media_type,nb_samples:frame_side_data=",
                "-of",
                "json",
                "-i",
                str(path),
            ],
            stdout_limit=1024 * 1024,
        )
    )
    assert declared["format"]["duration"] == "1.000000"
    decoded_samples = sum(frame["nb_samples"] for frame in raw["frames"] if frame["media_type"] == "audio")
    assert decoded_samples / 48000 >= 4
    with pytest.raises(ProbeError, match="inconsistent_timing"):
        await decode_file(path, expected_sha256=digest(path))


@pytest.mark.parametrize("seconds,megabytes", [(5, 20), (20, 80)])
async def test_admitted_large_files_have_no_small_metadata_probe_cutoff(seconds, megabytes, synthetic_videos, tmp_path):
    path = tmp_path / "large.mp4"
    shutil.copyfile(synthetic_videos[seconds], path)
    # A legal large free box gives an admitted-size fixture without changing
    # pictures, sound, timestamps, or allocating huge frame buffers in the test.
    size = megabytes * 1024 * 1024
    with path.open("ab") as file:
        file.write(size.to_bytes(4, "big") + b"free")
        file.truncate(file.tell() + size - 8)
    result = await decode_file(path, expected_sha256=digest(path))
    assert seconds <= result.duration < seconds + 1
    assert result.sha256 == digest(path)


async def test_standard_b_frames_video_only(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("Native synthetic media fixture generation unavailable")
    path = tmp_path / "bframes.mp4"
    subprocess.run(  # noqa: ASYNC221 - bounded synthetic fixture setup, no concurrent service
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=25:d=5",
            "-c:v",
            "libx264",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    result = await decode_file(path, expected_sha256=digest(path))
    assert 5 <= result.duration < 6
    assert result.audio_duration is None


@pytest.mark.parametrize("field,value", [("rate", "24000"), ("nb_samples", "2048"), ("channels", "2")])
def test_actual_decoded_audio_rate_samples_and_channels_must_match(field, value):
    from media_probe.decoder import verify_audio_decode_log

    log = (
        "[Parsed_ashowinfo_0 @ 0x123] [info] n:0 pts:0 pts_time:0 fmt:fltp "
        "channels:1 chlayout:mono rate:48000 nb_samples:1024 checksum:00000000\n"
    )
    expected = {"rate": "48000", "nb_samples": "1024", "channels": "1"}[field]
    log = log.replace(f"{field}:{expected}", f"{field}:{value}")
    document = {
        "streams": [{"index": 1, "codec_type": "audio", "sample_rate": "48000", "channels": 1}],
        "packets_and_frames": [{"type": "frame", "stream_index": 1, "nb_samples": 1024, "pts": 0}],
    }
    with pytest.raises(ProbeError):
        verify_audio_decode_log(log.encode(), document)


async def test_decoder_zero_exit_with_error_output_is_rejected():
    with pytest.raises(ProbeError, match="decode_failed"):
        await run_bounded(
            ["/usr/bin/python3", "-c", "import sys;sys.stderr.write('malformed input')"], stdout_limit=100
        )


async def test_decoder_nonzero_exit_is_rejected():
    with pytest.raises(ProbeError, match="decode_failed"):
        await run_bounded(["/usr/bin/python3", "-c", "raise SystemExit(3)"], stdout_limit=100)


async def test_native_process_resource_limits_and_empty_secret_environment():
    import json

    result = await run_bounded(
        [
            "/usr/bin/python3",
            "-c",
            "import json,os,resource; print(json.dumps({'env':sorted(os.environ),"
            "'memory':resource.getrlimit(resource.RLIMIT_AS), 'cpu':resource.getrlimit(resource.RLIMIT_CPU),"
            "'files':resource.getrlimit(resource.RLIMIT_FSIZE), 'fds':resource.getrlimit(resource.RLIMIT_NOFILE)}))",
        ],
        stdout_limit=1024,
    )
    observed = json.loads(result)
    assert set(observed["env"]) <= {"PATH", "LANG", "LC_ALL", "HOME"}
    assert observed["memory"] == [1536 * 1024 * 1024] * 2
    assert observed["cpu"] == [40, 40]
    assert observed["files"] == [0, 0]
    assert observed["fds"] == [64, 64]


def test_native_input_options_restrict_protocol_demuxer_codecs_and_external_references():
    from media_probe.decoder import input_options

    options = input_options()
    for key, value in {
        "-protocol_whitelist": "file",
        "-format_whitelist": "mov",
        "-codec_whitelist": "h264,aac",
        "-enable_drefs": "0",
        "-use_absolute_path": "0",
    }.items():
        assert options[options.index(key) + 1] == value
    assert not any(value in options for value in ("http", "https", "concat", "data", "-read_intervals"))


@pytest.mark.parametrize("mutation", ["repeated_edit", "reverse_rate", "external_reference", "fragmented"])
async def test_rejects_ambiguous_container_structure_before_native_decode(
    synthetic_videos, tmp_path, monkeypatch, mutation
):
    from media_probe.container_policy import inspect_edit_policy

    data = bytearray(synthetic_videos[5].read_bytes())
    if mutation in {"repeated_edit", "reverse_rate"}:
        location = data.index(b"elst") + 4
        if mutation == "repeated_edit":
            data[location + 4 : location + 8] = (2).to_bytes(4, "big")
        else:
            data[location + 16 : location + 18] = (2).to_bytes(2, "big")
    elif mutation == "external_reference":
        location = data.index(b"url ") + 4
        data[location : location + 4] = bytes(4)
    else:
        data.extend((8).to_bytes(4, "big") + b"moof")
    path = tmp_path / "unsafe-structure.mp4"
    path.write_bytes(data)
    with pytest.raises(ProbeError, match="unsupported_media"):
        inspect_edit_policy(path)
    called = []
    monkeypatch.setattr("media_probe.decoder.run_bounded", lambda *a, **k: called.append(True))
    with pytest.raises(ProbeError, match="unsupported_media"):
        await decode_file(path, expected_sha256=digest(path))
    assert not called


async def test_native_allocation_beyond_address_space_limit_is_rejected():
    with pytest.raises(ProbeError, match="decode_failed"):
        await run_bounded(["/usr/bin/python3", "-c", "bytearray(2 * 1024**3)"], stdout_limit=100)


async def test_native_cpu_spinner_is_killed_at_short_test_deadline(monkeypatch):
    monkeypatch.setattr("media_probe.decoder.MAX_SECONDS", 0.05)
    with pytest.raises(ProbeError, match="timeout"):
        await run_bounded(["/usr/bin/python3", "-c", "while True: pass"], stdout_limit=100)
