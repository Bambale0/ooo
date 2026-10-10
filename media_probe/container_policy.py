"""Narrow structural allowlist; never used to establish media duration.

The full decoder remains mandatory. This only excludes ambiguous edit programs,
fragmented movies and external data references before native decoding.
"""

import struct
from pathlib import Path

from media_probe.measurement import ProbeError

CONTAINERS = {b"moov", b"trak", b"edts", b"mdia", b"minf", b"dinf"}


def inspect_edit_policy(path: Path) -> list[int | None]:
    edits: list[int | None] = []
    boxes_seen = 0
    moov_count = 0
    mdat_count = 0

    with path.open("rb") as file:
        file.seek(0, 2)
        total = file.tell()

        def walk(start: int, end: int, *, track: int | None = None, depth: int = 0) -> None:
            nonlocal boxes_seen, moov_count, mdat_count
            if depth > 6:
                raise ProbeError("unsupported_media")
            while start < end:
                file.seek(start)
                header = file.read(8)
                if len(header) != 8:
                    raise ProbeError()
                size, kind = struct.unpack("!I4s", header)
                header_size = 8
                if size == 1:
                    extended = file.read(8)
                    if len(extended) != 8:
                        raise ProbeError()
                    size = struct.unpack("!Q", extended)[0]
                    header_size = 16
                elif size == 0:
                    if kind != b"mdat" or depth != 0:
                        raise ProbeError("unsupported_media")
                    size = end - start
                if size < header_size or start + size > end:
                    raise ProbeError()
                boxes_seen += 1
                if boxes_seen > 256:
                    raise ProbeError("resource_limit")
                payload, box_end = start + header_size, start + size
                if kind in {b"moof", b"mvex", b"cmov", b"rmra"}:
                    raise ProbeError("unsupported_media")
                if kind == b"moov":
                    moov_count += 1
                if kind == b"mdat":
                    mdat_count += 1
                if kind == b"trak":
                    if depth != 1 or len(edits) >= 2:
                        raise ProbeError("unsupported_media")
                    edits.append(None)
                    walk(payload, box_end, track=len(edits) - 1, depth=depth + 1)
                elif kind in CONTAINERS:
                    walk(payload, box_end, track=track, depth=depth + 1)
                elif kind == b"elst":
                    if track is None or edits[track] is not None or box_end - payload not in {20, 28}:
                        raise ProbeError("unsupported_media")
                    file.seek(payload)
                    value = file.read(box_end - payload)
                    version, flags = value[0], value[1:4]
                    if flags != b"\x00\x00\x00" or value[4:8] != b"\x00\x00\x00\x01":
                        raise ProbeError("unsupported_media")
                    if version == 0 and len(value) == 20:
                        duration, media_time, rate, fraction = struct.unpack("!IiHH", value[8:])
                    elif version == 1 and len(value) == 28:
                        duration, media_time, rate, fraction = struct.unpack("!QqHH", value[8:])
                    else:
                        raise ProbeError("unsupported_media")
                    if not duration or media_time < 0 or (rate, fraction) != (1, 0):
                        raise ProbeError("unsupported_media")
                    edits[track] = media_time
                elif kind == b"dref":
                    # Exactly one self-contained URL entry, with no pathname.
                    file.seek(payload)
                    value = file.read(min(21, box_end - payload))
                    if value != b"\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x0curl \x00\x00\x00\x01":
                        raise ProbeError("unsupported_media")
                start = box_end
            if start != end:
                raise ProbeError()

        walk(0, total)
    if moov_count != 1 or mdat_count != 1 or not edits:
        raise ProbeError("unsupported_media")
    return edits
