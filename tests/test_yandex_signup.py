"""Standalone browser onboarding; real pool assignment, isolated database/provider."""

import re
from decimal import Decimal

import pytest
import test_yandex_cabinet as oauth_tests
from sqlalchemy import func, select

from app.accounts.models import ApiKey, ConsentAcceptance, Partner, PartnerApplication, PartnerIdentity
from app.dashboard import web, yandex_signup
from app.infrastructure.config import get_settings
from app.providers.key_pool import add_pool_keys
from app.providers.models import ProviderCredential, ProviderKeyPoolItem

browser = oauth_tests.browser
oauth = oauth_tests.oauth
web_partner = oauth_tests.web_partner


@pytest.fixture
async def pool(db_session, monkeypatch):
    async def valid(self, api_key):
        return True

    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", valid)
    monkeypatch.setattr(get_settings(), "argolink_registration_key_pool_json", None)
    monkeypatch.setattr(get_settings(), "argolink_registration_shared_pool_item_id", None)
    monkeypatch.setattr(get_settings(), "argolink_registration_claim_budget", 10)
    await add_pool_keys(
        db_session, ["synthetic-yandex-key-one", "synthetic-yandex-key-two"], source="test", actor="test",
    )
    await db_session.commit()


async def signup_form(browser, *, mode="register"):
    response = await oauth_tests._finish(browser, await oauth_tests._start(browser, mode))
    assert response.status_code == 200
    assert "/cabinet/yandex/register" in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    cookie = response.headers["set-cookie"]
    assert all(flag in cookie for flag in ("__Host-", "Secure", "HttpOnly", "SameSite=strict", "Path=/"))
    assert web._SESSION_COOKIE not in browser.cookies
    page = await browser.get("/cabinet/yandex/register")
    assert page.status_code == 200
    assert page.headers["referrer-policy"] == "same-origin"
    assert "Telegram для регистрации и входа не требуется" in page.text
    return {
        "csrf": re.search(r'name="csrf" value="([^"]+)"', page.text).group(1),
        "company_name": "Independent Company", "project_name": "Yandex Project",
        "accepted_terms": "yes", "accepted_privacy": "yes",
    }


@pytest.mark.parametrize("mode", ["login", "register"])
@pytest.mark.parametrize("telegram_configured", [False, True])
async def test_independent_signup_then_login_preserves_account_and_provider_key(
    browser, oauth, pool, db_session, web_partner, mode, telegram_configured, monkeypatch,
):
    if not telegram_configured:
        monkeypatch.setattr(get_settings(), "telegram_bot_token", None)
        monkeypatch.setattr(get_settings(), "telegram_bot_username", None)
    fields = await signup_form(browser, mode=mode)
    response = await browser.post("/cabinet/yandex/register", data=fields, headers={"origin": "https://testserver"})
    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "no-referrer"
    session = browser.cookies[web._SESSION_COOKIE]
    partner_id = await web._read_session(session)
    partner = await db_session.get(Partner, partner_id)
    assert partner.telegram_id is None
    assert partner.company_name == fields["company_name"]
    assert partner.balance_rub == partner.credit_limit_rub == Decimal("0")
    assert partner.requires_dedicated_argolink_credential
    identity = await db_session.scalar(select(PartnerIdentity))
    assert identity.partner_id == partner_id and identity.subject == "1234567"
    application = await db_session.get(PartnerApplication, partner.application_id)
    assert application.telegram_id is None and application.yandex_subject == "1234567"
    assert application.status == "approved"
    consents = list(await db_session.scalars(select(ConsentAcceptance)))
    assert {c.document_type for c in consents} == {"terms", "privacy_policy"}
    assert all(c.partner_id == partner_id and c.telegram_id is None for c in consents)
    credential = await db_session.scalar(select(ProviderCredential))
    assert credential.partner_id == partner_id and credential.registration_telegram_id is None
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 3
    page = await browser.get("/cabinet?section=settings")
    assert page.status_code == 200 and "Не подключён" in page.text
    assert page.headers["referrer-policy"] == "same-origin"
    csrf = web._csrf_for_session(session, get_settings().telegram_bot_token or "")
    created = await browser.post("/cabinet/action", data={
        "action": "create_key", "csrf": csrf, "name": "Yandex API key",
    }, headers={"origin": "https://testserver"})
    assert created.status_code == 200
    assert await db_session.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.partner_id == partner_id)) == 1
    assert (await browser.post("/cabinet/logout", data={"csrf": csrf})).status_code == 303
    assert (await oauth_tests._finish(browser, await oauth_tests._start(browser))).status_code == 200
    assert await web._read_session(browser.cookies[web._SESSION_COOKIE]) == partner_id
    assert await db_session.scalar(select(func.count()).select_from(PartnerApplication)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 1


@pytest.mark.parametrize("bad", ["origin", "null_origin", "csrf", "cookie", "terms", "privacy", "name", "version"])
async def test_signup_rejects_unverified_or_unconsented_registration(
    browser, oauth, pool, db_session, monkeypatch, bad,
):
    fields = await signup_form(browser)
    origin = "https://testserver"
    if bad == "origin":
        origin = "https://attacker.example"
    elif bad == "null_origin":
        origin = "null"
    elif bad == "csrf":
        fields["csrf"] = "x" * 43
    elif bad == "cookie":
        browser.cookies.delete(yandex_signup._COOKIE)
    elif bad in {"terms", "privacy"}:
        fields["accepted_" + bad] = ""
    elif bad == "name":
        fields["company_name"] = " "
    elif bad == "version":
        monkeypatch.setattr(get_settings(), "legal_document_version", "new-version")
    result = await browser.post("/cabinet/yandex/register", data=fields, headers={"origin": origin})
    assert result.status_code in {403, 422}
    for model in (Partner, PartnerApplication, PartnerIdentity, ConsentAcceptance, ProviderCredential):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0
    assert web._SESSION_COOKIE not in browser.cookies


async def test_registration_budget_failure_is_retryable_without_duplicate_application(
    browser, oauth, pool, db_session, monkeypatch,
):
    fields = await signup_form(browser)
    monkeypatch.setattr(get_settings(), "argolink_registration_claim_budget", 0)
    for _ in range(2):
        result = await browser.post("/cabinet/yandex/register", data=fields, headers={"origin": "https://testserver"})
        assert result.status_code == 503
        assert "provider" not in result.text and "ArgoLink" not in result.text
    assert await db_session.scalar(select(func.count()).select_from(PartnerApplication)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ConsentAcceptance)) == 2
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0
    assert all(p.status == "available" for p in await db_session.scalars(select(ProviderKeyPoolItem)))
    monkeypatch.setattr(get_settings(), "argolink_registration_claim_budget", 10)
    assert (await browser.post(
        "/cabinet/yandex/register", data=fields, headers={"origin": "https://testserver"},
    )).status_code == 200
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 1


