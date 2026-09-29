from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_cost_coverage import _seed_generation_preflight
from test_native_inference import setup as setup_native

from app.accounts.models import Partner
from app.billing.capital import capital_state, require_provider_capital
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.payments.models import PaymentInvoice
from app.providers.argolink import ArgoLinkAdapter
from app.providers.base import ProviderAdapterError

# The shared fixture replaces this method to keep unrelated tests offline.
ACTUAL_PREPAID_BALANCE = ArgoLinkAdapter.prepaid_balance_usdt
WALLET_RESPONSE = {"isValid": True, "mode": "unrestricted", "unit": "USD", "balance": "9.76346767"}


@pytest.mark.parametrize("unit", ["USD", "USDT"])
async def test_provider_wallet_parses_money_without_binary_float(unit):
    body = (
        '{"isValid":true,"mode":"unrestricted","unit":"' + unit + '","balance":9.763467670123456789,"remaining":99999}'
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
        base_url="https://argolink.io",
    ) as client:
        adapter = ArgoLinkAdapter(api_key="test-provider-key", client=client)
        assert await ACTUAL_PREPAID_BALANCE(adapter) == Decimal("9.763467670123456789")


@pytest.mark.parametrize(
    "changes",
    [
        {"balance": None},
        {"balance": True},
        {"balance": False},
        {"balance": "-0.01"},
        {"balance": "NaN"},
        {"balance": "Infinity"},
        {"balance": "-Infinity"},
        {"balance": "unknown"},
        {"balance": []},
        {"isValid": False},
        {"isValid": 1},
        {"isValid": None},
        {"mode": "quota", "quota": {"remaining": "10000"}},
        {"mode": "subscription"},
        {"mode": "unknown"},
        {"mode": None},
        {"quota": {"remaining": "10000"}},
        {"subscription": {"remaining": "10000"}},
        {"status": "disabled"},
        {"unit": "RUB"},
        {"unit": None},
    ],
)
async def test_invalid_or_nonwallet_provider_funds_fail_closed(changes):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={**WALLET_RESPONSE, **changes})),
        base_url="https://argolink.io",
    ) as client:
        with pytest.raises(ProviderAdapterError) as rejected:
            await ACTUAL_PREPAID_BALANCE(ArgoLinkAdapter(api_key="test-provider-key", client=client))
    assert rejected.value.public_code == "provider_temporarily_unavailable"


async def test_remaining_and_quota_do_not_replace_explicit_provider_balance():
    response = {"isValid": True, "mode": "unrestricted", "unit": "USD", "remaining": 10000}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
        base_url="https://argolink.io",
    ) as client:
        with pytest.raises(ProviderAdapterError):
            await ACTUAL_PREPAID_BALANCE(ArgoLinkAdapter(api_key="test-provider-key", client=client))


async def test_zero_provider_balance_is_valid_money():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={**WALLET_RESPONSE, "balance": 0})),
        base_url="https://argolink.io",
    ) as client:
        assert await ACTUAL_PREPAID_BALANCE(ArgoLinkAdapter(api_key="test-provider-key", client=client)) == Decimal(0)


