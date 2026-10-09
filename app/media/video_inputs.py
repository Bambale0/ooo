"""Conservative video admission evidence over a fresh, server-owned provider copy.

Temporary bytes never become an application storage service. The provider is a
trusted storage boundary, not a documented write-once store: its write ticket is
used once, kept private, and discarded. Unsupported inputs retain the old quote.
"""

import asyncio
import copy
import hashlib
import json
import logging
import tempfile
import time
from datetime import datetime
from fractions import Fraction
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import httpx

from app.contracts.registry import SEEDANCE_25_FAMILY, normalized_video, video_reserve_seconds
from app.infrastructure.config import get_settings
from app.infrastructure.public_http import public_http_client
from media_probe.measurement import ProbeError

logger = logging.getLogger(__name__)
POLICY = "isolated-full-decode-v1"
CHUNK_BYTES = 64 * 1024
# APIX's checked-in submit timeout is 120s. Leave ample time for the unchanged
# admission/financial checks; slow media safely keeps the ordinary upper bound.
PREPARATION_TIMEOUT_SECONDS = 45
MIN_READ_LIFETIME_SECONDS = 3600
_active_partners = WeakKeyDictionary()


class MeasurementUnavailable(Exception):
    """No smaller financial hold may be inferred from this input."""


def eligible_video_input(body: dict) -> bool:
    video = normalized_video(body)
    # Seedance 2.0 reference accounting on InfAI needs a separate reviewed
    # output/input settlement contract; lowering that reserve could newly enable
    # a fallback lane. Keep its present quotes and routing entirely unchanged.
    return bool(video.get("model") in SEEDANCE_25_FAMILY and video.get("reference_videos"))


