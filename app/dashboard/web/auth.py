"""Web UI authentication for Partner Cabinet."""

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select

from app.accounts.models import Partner
from app.api.dependencies import DbSession
from app.dashboard.web.session import verify_session_token


async def get_session_partner(
    db: DbSession,
    session_token: Annotated[str | None, Cookie(alias="cabinet_session")] = None,
) -> Partner:
    """Get authenticated partner from session cookie."""
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    
    # Verify token
    partner_id = verify_session_token(session_token)
    if not partner_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session",
        )
    
    # Fetch partner
    query = select(Partner).where(Partner.id == partner_id)
    result = await db.execute(query)
    partner = result.scalar_one_or_none()
    
    if not partner or partner.status == "deleted":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Partner not found",
        )
    
    return partner


def require_web_auth(partner: Partner = Depends(get_session_partner)) -> Partner:
    """Require authenticated partner."""
    return partner