@pytest.mark.parametrize("failure", ["unauthorized", "server_error", "timeout", "malformed", "array"])
async def test_provider_balance_api_failures_are_neutral(failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "malformed":
            return httpx.Response(200, text="not json")
        if failure == "array":
            return httpx.Response(200, json=[])
        return httpx.Response(401 if failure == "unauthorized" else 503, json={"balance": 10000})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://argolink.io") as client:
        with pytest.raises(ProviderAdapterError) as rejected:
            await ACTUAL_PREPAID_BALANCE(ArgoLinkAdapter(api_key="test-provider-key", client=client))
    assert rejected.value.public_code == "provider_temporarily_unavailable"


async def test_prepaid_funds_admit_generation_without_increasing_withdrawable_cash(db_session, monkeypatch):
    partner_id, _ = await _seed_generation_preflight(db_session)
    monkeypatch.setattr(get_settings(), "opening_working_capital_usdt", Decimal(0))
    monkeypatch.setattr("app.billing.capital.wallet_balance", AsyncMock(return_value=(Decimal(0), 0, "fresh")))
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", AsyncMock(return_value=Decimal("9.76346767")))
    before = await capital_state(db_session)

    await require_provider_capital(db_session, Decimal(".85"), partner_id=partner_id)

    after = await capital_state(db_session)
    assert after["safe"] == before["safe"] <= 0
    assert after["components"]["accessible_wallet_usdt"] == Decimal(0)
    assert after["components"]["book_working_capital_usdt"] == Decimal(0)


async def test_provider_capital_does_not_require_crypto_wallet_availability(db_session, monkeypatch):
    partner_id, _ = await _seed_generation_preflight(db_session)
    wallet = AsyncMock(side_effect=AssertionError("Provider funding must not query Crypto Pay"))
    monkeypatch.setattr("app.billing.capital.wallet_balance", wallet)
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", AsyncMock(return_value=Decimal(".85")))

    await require_provider_capital(db_session, Decimal(".85"), partner_id=partner_id)

    wallet.assert_not_awaited()


@pytest.mark.parametrize("balance", [Decimal(0), Decimal(".84999999")])
async def test_insufficient_provider_balance_cannot_use_funded_crypto_wallet(db_session, monkeypatch, balance):
    partner_id, _ = await _seed_generation_preflight(db_session)
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", AsyncMock(return_value=balance))
    with pytest.raises(HTTPException) as rejected:
        await require_provider_capital(db_session, Decimal(".85"), partner_id=partner_id)
    assert (rejected.value.status_code, rejected.value.detail) == (503, "provider_temporarily_unavailable")


async def test_unavailable_provider_balance_cannot_use_funded_crypto_wallet(db_session, monkeypatch):
    partner_id, _ = await _seed_generation_preflight(db_session)
    monkeypatch.setattr(
        ArgoLinkAdapter,
        "prepaid_balance_usdt",
        AsyncMock(side_effect=ProviderAdapterError("provider_temporarily_unavailable")),
    )
    with pytest.raises(HTTPException) as rejected:
        await require_provider_capital(db_session, Decimal(".85"), partner_id=partner_id)
    assert (rejected.value.status_code, rejected.value.detail) == (503, "provider_temporarily_unavailable")


@pytest.mark.parametrize(
    "generation_status",
    ["queued", "sent_to_provider", "processing", "timeout", "submitting", "reconciliation_required"],
)
async def test_other_partner_active_reserve_restricts_shared_provider_funds(db_session, monkeypatch, generation_status):
    partner_id, _ = await _seed_generation_preflight(db_session)
    other = Partner(telegram_id="other-funded-partner", company_name="Other", project_name="Other")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Generation(
            partner_id=other.id,
            model_id="other-model",
            model_slug="seedance-2.5",
            mode="default",
            resolution="480p",
            status=generation_status,
            idempotency_key="other-provider-reserve",
            partner_price_rub=Decimal("200"),
            provider_cost_usdt_snapshot=Decimal("1.25"),
            prompt="test",
        )
    )
    await db_session.flush()
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", AsyncMock(return_value=Decimal("2")))
    with pytest.raises(HTTPException) as rejected:
        await require_provider_capital(db_session, Decimal(".76"), partner_id=partner_id)
    assert rejected.value.status_code == 503
    await require_provider_capital(db_session, Decimal(".75"), partner_id=partner_id)


async def test_pending_paid_deposit_and_required_float_remain_reserved(db_session, monkeypatch):
    partner_id, _ = await _seed_generation_preflight(db_session)
    db_session.add(
        PaymentInvoice(
            partner_id=partner_id,
            idempotency_key="pending-provider-capital",
            status="paid_waiting_credit",
            requested_rub=Decimal("200"),
            paid_at=utc_now(),
            paid_asset="USDT",
            paid_amount=Decimal("2"),
            refunded_rub=Decimal("50"),
        )
    )
    await db_session.flush()
    monkeypatch.setattr(get_settings(), "required_provider_float_usdt", Decimal(".25"))
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", AsyncMock(return_value=Decimal("2")))
    with pytest.raises(HTTPException) as rejected:
        await require_provider_capital(db_session, Decimal(".26"), partner_id=partner_id)
    assert rejected.value.status_code == 503
    await require_provider_capital(db_session, Decimal(".25"), partner_id=partner_id)


async def test_unpriced_paid_pending_deposit_blocks_provider_admission(db_session, monkeypatch):
    partner_id, _ = await _seed_generation_preflight(db_session)
    db_session.add(
        PaymentInvoice(
            partner_id=partner_id,
            idempotency_key="unpriced-provider-capital",
            status="paid_waiting_credit",
            requested_rub=Decimal("200"),
            paid_at=utc_now(),
            paid_asset="TON",
            paid_amount=Decimal("2"),
        )
    )
    await db_session.flush()
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", AsyncMock(return_value=Decimal("10000")))
    with pytest.raises(HTTPException) as rejected:
        await require_provider_capital(db_session, Decimal(".01"), partner_id=partner_id)
    assert rejected.value.status_code == 503


