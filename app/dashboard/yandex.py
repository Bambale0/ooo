"""Browser-bound authorization code + PKCE, independent signup and explicit linking."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.accounts.models import Partner, PartnerIdentity
from app.api.dependencies import DbSession
from app.infrastructure.config import get_settings

router = APIRouter()
_COOKIE = "__Host-neironych_oauth"
_PROOF_COOKIE = "__Host-neironych_telegram_proof"
_TTL = 300


def configured() -> bool:
    s = get_settings()
    return bool(s.yandex_client_id and s.yandex_client_secret and s.yandex_callback_url)


def configuration_errors(settings=None) -> list[str]:
    s = settings or get_settings()
    values = (s.yandex_client_id, s.yandex_client_secret, s.yandex_callback_url)
    if not any(values):
        return []
    if not all(values) or not s.yandex_client_secret.get_secret_value():
        return ["YANDEX_CLIENT_ID, YANDEX_CLIENT_SECRET and YANDEX_CALLBACK_URL must be set together"]
    try:
        callback = urlsplit(s.yandex_callback_url)
        public = urlsplit(s.public_api_base_url)
    except ValueError:
        return ["YANDEX_CALLBACK_URL and PUBLIC_API_BASE_URL must be valid HTTPS URLs"]
    if (
        callback.scheme != "https"
        or not callback.hostname
        or callback.username
        or callback.password
        or (callback.scheme, callback.netloc) != (public.scheme, public.netloc)
        or callback.path != "/cabinet/yandex/callback"
        or callback.query
        or callback.fragment
    ):
        return ["YANDEX_CALLBACK_URL must be the exact public HTTPS /cabinet/yandex/callback URL"]
    return []


def telegram_proof(session: str) -> str:
    from app.dashboard.web import _csrf_for_session

    timestamp = str(int(time.time()))
    signature = _csrf_for_session(session + ":" + timestamp, get_settings().telegram_bot_token or "")
    return timestamp + "." + signature


def _recent_telegram(request: Request, session: str) -> bool:
    proof = request.cookies.get(_PROOF_COOKIE, "")
    timestamp, _, signature = proof.partition(".")
    if (
        not re.fullmatch(r"[0-9]{1,12}", timestamp)
        or not re.fullmatch(r"[a-f0-9]{64}", signature)
        or not 0 <= time.time() - int(timestamp) <= _TTL
    ):
        return False
    from app.dashboard.web import _csrf_for_session

    expected = _csrf_for_session(session + ":" + timestamp, get_settings().telegram_bot_token or "")
    return hmac.compare_digest(expected, signature)


def _flow_key(state: str, browser: str) -> str:
    return "cabinet:oauth:" + hashlib.sha256((state + ":" + browser).encode()).hexdigest()


async def _save_flow(state: str, browser: str, data: dict) -> None:
    client = redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=3)
    try:
        await client.set(_flow_key(state, browser), json.dumps(data), ex=_TTL)
    finally:
        await client.aclose()


async def _take_flow(state: str, browser: str) -> dict | None:
    client = redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=3)
    try:
        value = await client.getdel(_flow_key(state, browser))
        return json.loads(value) if value else None
    finally:
        await client.aclose()


async def _bound_session(key: str, *, destroy: bool = False) -> str | None:
    client = redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=3)
    try:
        return await client.getdel(key) if destroy else await client.get(key)
    finally:
        await client.aclose()


async def _subject(code: str, verifier: str) -> str:
    s = get_settings()
    # Fixed endpoints, verified TLS, no redirects, no provider tokens in browser/storage/logs.
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        token = await client.post(
            "https://oauth.yandex.ru/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": s.yandex_client_id,
                "client_secret": s.yandex_client_secret.get_secret_value(),
                "redirect_uri": s.yandex_callback_url,
                "code_verifier": verifier,
            },
        )
        token.raise_for_status()
        access = token.json()["access_token"]
        if not isinstance(access, str) or not access or len(access) > 8192:
            raise ValueError("invalid_token")
        info = await client.get(
            "https://login.yandex.ru/info",
            params={"format": "json"},
            headers={"Authorization": "OAuth " + access},
        )
        info.raise_for_status()
        profile = info.json()
        if not isinstance(profile, dict):
            raise ValueError("invalid_identity")
        subject = profile.get("id")
        if profile.get("client_id") != s.yandex_client_id or not isinstance(subject, str):
            raise ValueError("invalid_identity")
        if not re.fullmatch(r"[0-9]{1,128}", subject):
            raise ValueError("invalid_identity")
        return subject


def _failure(status: int, detail: str) -> HTMLResponse:
    from app.dashboard.web import _document

    messages = {
        "sign_in_with_telegram_again_before_linking": (
            "Войдите заново через Telegram и подключите Яндекс ID в настройках."
        ),
        "sign_in_with_telegram_and_link_yandex_in_settings": (
            "Этот Яндекс ID ещё не подключён. Войдите через Telegram и подключите его в настройках кабинета."
        ),
        "identity_already_linked": "Яндекс ID уже подключён к другому аккаунту или в кабинете уже есть привязка.",
        "yandex_authorization_cancelled": "Вы отменили вход через Яндекс ID.",
        "yandex_login_unavailable": "Вход через Яндекс ID пока не настроен. Используйте Telegram.",
        "yandex_identity_unavailable": "Яндекс ID временно недоступен. Попробуйте войти ещё раз.",
    }
    message = messages.get(detail, "Не удалось подтвердить вход. Вернитесь в кабинет и попробуйте ещё раз.")
    response = _document(
        "Вход через Яндекс ID",
        '<section class="login-card"><h1>Вход через Яндекс ID</h1>'
        f'<p>{message}</p><a href="/cabinet">Вернуться в кабинет</a></section>',
    )
    response.status_code = status
    response.headers["Referrer-Policy"] = "no-referrer"
    response.delete_cookie(_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
    return response


@router.post("/cabinet/yandex/start", include_in_schema=False)
async def start(request: Request, db: DbSession, csrf: str = Form(...), mode: str = Form("login")):
    from app.dashboard.portal import _check_write
    from app.dashboard.web import _HEADERS, _LOGIN_COOKIE, _partner_from_cookie, _session_key

    if not configured() or configuration_errors():
        return _failure(503, "yandex_login_unavailable")
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,120}", csrf):
        return _failure(403, "invalid_login_challenge")
    partner, session = await _partner_from_cookie(request, db)
    bound = {}
    if mode == "link":
        if partner is None or session is None:
            return _failure(401, "cabinet_not_authenticated")
        _check_write(request, session, csrf)
        if not _recent_telegram(request, session):
            return _failure(403, "sign_in_with_telegram_again_before_linking")
        bound = {"partner_id": partner.id, "session_key": _session_key(session)}
    elif mode in {"login", "register"}:
        if partner is not None:
            return RedirectResponse("/cabinet", status_code=303, headers=_HEADERS)
        origin = request.headers.get("origin", "")
        if origin != get_settings().public_api_base_url.rstrip("/"):
            return _failure(403, "invalid_login_origin")
        challenge = request.cookies.get(_LOGIN_COOKIE, "")
        if not challenge or not hmac.compare_digest(challenge, csrf):
            return _failure(403, "invalid_login_challenge")
    else:
        return _failure(400, "invalid_login_mode")
    state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    try:
        await _save_flow(state, browser, {"verifier": verifier, "mode": mode, **bound})
    except RedisError:
        return _failure(503, "cabinet_session_unavailable")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = RedirectResponse(
        "https://oauth.yandex.ru/authorize?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": get_settings().yandex_client_id,
                "redirect_uri": get_settings().yandex_callback_url,
                "scope": "login:info",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "force_confirm": "yes",
            }
        ),
        status_code=303,
        headers=_HEADERS,
    )
    # Only this short-lived nonce uses Lax so it survives the provider's top-level GET.
    response.set_cookie(_COOKIE, browser, max_age=_TTL, secure=True, httponly=True, samesite="lax", path="/")
    return response


@router.get("/cabinet/yandex/callback", include_in_schema=False)
async def callback(request: Request, db: DbSession):
    from app.dashboard.web import _store_session

    if not configured() or configuration_errors():
        return _failure(503, "yandex_login_unavailable")
    state = request.query_params.get("state", "")
    browser = request.cookies.get(_COOKIE, "")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", browser)
        or any(len(request.query_params.getlist(key)) > 1 for key in ("state", "code", "error"))
    ):
        return _failure(403, "invalid_oauth_state")
    try:
        flow = await _take_flow(state, browser)
        if not flow:
            return _failure(403, "invalid_or_expired_oauth_state")
        if request.query_params.get("error"):
            return _failure(400, "yandex_authorization_cancelled")
        code = request.query_params.get("code", "")
        if not code or len(code) > 2048:
            return _failure(400, "invalid_oauth_code")
        if flow["mode"] == "link" and await _bound_session(flow["session_key"]) != flow["partner_id"]:
            return _failure(403, "link_session_expired")
        try:
            subject = await _subject(code, flow["verifier"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return _failure(502, "yandex_identity_unavailable")
        from app.infrastructure.security import hash_secret
        from app.providers.key_pool import lock_key_hashes

        await lock_key_hashes(db, [hash_secret(f"partner-registration:yandex:{subject}")])
        identity = (
            await db.execute(
                select(PartnerIdentity).where(
                    PartnerIdentity.provider == "yandex",
                    PartnerIdentity.subject == subject,
                )
            )
        ).scalar_one_or_none()
        if flow["mode"] == "link":
            partner = await db.get(Partner, flow["partner_id"])
            if partner is None or partner.status != "active":
                return _failure(403, "partner_account_not_active")
            if identity and identity.partner_id != partner.id:
                return _failure(409, "identity_already_linked")
            # Consume the initiating session before durable mutation. Logout during exchange wins.
            if await _bound_session(flow["session_key"], destroy=True) != partner.id:
                return _failure(403, "link_session_expired")
            if identity is None:
                try:
                    async with db.begin_nested():
                        db.add(PartnerIdentity(partner_id=partner.id, provider="yandex", subject=subject))
                        await db.flush()
                except IntegrityError:
                    return _failure(409, "identity_already_linked")
            await db.commit()
        else:
            partner = await db.get(Partner, identity.partner_id) if identity else None
            if partner is None:
                if identity is not None:
                    return _failure(403, "partner_account_not_active")
                from app.dashboard.yandex_signup import begin_signup

                return await begin_signup(subject)
            if partner.status != "active":
                return _failure(403, "partner_account_not_active")
        token = secrets.token_urlsafe(32)
        await _store_session(token, partner.id)
    except RedisError:
        return _failure(503, "cabinet_session_unavailable")
    return session_response(token)


def session_response(token: str) -> HTMLResponse:
    from app.dashboard.web import _HEADERS, _LOGIN_COOKIE, _SESSION_COOKIE, _SESSION_TTL

    # A same-origin document starts a fresh navigation so Strict session cookies work
    # even in browsers that retain a cross-site redirect chain after OAuth.
    response = HTMLResponse(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0;url=/cabinet?section=settings">'
        "<title>Вход выполнен</title></head><body>"
        '<a href="/cabinet?section=settings">Открыть личный кабинет</a></body></html>',
        headers=_HEADERS,
    )
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=_SESSION_TTL,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.delete_cookie(_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
    response.delete_cookie(_LOGIN_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
    response.delete_cookie(_PROOF_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
    response.delete_cookie("__Host-neironych_signup", secure=True, httponly=True, samesite="strict", path="/")
    return response


from app.dashboard.yandex_signup import router as signup_router  # noqa: E402

router.include_router(signup_router)
