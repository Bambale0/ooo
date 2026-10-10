"""Partner history is a read model, not the internal reservation journal."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_telegram_cabinet import cabinet as cabinet_fixture
from test_telegram_cabinet import partner

from app.billing.models import LedgerEntry
from app.generations.models import Generation

cabinet = cabinet_fixture
AT = datetime(2026, 10, 10, 3, 14, tzinfo=UTC)


async def generation(db, owner, *, status="completed", actual="499.80", entries=None, at=AT):
    row = Generation(
        partner_id=owner.id,
        model_id=str(uuid4()),
        model_slug="seedance-2.5",
        mode="videos/generations",
        resolution="720p",
        prompt="synthetic",
        idempotency_key=str(uuid4()),
        partner_price_rub=Decimal("975.80"),
        status=status,
        actual_charge_rub=Decimal(actual) if actual is not None else None,
        created_at=at,
    )
    db.add(row)
    await db.flush()
    lines = []
    for i, (kind, amount) in enumerate(
        entries
        if entries is not None
        else [("generation_reserve", "-975.80"), ("generation_usage_adjustment", "476.00")]
    ):
        line = LedgerEntry(
            partner_id=owner.id,
            generation_id=row.id,
            operation_type=kind,
            amount_rub=Decimal(amount),
            balance_after_rub=Decimal("1000"),
            idempotency_key=str(uuid4()),
            created_at=at + timedelta(minutes=6 * i),
        )
        db.add(line)
        lines.append(line)
    await db.commit()
    return row, lines


async def test_final_charge_replaces_reserve_and_adjustment_in_partner_history(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    gen, rows = await generation(db_session, owner)
    before = [(r.id, r.amount_rub, r.created_at.replace(tzinfo=UTC)) for r in rows]
    text = (await feed(callback="history:0"))[-1].text
    assert "10.10 06:20 МСК" in text
    assert "-499.80" in text and "seedance-2.5" in text
    assert text.count(gen.id) == 1
    assert "975.80" not in text and "476.00" not in text
    assert "резерв" not in text.lower()
    assert "Ledger UUID" not in text
    assert [(r.id, r.amount_rub, r.created_at.replace(tzinfo=UTC)) for r in rows] == before
    assert owner.balance_rub == Decimal("1000")
    assert (await db_session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one() == 2
    admin = (await feed(user=999, callback=f"admin_partner_ledger:{owner.id}"))[-1].text
    assert "-975.80" in admin and "+476.00" in admin
    assert all(r.id in admin for r in rows)
    assert "10.10.2026 06:14 МСК" in admin


@pytest.mark.parametrize(
    "actual,entries",
    [
        ("2.00", [("generation_reserve", "-2.00"), ("generation_usage_adjustment", "0.00")]),
        ("15.00", [("generation_reserve", "-10.00"), ("generation_usage_adjustment", "-5.00")]),
        (
            "60.00",
            [
                ("generation_reserve", "-100.00"),
                ("generation_reserve_release", "100.00"),
                ("generation_late_charge", "-100.00"),
                ("generation_usage_adjustment", "40.00"),
            ],
        ),
    ],
)
async def test_zero_extra_and_late_adjustments_are_one_actual_charge(cabinet, db_session, actual, entries):
    feed, _ = cabinet
    owner = await partner(db_session)
    gen, _ = await generation(db_session, owner, actual=actual, entries=entries)
    text = (await feed(callback="history:0"))[-1].text
    assert f"-{actual}" in text and text.count(gen.id) == 1
    assert "+0.00" not in text and "Ledger UUID" not in text


@pytest.mark.parametrize(
    "status,actual,entries",
    [
        ("queued", None, [("generation_reserve", "-975.80")]),
        ("processing", None, [("generation_reserve", "-975.80")]),
        ("reconciliation_required", None, [("generation_reserve", "-975.80")]),
        ("failed", None, [("generation_reserve", "-975.80"), ("generation_reserve_release", "975.80")]),
        ("timeout", None, [("generation_reserve", "-975.80")]),
        ("completed", "0", [("generation_reserve", "-975.80"), ("generation_reserve_release", "975.80")]),
        ("completed", "0", []),
    ],
)
async def test_unsettled_refunded_and_free_jobs_do_not_become_paid_events(cabinet, db_session, status, actual, entries):
    feed, _ = cabinet
    owner = await partner(db_session)
    gen, _ = await generation(db_session, owner, status=status, actual=actual, entries=entries)
    text = (await feed(callback="history:0"))[-1].text
    assert gen.id not in text and "975.80" not in text and "+0.00" not in text


@pytest.mark.parametrize(
    "status,entries",
    [
        ("completed", [("generation_reserve", "-12.00")]),
        ("failed", [("generation_charge", "-12.00")]),
    ],
)
async def test_legacy_real_charges_not_guessed_from_reserved_price(cabinet, db_session, status, entries):
    feed, _ = cabinet
    owner = await partner(db_session)
    gen, _ = await generation(db_session, owner, status=status, actual=None, entries=entries)
    text = (await feed(callback="history:0"))[-1].text
    assert "-12.00" in text and gen.id in text and "975.80" not in text


async def test_cash_movements_tenant_scope_and_pagination_after_filtering(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    other = await partner(db_session, "456")
    hidden, _ = await generation(db_session, other, at=AT + timedelta(days=2))
    for _ in range(12):
        await generation(
            db_session,
            owner,
            status="processing",
            actual=None,
            entries=[("generation_reserve", "-975.80")],
            at=AT + timedelta(days=1),
        )
    ids = []
    for _ in range(10):
        gen, _ = await generation(
            db_session,
            owner,
            actual="2.00",
            entries=[("generation_reserve", "-2"), ("generation_usage_adjustment", "0")],
        )
        ids.append(gen.id)
    cash_ids = []
    for kind, amount in [
        ("payment_credit", "1000"),
        ("manual_adjustment", "-50"),
        ("payment_refund_adjustment", "-20"),
    ]:
        item = LedgerEntry(
            partner_id=owner.id,
            operation_type=kind,
            amount_rub=Decimal(amount),
            balance_after_rub=Decimal("1000"),
            idempotency_key=str(uuid4()),
            created_at=AT + timedelta(minutes=10),
        )
        db_session.add(item)
        await db_session.flush()
        cash_ids.append(item.id)
    # A malformed cross-tenant link must not become a partner-visible generation.
    db_session.add(
        LedgerEntry(
            partner_id=owner.id,
            generation_id=hidden.id,
            operation_type="generation_charge",
            amount_rub=Decimal("-77"),
            balance_after_rub=Decimal("1000"),
            idempotency_key=str(uuid4()),
            created_at=AT,
        )
    )
    await db_session.commit()
    first = (await feed(callback="history:0"))[-1]
    second = (await feed(callback="history:1"))[-1]
    all_ids = ids + cash_ids
    assert sum(i in first.text for i in all_ids) == 8
    assert sum(i in second.text for i in all_ids) == 5
    assert all((i in first.text) != (i in second.text) for i in all_ids)
    assert hidden.id not in first.text + second.text and "-77.00" not in first.text + second.text
    assert "975.80" not in first.text + second.text
    assert "+1000.00" in first.text and "-50.00" in first.text and "-20.00" in first.text
    assert any(b.callback_data == "history:1" for r in first.reply_markup.inline_keyboard for b in r)
    assert not any(b.callback_data == "history:2" for r in second.reply_markup.inline_keyboard for b in r)


async def test_uuid_search_does_not_call_pending_reserve_actual_charge(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    pending, _ = await generation(
        db_session, owner, status="processing", actual=None, entries=[("generation_reserve", "-975.80")]
    )
    final, _ = await generation(db_session, owner)
    await feed(callback="search_prompt")
    text = (await feed(text=pending.id))[-1].text
    assert "975.80" not in text
    await feed(callback="search_prompt")
    text = (await feed(text=final.id))[-1].text
    assert "499.80" in text and "975.80" not in text


@pytest.mark.integration
async def test_postgres_actual_history_projection_is_exact_and_rollback_only():
    import os

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.billing.history import final_generation_charge, partner_transactions

    database_url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(
                    bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                ) as db:
                    owner = await partner(db, "history-" + uuid4().hex)
                    gen, _ = await generation(db, owner)
                    await generation(
                        db,
                        owner,
                        status="reconciliation_required",
                        actual=None,
                        entries=[("generation_reserve", "-975.80")],
                    )
                    rows = await partner_transactions(db, owner.id)
                    assert len(rows) == 1
                    assert rows[0].generation_id == gen.id
                    assert rows[0].amount_rub == Decimal("-499.80")
                    assert rows[0].created_at == AT + timedelta(minutes=6)
                    assert await final_generation_charge(db, gen) == Decimal("499.80")
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