@pytest.mark.parametrize("native", [False, True], ids=["generation-api", "native-video-api"])
async def test_provider_funding_reject_preserves_request_for_retry_without_wallet_cash(
    client, db_session, monkeypatch, native
):
    upstream = None
    if native:

        def no_submission(request):
            raise AssertionError("Durable admission must not submit provider work")

        partner, headers, upstream = await setup_native(
            db_session,
            monkeypatch,
            no_submission,
            model="seedance-2.5",
            category="video",
            rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
        )
        partner_id = partner.id
        url = "/v1/videos/generations"
        body = {"model": "seedance-2.5", "prompt": "test", "duration": 4, "resolution": "480p"}
    else:
        partner_id, token = await _seed_generation_preflight(db_session)
        partner = await db_session.get(Partner, partner_id)
        headers = {"Authorization": f"Bearer {token}"}
        url = "/api/v1/generations"
        body = {
            "model_slug": "seedance-2.5",
            "mode": "text_to_video",
            "resolution": "720p",
            "duration_seconds": 5,
            "prompt": "test",
            "idempotency_key": "prepaid-retry",
        }
    partner.balance_rub = Decimal("500")
    partner.cost_coverage_rub = Decimal(0)
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "opening_working_capital_usdt", Decimal(0))
    monkeypatch.setattr("app.billing.capital.wallet_balance", AsyncMock(return_value=(Decimal(0), 0, "fresh")))
    balance = AsyncMock(return_value=Decimal(0))
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", balance)

    try:
        rejected = await client.post(url, headers=headers, json=body)
        assert rejected.status_code == 503
        assert "provider_temporarily_unavailable" in rejected.text
        for table in (Generation, LedgerEntry, CoverageLedgerEntry):
            assert (await db_session.execute(select(table))).scalars().all() == []
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("500")

        balance.return_value = Decimal("9.76346767")
        accepted = await client.post(url, headers=headers, json=body)
        assert accepted.status_code == 202, accepted.text
        again = await client.post(url, headers=headers, json=body)
        assert again.status_code == 202, again.text
        assert len((await db_session.execute(select(Generation))).scalars().all()) == 1
        assert len((await db_session.execute(select(LedgerEntry))).scalars().all()) == 1
    finally:
        if upstream is not None:
            await upstream.aclose()


@pytest.mark.integration
async def test_postgres_serializes_provider_reservations_across_partner_keys(monkeypatch):
    import asyncio
    import os
    from types import SimpleNamespace
    from uuid import uuid4

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL URL not configured")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    adapter = SimpleNamespace(prepaid_balance_usdt=AsyncMock(return_value=Decimal(".1")))
    monkeypatch.setattr("app.billing.capital.get_partner_provider_adapter", AsyncMock(return_value=adapter))
    monkeypatch.setattr(get_settings(), "required_provider_float_usdt", Decimal(0))
    ids = []
    async with factory() as db:
        for _ in range(2):
            partner = Partner(telegram_id=str(uuid4()), company_name="Provider race", project_name="Test")
            db.add(partner)
            await db.flush()
            ids.append(partner.id)
        await db.commit()

    async def reserve(partner_id):
        async with factory() as db:
            try:
                await require_provider_capital(db, Decimal(".06"), partner_id=partner_id)
                db.add(
                    Generation(
                        partner_id=partner_id,
                        model_id="provider-capital-race",
                        model_slug="seedance-2.5",
                        mode="default",
                        resolution="480p",
                        status="queued",
                        idempotency_key=str(uuid4()),
                        partner_price_rub=Decimal("12"),
                        provider_cost_usdt_snapshot=Decimal(".06"),
                        prompt="test",
                    )
                )
                await db.commit()
                return True
            except HTTPException:
                await db.rollback()
                return False

    try:
        results = await asyncio.wait_for(asyncio.gather(*(reserve(partner_id) for partner_id in ids)), timeout=10)
        assert sorted(results) == [False, True]
    finally:
        async with factory() as db:
            await db.execute(delete(Generation).where(Generation.partner_id.in_(ids)))
            await db.execute(delete(Partner).where(Partner.id.in_(ids)))
            await db.commit()
        await engine.dispose()
