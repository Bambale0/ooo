"""Partner-key-only read-only balance endpoint; never expose other tenants."""

from decimal import Decimal

from app.accounts.models import Partner
from test_tenant_isolation import _create_partner_with_key


async def test_partner_key_reads_only_own_rub_balance(client, db_session, admin_headers):
    alpha, _ = await _create_partner_with_key(
        db_session, telegram_id="balance-client-alpha", token="nrn_balance_alpha_secret",
    )
    beta, _ = await _create_partner_with_key(
        db_session, telegram_id="balance-client-beta", token="nrn_balance_beta_secret",
    )
    alpha.balance_rub = Decimal("999.99")
    alpha.credit_limit_rub = Decimal("250.00")
    beta.balance_rub = Decimal("5999.25")
    await db_session.commit()

    endpoint = "/api/v1/billing/self/balance"
    response_a = await client.get(endpoint, headers={"Authorization": "Bearer nrn_balance_alpha_secret"})
    assert response_a.status_code == 200
    assert response_a.headers["cache-control"] == "no-store"
    assert response_a.json() == {
        "partner_id": alpha.id,
        "balance_rub": "999.99",
        "credit_limit_rub": "250.00",
        "available_balance_rub": "1249.99",
    }
    response_b = await client.get(endpoint, headers={"x-api-key": "nrn_balance_beta_secret"})
    assert response_b.status_code == 200
    assert response_b.json()["partner_id"] == beta.id
    assert response_b.json()["balance_rub"] == "5999.25"
    assert response_b.json()["credit_limit_rub"] == "0.00"
    admin_only = await client.get(endpoint, headers=admin_headers)
    assert admin_only.status_code == 401

    # No new data was created: all values came from current partner account.
    await db_session.refresh(alpha)
    assert alpha.balance_rub == Decimal("999.99")
    assert alpha.credit_limit_rub == Decimal("250.00")


async def test_balance_endpoint_rejects_missing_revoked_and_inactive_partner_keys(client, db_session):
    endpoint = "/api/v1/billing/self/balance"
    partner, api_key = await _create_partner_with_key(
        db_session, telegram_id="balance-client-disabled", token="nrn_balance_revoked_secret",
    )
    await db_session.commit()

    missing = await client.get(endpoint)
    assert missing.status_code == 401
    assert missing.json()["detail"] == "api_key_required"

    api_key.is_active = False
    await db_session.commit()
    revoked = await client.get(endpoint, headers={"Authorization": "Bearer nrn_balance_revoked_secret"})
    assert revoked.status_code == 401

    api_key.is_active = True
    partner.status = "suspended"
    await db_session.commit()
    disabled = await client.get(endpoint, headers={"Authorization": "Bearer nrn_balance_revoked_secret"})
    assert disabled.status_code == 401
