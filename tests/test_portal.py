"""Full partner portal workflows: tenant isolation, forms, billing and no-store secrets."""

import hashlib
import hmac
import re
import time
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.accounts.models import ApiKey, Partner
from app.billing.models import LedgerEntry
from app.catalog.models import Model, PartnerPrice
from app.dashboard import web as cabinet
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.security import create_api_key
from app.main import app
from app.payments.models import PaymentInvoice


@pytest.fixture
async def portal_browser(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "test-portal-telegram-token")
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
async def portal_accounts(db_session):
    own = Partner(
        telegram_id="602340101", company_name="North Cloud", project_name="Neironych Studio",
        status="active", balance_rub=Decimal("420.75"), payment_email="owner@example.org",
    )
    other = Partner(
        telegram_id="602340102", company_name="Foreign Tenant Secret", project_name="Foreign Tenant Studio",
        status="active", balance_rub=Decimal("999999.00"),
    )
    db_session.add_all([own, other])
    await db_session.flush()

    own_token, own_hash = create_api_key()
    other_token, other_hash = create_api_key()
    own_key = ApiKey(
        partner_id=own.id, name="My production key", key_hash=own_hash,
        key_prefix=own_token[:8], is_active=True,
    )
    other_key = ApiKey(
        partner_id=other.id, name="foreign-key-hidden", key_hash=other_hash,
        key_prefix=other_token[:8], is_active=True,
    )
    db_session.add_all([own_key, other_key])
    db_session.add_all([
        Generation(
            partner_id=own.id, model_id="own-model-id", model_slug="gemini-own",
            mode="text-to-video", resolution="720p", duration_seconds=4,
            idempotency_key="portal-own-gen", partner_price_rub=Decimal("21.00"),
            prompt="Visible own prompt", status="completed", actual_charge_rub=Decimal("18.50"),
        ),
        Generation(
            partner_id=other.id, model_id="other-model-id", model_slug="foreign-model-hidden",
            mode="text-to-video", resolution="720p", duration_seconds=4,
            idempotency_key="portal-other-gen", partner_price_rub=Decimal("900.00"),
            prompt="Foreign Tenant Secret prompt", status="completed",
            actual_charge_rub=Decimal("800.00"),
        ),
    ])
    db_session.add_all([
        LedgerEntry(
            partner_id=own.id, operation_type="generation_charge",
            amount_rub=Decimal("-18.50"), balance_after_rub=Decimal("420.75"),
            idempotency_key="portal-own-ledger",
        ),
        LedgerEntry(
            partner_id=other.id, operation_type="generation_charge",
            amount_rub=Decimal("-800"), balance_after_rub=Decimal("999999"),
            idempotency_key="portal-foreign-ledger",
        ),
    ])
    db_session.add_all([
        PaymentInvoice(
            partner_id=own.id, provider="crypto_pay", idempotency_key="portal-own-invoice",
            requested_rub=Decimal("1000.00"), status="creating",
        ),
        PaymentInvoice(
            partner_id=other.id, provider="crypto_pay", idempotency_key="portal-foreign-invoice",
            requested_rub=Decimal("90000.00"), status="creating",
        ),
    ])
    await db_session.commit()
    return own, other, own_key, other_key, own_token


def _telegram_data(telegram_id: str) -> dict:
    fields = {"id": int(telegram_id), "first_name": "Portal", "auth_date": int(time.time())}
    message = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    digest = hashlib.sha256(get_settings().telegram_bot_token.encode()).digest()
    fields["hash"] = hmac.new(digest, message.encode(), hashlib.sha256).hexdigest()
    return fields


async def _authenticate(browser, partner):
    response = await browser.get("/cabinet")
    csrf = re.search(r'<meta name="tg-login-csrf" content="([^"]+)"', response.text).group(1)
    payload = {**_telegram_data(partner.telegram_id), "csrf": csrf}
    login = await browser.post(
        "/cabinet/telegram", json=payload,
        headers={"Origin": get_settings().public_api_base_url.rstrip("/")},
    )
    assert login.status_code == 200, login.text
    session = browser.cookies.get(cabinet._SESSION_COOKIE)
    assert session
    return cabinet._csrf_for_session(session, get_settings().telegram_bot_token)


