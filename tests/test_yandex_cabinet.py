"""Account linking and OAuth denial paths; no live provider calls or financial writes."""

import base64
import hashlib
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import test_web_cabinet as telegram_tests
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select

from app.accounts.models import Partner, PartnerIdentity
from app.dashboard import web, yandex, yandex_signup
from app.infrastructure.config import get_settings

browser = telegram_tests.browser
web_partner = telegram_tests.web_partner
_login = telegram_tests._login


@pytest.fixture
def oauth(monkeypatch, browser):
    s = get_settings()
    monkeypatch.setattr(s, "public_api_base_url", "https://testserver")
    monkeypatch.setattr(s, "yandex_client_id", "test-client-id")
    monkeypatch.setattr(s, "yandex_client_secret", SecretStr("test-client-secret"))
    monkeypatch.setattr(s, "yandex_callback_url", "https://testserver/cabinet/yandex/callback")
    monkeypatch.setattr(s, "terms_url", "https://testserver/terms")
    monkeypatch.setattr(s, "privacy_policy_url", "https://testserver/privacy")
    flows = {}
    signups = {}
    bound_tokens = {}

    async def save(state, nonce, data):
        flows[yandex._flow_key(state, nonce)] = data
        if data["mode"] == "link":
            bound_tokens[data["session_key"]] = browser.cookies[web._SESSION_COOKIE]

    async def take(state, nonce):
        return flows.pop(yandex._flow_key(state, nonce), None)

    async def bound(key, *, destroy=False):
        token = bound_tokens.get(key)
        if token:
            partner_id = await web._read_session(token)
            if destroy:
                await web._destroy_session(token)
            return partner_id
        return None

    async def subject(code, verifier):
        assert code == "verified-code"
        assert len(verifier) == 43
        return "1234567"

    async def save_signup(token, data):
        signups[token] = data

    async def read_signup(token, *, consume=False):
        return signups.pop(token, None) if consume else signups.get(token)

    monkeypatch.setattr(yandex, "_save_flow", save)
    monkeypatch.setattr(yandex, "_take_flow", take)
    monkeypatch.setattr(yandex, "_bound_session", bound)
    monkeypatch.setattr(yandex, "_subject", subject)
    monkeypatch.setattr(yandex_signup, "_save", save_signup)
    monkeypatch.setattr(yandex_signup, "_read", read_signup)
    return flows


async def _start(browser, mode="login", **overrides):
    page = await browser.get("/cabinet" + ("?section=settings" if mode == "link" else ""))
    if mode == "link":
        csrf = web._csrf_for_session(browser.cookies[web._SESSION_COOKIE], get_settings().telegram_bot_token)
    else:
        csrf = re.search(r'<meta name="tg-login-csrf" content="([^"]+)"', page.text).group(1)
    return await browser.post(
        "/cabinet/yandex/start",
        data={"mode": mode, "csrf": csrf, **overrides},
        headers={"origin": "https://testserver"},
    )


async def _finish(browser, response, **params):
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    return await browser.get(
        "/cabinet/yandex/callback",
        params={"state": state, "code": "verified-code", **params},
        headers={"Cookie": f"{yandex._COOKIE}={browser.cookies.get(yandex._COOKIE, '')}"},
    )


