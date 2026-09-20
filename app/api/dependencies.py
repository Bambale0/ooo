from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import ApiKey, Partner
from app.infrastructure.config import get_settings
from app.infrastructure.database import get_db_session
from app.infrastructure.security import constant_time_equals, hash_secret

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def require_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = f"Bearer {get_settings().admin_api_token}"
    if authorization is None or not constant_time_equals(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin_auth_required")


async def get_current_partner(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Partner:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="api_key_required")
    token_hash = hash_secret(authorization.removeprefix("Bearer ").strip())
    result = await db.execute(
        select(Partner)
        .join(ApiKey, ApiKey.partner_id == Partner.id)
        .where(ApiKey.key_hash == token_hash, ApiKey.is_active.is_(True), Partner.status == "active")
    )
    partner = result.scalar_one_or_none()
    if partner is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_api_key")
    return partner
