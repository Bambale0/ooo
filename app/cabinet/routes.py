"""Partner web cabinet: Telegram identity, revocable sessions and tenant-scoped pages."""

import json
import secrets
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select, update

from app.accounts.models import Partner
from app.api.dependencies import DbSession
from app.cabinet.models import CabinetWebSession
from app.cabinet.security import (
    LOGIN_NONCE_COOKIE,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    hash_browser_token,
    is_secure_cookie,
    issue_browser_token,
    verify_telegram_login,
)
from app.cabinet.ui import SECTIONS, render_cabinet, render_login
from app.infrastructure.config import get_settings

router = APIRouter(include_in_schema=False)

PRIVATE_HEADERS = {
    "Cache-Control": "no-store, private",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; "
        "script-src 'self' https://telegram.org; frame-src https://oauth.telegram.org https://telegram.org; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}


async def _authenticated_partner(request: Request, db: DbSession) -> Partner | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not 40 <= len(token) <= 256:
        return None
    row = (
        await db.execute(
            select(Partner)
            .join(CabinetWebSession, CabinetWebSession.partner_id == Partner.id)
            .where(
                CabinetWebSession.token_hash == hash_browser_token(token),
                CabinetWebSession.expires_at > datetime.now(UTC),
                CabinetWebSession.revoked_at.is_(None),
                Partner.status == "active",
            )
        )
    ).scalar_one_or_none()
    return row


def _private_html(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers=PRIVATE_HEADERS)


def _private_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303, headers=PRIVATE_HEADERS)


@router.get("/cabinet/login")
async def web_login(request: Request, db: DbSession) -> Response:
    if await _authenticated_partner(request, db):
        return _private_redirect("/cabinet/overview")
    response = _private_html(render_login(get_settings().telegram_bot_username))
    response.set_cookie(
        LOGIN_NONCE_COOKIE, secrets.token_urlsafe(24),
        path="/cabinet", httponly=True, secure=is_secure_cookie(),
        samesite="lax", max_age=300,
    )
    return response


async def _small_json_body(request: Request) -> dict[str, str | int]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "json_required")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > 4096:
            raise HTTPException(413, "login_payload_too_large")
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "invalid_json") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "invalid_json")
    return data


@router.post("/cabinet/auth/telegram")
async def web_telegram_auth(request: Request, db: DbSession) -> Response:
    if not request.cookies.get(LOGIN_NONCE_COOKIE):
        raise HTTPException(401, "login_challenge_required")
    # Strict same-origin POST; no unsigned login parameters in URLs or access logs.
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.scheme not in {"https", "http"} or parsed.netloc != request.headers.get("host"):
            raise HTTPException(403, "origin_rejected")
    user_id = verify_telegram_login(await _small_json_body(request))
    partner = (
        await db.execute(select(Partner).where(Partner.telegram_id == user_id, Partner.status == "active"))
    ).scalar_one_or_none()
    if partner is None:
        raise HTTPException(403, "partner_account_required")
    token, token_hash = issue_browser_token()
    db.add(
        CabinetWebSession(
            partner_id=partner.id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(seconds=SESSION_TTL_SECONDS),
        )
    )
    await db.flush()
    response = JSONResponse({"ok": True}, headers=PRIVATE_HEADERS)
    response.set_cookie(
        SESSION_COOKIE, token, path="/cabinet", httponly=True,
        secure=is_secure_cookie(), samesite="strict", max_age=SESSION_TTL_SECONDS,
    )
    response.delete_cookie(LOGIN_NONCE_COOKIE, path="/cabinet")
    return response


@router.post("/cabinet/logout")
async def web_logout(request: Request, db: DbSession) -> Response:
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.scheme not in {"https", "http"} or parsed.netloc != request.headers.get("host"):
            raise HTTPException(403, "origin_rejected")
    token = request.cookies.get(SESSION_COOKIE, "")
    if 40 <= len(token) <= 256:
        await db.execute(
            update(CabinetWebSession)
            .where(CabinetWebSession.token_hash == hash_browser_token(token))
            .values(revoked_at=datetime.now(UTC))
        )
    response = _private_redirect("/cabinet/login")
    response.delete_cookie(SESSION_COOKIE, path="/cabinet")
    return response


@router.get("/cabinet/assets/{asset}")
async def cabinet_asset(asset: str) -> Response:
    allowed = {"cabinet.css": "text/css", "cabinet-login.js": "text/javascript"}
    if asset not in allowed:
        raise HTTPException(404)
    return Response(
        files("app.cabinet").joinpath("static", asset).read_bytes(),
        media_type=allowed[asset],
        headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/cabinet")
async def cabinet_entry(request: Request, db: DbSession) -> Response:
    if not await _authenticated_partner(request, db):
        return _private_redirect("/cabinet/login")
    return _private_redirect("/cabinet/overview")


@router.get("/cabinet/{section}")
async def cabinet_section(section: str, request: Request, db: DbSession) -> Response:
    if section not in SECTIONS:
        raise HTTPException(404)
    partner = await _authenticated_partner(request, db)
    if partner is None:
        return _private_redirect("/cabinet/login")
    return _private_html(await render_cabinet(db, partner, section))