def snapshot_is_fresh(snapshot: dict, *, now: int | None = None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    expiry = snapshot.get("expires_at")
    return (
        snapshot.get("policy") == POLICY
        and isinstance(expiry, int)
        and not isinstance(expiry, bool)
        and expiry >= (int(time.time()) if now is None else now) + MIN_READ_LIFETIME_SECONDS
    )


def _url(value) -> str:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise MeasurementUnavailable("invalid_url")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise MeasurementUnavailable("invalid_url") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise MeasurementUnavailable("invalid_url")
    if port is not None and not 1 <= port <= 65535:
        raise MeasurementUnavailable("invalid_url")
    return value


def _expiry(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is not None:
            return int(instant.timestamp())
    raise MeasurementUnavailable("invalid_expiry")


def _check_response(response: httpx.Response, maximum: int) -> None:
    if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
        raise MeasurementUnavailable("incomplete_response")
    length = response.headers.get("content-length")
    if length is not None and (not length.isdecimal() or not 0 < int(length) <= maximum):
        raise MeasurementUnavailable("invalid_length")


async def _download(client, url: str, file, *, maximum: int) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    async with client.stream("GET", _url(url), headers={"Accept-Encoding": "identity"}) as response:
        _check_response(response, maximum)
        async for chunk in response.aiter_raw(chunk_size=CHUNK_BYTES):
            size += len(chunk)
            if size > maximum:
                raise MeasurementUnavailable("oversized_input")
            digest.update(chunk)
            if file is not None:
                file.write(chunk)
        declared = response.headers.get("content-length")
        if not size or (declared is not None and size != int(declared)):
            raise MeasurementUnavailable("truncated_input")
    if file is not None:
        file.flush()
    return digest.hexdigest(), size


async def _inspect(file, *, digest: str, size: int):
    # Only the credential-free socket service may execute native media parsers.
    # Application processes never import the decoder or invoke FFmpeg.
    from media_probe.client import measure_file

    return await measure_file(
        file, socket_path=get_settings().media_probe_socket,
        size_bytes=size, sha256=digest, timeout_seconds=PREPARATION_TIMEOUT_SECONDS,
    )


async def _file_chunks(file):
    file.seek(0)
    while chunk := file.read(CHUNK_BYTES):
        yield chunk


async def _upload_adapter(db, partner_id):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.providers.service import get_active_provider_credential, get_partner_provider_adapter

    # Free upload preparation must not invert admission's Partner→Credential
    # lock order or hold a credential lock across network/decoder work.
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    async with factory() as snapshot:
        credential = await get_active_provider_credential(snapshot, partner_id, "argolink", for_update=False)
        if credential is None:
            raise MeasurementUnavailable("credential_unavailable")
        return await get_partner_provider_adapter(snapshot, partner_id, "argolink", credential_id=credential.id)


async def _stage(
    client, adapter, file, *, model: str, digest: str, size: int, content_type: str, original_urls: set[str]
) -> dict:
    response = await adapter.native_request(
        "media/uploads", {"model": model, "type": "video", "content_type": content_type, "size_bytes": size},
        headers={"Accept-Encoding": "identity"},
    )
    try:
        if response.status_code not in {200, 201} or response.headers.get("content-encoding", "identity") != "identity":
            raise MeasurementUnavailable("ticket_unavailable")
        raw = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=4096):
            raw.extend(chunk)
            if len(raw) > 16 * 1024:
                raise MeasurementUnavailable("oversized_ticket")
        ticket = json.loads(raw)
    finally:
        await response.aclose()
    write_url, read_url = _url(ticket["upload_url"]), _url(ticket["media_url"])
    expires = _expiry(ticket["expires_at"])
    if (
        write_url == read_url or read_url in original_urls
        or _expiry(ticket["upload_expires_at"]) < int(time.time()) + 60
        or not snapshot_is_fresh({"policy": POLICY, "expires_at": expires})
    ):
        raise MeasurementUnavailable("unsafe_ticket")
    # No provider bearer token or caller headers accompany signed storage URLs.
    async with client.stream(
        "PUT", write_url, content=_file_chunks(file),
        headers={"Content-Type": content_type, "Content-Length": str(size), "Accept-Encoding": "identity"},
    ) as uploaded:
        if uploaded.status_code not in {200, 201, 204}:
            raise MeasurementUnavailable("upload_unavailable")
    copied_digest, copied_size = await _download(client, read_url, None, maximum=size)
    if (copied_digest, copied_size) != (digest, size):
        raise MeasurementUnavailable("copy_mismatch")
    return {"url": read_url, "expires_at": expires, "sha256": digest, "size_bytes": size}


def _replace_video_urls(original: dict, replacements: list[str]) -> dict:
    body = copy.deepcopy(original)
    urls = iter(replacements)
    if "reference_videos" in body:
        for item in body["reference_videos"]:
            item["url"] = next(urls)
    elif "video_urls" in body:
        body["video_urls"] = [next(urls) for _ in body["video_urls"]]
    else:
        for item in body.get("input_references", []):
            if item.get("type") == "video_url":
                if isinstance(item["video_url"], dict):
                    item["video_url"]["url"] = next(urls)
                else:
                    item["video_url"] = next(urls)
    if next(urls, None) is not None:
        raise MeasurementUnavailable("reference_count_mismatch")
    return body


async def prepare_video_inputs(db, partner_id: str, original: dict) -> dict | None:
    """Best-effort optimization BEFORE taking a financial row lock or reserve.

    Failure never turns an uncertain measurement into a smaller hold. Duplicate
    completed admissions return before this call. A concurrent losing admission
    can leave only a free, expiring provider object, never an extra paid job.
    """
    if not eligible_video_input(original) or not get_settings().media_probe_socket:
        return None
    video = normalized_video(original)
    # The edit/output equality contract has been verified for this exact model.
    edit = video.get("omni_reference_task_type") == "edit"
    if edit and video["model"] != "seedance-2.5":
        return None
    loop = asyncio.get_running_loop()
    active = _active_partners.setdefault(loop, set())
    # Never queue while holding a checked-out read transaction. At most two
    # preparations per process and one per partner; contention keeps the old
    # safe quote instead of starving the database or creating new input limits.
    if len(active) >= 2 or partner_id in active:
        return None
    active.add(partner_id)
    try:
        async with asyncio.timeout(PREPARATION_TIMEOUT_SECONDS):
            return await _prepare(db, partner_id, original, video, edit)
    except (
        MeasurementUnavailable, ProbeError, httpx.HTTPError, httpx.InvalidURL,
        OSError, ValueError, KeyError, TypeError, TimeoutError,
    ):
        # Neither raw media URLs nor signed tickets may enter application logs.
        logger.info("video_input_measurement_unavailable")
        return None
    finally:
        active.remove(partner_id)


async def _prepare(db, partner_id, original, video, edit):
    refs = video["reference_videos"]
    upper = 30 if video["model"] in SEEDANCE_25_FAMILY else 15
    maximum = 100 * 1024 * 1024 if upper == 30 else 50_000_000
    original_urls = {_url(item["url"]) for item in refs}
    assets, durations, cached = [], [], {}
    adapter = None
    async with public_http_client(timeout=httpx.Timeout(30, connect=5, pool=5)) as client:
        for item in refs:
            source = item["url"]
            if source in cached:
                asset, seconds = cached[source]
            else:
                with tempfile.TemporaryFile(prefix="neironych-video-") as file:
                    digest, size = await _download(client, source, file, maximum=maximum)
                    bound = await _inspect(file, digest=digest, size=size)
                    if bound is None or not Fraction(4 if edit else 2) <= bound.duration <= upper:
                        raise MeasurementUnavailable("unproven_duration")
                    if bound.sha256 != digest:
                        raise MeasurementUnavailable("measurement_identity_mismatch")
                    seconds = bound.duration
                    if adapter is None:
                        adapter = await _upload_adapter(db, partner_id)
                    asset = await _stage(
                        client, adapter, file, model=video["model"], digest=digest, size=size,
                        content_type=bound.content_type, original_urls=original_urls,
                    )
                    asset.update(duration=str(seconds), video_duration=str(bound.video_duration),
                                 audio_duration=str(bound.audio_duration) if bound.audio_duration else None)
                cached[source] = asset, seconds
            assets.append(dict(asset))
            durations.append(seconds)
    if sum(durations) > upper:
        raise MeasurementUnavailable("reference_duration_limit")
    def ceil(value):
        return (value.numerator + value.denominator - 1) // value.denominator

    output = ceil(durations[0]) if edit else int(video.get("duration", 5))
    billable = output + sum(ceil(value) for value in durations)
    if billable >= video_reserve_seconds(original):
        return None
    return {
        "policy": POLICY,
        "body": _replace_video_urls(original, [asset["url"] for asset in assets]),
        "billable_seconds": billable,
        "output_seconds": None if edit else output,
        "expires_at": min(asset["expires_at"] for asset in assets),
        "assets": assets,
    }
