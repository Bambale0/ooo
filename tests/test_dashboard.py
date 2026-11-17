"""Regression tests for the authenticated Partner Dashboard endpoints."""

from decimal import Decimal

import pytest

from app.accounts.models import ApiKey, Partner
from app.generations.models import Generation
from app.infrastructure.security import create_api_key
from app.payments.models import PaymentInvoice


@pytest.fixture
async def dashboard_partner_auth(db_session):
    partner = Partner(
        telegram_id="dashboard-test-partner",
        company_name="Dashboard Test",
        project_name="Demo",
        status="active",
        balance_rub=Decimal("100.00"),
    )
    db_session.add(partner)
    await db_session.flush()
    token, key_hash = create_api_key()
    db_session.add(
        ApiKey(
            partner_id=partner.id,
            name="Primary",
            key_hash=key_hash,
            key_prefix=token[:8],
            is_active=True,
        )
    )
    await db_session.commit()
    return partner, {"Authorization": f"Bearer {token}"}


async def test_dashboard_rejects_unauthenticated_access(client):
    response = await client.get("/api/v1/dashboard/profile")
    assert response.status_code == 401


async def test_dashboard_profile_and_balance(client, dashboard_partner_auth):
    partner, headers = dashboard_partner_auth
    response = await client.get("/api/v1/dashboard/profile", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["partner_id"] == partner.id

    response = await client.get("/api/v1/dashboard/balance", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["balance_rub"] == "100.00"
    assert response.headers["cache-control"] == "no-store"


async def test_dashboard_profile_email_validation(client, dashboard_partner_auth):
    _, headers = dashboard_partner_auth
    rejected = await client.patch(
        "/api/v1/dashboard/profile", headers=headers, json={"payment_email": "invalid email"}
    )
    assert rejected.status_code == 422

    updated = await client.patch(
        "/api/v1/dashboard/profile", headers=headers, json={"payment_email": "owner@example.org"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["payment_email"] == "owner@example.org"


async def test_dashboard_api_key_issue_and_revoke(client, dashboard_partner_auth):
    _, headers = dashboard_partner_auth
    listed = await client.get("/api/v1/dashboard/api-keys", headers=headers)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1

    created = await client.post(
        "/api/v1/dashboard/api-keys", headers=headers, json={"name": "Secondary"}
    )
    assert created.status_code == 201, created.text
    key = created.json()
    assert key["api_key"].startswith("nrn_")
    assert key["key_prefix"] == key["api_key"][:8]

    revoked = await client.delete(f"/api/v1/dashboard/api-keys/{key['id']}", headers=headers)
    assert revoked.status_code == 204

    rejected = await client.get(
        "/api/v1/dashboard/profile", headers={"Authorization": f"Bearer {key['api_key']}"}
    )
    assert rejected.status_code == 401


async def test_dashboard_rejects_local_webhook(client, dashboard_partner_auth):
    _, headers = dashboard_partner_auth
    keys = (await client.get("/api/v1/dashboard/api-keys", headers=headers)).json()
    response = await client.patch(
        f"/api/v1/dashboard/api-keys/{keys[0]['id']}/webhook",
        headers=headers,
        json={"webhook_url": "https://127.0.0.1/private"},
    )
    assert response.status_code == 422, response.text
    assert (await client.get("/api/v1/dashboard/api-keys", headers=headers)).json()[0]["webhook_url"] is None


async def test_dashboard_empty_usage_summary(client, dashboard_partner_auth):
    _, headers = dashboard_partner_auth
    response = await client.get("/api/v1/dashboard/usage-summary", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["total_generations"] == 0


async def test_dashboard_generation_history_reads_real_columns(client, db_session, dashboard_partner_auth):
    partner, headers = dashboard_partner_auth
    db_session.add(
        Generation(
            partner_id=partner.id,
            model_id="test-model-id",
            model_slug="test-model",
            mode="text-to-video",
            resolution="720p",
            duration_seconds=5,
            idempotency_key="dashboard-gen-1",
            partner_price_rub=Decimal("9.00"),
            prompt="test",
            status="completed",
            actual_charge_rub=Decimal("4.25"),
        )
    )
    await db_session.commit()
    response = await client.get("/api/v1/dashboard/generations", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["generations"][0]["model"] == "test-model"
    assert response.json()["generations"][0]["cost_rub"] == "4.25"


async def test_dashboard_invoice_history_reads_real_columns(client, db_session, dashboard_partner_auth):
    partner, headers = dashboard_partner_auth
    db_session.add(
        PaymentInvoice(
            partner_id=partner.id,
            provider="crypto_pay",
            idempotency_key="dashboard-invoice-1",
            requested_rub=Decimal("25.00"),
            invoice_url="https://example.org/pay",
            status="creating",
        )
    )
    await db_session.commit()
    response = await client.get("/api/v1/dashboard/invoices", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["invoices"][0]["amount_rub"] == "25.00"
    assert response.json()["invoices"][0]["payment_url"] == "https://example.org/pay"
    assert "external_id" not in response.json()["invoices"][0]
