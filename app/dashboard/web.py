"""Server-rendered partner cabinet with verified Telegram sign-in and revocable sessions."""

import hashlib
import hmac
import re
import secrets
import time
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
from urllib.parse import urlsplit

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError
from sqlalchemy import func, select

from app.accounts.models import ApiKey, Partner
from app.api.dependencies import DbSession
from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.payments.models import PaymentInvoice

router = APIRouter(tags=["Partner web cabinet"])
_SESSION_COOKIE = "__Host-neironych_session"
_LOGIN_COOKIE = "__Host-neironych_login"
_SESSION_TTL = 12 * 3600
_AUTH_TTL = 300
_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_STATUS = {
    "active": "Активен",
    "queued": "В очереди",
    "submitting": "Отправляется",
    "processing": "В работе",
    "completed": "Готово",
    "failed": "Ошибка",
    "cancelled": "Отменено",
    "paid": "Оплачен",
    "credited": "Зачислен",
    "creating": "Создаётся",
    "expired": "Истёк",
}


class TelegramLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    auth_date: int = Field(gt=0)
    hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    csrf: str = Field(min_length=30, max_length=120)
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None


def _verify_telegram_login(data: TelegramLogin, bot_token: str, *, now: int | None = None) -> bool:
    """Verify the Telegram Login Widget HMAC and its short freshness window."""
    timestamp = int(time.time()) if now is None else now
    if data.auth_date < timestamp - _AUTH_TTL or data.auth_date > timestamp + 30:
        return False
    fields = data.model_dump(exclude={"hash", "csrf"}, exclude_none=True)
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, data.hash.lower())


def _csrf_for_session(session_id: str, bot_token: str) -> str:
    return hmac.new(
        hashlib.sha256(("cabinet-csrf:" + bot_token).encode("utf-8")).digest(),
        session_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _session_key(token: str) -> str:
    return "cabinet:session:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _store_session(token: str, partner_id: str) -> None:
    client = redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=3)
    try:
        await client.set(_session_key(token), partner_id, ex=_SESSION_TTL)
    finally:
        await client.aclose()


async def _read_session(token: str) -> str | None:
    client = redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=3)
    try:
        return await client.get(_session_key(token))
    finally:
        await client.aclose()


async def _destroy_session(token: str) -> None:
    client = redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=3)
    try:
        await client.delete(_session_key(token))
    finally:
        await client.aclose()


def _money(value: Decimal | None) -> str:
    return f"{Decimal(value or 0):,.2f}".replace(",", " ") + " ₽"


def _date(value: object) -> str:
    if not isinstance(value, datetime):
        return "—"
    utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return utc_value.strftime("%d.%m.%Y %H:%M UTC")


def _safe(value: object) -> str:
    return escape(str(value if value is not None else "—"), quote=True)


def _document(title: str, body: str, *, login: bool = False, csrf: str = "") -> HTMLResponse:
    extra = (
        '<script src="/ui/cabinet.js?v=1" defer></script>'
        f'<meta name="tg-login-csrf" content="{escape(csrf, quote=True)}">'
        if login
        else ""
    )
    html = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f'<title>{_safe(title)} — Нейроныч</title>'
        '<link rel="icon" href="/ui/logo-mark.svg" type="image/svg+xml">'
        '<link rel="stylesheet" href="/ui/cabinet.css?v=1">'
        f'{extra}</head><body><a class="skip" href="#main">К содержимому</a>'
        '<header class="topbar"><a class="brand" href="/">'
        '<img src="/ui/logo-mark.svg" alt="" width="36" height="36"> Нейроныч'
        '<span>API PLATFORM</span></a>'
        '<nav aria-label="Основная навигация">'
        '<a href="/">Главная</a><a href="/prices">Тарифы</a><a href="/docs">Документация</a>'
        '<a aria-current="page" href="/cabinet">Личный кабинет</a></nav></header>'
        f'<main id="main" class="workspace">{body}</main>'
        '<footer class="footer">Нейроныч · Партнёрский кабинет</footer></body></html>'
    )
    return HTMLResponse(html, headers=_HEADERS)


