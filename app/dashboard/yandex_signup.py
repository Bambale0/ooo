"""Independent web registration after a verified Yandex authorization code exchange."""

import hashlib
import hmac
import json
import re
import secrets
from html import escape

import redis.asyncio as redis
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from redis.exceptions import RedisError
from sqlalchemy import select

from app.accounts.models import ConsentAcceptance, Partner, PartnerApplication, PartnerIdentity
from app.accounts.router import auto_activate_application
from app.api.dependencies import DbSession
from app.infrastructure.config import get_settings
from app.infrastructure.security import hash_secret
from app.providers.key_pool import lock_key_hashes

router = APIRouter()
_COOKIE = "__Host-neironych_signup"
_TTL = 600


def _key(token: str) -> str:
    return "cabinet:yandex-signup:" + hashlib.sha256(token.encode()).hexdigest()


async def _save(token: str, data: dict) -> None:
    client = redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=3)
    try:
        await client.set(_key(token), json.dumps(data), ex=_TTL)
    finally:
        await client.aclose()


async def _read(token: str, *, consume: bool = False) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    client = redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=3)
    try:
        value = await client.getdel(_key(token)) if consume else await client.get(_key(token))
        return json.loads(value) if value else None
    finally:
        await client.aclose()


async def begin_signup(subject: str) -> HTMLResponse:
    from app.dashboard.web import _HEADERS, _LOGIN_COOKIE
    from app.dashboard.yandex import _COOKIE as oauth_cookie

    token = secrets.token_urlsafe(32)
    await _save(token, {
        "subject": subject, "csrf": secrets.token_urlsafe(32),
        "legal_version": get_settings().legal_document_version,
    })
    # A same-origin document allows the following navigation to send Strict cookies.
    response = HTMLResponse(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0;url=/cabinet/yandex/register">'
        '<title>Регистрация</title></head><body>'
        '<a href="/cabinet/yandex/register">Продолжить регистрацию</a></body></html>', headers=_HEADERS,
    )
    response.set_cookie(_COOKIE, token, max_age=_TTL, secure=True, httponly=True, samesite="strict", path="/")
    response.delete_cookie(oauth_cookie, secure=True, httponly=True, samesite="lax", path="/")
    response.delete_cookie(_LOGIN_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
    return response


def _form(data: dict, *, message: str = "", status: int = 200) -> HTMLResponse:
    from app.dashboard.web import _document
    from app.dashboard.yandex import _failure

    settings = get_settings()
    if not settings.terms_url or not settings.privacy_policy_url:
        return _failure(503, "yandex_login_unavailable")
    csrf, version = escape(data["csrf"], quote=True), escape(data["legal_version"])
    response = _document("Регистрация через Яндекс ID", (
        '<section class="login-card"><h1>Регистрация через Яндекс ID</h1>'
        '<p>Яндекс ID подтверждён. Telegram для регистрации и входа не требуется.</p>'
        '<p>Если у вас уже есть аккаунт с балансом, войдите в него и подключите Яндекс '
        'в настройках. Эта регистрация создаёт отдельный аккаунт.</p>'
        f'<p role="status">{escape(message)}</p>'
        '<form class="signup-form" method="post" action="/cabinet/yandex/register">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        '<label>Компания или имя<input name="company_name" required minlength="2" maxlength="255"></label>'
        '<label>Название проекта<input name="project_name" required minlength="2" maxlength="255"></label>'
        f'<p>Версия документов: {version}</p>'
        '<label class="consent"><input type="checkbox" name="accepted_terms" value="yes" required> <span>'
        f'Принимаю <a href="{escape(settings.terms_url, quote=True)}" target="_blank" rel="noopener noreferrer">'
        'условия использования</a></span></label>'
        '<label class="consent"><input type="checkbox" name="accepted_privacy" value="yes" required> <span>'
        f'Принимаю <a href="{escape(settings.privacy_policy_url, quote=True)}" target="_blank" '
        'rel="noopener noreferrer">политику конфиденциальности</a></span></label>'
        '<button class="primary" type="submit">Создать аккаунт</button></form>'
        '<a href="/cabinet">Вернуться ко входу</a></section>'
    ))
    response.status_code = status
    return response


@router.get("/cabinet/yandex/register", include_in_schema=False)
async def registration_page(request: Request):
    from app.dashboard.yandex import _failure, configuration_errors, configured

    if not configured() or configuration_errors():
        return _failure(503, "yandex_login_unavailable")
    try:
        data = await _read(request.cookies.get(_COOKIE, ""))
    except RedisError:
        return _failure(503, "cabinet_session_unavailable")
    return _form(data) if data else _failure(403, "invalid_or_expired_oauth_state")


@router.post("/cabinet/yandex/register", include_in_schema=False)
async def register(
    request: Request, db: DbSession, csrf: str = Form(""), company_name: str = Form(""),
    project_name: str = Form(""), accepted_terms: str = Form(""), accepted_privacy: str = Form(""),
):
    from app.dashboard.web import _store_session
    from app.dashboard.yandex import _failure, configuration_errors, configured, session_response

    settings = get_settings()
    if not configured() or configuration_errors():
        return _failure(503, "yandex_login_unavailable")
    if request.headers.get("origin", "") != settings.public_api_base_url.rstrip("/"):
        return _failure(403, "invalid_login_origin")
    token = request.cookies.get(_COOKIE, "")
    try:
        data = await _read(token)
        if not data or not csrf or not hmac.compare_digest(data["csrf"], csrf):
            return _failure(403, "invalid_login_challenge")
        if data["legal_version"] != settings.legal_document_version:
            return _failure(403, "invalid_or_expired_oauth_state")
        if not settings.terms_url or not settings.privacy_policy_url:
            return _failure(503, "yandex_login_unavailable")
        company_name, project_name = company_name.strip(), project_name.strip()
        if (not (2 <= len(company_name) <= 255 and 2 <= len(project_name) <= 255)
                or any(ord(ch) < 32 for ch in company_name + project_name)):
            return _form(data, message="Укажите имя или компанию и проект (от 2 до 255 символов).", status=422)
        if accepted_terms != "yes" or accepted_privacy != "yes":
            return _form(data, message="Для регистрации примите оба документа.", status=422)
        subject = data["subject"]
        # The same lock is used by activation and pool assignment. Concurrent
        # browser flows for one subject cannot create accounts or claim keys twice.
        await lock_key_hashes(db, [hash_secret(f"partner-registration:yandex:{subject}")])
        identity = await db.scalar(select(PartnerIdentity).where(
            PartnerIdentity.provider == "yandex", PartnerIdentity.subject == subject,
        ))
        partner = await db.get(Partner, identity.partner_id) if identity else None
        if identity is None:
            application = await db.scalar(
                select(PartnerApplication).where(PartnerApplication.yandex_subject == subject)
            )
            if application is None:
                application = PartnerApplication(
                    telegram_id=None, yandex_subject=subject, company_name=company_name, project_name=project_name,
                    terms_version=data["legal_version"], privacy_policy_version=data["legal_version"],
                )
                db.add(application)
                await db.flush()
                for kind in ("terms", "privacy_policy"):
                    db.add(ConsentAcceptance(
                        telegram_id=None, partner_application_id=application.id, document_type=kind,
                        document_version=data["legal_version"],
                    ))
            if application.status != "pending":
                return _failure(403, "partner_account_not_active")
            application.company_name, application.project_name = company_name, project_name
            if application.terms_version != data["legal_version"]:
                application.terms_version = application.privacy_policy_version = data["legal_version"]
                for kind in ("terms", "privacy_policy"):
                    db.add(ConsentAcceptance(
                        telegram_id=None, partner_application_id=application.id, document_type=kind,
                        document_version=data["legal_version"],
                    ))
            try:
                async with db.begin_nested():
                    partner = await auto_activate_application(application.id, db)
                    await db.flush()
            except HTTPException as exc:
                if exc.status_code not in {409, 503}:
                    raise
                await db.commit()
                return _form(data, message="Регистрация временно недоступна. Повторите попытку позже.", status=503)
        if partner is None or partner.status != "active":
            return _failure(403, "partner_account_not_active")
        if await _read(token, consume=True) != data:
            await db.rollback()
            return _failure(403, "invalid_or_expired_oauth_state")
        await db.commit()
        session = secrets.token_urlsafe(32)
        await _store_session(session, partner.id)
        return session_response(session)
    except RedisError:
        await db.rollback()
        return _failure(503, "cabinet_session_unavailable")
