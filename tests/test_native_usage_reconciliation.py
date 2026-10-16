from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_native_inference import setup

from app.generations.models import Generation
from app.inference.accounting import settle_actual
from app.infrastructure.retry import utc_now
from app.providers.models import ProviderAttempt


async def unpriced(client, db_session, monkeypatch):
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(200, json={"output": [{"content": "valid unpriced result"}]}),
    )
    response = await client.post(
        "/v1/responses", headers=headers, json={"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    )
    assert response.status_code == 200
    return (
        partner,
        await db_session.scalar(select(Generation)),
        await db_session.scalar(select(ProviderAttempt)),
        upstream,
    )


async def test_cost_then_usage_reconciliation_never_releases_coverage_twice(
    client, db_session, monkeypatch, admin_headers
):
    partner, g, a, upstream = await unpriced(client, db_session, monkeypatch)
    cost = await client.post(
        f"/api/v1/providers/reconciliation/{g.id}/attempts/{a.id}/cost",
        headers=admin_headers,
        json={"outcome": "charged", "provider_cost_usdt": "0.00005", "reason": "Confirmed cost from supplier evidence"},
    )
    assert cost.status_code == 200, cost.text
    await db_session.refresh(partner)
    assert partner.cost_coverage_rub == Decimal("999999.99")
    usage = await client.post(
        f"/api/v1/providers/reconciliation/{g.id}",
        headers=admin_headers,
        json={
            "outcome": "completed",
            "units": {"input_tokens": 20, "output_tokens": 30},
            "reason": "Recovered final token usage from supplier",
        },
    )
    assert usage.status_code == 200, usage.text
    await db_session.refresh(partner)
    assert partner.cost_coverage_rub == Decimal("999999.99")
    assert partner.balance_rub == Decimal("999999.95")
    assert g.actual_provider_cost_usdt == Decimal(".00005")
    assert not g.request_payload.get("usage_reconciliation_pending")
    assert g.provider_cost_hold_usdt == 0
    await upstream.aclose()


async def test_expired_usage_cannot_race_the_worker_to_create_a_charge(client, db_session, monkeypatch):
    partner, g, a, upstream = await unpriced(client, db_session, monkeypatch)
    g.client_finalized_at = utc_now() - timedelta(hours=1)
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await settle_actual(db_session, g, {"input_tokens": 20, "output_tokens": 30})
    assert error.value.status_code == 409
    assert g.actual_charge_rub is None and partner.balance_rub == Decimal("1000000")
    await upstream.aclose()


async def test_repeated_settlement_with_changed_usage_is_a_conflict(client, db_session, monkeypatch):
    partner, g, a, upstream = await unpriced(client, db_session, monkeypatch)
    await settle_actual(db_session, g, {"input_tokens": 20, "output_tokens": 30})
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await settle_actual(db_session, g, {"input_tokens": 20, "output_tokens": 31})
    assert error.value.status_code == 409
    assert partner.balance_rub == Decimal("999999.95")
    await upstream.aclose()
