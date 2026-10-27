"""Approved credit affects admission, never cash or already confirmed accounting."""

from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from test_native_inference import setup

from app.accounts.models import Partner, PartnerCreditLimitHistory
from app.billing.models import LedgerEntry
from app.billing.service import require_sufficient_balance, set_partner_credit_limit
from app.generations.models import Generation


async def partner(db, telegram_id="credit-partner", **kwargs):
    row = Partner(telegram_id=telegram_id, company_name="Test", project_name="Credit", **kwargs)
    db.add(row)
    await db.commit()
    return row


@pytest.mark.parametrize("cash,limit,allowed", [
    ("0.01", "0", True), ("0", "0", False), ("-0.01", "0", False),
    ("-4999.99", "5000", True), ("-5000", "5000", False), ("-5000.01", "5000", False),
])
async def test_admission_boundary(db_session, cash, limit, allowed):
    owner = await partner(db_session, balance_rub=Decimal(cash), credit_limit_rub=Decimal(limit))
    if allowed:
        await require_sufficient_balance(owner, Decimal("0.01"))
    else:
        with pytest.raises(HTTPException) as caught:
            await require_sufficient_balance(owner, Decimal("0.01"))
        assert caught.value.status_code == 402


async def test_admin_limit_update_is_audited_idempotent_and_does_not_credit_cash(client, db_session, admin_headers):
    owner = await partner(db_session, balance_rub=Decimal("284.70"))
    other = await partner(db_session, "other-partner", balance_rub=Decimal("0"))
    url = f"/api/v1/billing/partners/{owner.id}/credit-limit"
    payload = {"credit_limit_rub": "5000.00", "reason": "Approved individual credit"}
    for _ in range(2):
        response = await client.put(url, headers=admin_headers, json=payload)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "partner_id": owner.id, "balance_rub": "284.70", "credit_limit_rub": "5000.00",
            "available_balance_rub": "5284.70",
        }
    history = list(await db_session.scalars(select(PartnerCreditLimitHistory)))
    assert len(history) == 1
    assert (history[0].old_limit_rub, history[0].new_limit_rub) == (Decimal(0), Decimal(5000))
    assert history[0].actor == "admin_api" and history[0].reason == payload["reason"]
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
    await db_session.refresh(other)
    assert other.credit_limit_rub == 0 and other.balance_rub == 0
    balance = await client.get(f"/api/v1/billing/partners/{owner.id}/balance", headers=admin_headers)
    assert balance.json() == response.json()


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "5000.001", "10000000000000000"])
async def test_invalid_limit_does_not_change_account(client, db_session, admin_headers, value):
    owner = await partner(db_session)
    response = await client.put(
        f"/api/v1/billing/partners/{owner.id}/credit-limit", headers=admin_headers,
        json={"credit_limit_rub": value, "reason": "Invalid limit check"},
    )
    assert response.status_code == 422
    await db_session.refresh(owner)
    assert owner.credit_limit_rub == 0
    assert await db_session.scalar(select(func.count()).select_from(PartnerCreditLimitHistory)) == 0


async def test_limit_cannot_be_changed_without_admin_auth(client, db_session):
    owner = await partner(db_session)
    response = await client.put(
        f"/api/v1/billing/partners/{owner.id}/credit-limit",
        json={"credit_limit_rub": "5000", "reason": "Unauthorized request"},
    )
    assert response.status_code in (401, 403)
    await db_session.refresh(owner)
    assert owner.credit_limit_rub == 0


async def test_lower_limit_blocks_debt_without_rewriting_cash_or_history(db_session):
    owner = await partner(db_session, balance_rub=Decimal("-3000"))
    await set_partner_credit_limit(db_session, owner.id, Decimal(5000), actor="test", reason="Allow credit")
    await require_sufficient_balance(owner, Decimal("0.01"))
    await set_partner_credit_limit(db_session, owner.id, Decimal(2000), actor="test", reason="Reduce credit")
    await db_session.commit()
    assert owner.balance_rub == Decimal(-3000)
    with pytest.raises(HTTPException):
        await require_sufficient_balance(owner, Decimal("0.01"))
    assert await db_session.scalar(select(func.count()).select_from(PartnerCreditLimitHistory)) == 2


async def test_limit_and_history_roll_back_together(db_session):
    owner = await partner(db_session)
    owner_id = owner.id
    await set_partner_credit_limit(db_session, owner_id, Decimal(5000), actor="test", reason="Interrupted update")
    await db_session.rollback()
    await db_session.refresh(owner)
    assert owner.credit_limit_rub == 0
    assert await db_session.scalar(select(func.count()).select_from(PartnerCreditLimitHistory)) == 0


async def test_native_charge_at_credit_boundary_is_once_and_next_request_is_blocked(client, db_session, monkeypatch):
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json={
            "id": "credit-usage", "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100},
        })

    owner, headers, provider = await setup(db_session, monkeypatch, reply, model="qwen3.8-max")
    owner.balance_rub, owner.credit_limit_rub = Decimal("-4999.90"), Decimal(5000)
    await db_session.commit()
    body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "Hello"}]}
    try:
        response = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert response.status_code == 200, response.text
        await db_session.refresh(owner)
        assert owner.balance_rub == Decimal("-5000.00")
        duplicate = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert duplicate.status_code == 200 and duplicate.json()["id"] == response.json()["id"]
        rejected = await client.post(
            "/v1/chat/completions", headers={**headers, "Idempotency-Key": "next-credit-request"}, json=body
        )
        assert rejected.status_code == 402
        assert len(calls) == 1
        assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
        assert await db_session.scalar(select(func.count()).select_from(Generation)) == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize("cash,status", [("-4999.99", 202), ("-5000.00", 402)])
async def test_queued_generation_uses_same_credit_boundary(client, db_session, monkeypatch, cash, status):
    def reject_upstream(request):
        raise AssertionError("Admission must not submit paid work synchronously")

    owner, headers, provider = await setup(
        db_session, monkeypatch, reject_upstream, model="seedance-2.5", category="video",
        rates=[("default", "480p", "second", Decimal(20), Decimal(".078"))],
    )
    owner.balance_rub, owner.credit_limit_rub = Decimal(cash), Decimal(5000)
    await db_session.commit()
    try:
        response = await client.post("/api/v1/generations", headers=headers, json={
            "model_slug": "seedance-2.5", "prompt": "Slow motion", "resolution": "480p",
            "mode": "default", "duration_seconds": 4, "idempotency_key": "credit-queued-generation",
        })
        assert response.status_code == status, response.text
        await db_session.refresh(owner)
        assert owner.balance_rub == Decimal(cash)
        assert await db_session.scalar(select(func.count()).select_from(Generation)) == (1 if status == 202 else 0)
        assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
    finally:
        await provider.aclose()