async def test_link_then_yandex_login_keeps_partner_balance_and_rotates_session(
    browser,
    oauth,
    web_partner,
    db_session,
):
    partner, stranger, _ = web_partner
    assert (await _login(browser, int(partner.telegram_id))).status_code == 200
    old_session = browser.cookies[web._SESSION_COOKIE]
    settings = await browser.get("/cabinet?section=settings")
    assert "Подключить Яндекс ID" in settings.text
    start = await _start(browser, "link")
    assert start.status_code == 303
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [get_settings().yandex_callback_url]
    flow = next(iter(oauth.values()))
    expected = base64.urlsafe_b64encode(hashlib.sha256(flow["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected]
    assert "client_secret" not in query
    assert "HttpOnly" in start.headers["set-cookie"] and "SameSite=lax" in start.headers["set-cookie"]
    # Strict session cookies are absent on a cross-site callback in real browsers;
    # linking uses the server-side hash saved by the same-origin POST.
    result = await _finish(browser, start)
    assert result.status_code == 200
    assert result.headers["referrer-policy"] == "no-referrer"
    session_cookie = next(h for h in result.headers.get_list("set-cookie") if h.startswith(web._SESSION_COOKIE))
    assert all(flag in session_cookie for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"))
    assert browser.cookies[web._SESSION_COOKIE] != old_session
    assert await web._read_session(old_session) is None
    identity = (await db_session.execute(select(PartnerIdentity))).scalar_one()
    assert identity.partner_id == partner.id
    settings = await browser.get("/cabinet?section=settings")
    assert "Яндекс ID подключён" in settings.text and "Подключить Яндекс ID" not in settings.text
    session = browser.cookies[web._SESSION_COOKIE]
    logout = await browser.post(
        "/cabinet/logout",
        data={
            "csrf": web._csrf_for_session(session, get_settings().telegram_bot_token),
        },
    )
    assert logout.status_code == 303
    start = await _start(browser)
    assert (await _finish(browser, start)).status_code == 200
    assert await web._read_session(browser.cookies[web._SESSION_COOKIE]) == partner.id
    page = await browser.get("/cabinet")
    assert partner.project_name in page.text and stranger.project_name not in page.text
    assert (await db_session.scalar(select(func.count()).select_from(Partner))) == 2
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("123.45")


async def test_unlinked_yandex_offers_registration_without_matching_existing_email(
    browser, oauth, web_partner, db_session,
):
    partner, _, _ = web_partner
    partner.payment_email = "same@yandex.ru"
    await db_session.commit()
    result = await _finish(browser, await _start(browser))
    assert result.status_code == 200
    assert "/cabinet/yandex/register" in result.text
    assert result.headers["referrer-policy"] == "no-referrer"
    assert web._SESSION_COOKIE not in browser.cookies
    assert await db_session.scalar(select(func.count()).select_from(PartnerIdentity)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 2


@pytest.mark.parametrize("kind", ["missing", "wrong_browser", "wrong_state", "expired", "replay", "duplicate"])
async def test_state_browser_expiry_and_replay_denied(browser, oauth, web_partner, kind):
    start = await _start(browser)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    if kind == "missing":
        browser.cookies.delete(yandex._COOKIE)
    elif kind == "wrong_browser":
        browser.cookies.delete(yandex._COOKIE)
        browser.cookies.set(yandex._COOKIE, "a" * 43, domain="testserver.local", path="/")
    elif kind == "wrong_state":
        state = "b" * 43
    elif kind == "expired":
        oauth.clear()
    elif kind == "replay":
        await _finish(browser, start)
    elif kind == "duplicate":
        state = [state, state]
    result = await browser.get("/cabinet/yandex/callback", params={"state": state, "code": "verified-code"})
    assert result.status_code == 403
    assert web._SESSION_COOKIE not in browser.cookies
    assert result.headers["cache-control"].startswith("private, no-store")


@pytest.mark.parametrize("change", ["csrf", "origin", "stale_proof", "no_proof", "proof_for_other_session"])
async def test_link_requires_csrf_origin_and_recent_telegram(browser, oauth, web_partner, change, monkeypatch):
    partner, _, _ = web_partner
    await _login(browser, int(partner.telegram_id))
    session = browser.cookies[web._SESSION_COOKIE]
    if change in {"stale_proof", "no_proof", "proof_for_other_session"}:
        browser.cookies.delete(yandex._PROOF_COOKIE)
        if change == "stale_proof":
            with monkeypatch.context() as m:
                m.setattr(yandex.time, "time", lambda: 1)
                proof = yandex.telegram_proof(session)
            browser.cookies.set(yandex._PROOF_COOKIE, proof, domain="testserver.local", path="/")
        elif change == "proof_for_other_session":
            browser.cookies.set(
                yandex._PROOF_COOKIE, yandex.telegram_proof("other"), domain="testserver.local", path="/"
            )
    csrf = web._csrf_for_session(session, get_settings().telegram_bot_token)
    result = await browser.post(
        "/cabinet/yandex/start",
        data={
            "mode": "link",
            "csrf": "invalid" if change == "csrf" else csrf,
        },
        headers={"origin": "https://attacker.example" if change == "origin" else "https://testserver"},
    )
    assert result.status_code == 403
    assert not oauth


@pytest.mark.parametrize("mode", ["login", "link"])
async def test_cannot_take_over_existing_identity(browser, oauth, web_partner, db_session, mode):
    partner, stranger, _ = web_partner
    db_session.add(PartnerIdentity(partner_id=stranger.id, provider="yandex", subject="1234567"))
    await db_session.commit()
    if mode == "link":
        await _login(browser, int(partner.telegram_id))
        result = await _finish(browser, await _start(browser, mode))
        assert result.status_code == 409
        assert await web._read_session(browser.cookies[web._SESSION_COOKIE]) == partner.id
    else:
        result = await _finish(browser, await _start(browser))
        assert result.status_code == 200
        assert await web._read_session(browser.cookies[web._SESSION_COOKIE]) == stranger.id
    assert (await db_session.execute(select(PartnerIdentity))).scalar_one().partner_id == stranger.id


async def test_cannot_replace_existing_yandex_identity(browser, oauth, web_partner, db_session):
    partner, _, _ = web_partner
    db_session.add(PartnerIdentity(partner_id=partner.id, provider="yandex", subject="7654321"))
    await db_session.commit()
    await _login(browser, int(partner.telegram_id))
    result = await _finish(browser, await _start(browser, "link"))
    assert result.status_code == 409
    assert (await db_session.execute(select(PartnerIdentity))).scalar_one().subject == "7654321"


@pytest.mark.parametrize("change", ["inactive", "logout", "logout_during_exchange"])
async def test_link_rechecks_account_and_initiating_session(
    browser,
    oauth,
    web_partner,
    db_session,
    monkeypatch,
    change,
):
    partner, _, _ = web_partner
    await _login(browser, int(partner.telegram_id))
    session = browser.cookies[web._SESSION_COOKIE]
    start = await _start(browser, "link")
    if change == "inactive":
        partner.status = "suspended"
        await db_session.commit()
    elif change == "logout":
        await web._destroy_session(session)
    else:

        async def revoke(code, verifier):
            await web._destroy_session(session)
            return "1234567"

        monkeypatch.setattr(yandex, "_subject", revoke)
    assert (await _finish(browser, start)).status_code == 403
    assert await db_session.scalar(select(func.count()).select_from(PartnerIdentity)) == 0


async def test_inactive_linked_account_cannot_login(browser, oauth, web_partner, db_session):
    partner, _, _ = web_partner
    partner.status = "suspended"
    db_session.add(PartnerIdentity(partner_id=partner.id, provider="yandex", subject="1234567"))
    await db_session.commit()
    assert (await _finish(browser, await _start(browser))).status_code == 403
    assert web._SESSION_COOKIE not in browser.cookies


@pytest.mark.parametrize("failure", ["denied", "missing_code", "provider", "redis_take", "redis_save", "session"])
async def test_failures_grant_no_session_and_hide_provider_details(
    browser, oauth, monkeypatch, failure, web_partner, db_session
):
    async def offline(*args, **kwargs):
        raise RedisConnectionError("private backend detail")

    if failure == "redis_save":
        monkeypatch.setattr(yandex, "_save_flow", offline)
        result = await _start(browser)
    else:
        start = await _start(browser)
        params = {}
        if failure == "denied":
            params = {"error": "access_denied", "error_description": "secret provider error"}
        elif failure == "missing_code":
            params = {"code": ""}
        elif failure == "provider":

            async def bad_subject(*args):
                raise httpx.ConnectError("secret provider error")

            monkeypatch.setattr(yandex, "_subject", bad_subject)
        elif failure == "redis_take":
            monkeypatch.setattr(yandex, "_take_flow", offline)
        elif failure == "session":
            partner, _, _ = web_partner
            db_session.add(PartnerIdentity(partner_id=partner.id, provider="yandex", subject="1234567"))
            await db_session.commit()
            monkeypatch.setattr(web, "_store_session", offline)
        result = await _finish(browser, start, **params)
    assert result.status_code in {400, 502, 503}
    assert "secret provider error" not in result.text and "private backend detail" not in result.text
    assert web._SESSION_COOKIE not in browser.cookies


@pytest.mark.parametrize("bad", [None, "partial", "http", "wrong_host", "wrong_path", "query", "fragment", "userinfo"])
async def test_optional_config_fails_closed(browser, oauth, monkeypatch, bad):
    s = get_settings()
    if bad in {None, "partial"}:
        monkeypatch.setattr(s, "yandex_client_secret", None)
        if bad is None:
            monkeypatch.setattr(s, "yandex_client_id", None)
            monkeypatch.setattr(s, "yandex_callback_url", None)
    else:
        url = {
            "http": "http://testserver/cabinet/yandex/callback",
            "wrong_host": "https://evil.example/cabinet/yandex/callback",
            "wrong_path": "https://testserver/other",
            "query": s.yandex_callback_url + "?foo=bar",
            "fragment": s.yandex_callback_url + "#fragment",
            "userinfo": "https://user@testserver/cabinet/yandex/callback",
        }[bad]
        monkeypatch.setattr(s, "yandex_callback_url", url)
    assert bool(yandex.configuration_errors()) == (bad is not None)
    page = await browser.get("/cabinet")
    assert "Войти с Яндекс ID" not in page.text
    result = await _start(browser)
    assert result.status_code == 503


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_public_header_links_configured_bot_without_application(client, monkeypatch, lang):
    monkeypatch.setattr(get_settings(), "telegram_bot_username", "configured_project_bot")
    for path in ("/", "/docs", "/prices"):
        page = await client.get(path, params={"lang": lang})
        assert page.status_code == 200
        assert 'href="https://t.me/configured_project_bot"' in page.text
        assert "Подать заявку" not in page.text and "Apply via Telegram" not in page.text
        assert "?start=apply" not in page.text and "apineurobrobot" not in page.text


async def test_missing_bot_config_never_invents_bot_link(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_username", None)
    page = await client.get("/")
    assert "https://t.me/" not in page.text


@pytest.mark.parametrize("bot", [None, "invalid-bot-name"])
@pytest.mark.parametrize("linked", [False, True])
async def test_yandex_login_survives_unavailable_telegram_widget_without_bypassing_link(
    browser, oauth, web_partner, db_session, monkeypatch, bot, linked
):
    partner, _, _ = web_partner
    if linked:
        db_session.add(PartnerIdentity(partner_id=partner.id, provider="yandex", subject="1234567"))
        await db_session.commit()
    monkeypatch.setattr(get_settings(), "telegram_bot_username", bot)
    page = await browser.get("/cabinet")
    assert "Войти с Яндекс ID" in page.text
    assert "data-telegram-login" not in page.text
    assert "https://t.me/" not in page.text
    assert "Зарегистрироваться с Яндекс ID" in page.text
    assert page.cookies.get(web._LOGIN_COOKIE)
    result = await _finish(browser, await _start(browser))
    if linked:
        assert result.status_code == 200
        assert await web._read_session(browser.cookies[web._SESSION_COOKIE]) == partner.id
    else:
        assert result.status_code == 200
        assert "/cabinet/yandex/register" in result.text
        assert web._SESSION_COOKIE not in browser.cookies
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 2
    assert await db_session.scalar(select(func.count()).select_from(PartnerIdentity)) == int(linked)
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("123.45")


async def test_no_provider_config_shows_unavailable_without_login_form(browser, monkeypatch):
    s = get_settings()
    for name in ("telegram_bot_username", "yandex_client_id", "yandex_client_secret", "yandex_callback_url"):
        monkeypatch.setattr(s, name, None)
    page = await browser.get("/cabinet")
    assert "Вход временно недоступен" in page.text
    assert "data-telegram-login" not in page.text
    assert "/cabinet/yandex/start" not in page.text


@pytest.mark.parametrize("change", ["missing_cookie", "csrf", "origin"])
async def test_yandex_login_start_requires_browser_challenge_and_origin(browser, oauth, change):
    page = await browser.get("/cabinet")
    csrf = re.search(r'<meta name="tg-login-csrf" content="([^"]+)"', page.text).group(1)
    if change == "missing_cookie":
        browser.cookies.delete(web._LOGIN_COOKIE)
    result = await browser.post(
        "/cabinet/yandex/start",
        data={"csrf": "x" * 43 if change == "csrf" else csrf},
        headers={"origin": "https://attacker.example" if change == "origin" else "https://testserver"},
    )
    assert result.status_code == 403
    assert not oauth
    assert yandex._COOKIE not in browser.cookies


async def test_link_survives_session_store_failure_without_duplicate_account(
    browser,
    oauth,
    web_partner,
    db_session,
    monkeypatch,
):
    partner, _, _ = web_partner
    await _login(browser, int(partner.telegram_id))
    old = browser.cookies[web._SESSION_COOKIE]
    start = await _start(browser, "link")

    async def offline(*args):
        raise RedisConnectionError("private error")

    monkeypatch.setattr(web, "_store_session", offline)
    assert (await _finish(browser, start)).status_code == 503
    assert await web._read_session(old) is None
    identity = (await db_session.execute(select(PartnerIdentity))).scalar_one()
    assert identity.partner_id == partner.id
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 2
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("123.45")
