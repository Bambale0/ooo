from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from app.accounts.models import Partner
from app.api.dependencies import DbSession, get_current_partner
from app.media.models import MediaAsset
from app.media.storage import get_media_storage

router = APIRouter()


@router.get("/{asset_id}/content")
async def read_media_content(
    asset_id: str,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> Response:
    asset = await db.get(MediaAsset, asset_id)
    if asset is None or asset.partner_id != partner.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media_asset_not_found")
    if asset.status != "stored":
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"detail": "asset_not_ingested", "status": asset.status},
        )
    if not asset.storage_key:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "media_storage_key_missing"},
        )
    storage = get_media_storage()
    local_path = await storage.local_path(asset.storage_key)
    if local_path is not None:
        return FileResponse(local_path, media_type=asset.content_type or "video/mp4")
    return RedirectResponse(asset.public_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
