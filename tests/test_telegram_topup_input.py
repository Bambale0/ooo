import logging
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.infrastructure.config import get_settings
from app.payments.models import PaymentInvoice
from app.telegram.models import BotAction, BotDialog
from tests.test_telegram_cabinet import cabinet as cabinet_fixture
from tests.test_telegram_cabinet import partner

cabinet = cabinet_fixture


@pytest.mark.parametrize("value", ["3000", " 3000 ", "\u200b3000", "\ufeff3000\u2060", " \u200b3000\ufeff "])
async def test_topup_accepts_digits_with_boundary_paste_marks(cabinet, db_session, monkeypatch, value):
    from tests.test_lava_top_payments import FakeLavaTopClient

    feed, _ = cabinet
    owner = await partner(db_session)
    owner.payment_email = "buyer@example.org"
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "payment_provider", "lava_top")
    fake = FakeLavaTopClient()
    monkeypatch.setattr("app.payments.service.get_lava_top_client", lambda: fake)
    await feed(callback="topup")
    methods = await feed(text=value)
    assert "3,300" in methods[-1].text, methods[-1].text
    assert "3,000" in methods[-1].text
    assert not fake.created
    assert await db_session.scalar(select(func.count()).select_from(PaymentInvoice)) == 0
    action = (await db_session.scalars(select(BotAction).where(BotAction.kind == "invoice_create"))).one()
    assert action.payload["amount"] == "3000"
    await feed(callback=f"confirm:{action.id}")
    await feed(callback=f"confirm:{action.id}")
    assert fake.created == [(3300, "buyer@example.org")]
    assert await db_session.scalar(select(func.count()).select_from(PaymentInvoice)) == 1
    assert owner.balance_rub == Decimal("1000")


@pytest.mark.parametrize(
    "value",
    ["999", "-3000", "3000.5", "3e3", "3 000", "3000 RUB", "\uff13\uff10\uff10\uff10", "30\u200b00"],
)
async def test_topup_never_silently_reinterprets_invalid_amount(cabinet, db_session, monkeypatch, value):
    feed, _ = cabinet
    owner = await partner(db_session)
    owner.payment_email = "buyer@example.org"
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "payment_provider", "lava_top")
    await feed(callback="topup")
    await feed(text=value)
    assert await db_session.scalar(select(func.count()).select_from(BotAction)) == 0
    assert await db_session.scalar(select(func.count()).select_from(PaymentInvoice)) == 0
    assert (await db_session.get(BotDialog, "123")).state == "topup"


async def test_topup_internal_value_error_is_not_reported_as_bad_input(cabinet, db_session, monkeypatch, caplog):
    feed, _ = cabinet
    owner = await partner(db_session)
    owner.payment_email = "private-buyer@example.org"
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "payment_provider", "lava_top")
    await feed(callback="topup")

    def broken_checkout(amount):
        raise ValueError("synthetic-secret private-buyer@example.org")

    monkeypatch.setattr("app.payments.lava_top.lava_top_checkout_amount", broken_checkout)
    with caplog.at_level(logging.WARNING, logger="app.telegram.app"):
        methods = await feed(text="3000")
    assert "техническая ошибка" in methods[-1].text.lower(), methods[-1].text
    records = [record for record in caplog.records if record.getMessage() == "cabinet_input_processing_failed"]
    assert len(records) == 1
    record = records[0]
    assert record.dialog_state == "topup"
    assert record.error_type == "ValueError"
    assert record.error_site_function == "broken_checkout"
    assert record.exc_info is None
    assert "synthetic-secret" not in caplog.text
    assert "private-buyer@example.org" not in caplog.text
    assert "synthetic-secret" not in repr(record.__dict__)
    assert "private-buyer@example.org" not in repr(record.__dict__)
    assert await db_session.scalar(select(func.count()).select_from(BotAction)) == 0
    assert (await db_session.get(BotDialog, "123")).state == "topup"
