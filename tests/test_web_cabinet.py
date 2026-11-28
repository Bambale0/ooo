"""Web cabinet security, Telegram signature, session and tenant-isolation regression tests."""

import hashlib
import hmac
import re
import time
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.accounts.models import ApiKey, Partner
from app.dashboard import web as cabinet
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.security import create_api_key
from app.main import app


@pytest.fixture
async def browser(client, monkeypatch):
    # The existing client fixture configures the isolated database dependency.
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "test-web-cabinet-bot-token")
    monkeypatch.setattr(get_settings(), "telegram_bot_username", "apineurobrobot")
    sessions = {}

    async def store(token, partner_id):
        sessions[token] = partner_id

    async def read(token):
        return sessions.get(token)

    async def destroy(token):
        sessions.pop(token, None)

    monkeypatch.setattr(cabinet, "_store_session", store)
    monkeypatch.setattr(cabinet, "_read_session", read)
    monkeypatch.setattr(cabinet, "_destroy_session", destroy)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as browser:
        yield browser


@pytest.fixture
async def web_partner(db_session):
    partner = Partner(
        telegram_id="523456789",
        company_name="My Own Company",
        project_name="My Private Project",
        status="active",
        balance_rub=Decimal("123.45"),
    )
    stranger = Partner(
        telegram_id="523456790",
        company_name="Other Tenant Company",
        project_name="Other Tenant Project",
        status="active",
        balance_rub=Decimal("99999.00"),
    )
    db_session.add_all([partner, stranger])
    await db_session.commit()
    token, key_hash = create_api_key()
    db_session.add(
        ApiKey(partner_id=partner.id, name="My Key", key_hash=key_hash, key_prefix=token[:8], is_active=True)
    )
    db_session.add(
        Generation(
            partner_id=stranger.id,
            model_id="other-model",
            model_slug="not-for-me",
            mode="text-to-video",
            resolution="720p",
            duration_seconds=4,
            idempotency_key="other-generation",
            partner_price_rub=Decimal("100"),
            prompt="private prompt",
        )
    )
    await db_session.commit()
    return partner, stranger, token


def _signed_telegram_auth(telegram_id: int, *, age_seconds: int = 0) -> dict:
    fields = {"id": telegram_id, "first_name": "Test", "auth_date": int(time.time()) - age_seconds}
    message = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hashlib.sha256(get_settings().telegram_bot_token.encode()).digest()
    fields["hash"] = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    return fields


async def _login(browser, telegram_id: int, **changes):
    page = await browser.get("/cabinet")
    assert page.status_code == 200
    csrf = re.search(r'<meta name="tg-login-csrf" content="([^"]+)"', page.text).group(1)
    auth = _signed_telegram_auth(telegram_id)
    auth["csrf"] = csrf
    auth.update(changes)
    return await browser.post(
        "/cabinet/telegram",
        json=auth,
        headers={"Origin": get_settings().public_api_base_url.rstrip("/")},
    )


async def test_public_cabinet_has_telegram_entrypoint(browser):
    page = await browser.get("/cabinet")
    assert page.status_code == 200
    assert 'data-telegram-login="apineurobrobot"' in page.text
    assert "Личный кабинет" in page.text
    assert page.headers["cache-control"].startswith("private, no-store")
    assert page.cookies.get(cabinet._LOGIN_COOKIE)
    assert (await browser.get("/ui/cabinet.css")).status_code == 200
    assert (await browser.get("/ui/cabinet.js")).status_code == 200


async def test_telegram_signature_verification_rejects_forgery_and_stale_data(browser, web_partner):
    payload = _signed_telegram_auth(523456789)
    valid = cabinet.TelegramLogin(csrf="x" * 35, **payload)
    assert cabinet._verify_telegram_login(valid, get_settings().telegram_bot_token)
    forged = cabinet.TelegramLogin(csrf="x" * 35, **{**payload, "id": 523456790})
    assert not cabinet._verify_telegram_login(forged, get_settings().telegram_bot_token)
    stale = cabinet.TelegramLogin(csrf="x" * 35, **_signed_telegram_auth(523456789, age_seconds=600))
    assert not cabinet._verify_telegram_login(stale, get_settings().telegram_bot_token)
    response = await _login(browser, 523456789, hash="a" * 64)
    assert response.status_code == 401


async def test_browser_cannot_login_without_challenge_or_right_origin(browser, web_partner):
    auth = _signed_telegram_auth(523456789)
    no_challenge = await browser.post(
        "/cabinet/telegram",
        json={**auth, "csrf": "x" * 35},
        headers={"Origin": get_settings().public_api_base_url.rstrip("/")},
    )
    assert no_challenge.status_code == 403

    page = await browser.get("/cabinet")
    csrf = re.search(r'<meta name="tg-login-csrf" content="([^"]+)"', page.text).group(1)
    wrong_origin = await browser.post(
        "/cabinet/telegram",
        json={**auth, "csrf": csrf},
        headers={"Origin": "https://attacker.example"},
    )
    assert wrong_origin.status_code == 403