def _login_page(csrf: str) -> HTMLResponse:
    bot = get_settings().telegram_bot_username or ""
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot):
        body = (
            '<section class="login-card"><p class="eyebrow">Личный кабинет</p>'
            '<h1>Вход временно недоступен</h1>'
            '<p>Telegram-бот ещё не настроен для авторизации на сайте.</p></section>'
        )
        return _document("Вход", body)
    body = (
        '<section class="login-card"><p class="eyebrow">Партнёрам</p>'
        '<h1>Личный кабинет</h1>'
        '<p>Войдите через Telegram, привязанный к вашему партнёрскому аккаунту. '
        'Пароль и API-ключ не потребуются.</p>'
        '<div class="telegram-widget">'
        '<script async src="https://telegram.org/js/telegram-widget.js?22"'
        f' data-telegram-login="{escape(bot, quote=True)}"'
        ' data-size="large" data-radius="8" data-userpic="false"'
        ' data-onauth="NeironychTelegramAuth(user)"></script></div>'
        '<p id="login-error" class="error" role="alert" hidden></p>'
        f'<p class="hint">Нет доступа? Откройте <a href="https://t.me/{escape(bot, quote=True)}"'
        ' target="_blank" rel="noopener noreferrer">бота Нейроныч</a> '
        'и проверьте статус партнёрского аккаунта.</p>'
        '</section>'
    )
    return _document("Вход", body, login=True, csrf=csrf)


