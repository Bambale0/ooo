"""Synthetic decoder observations: no user media and no native parser invocation."""

import copy
from fractions import Fraction

import pytest

from media_probe.measurement import ProbeError, validate_observation


def observation(seconds=5, *, audio=True):
    ticks = seconds * 12800
    streams = [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "width": 64,
            "height": 64,
            "profile": "Constrained Baseline",
            "pix_fmt": "yuv420p",
            "r_frame_rate": "25/1",
            "avg_frame_rate": "25/1",
            "time_base": "1/12800",
            "start_pts": 0,
            "duration_ts": ticks,
            "nb_frames": str(seconds * 25),
            "disposition": {},
        }
    ]
    entries = []
    for n in range(seconds * 25):
        entries.extend(
            [
                {
                    "type": "packet",
                    "stream_index": 0,
                    "pts": n * 512,
                    "dts": n * 512,
                    "duration": 512,
                    "size": "10",
                    "flags": "K__" if n == 0 else "___",
                },
                {
                    "type": "frame",
                    "media_type": "video",
                    "stream_index": 0,
                    "pts": n * 512,
                    "duration": 512,
                    "width": 64,
                    "height": 64,
                },
            ]
        )
    if audio:
        samples = seconds * 48000
        frames = (samples + 1023) // 1024
        streams.append(
            {
                "index": 1,
                "codec_name": "aac",
                "codec_type": "audio",
                "profile": "LC",
                "sample_rate": "48000",
                "channels": 1,
                "time_base": "1/48000",
                "start_pts": 0,
                "duration_ts": samples,
                "nb_frames": str(frames),
                "disposition": {},
            }
        )
        for n in range(frames):
            duration = min(1024, samples - n * 1024)
            entries.extend(
                [
                    {
                        "type": "packet",
                        "stream_index": 1,
                        "pts": n * 1024,
                        "dts": n * 1024,
                        "duration": duration,
                        "size": "5",
                        "flags": "K__",
                    },
                    {
                        "type": "frame",
                        "media_type": "audio",
                        "stream_index": 1,
                        "pts": n * 1024,
                        "duration": duration,
                        "nb_samples": 1024,
                    },
                ]
            )
    doc = {
        "streams": streams,
        "format": {
            "nb_streams": len(streams),
            "duration": str(seconds),
            "start_time": "0.000000",
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "size": "10000",
        },
        "packets_and_frames": entries,
    }
    declared = copy.deepcopy(doc)
    declared.pop("packets_and_frames")
    return declared, doc


def check(declared, decoded):
    return validate_observation(declared, decoded, sha256="a" * 64, size_bytes=10000)


@pytest.mark.parametrize("seconds", [5, 20])
def test_measures_complete_decode_including_audio_padding(seconds):
    declared, decoded = observation(seconds)
    result = check(declared, decoded)
    assert result is not None
    assert result.duration >= seconds
    assert result.duration == Fraction(((seconds * 48000 + 1023) // 1024) * 1024, 48000)
    assert result.sha256 == "a" * 64


def test_rejects_one_second_declared_but_four_seconds_decoded_audio():
    declared, decoded = observation(1)
    _, long_audio = observation(4)
    decoded["packets_and_frames"] = [row for row in decoded["packets_and_frames"] if row["stream_index"] == 0]
    for row in long_audio["packets_and_frames"]:
        if row["stream_index"] == 1:
            row["pts"] //= 4
            row["duration"] //= 4
            if "dts" in row:
                row["dts"] //= 4
            decoded["packets_and_frames"].append(row)
    for doc in (declared, decoded):
        doc["streams"][1]["nb_frames"] = long_audio["streams"][1]["nb_frames"]
    with pytest.raises(ProbeError):
        check(declared, decoded)


def test_rejects_audio_sample_count_hidden_by_short_packet_duration():
    declared, decoded = observation(5)
    for row in decoded["packets_and_frames"]:
        if row["type"] == "frame" and row["stream_index"] == 1:
            row["nb_samples"] *= 4
    with pytest.raises(ProbeError):
        check(declared, decoded)


def test_rejects_vfr_even_when_average_duration_matches():
    declared, decoded = observation(5, audio=False)
    frames = [row for row in decoded["packets_and_frames"] if row["type"] == "frame"]
    frames[10]["pts"] += 1
    with pytest.raises(ProbeError):
        check(declared, decoded)


def test_rejects_missing_final_decoded_frame():
    declared, decoded = observation(5, audio=False)
    decoded["packets_and_frames"].pop()
    with pytest.raises(ProbeError):
        check(declared, decoded)


def test_rejects_long_audio_and_hidden_edit_trim():
    declared, decoded = observation(5)
    declared["format"]["duration"] = "1"
    for stream in declared["streams"]:
        stream["duration_ts"] //= 5
    with pytest.raises(ProbeError):
        check(declared, decoded)


def test_rejects_ambiguous_second_video():
    declared, decoded = observation(5)
    for doc in (declared, decoded):
        doc["streams"].append(copy.deepcopy(doc["streams"][0]))
        doc["format"]["nb_streams"] = 3
    with pytest.raises(ProbeError):
        check(declared, decoded)