async def test_logged_in_user_sees_only_own_account_and_masked_keys(browser, web_partner):
    partner, _, secret = web_partner
    response = await _login(browser, int(partner.telegram_id))
    assert response.status_code == 200, response.text
    assert response.json()["redirect"] == "/cabinet"
    assert response.cookies.get(cabinet._SESSION_COOKIE)
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]

    page = await browser.get("/cabinet")
    assert page.status_code == 200
    assert "My Private Project" in page.text
    assert "123.45" in page.text
    assert "Other Tenant Project" not in page.text
    assert "99999" not in page.text
    assert "not-for-me" not in page.text
    assert "My Key" in page.text
    assert secret not in page.text


async def test_missing_partner_does_not_create_new_account(browser):
    response = await _login(browser, 555555555)
    assert response.status_code == 403
    assert response.json()["detail"] == "partner_account_not_active"
    assert "data-telegram-login" in (await browser.get("/cabinet")).text


async def test_logout_is_csrf_protected_and_revokes_session(browser, web_partner):
    partner, _, _ = web_partner
    login = await _login(browser, int(partner.telegram_id))
    assert login.status_code == 200
    forbidden = await browser.post("/cabinet/logout", data={"csrf": "0" * 64})
    assert forbidden.status_code == 403
    assert "My Private Project" in (await browser.get("/cabinet")).text

    session_id = browser.cookies.get(cabinet._SESSION_COOKIE)
    csrf = cabinet._csrf_for_session(session_id, get_settings().telegram_bot_token)
    logout = await browser.post("/cabinet/logout", data={"csrf": csrf}, follow_redirects=False)
    assert logout.status_code == 303
    assert (await browser.get("/cabinet")).status_code == 200
    assert "data-telegram-login" in (await browser.get("/cabinet")).text


async def test_inactive_account_cannot_reuse_existing_session(browser, db_session, web_partner):
    partner, _, _ = web_partner
    login = await _login(browser, int(partner.telegram_id))
    assert login.status_code == 200
    partner.status = "suspended"
    await db_session.commit()
    page = await browser.get("/cabinet")
    assert page.status_code == 200
    assert "My Private Project" not in page.text
    assert 'data-telegram-login="apineurobrobot"' in page.text


async def test_referral_cabinet_uses_existing_ledger_and_is_tenant_scoped(browser, db_session, web_partner):
    from app.referrals.service import API_TOPUP_RATE, accrue_commission, bind_referral, get_or_create_profile

    partner, stranger, _ = web_partner
    profile = await get_or_create_profile(db_session, partner.id)
    other_profile = await get_or_create_profile(db_session, stranger.id)
    assert await bind_referral(db_session, referred_telegram_id="345678901", code=profile.code)
    assert await bind_referral(db_session, referred_telegram_id="345678902", code=other_profile.code)
    for telegram_id, kind, event_key, base, reverse in (
        ("345678901", "api_topup", "web-referral-test-topup", "1000", False),
        ("345678901", "api_topup_refund", "web-referral-test-refund", "100", True),
        ("345678902", "api_topup", "web-referral-test-other", "9000", False),
    ):
        await accrue_commission(
            db_session, referred_telegram_id=telegram_id, kind=kind,
            source_id=event_key, event_key=event_key, base_amount_rub=Decimal(base),
            rate=API_TOPUP_RATE, reverse=reverse,
        )
    await db_session.commit()
    url = "/cabinet?section=referrals"
    anonymous = await browser.get(url)
    assert profile.code not in anonymous.text
    login = await _login(browser, int(partner.telegram_id))
    assert login.status_code == 200
    response = await browser.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("private, no-store")
    assert 'aria-current=page' in response.text
    assert f"https://t.me/apineurobrobot?start=ref_{profile.code}" in response.text
    assert 'data-copy-target="referral-link"' in response.text
    assert "63.00 ₽" in response.text
    assert "-7.00 ₽" in response.text
    assert "70.00 ₽" in response.text
    assert "630.00 ₽" not in response.text
    assert other_profile.code not in response.text
    assert "345678901" not in response.text
    assert "345678902" not in response.text
    assert profile.code in (await browser.get(url)).text


async def test_referral_cabinet_creates_stable_profile_for_new_partner(browser, web_partner):
    partner, _, _ = web_partner
    login = await _login(browser, int(partner.telegram_id))
    assert login.status_code == 200
    url = "/cabinet?section=referrals"
    first = await browser.get(url)
    second = await browser.get(url)
    assert first.status_code == second.status_code == 200
    pattern = r"https://t.me/apineurobrobot\?start=ref_([a-f0-9]+)"
    match = re.search(pattern, first.text)
    assert match is not None
    assert match.group(1) == re.search(pattern, second.text).group(1)
    assert "Пока пусто" in first.text