async def _partner_from_cookie(request: Request, db: DbSession) -> tuple[Partner | None, str | None]:
    session_id = request.cookies.get(_SESSION_COOKIE)
    if not session_id or len(session_id) > 128:
        return None, None
    try:
        partner_id = await _read_session(session_id)
    except RedisError as exc:
        raise HTTPException(503, "cabinet_session_unavailable") from exc
    if not partner_id:
        return None, None
    partner = await db.get(Partner, partner_id)
    if partner is None or partner.status != "active":
        return None, None
    return partner, session_id


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]], empty: str) -> str:
    if not rows:
        return f'<div class="empty">{_safe(empty)}</div>'
    head = "".join(f"<th scope=\"col\">{_safe(name)}</th>" for name in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


@router.get("/cabinet", response_class=HTMLResponse, include_in_schema=False)
async def cabinet(request: Request, db: DbSession) -> HTMLResponse:
    partner, session_id = await _partner_from_cookie(request, db)
    if partner is None or session_id is None:
        csrf = secrets.token_urlsafe(32)
        response = _login_page(csrf)
        response.set_cookie(
            _LOGIN_COOKIE, csrf, max_age=_AUTH_TTL, secure=True, httponly=True, samesite="strict", path="/"
        )
        return response

    keys = (
        (await db.execute(select(ApiKey).where(ApiKey.partner_id == partner.id).order_by(ApiKey.created_at.desc())))
        .scalars()
        .all()
    )
    generations = (
        (await db.execute(
            select(Generation).where(Generation.partner_id == partner.id)
            .order_by(Generation.created_at.desc()).limit(20)
        )).scalars().all()
    )
    ledger = (
        (await db.execute(
            select(LedgerEntry).where(LedgerEntry.partner_id == partner.id)
            .order_by(LedgerEntry.created_at.desc()).limit(20)
        )).scalars().all()
    )
    invoices = (
        (await db.execute(
            select(PaymentInvoice).where(PaymentInvoice.partner_id == partner.id)
            .order_by(PaymentInvoice.created_at.desc()).limit(15)
        )).scalars().all()
    )
    count = (
        await db.execute(select(func.count()).select_from(Generation).where(Generation.partner_id == partner.id))
    ).scalar_one()

    key_rows = [
        (
            _safe(key.name),
            f"<code>{_safe(key.key_prefix)}••••</code>",
            '<span class="status ok">Активен</span>' if key.is_active else '<span class="status">Отключён</span>',
            _safe(_date(key.created_at)),
        )
        for key in keys
    ]
    generation_rows = [
        (
            f"<code>{_safe(gen.id[:8])}</code>",
            _safe(gen.model_slug),
            _safe(_STATUS.get(gen.status, gen.status)),
            _safe(_money(gen.actual_charge_rub)) if gen.actual_charge_rub is not None else "—",
            _safe(_date(gen.created_at)),
        )
        for gen in generations
    ]
    ledger_rows = [
        (_safe(_date(item.created_at)), _safe(item.operation_type), _safe(_money(item.amount_rub)),
         _safe(_money(item.balance_after_rub)))
        for item in ledger
    ]
    invoice_rows = [
        (
            _safe(_date(inv.created_at)),
            _safe(_money(inv.requested_rub)),
            _safe(_STATUS.get(inv.status, inv.status)),
        )
        for inv in invoices
    ]
    csrf = _csrf_for_session(session_id, get_settings().telegram_bot_token or "")
    bot = get_settings().telegram_bot_username or "apineurobrobot"
    body = (
        '<section class="welcome"><div><p class="eyebrow">Ваш кабинет</p>'
        f'<h1>{_safe(partner.project_name)}</h1><p>{_safe(partner.company_name)}</p></div>'
        f'<form action="/cabinet/logout" method="post"><input type="hidden" name="csrf" value="{csrf}">'
        '<button class="outline" type="submit">Выйти</button></form></section>'
        '<nav class="section-nav" aria-label="Разделы кабинета">'
        '<a href="#overview">Обзор</a><a href="#keys">API-ключи</a>'
        '<a href="#generations">Генерации</a><a href="#transactions">Операции</a>'
        '<a href="#invoices">Счета</a></nav>'
        '<section id="overview"><div class="stats">'
        f'<article class="stat"><span>Баланс</span><strong>{_safe(_money(partner.balance_rub))}</strong></article>'
        f'<article class="stat"><span>Доступно с лимитом</span><strong>{_safe(_money(partner.available_balance_rub))}</strong></article>'
        f'<article class="stat"><span>Всего генераций</span><strong>{count}</strong></article>'
        f'<article class="stat"><span>Активных ключей</span><strong>{sum(key.is_active for key in keys)}</strong></article>'
        '</div><div class="help-banner"><p>Пополнение баланса, управление ключами и поддержка '
        'доступны в Telegram-боте.</p>'
        f'<a class="primary" href="https://t.me/{escape(bot, quote=True)}" '
        'target="_blank" rel="noopener noreferrer">Открыть Telegram →</a></div></section>'
        '<section id="keys"><div class="section-heading"><h2>API-ключи</h2>'
        '<p>Секреты не отображаются повторно. Для создания и отзыва ключей откройте бота.</p></div>'
        + _table(("Название", "Префикс", "Статус", "Создан"), key_rows, "Пока нет API-ключей.")
        + '</section><section id="generations"><div class="section-heading"><h2>Генерации</h2>'
        '<p>Последние 20 задач и фактические списания.</p></div>'
        + _table(("ID", "Модель", "Статус", "Списано", "Создана"), generation_rows, "Пока нет генераций.")
        + '</section><section id="transactions"><div class="section-heading"><h2>История баланса</h2>'
        '<p>Последние 20 операций по вашему счёту.</p></div>'
        + _table(("Дата", "Операция", "Сумма", "Баланс после"), ledger_rows, "Пока нет операций.")
        + '</section><section id="invoices"><div class="section-heading"><h2>Счета</h2>'
        '<p>Последние 15 запросов на пополнение.</p></div>'
        + _table(("Дата", "Сумма", "Статус"), invoice_rows, "Пока нет счетов.")
        + '</section>'
    )
    return _document("Личный кабинет", body)


@router.post("/cabinet/telegram", include_in_schema=False)
async def telegram_login(request: Request, payload: TelegramLogin, db: DbSession) -> JSONResponse:
    settings = get_settings()
    allowed = urlsplit(settings.public_api_base_url.rstrip("/"))
    origin = urlsplit(request.headers.get("origin", ""))
    if (origin.scheme, origin.netloc) != (allowed.scheme, allowed.netloc):
        raise HTTPException(403, "invalid_login_origin")
    csrf_cookie = request.cookies.get(_LOGIN_COOKIE, "")
    if not csrf_cookie or not hmac.compare_digest(csrf_cookie, payload.csrf):
        raise HTTPException(403, "invalid_login_challenge")
    if not settings.telegram_bot_token or not _verify_telegram_login(payload, settings.telegram_bot_token):
        raise HTTPException(401, "invalid_telegram_login")

    partner = (
        await db.execute(select(Partner).where(Partner.telegram_id == str(payload.id), Partner.status == "active"))
    ).scalar_one_or_none()
    if partner is None:
        raise HTTPException(403, "partner_account_not_active")

    token = secrets.token_urlsafe(32)
    try:
        await _store_session(token, partner.id)
    except RedisError as exc:
        raise HTTPException(503, "cabinet_session_unavailable") from exc
    response = JSONResponse({"redirect": "/cabinet"}, headers=_HEADERS)
    response.set_cookie(
        _SESSION_COOKIE, token, max_age=_SESSION_TTL, secure=True, httponly=True, samesite="strict", path="/"
    )
    response.delete_cookie(_LOGIN_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
    return response


@router.post("/cabinet/logout", include_in_schema=False)
async def logout(request: Request, db: DbSession, csrf: str = Form(...)) -> RedirectResponse:
    partner, session_id = await _partner_from_cookie(request, db)
    if partner is None or session_id is None:
        raise HTTPException(401, "cabinet_not_authenticated")
    expected = _csrf_for_session(session_id, get_settings().telegram_bot_token or "")
    if not hmac.compare_digest(expected, csrf):
        raise HTTPException(403, "invalid_csrf")
    try:
        await _destroy_session(session_id)
    except RedisError as exc:
        raise HTTPException(503, "cabinet_session_unavailable") from exc
    response = RedirectResponse("/cabinet", status_code=303, headers=_HEADERS)
    response.delete_cookie(_SESSION_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
    return response
