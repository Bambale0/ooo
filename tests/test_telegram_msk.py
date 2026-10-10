"""Presentation-only timezone regressions, with real isolated DB/dispatcher."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_telegram_cabinet import cabinet as cabinet_fixture
from test_telegram_cabinet import partner

from app.billing.models import LedgerEntry, MarginThresholdHistory
from app.catalog.models import Model, PartnerPriceHistory
from app.telegram.admin_partners import _date

cabinet = cabinet_fixture
MSK = "МСК"


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime(2026, 10, 10, 3, 14, tzinfo=UTC), "10.10.2026 06:14"),
        (datetime(2026, 10, 10, 3, 14), "10.10.2026 06:14"),
        (datetime(2026, 10, 10, 6, 14, tzinfo=timezone(timedelta(hours=3))), "10.10.2026 06:14"),
        (datetime(2026, 10, 10, 8, 14, tzinfo=timezone(timedelta(hours=5))), "10.10.2026 06:14"),
        (datetime(2026, 10, 9, 22, 59, tzinfo=UTC), "10.10.2026 01:59"),
        (datetime(2026, 12, 31, 22, 0, tzinfo=UTC), "01.01.2027 01:00"),
        (datetime(2028, 2, 29, 22, 0, tzinfo=UTC), "01.03.2028 01:00"),
    ],
)
def test_admin_dates_are_msk_not_server_local_and_do_not_mutate(value, expected):
    before = value.isoformat()
    assert _date(value) == f"{expected} {MSK}"
    assert value.isoformat() == before


@pytest.mark.parametrize("value", [None, ""])
def test_missing_admin_date_remains_placeholder(value):
    assert _date(value) == "—"


async def test_admin_ledger_keeps_moscow_time_and_all_internal_entries(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    other = await partner(db_session, "456")
    gen_id = str(uuid4())
    rows = []
    for index, (kind, amount, hour, minute) in enumerate(
        [
            ("generation_reserve", "-975.80", 3, 14),
            ("generation_usage_adjustment", "476.00", 3, 20),
            ("generation_usage_adjustment", "0.00", 3, 1),
            ("generation_usage_adjustment", "-5.00", 3, 22),
        ]
    ):
        row = LedgerEntry(
            partner_id=owner.id,
            operation_type=kind,
            amount_rub=Decimal(amount),
            balance_after_rub=Decimal("1000"),
            idempotency_key=f"msk-ledger-{index}",
            generation_id=gen_id,
            created_at=datetime(2026, 10, 10, hour, minute, tzinfo=UTC),
        )
        rows.append(row)
    hidden = LedgerEntry(
        partner_id=other.id,
        operation_type="generation_charge",
        amount_rub=Decimal("-9"),
        balance_after_rub=Decimal("991"),
        idempotency_key="other-msk-ledger",
        created_at=datetime(2026, 10, 10, 3, 25, tzinfo=UTC),
    )
    db_session.add_all([*rows, hidden])
    await db_session.commit()
    before = [(r.id, r.amount_rub, r.created_at.replace(tzinfo=UTC)) for r in rows]
    text = (await feed(user=999, callback=f"admin_partner_ledger:{owner.id}"))[-1].text
    for minute in ("14", "20", "01", "22"):
        assert f"10.10.2026 06:{minute} {MSK}" in text
    assert "generation_reserve" in text and "-975.80" in text
    assert "generation_usage_adjustment" in text and "+476.00" in text
    assert "+0.00" in text and "-5.00" in text
    assert hidden.id not in text and gen_id in text
    assert text.index(rows[3].id) < text.index(rows[1].id) < text.index(rows[0].id)
    assert all(r.id in text for r in rows)
    assert [(r.id, r.amount_rub, r.created_at.replace(tzinfo=UTC)) for r in rows] == before
    assert owner.balance_rub == Decimal("1000")
    assert (await db_session.execute(select(func.count()).select_from(LedgerEntry))).scalar() == 5
    admin_text = (await feed(user=999, callback=f"admin_partner_ledger:{owner.id}"))[-1].text
    assert f"10.10.2026 06:14 {MSK}" in admin_text
    assert hidden.id not in admin_text


async def test_admin_partner_date_rollover(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    owner.created_at = datetime(2026, 10, 9, 22, 59, tzinfo=UTC)
    await db_session.commit()
    text = (await feed(user=999, callback=f"admin_partner_view:{owner.id}"))[-1].text
    assert f"10.10.2026 01:59 {MSK}" in text
    assert owner.created_at.replace(tzinfo=UTC) == datetime(2026, 10, 9, 22, 59, tzinfo=UTC)


async def test_threshold_and_price_history_use_moscow_date(cabinet, db_session):
    feed, _ = cabinet
    when = datetime(2026, 10, 9, 22, 59, tzinfo=UTC)
    row = MarginThresholdHistory(
        scope="global", old_value=Decimal("10"), new_value=Decimal("12"), actor="999", created_at=when
    )
    model = Model(slug="test-msk-model", name="Test MSK", modality="video")
    db_session.add_all([row, model])
    await db_session.flush()
    price = PartnerPriceHistory(
        model_id=model.id,
        new_price_rub=Decimal("20"),
        new_provider_cost_usdt=Decimal(".1"),
        rub_per_usdt_snapshot=Decimal("100"),
        created_at=when,
    )
    db_session.add(price)
    await db_session.commit()
    threshold_text = (await feed(user=999, callback="admin_threshold_history:0"))[-1].text
    assert f"10.10 01:59 {MSK}" in threshold_text
    price_text = (await feed(user=999, callback="admin_catalog:0"))[-1].text
    assert f"История 10.10 {MSK}" in price_text
