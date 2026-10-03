import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.accounts.models import Partner
from app.api.dependencies import DbSession, get_current_partner
from app.infrastructure.config import get_settings
from app.media.models import MediaAsset
from app.media.service import result_download_signature
from app.providers.base import ProviderAdapterError
from app.providers.models import ProviderAttempt
from app.providers.service import get_partner_provider_adapter

router = APIRouter()


@router.get("/trials/{generation_id}/{expires}/{token}", include_in_schema=False)
async def trial_content(generation_id: str, expires: int, token: str, request: Request, db: DbSession):
    import hmac
    import time

    from app.generations.models import Generation
    from app.telegram.trials import signature

    generation = await db.get(Generation, generation_id)
    if (
        generation is None
        or not (generation.request_payload or {}).get("trial_telegram_id")
        or not int(time.time()) < expires <= int(time.time()) + 86400
        or not hmac.compare_digest(token, signature(generation.id, generation.partner_id, expires))
    ):
        raise HTTPException(404, "content_not_available")
    partner = await db.get(Partner, generation.partner_id)
    if not partner or partner.status != "active":
        raise HTTPException(404, "content_not_available")
    asset = (await db.execute(select(MediaAsset).where(MediaAsset.generation_id == generation.id))).scalar_one_or_none()
    if not asset:
        raise HTTPException(404, "content_not_available")
    response = await read_media_content(asset.id, request, db, partner)
    response.headers.update({"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
    return response


@router.get("/results/{generation_id}/{expires}/{token}", include_in_schema=False)
async def result_content(generation_id: str, expires: int, token: str, request: Request, db: DbSession):
    from app.generations.models import Generation

    settings = get_settings()
    now = int(time.time())
    generation = await db.get(Generation, generation_id)
    if (
        generation is None
        or generation.status != "completed"
        or not now < expires <= now + settings.media_share_link_ttl_seconds + 60
        or len(token) != 64
        or not hmac.compare_digest(token, result_download_signature(generation.id, generation.partner_id, expires))
    ):
        raise HTTPException(404, "content_not_available")
    partner = await db.get(Partner, generation.partner_id)
    if partner is None or partner.status != "active":
        raise HTTPException(404, "content_not_available")
    asset = (
        await db.execute(
            select(MediaAsset).where(
                MediaAsset.generation_id == generation.id,
                MediaAsset.partner_id == partner.id,
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(404, "content_not_available")
    response = await read_media_content(asset.id, request, db, partner)
    response.headers.update({"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
    return response


@router.get("/{asset_id}/content")
async def read_media_content(
    asset_id: str,
    request: Request,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> StreamingResponse:
    asset = await db.get(MediaAsset, asset_id)
    if asset is None or asset.partner_id != partner.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media_asset_not_found")
    if asset.status != "provider_ready":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="media_asset_not_ready")

    attempt = (
        await db.execute(
            select(ProviderAttempt).where(
                ProviderAttempt.generation_id == asset.generation_id,
                ProviderAttempt.provider == asset.provider,
            )
        )
    ).scalar_one_or_none()
    adapter = await get_partner_provider_adapter(
        db,
        partner.id,
        asset.provider,
        **({"credential_id": attempt.credential_id} if attempt and attempt.credential_id else {}),
    )
    try:
        provider_stream = await adapter.open_result_stream(
            asset.provider_content_url,
            range_header=request.headers.get("range"),
        )
    except ProviderAdapterError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=exc.public_code,
        ) from exc

    response_headers: dict[str, str] = {}
    if provider_stream.content_length is not None:
        response_headers["Content-Length"] = str(provider_stream.content_length)
    if provider_stream.content_range:
        response_headers["Content-Range"] = provider_stream.content_range
    if provider_stream.accept_ranges:
        response_headers["Accept-Ranges"] = provider_stream.accept_ranges

    return StreamingResponse(
        provider_stream.body,
        status_code=provider_stream.status_code,
        media_type=provider_stream.content_type or asset.content_type or "application/octet-stream",
        headers=response_headers,
    )