def _form(csrf, action, **fields):
    return {"csrf": csrf, "action": action, **fields}


async def _post(browser, csrf, action, **fields):
    return await browser.post(
        "/cabinet/action", data=_form(csrf, action, **fields),
        headers={"Origin": get_settings().public_api_base_url.rstrip("/")},
        follow_redirects=False,
    )


async def test_portal_sections_and_assets_are_real_and_private(portal_browser, portal_accounts):
    own, _, _, _, raw_secret = portal_accounts
    assert (await portal_browser.get("/cabinet/export/logs")).status_code == 401
    without_login = await portal_browser.post(
        "/cabinet/action", data={"action": "create_key", "csrf": "x"}
    )
    assert without_login.status_code == 401
    csrf = await _authenticate(portal_browser, own)
    assert len(csrf) == 64
    for section, expected in (
        ("overview", "Обзор использования"),
        ("activity", "Аналитика"),
        ("logs", "Журнал запросов"),
        ("models", "Модели и тарифы"),
        ("keys", "Создать новый ключ"),
        ("credits", "Пополнить баланс"),
        ("invoices", "Счета"),
        ("ledger", "Операции по балансу"),
        ("settings", "Настройки"),
    ):
        response = await portal_browser.get("/cabinet", params={"section": section})
        assert response.status_code == 200, (section, response.text)
        assert expected in response.text
        assert "Foreign Tenant Secret" not in response.text
        assert raw_secret not in response.text
        assert response.headers["cache-control"].startswith("private, no-store")
    assert (await portal_browser.get("/ui/portal.css")).status_code == 200
    assert (await portal_browser.get("/ui/portal.js")).status_code == 200


async def test_key_creation_is_one_time_and_revoke_tenant_scoped(
    portal_browser, db_session, portal_accounts
):
    own, _, _, foreign_key, _ = portal_accounts
    csrf = await _authenticate(portal_browser, own)
    denied = await _post(portal_browser, csrf, "revoke_key", key_id=foreign_key.id)
    assert denied.status_code == 404
    assert foreign_key.is_active

    created = await _post(portal_browser, csrf, "create_key", name="Mobile backend")
    assert created.status_code == 200, created.text
    secret = re.search(r'<code id="new-api-key">([^<]+)</code>', created.text).group(1)
    assert secret.startswith("nrn_")
    assert created.headers["cache-control"].startswith("private, no-store")
    page = await portal_browser.get("/cabinet?section=keys")
    assert "Mobile backend" in page.text
    assert secret not in page.text

    new_key = (
        await db_session.execute(
            select(ApiKey).where(ApiKey.partner_id == own.id, ApiKey.name == "Mobile backend")
        )
    ).scalar_one()
    revoked = await _post(portal_browser, csrf, "revoke_key", key_id=new_key.id)
    assert revoked.status_code == 303
    await db_session.refresh(new_key)
    assert not new_key.is_active
    await db_session.refresh(foreign_key)
    assert foreign_key.is_active


async def test_csrf_and_origin_guard_all_browser_mutations(portal_browser, db_session, portal_accounts):
    own, _, _, _, _ = portal_accounts
    csrf = await _authenticate(portal_browser, own)
    bad_csrf = await _post(portal_browser, "f" * 64, "create_key", name="forged")
    assert bad_csrf.status_code == 403
    bad_origin = await portal_browser.post(
        "/cabinet/action",
        data=_form(csrf, "create_key", name="forged"),
        headers={"Origin": "https://evil.example"},
    )
    assert bad_origin.status_code == 403
    count = (await db_session.execute(select(ApiKey).where(ApiKey.partner_id == own.id))).scalars().all()
    assert len(count) == 1


async def test_ledger_generations_exports_exclude_other_tenants(portal_browser, portal_accounts):
    own, _, _, _, _ = portal_accounts
    await _authenticate(portal_browser, own)
    for section in ("overview", "logs", "activity", "ledger", "invoices", "credits"):
        result = await portal_browser.get("/cabinet", params={"section": section})
        assert "foreign-model-hidden" not in result.text
        assert "999999" not in result.text
        assert "90000" not in result.text
    logs = (await portal_browser.get("/cabinet/export/logs")).text
    assert "gemini-own" in logs
    assert "foreign-model-hidden" not in logs
    ledger = (await portal_browser.get("/cabinet/export/ledger")).text
    assert "420.75" in ledger
    assert "999999" not in ledger
    invoices = (await portal_browser.get("/cabinet/export/invoices")).text
    assert "1000" in invoices
    assert "90000" not in invoices