async def test_signup_replay_cannot_create_a_second_account(browser, oauth, pool, db_session):
    fields = await signup_form(browser)
    token = browser.cookies[yandex_signup._COOKIE]
    first = await browser.post("/cabinet/yandex/register", data=fields, headers={"origin": "https://testserver"})
    assert first.status_code == 200
    replay = await browser.post("/cabinet/yandex/register", data=fields, headers={
        "origin": "https://testserver", "Cookie": f"{yandex_signup._COOKIE}={token}",
    })
    assert replay.status_code == 403
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 1


async def test_same_origin_forms_and_private_oauth_errors(browser, oauth):
    page = await browser.get("/cabinet")
    assert page.headers["referrer-policy"] == "same-origin"
    assert 'name="mode" value="register"' in page.text
    assert "Зарегистрироваться с Яндекс ID" in page.text
    result = await browser.post("/cabinet/yandex/start", data={"csrf": "x" * 43}, headers={"origin": "null"})
    assert result.status_code == 403
    assert result.headers["referrer-policy"] == "no-referrer"


async def test_signup_survives_session_store_failure_and_login_recovers(browser, oauth, pool, db_session, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    fields = await signup_form(browser)

    async def offline(*args):
        raise RedisConnectionError("private backend detail")

    with monkeypatch.context() as context:
        context.setattr(web, "_store_session", offline)
        result = await browser.post("/cabinet/yandex/register", data=fields, headers={"origin": "https://testserver"})
        assert result.status_code == 503 and "private backend detail" not in result.text
    assert web._SESSION_COOKIE not in browser.cookies
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 1
    assert await db_session.scalar(select(func.count()).select_from(PartnerIdentity)) == 1
    assert (await oauth_tests._finish(browser, await oauth_tests._start(browser))).status_code == 200
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 1


@pytest.mark.parametrize("shared", [False, True])
async def test_two_yandex_accounts_have_distinct_ownership_without_telegram(
    browser, oauth, pool, db_session, monkeypatch, shared,
):
    from app.dashboard import yandex
    from app.providers.service import get_active_provider_credential

    if shared:
        item = await db_session.scalar(select(ProviderKeyPoolItem).limit(1))
        monkeypatch.setattr(get_settings(), "argolink_registration_shared_pool_item_id", item.id)
    partners, credentials = [], []
    for subject in ("1234567", "7654321"):
        async def verified(code, verifier, subject=subject):
            return subject

        monkeypatch.setattr(yandex, "_subject", verified)
        fields = await signup_form(browser)
        result = await browser.post("/cabinet/yandex/register", data=fields, headers={"origin": "https://testserver"})
        assert result.status_code == 200
        session = browser.cookies[web._SESSION_COOKIE]
        partner = await db_session.get(Partner, await web._read_session(session))
        assert partner.telegram_id is None and partner.balance_rub == Decimal("0")
        partners.append(partner.id)
        credentials.append((await get_active_provider_credential(db_session, partner.id, "argolink")).id)
        await browser.post("/cabinet/logout", data={
            "csrf": web._csrf_for_session(session, get_settings().telegram_bot_token),
        })
    assert len(set(partners)) == 2
    assert len(set(credentials)) == (1 if shared else 2)
    assert await db_session.scalar(select(func.count()).select_from(PartnerApplication)) == 2
    assert await db_session.scalar(select(func.count()).select_from(PartnerIdentity)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ConsentAcceptance)) == 4
