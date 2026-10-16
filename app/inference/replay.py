"""Transactional result recovery with tenant isolation and bounded retention."""

import base64
import zlib
from datetime import UTC, timedelta

from fastapi.responses import JSONResponse, Response

from app.generations.models import NativeResponse
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.infrastructure.security import decrypt_secret, encrypt_secret


def result_unavailable(generation, code, status=410):
    return JSONResponse(
        {"error": {"type": code, "message": code}, "request_id": generation.id},
        status_code=status,
        headers={"X-Request-Id": generation.id},
    )


async def save_response(db, generation, response):
    """Called under the generation lock, in the settlement/outbox transaction."""
    if await db.get(NativeResponse, generation.id) is not None:
        return
    settings = get_settings()
    if len(response.body) > settings.native_response_max_bytes:
        raise ValueError("response_too_large")
    encoded = base64.b64encode(zlib.compress(response.body, 1)).decode("ascii")
    row = NativeResponse(
        generation_id=generation.id,
        status_code=response.status_code,
        media_type=response.media_type or "application/json",
        headers={
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"retry-after", "cache-control", "x-accel-buffering"}
        },
        encrypted_body=encrypt_secret(encoded, settings.provider_credentials_master_key),
        body_size=len(response.body),
        expires_at=utc_now() + timedelta(seconds=settings.native_response_replay_ttl_seconds),
    )
    db.add(row)
    await db.flush()


async def replay_response(db, generation):
    """Generation was resolved by partner + idempotency key before this call."""
    row = await db.get(NativeResponse, generation.id)
    if row is None:
        if generation.client_finalized_at is not None:
            return result_unavailable(generation, "idempotent_result_expired")
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if expires <= utc_now():
        return result_unavailable(generation, "idempotent_result_expired")
    settings = get_settings()
    try:
        encoded = decrypt_secret(row.encrypted_body, settings.provider_credentials_master_key)
        compressed = base64.b64decode(encoded, validate=True)
        decoder = zlib.decompressobj()
        body = decoder.decompress(compressed, settings.native_response_max_bytes + 1)
        if not decoder.eof or len(body) != row.body_size or len(body) > settings.native_response_max_bytes:
            raise ValueError("invalid_replay_size")
    except (ValueError, zlib.error):
        return result_unavailable(generation, "result_temporarily_unavailable", 503)
    return Response(
        content=body,
        status_code=row.status_code,
        media_type=row.media_type,
        headers={**row.headers, "X-Request-Id": generation.id},
    )
