"""Strict decoded-media consistency checks; never proof of provider billing.

All arithmetic remains rational. Container timing is corroborating evidence only:
video cadence, packet coverage and decoded AAC sample counts are independent checks.
"""

import re
from dataclasses import dataclass
from fractions import Fraction

MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_DURATION = Fraction(31)  # Allows the final AAC frame beyond a nominal 30 s input.
MAX_PIXELS = 4096 * 2160
MAX_VIDEO_FRAMES = 1800
MAX_AUDIO_FRAMES = 1500
MOV_FORMAT = "mov,mp4,m4a,3gp,3g2,mj2"
RATIONAL = re.compile(r"-?[0-9]{1,12}(?:/[0-9]{1,12}|\.[0-9]{1,9})?\Z")
ERROR_CODES = frozenset(
    {
        "invalid_media",
        "unsupported_media",
        "inconsistent_timing",
        "resource_limit",
        "timeout",
        "busy",
        "unavailable",
        "invalid_request",
        "hash_mismatch",
        "decode_failed",
        "cancelled",
    }
)


class ProbeError(Exception):
    """A bounded, public error code with no filenames or decoder output."""

    def __init__(self, code: str = "invalid_media") -> None:
        self.code = code if isinstance(code, str) and code in ERROR_CODES else "invalid_media"
        super().__init__(self.code)


@dataclass(frozen=True)
class Measurement:
    sha256: str
    duration: Fraction
    video_duration: Fraction
    audio_duration: Fraction | None
    content_type: str = "video/mp4"


def integer(value: object, *, minimum: int = 0, maximum: int = 10**12) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ProbeError()
    if isinstance(value, str) and not re.fullmatch(r"-?[0-9]{1,13}", value):
        raise ProbeError()
    number = int(value)
    if not minimum <= number <= maximum:
        raise ProbeError()
    return number


def rational(value: object) -> Fraction:
    if not isinstance(value, str) or not RATIONAL.fullmatch(value):
        raise ProbeError()
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ProbeError() from exc


def require(condition: bool, code: str = "inconsistent_timing") -> None:
    if not condition:
        raise ProbeError(code)


def streams_from(document: dict, *, size_bytes: int) -> dict[int, dict]:
    """Constrain the decoder's surface before attempting the complete decode."""
    try:
        streams = document["streams"]
        fmt = document["format"]
        require(isinstance(streams, list) and 1 <= len(streams) <= 2, "unsupported_media")
        require(integer(fmt["nb_streams"]) == len(streams))
        require(fmt["format_name"] == MOV_FORMAT, "unsupported_media")
        require(integer(fmt["size"]) == size_bytes)
        require(rational(fmt["start_time"]) == 0)
        require(0 < rational(fmt["duration"]) <= MAX_DURATION)
        result = {}
        types = []
        for stream in streams:
            index = integer(stream["index"], maximum=1)
            require(index not in result, "unsupported_media")
            result[index] = stream
            kind = stream["codec_type"]
            types.append(kind)
            require(kind in {"video", "audio"}, "unsupported_media")
            require(not stream.get("disposition", {}).get("attached_pic"), "unsupported_media")
            require(integer(stream["start_pts"], minimum=-(10**12)) == 0)
            require(0 < rational(stream["time_base"]) <= 1)
            require(integer(stream["duration_ts"], minimum=1) * rational(stream["time_base"]) <= MAX_DURATION)
            integer(stream["nb_frames"], minimum=1, maximum=MAX_VIDEO_FRAMES if kind == "video" else MAX_AUDIO_FRAMES)
            if kind == "video":
                require(stream["codec_name"] == "h264", "unsupported_media")
                require(stream["profile"] in {"Constrained Baseline", "Baseline", "Main", "High"}, "unsupported_media")
                require(stream["pix_fmt"] == "yuv420p", "unsupported_media")
                width = integer(stream["width"], minimum=16, maximum=4096)
                height = integer(stream["height"], minimum=16, maximum=4096)
                require(width * height <= MAX_PIXELS, "resource_limit")
                fps = rational(stream["r_frame_rate"])
                require(1 <= fps <= 60, "unsupported_media")
                require(rational(stream["avg_frame_rate"]) == fps)
            else:
                require(stream["codec_name"] == "aac" and stream["profile"] == "LC", "unsupported_media")
                require(integer(stream["sample_rate"]) in {32000, 44100, 48000}, "unsupported_media")
                integer(stream["channels"], minimum=1, maximum=2)
        require(types.count("video") == 1 and types.count("audio") <= 1, "unsupported_media")
        return result
    except (KeyError, TypeError, AttributeError) as exc:
        raise ProbeError() from exc


def _video(stream: dict, packets: list[dict], frames: list[dict]) -> Fraction:
    count = integer(stream["nb_frames"], minimum=1, maximum=MAX_VIDEO_FRAMES)
    require(count == len(packets) == len(frames))
    period = 1 / rational(stream["r_frame_rate"])
    time_base = rational(stream["time_base"])
    start = integer(stream["start_pts"])
    # A small initial composition delay is permitted for ordinary H.264 B-frames.
    require(0 <= start * time_base <= 2 * period)
    expected = [start * time_base + n * period for n in range(count)]
    pts = []
    last_dts = None
    for packet in packets:
        pts.append(integer(packet["pts"], minimum=-(10**12)) * time_base)
        dts = integer(packet["dts"], minimum=-(10**12)) * time_base
        require(dts == 0 if last_dts is None else dts - last_dts == period)
        last_dts = dts
        require(integer(packet["duration"], minimum=1) * time_base == period)
    require(sorted(pts) == expected)
    for n, frame in enumerate(frames):
        require(frame["media_type"] == "video")
        require(integer(frame["pts"], minimum=-(10**12)) * time_base == expected[n])
        require(integer(frame["duration"], minimum=1) * time_base == period)
        require(frame["width"] == stream["width"] and frame["height"] == stream["height"])
    duration = count * period
    require(integer(stream["duration_ts"], minimum=1) * time_base == duration)
    # Count the raw presentation offset conservatively rather than subtracting edits.
    return duration + start * time_base


