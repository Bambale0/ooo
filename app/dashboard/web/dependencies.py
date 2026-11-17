"""Dependencies for web UI authentication and context."""

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.accounts.models import Partner
from app.api.dependencies import DbSession
from app.infrastructure.security import verify_session_token


async def get_session_partner(
    request: Request,
    db: DbSession,
    session_token: Annotated[str | None, Cookie(alias="partner_session")] = None,
) -> Partner:
    """Get authenticated partner from session cookie."""
    if not session_token:
        # Redirect to login page
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Not authenticated",
            headers={"Location": "/cabinet/login"}
        )
    
    # Verify session token and extract partner_id
    partner_id = verify_session_token(session_token)
    if not partner_id:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Invalid session",
            headers={"Location": "/cabinet/login"}
        )
    
    # Fetch partner
    query = select(Partner).where(Partner.id == partner_id)
    result = await db.execute(query)
    partner = result.scalar_one_or_none()
    
    if not partner or partner.status == "deleted":
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Partner not found",
            headers={"Location": "/cabinet/login"}
        )
    
    return partner


def require_web_auth(partner: Partner = Depends(get_session_partner)) -> Partner:
    """Require authenticated partner for web UI."""
    return partner