async def test_catalog_shows_production_but_never_draft(portal_browser, db_session, portal_accounts):
    own, _, _, _, _ = portal_accounts
    public = Model(
        slug="public-portal-model", name="Public Example Model", modality="image", status="production",
        has_provider_integration=True, has_public_docs=True, has_successful_smoke=True,
    )
    draft = Model(slug="private-incomplete-draft", name="Draft Supplier Secret", modality="image", status="draft")
    db_session.add_all([public, draft])
    await db_session.flush()
    db_session.add_all([
        PartnerPrice(
            model_id=public.id, price_rub=Decimal("12.50"), provider_cost_usdt=Decimal("0.01"),
            mode="default", resolution="default", billing_unit="generation",
        ),
        PartnerPrice(
            model_id=draft.id, price_rub=Decimal("99.00"), provider_cost_usdt=Decimal("0.01"),
            mode="default", resolution="default", billing_unit="generation",
        ),
    ])
    await db_session.commit()
    await _authenticate(portal_browser, own)
    response = await portal_browser.get("/cabinet?section=models")
    assert response.status_code == 200, response.text
    assert "Public Example Model" in response.text
    assert "12.50" in response.text
    assert "Draft Supplier Secret" not in response.text


async def test_payment_email_update_and_invalid_amount_do_not_create_invoice(
    portal_browser, db_session, portal_accounts
):
    own, _, _, _, _ = portal_accounts
    csrf = await _authenticate(portal_browser, own)
    invalid = await _post(
        portal_browser, csrf, "create_invoice",
        requested_rub="12", idempotency_key="invoice-portal-err", buyer_email="owner@example.org",
    )
    assert invalid.status_code == 422
    updated = await _post(portal_browser, csrf, "update_email", buyer_email="new@example.org")
    assert updated.status_code == 303
    await db_session.refresh(own)
    assert own.payment_email == "new@example.org"
    assert "new@example.org" in (await portal_browser.get("/cabinet?section=settings")).text
    old_invoices = (await db_session.execute(
        select(PaymentInvoice).where(PaymentInvoice.partner_id == own.id)
    )).scalars().all()
    assert len(old_invoices) == 1


async def test_invoice_creation_reuses_business_service(
    portal_browser, monkeypatch, portal_accounts
):
    own, _, _, _, _ = portal_accounts
    csrf = await _authenticate(portal_browser, own)
    observed = []

    async def fake_create(payload, db, partner):
        observed.append((payload, partner.id))
        return None

    monkeypatch.setattr("app.payments.router.create_invoice", fake_create)
    result = await _post(
        portal_browser, csrf, "create_invoice",
        requested_rub="1500", idempotency_key="invoice-browser-replay-1",
        buyer_email="owner@example.org",
    )
    assert result.status_code == 303, result.text
    assert result.headers["location"].startswith("/cabinet?section=invoices")
    assert observed[0][0].requested_rub == 1500
    assert observed[0][0].idempotency_key == "invoice-browser-replay-1"
    assert observed[0][1] == own.id


async def test_generation_detail_does_not_resolve_foreign_id(
    portal_browser, db_session, portal_accounts
):
    own, other, _, _, _ = portal_accounts
    target = (
        await db_session.execute(select(Generation).where(Generation.partner_id == other.id))
    ).scalar_one()
    await _authenticate(portal_browser, own)
    result = await portal_browser.get("/cabinet", params={"section": "logs", "generation": target.id})
    assert result.status_code == 200
    assert "Foreign Tenant Secret prompt" not in result.text
    assert "foreign-model-hidden" not in result.text


async def test_unknown_action_and_export_rejected(portal_browser, portal_accounts):
    own, _, _, _, _ = portal_accounts
    csrf = await _authenticate(portal_browser, own)
    response = await _post(portal_browser, csrf, "credit_balance", requested_rub="10000000")
    assert response.status_code == 422
    assert (await portal_browser.get("/cabinet/export/private-secrets")).status_code == 404