def _audio(stream: dict, packets: list[dict], frames: list[dict]) -> Fraction:
    count = integer(stream["nb_frames"], minimum=1, maximum=MAX_AUDIO_FRAMES)
    require(count == len(packets) == len(frames))
    rate = integer(stream["sample_rate"], minimum=1)
    time_base = rational(stream["time_base"])
    require(time_base == Fraction(1, rate))
    require(integer(stream["start_pts"]) == 0)
    samples = 0
    declared_samples = 0
    for n, (packet, frame) in enumerate(zip(packets, frames, strict=True)):
        require(frame["media_type"] == "audio")
        require(integer(frame["nb_samples"], minimum=1) == 1024, "unsupported_media")
        require(integer(frame["pts"]) == integer(packet["pts"]) == integer(packet["dts"]) == samples)
        frame_duration = integer(frame["duration"], minimum=1, maximum=1024)
        packet_duration = integer(packet["duration"], minimum=1, maximum=1024)
        require(frame_duration == packet_duration)
        # The final compressed AAC frame may contain encoder padding. Never let
        # a shorter packet duration hide a full decoded frame anywhere else.
        require(n == count - 1 or frame_duration == 1024)
        for side in packet.get("side_data_list", []):
            if side.get("side_data_type") == "Skip Samples":
                require(integer(side.get("skip_samples", 0)) == 0)
                require(integer(side.get("discard_padding", 0)) == 0)
        samples += 1024
        declared_samples += packet_duration
    require(integer(stream["duration_ts"], minimum=1) == declared_samples)
    return Fraction(samples, rate)


def validate_observation(declared: dict, decoded: dict, *, sha256: str, size_bytes: int) -> Measurement:
    try:
        normal = streams_from(declared, size_bytes=size_bytes)
        # Raw timestamps may include a bounded H.264 composition delay. Validate
        # that exception explicitly in _video, while retaining all other checks.
        raw_for_shape = dict(decoded)
        raw_for_shape["streams"] = [dict(s, start_pts=0) for s in decoded["streams"]]
        raw_for_shape["format"] = dict(decoded["format"], start_time="0")
        raw = streams_from(raw_for_shape, size_bytes=size_bytes)
        raw = {s["index"]: s for s in decoded["streams"]}
        require(normal.keys() == raw.keys())
        raw_starts = [integer(s["start_pts"]) * rational(s["time_base"]) for s in raw.values()]
        require(abs(rational(decoded["format"]["start_time"]) - min(raw_starts)) <= Fraction(1, 10**6))
        raw_ends = [
            integer(s["duration_ts"], minimum=1) * rational(s["time_base"]) + start
            for s, start in zip(raw.values(), raw_starts, strict=True)
        ]
        require(abs(rational(decoded["format"]["duration"]) - (max(raw_ends) - min(raw_starts))) <= Fraction(1, 1000))
        rows = decoded["packets_and_frames"]
        require(isinstance(rows, list) and 0 < len(rows) <= 2 * (MAX_VIDEO_FRAMES + MAX_AUDIO_FRAMES), "resource_limit")
        grouped = {index: {"packet": [], "frame": []} for index in raw}
        for row in rows:
            kind = row["type"]
            index = integer(row["stream_index"], maximum=1)
            require(index in grouped and kind in {"packet", "frame"})
            if kind == "packet":
                integer(row["size"], minimum=1, maximum=MAX_INPUT_BYTES)
                require("C" not in row.get("flags", "") and "D" not in row.get("flags", ""))
            grouped[index][kind].append(row)
        durations = {}
        declared_durations = []
        for index, stream in raw.items():
            original = normal[index]
            for key in ("codec_name", "codec_type", "time_base", "nb_frames"):
                require(original[key] == stream[key])
            time_base = rational(stream["time_base"])
            kind = stream["codec_type"]
            raw_duration = integer(stream["duration_ts"], minimum=1) * time_base
            normal_duration = integer(original["duration_ts"], minimum=1) * time_base
            declared_durations.append(normal_duration)
            if kind == "video":
                duration = _video(stream, **{k + "s": v for k, v in grouped[index].items()})
                require(normal_duration == raw_duration)
            else:
                duration = _audio(stream, **{k + "s": v for k, v in grouped[index].items()})
                # The sole permitted edit is at most one AAC-LC priming frame.
                require(0 <= raw_duration - normal_duration <= Fraction(1024, integer(stream["sample_rate"])))
            durations[kind] = duration
        container_duration = rational(declared["format"]["duration"])
        require(abs(container_duration - max(declared_durations)) <= Fraction(1, 1000))
        estimate = max(container_duration, *durations.values())
        require(0 < estimate <= MAX_DURATION, "resource_limit")
        require(re.fullmatch(r"[a-f0-9]{64}", sha256) is not None)
        return Measurement(sha256, estimate, durations["video"], durations.get("audio"))
    except (KeyError, TypeError, AttributeError, ValueError, ZeroDivisionError) as exc:
        raise ProbeError() from exc
