from decimal import Decimal

from test_telegram_cabinet import cabinet as cabinet_fixture
from test_telegram_cabinet import partner

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.payments.models import PaymentInvoice

cabinet = cabinet_fixture


async def test_admin_and_partner_history_display_full_linked_uuids(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    generation = Generation(
        partner_id=owner.id,
        model_id="model",
        model_slug="nano-banana-pro",
        mode="images/edits",
        resolution="1K",
        status="failed",
        prompt="",
        idempotency_key="uuid-history-test",
        partner_price_rub=Decimal("10"),
    )
    payment = PaymentInvoice(
        partner_id=owner.id, requested_rub=Decimal("1000"), idempotency_key="uuid-payment-test", status="credited"
    )
    db_session.add_all([generation, payment])
    await db_session.flush()
    ledger = LedgerEntry(
        partner_id=owner.id,
        generation_id=generation.id,
        operation_type="generation_reserve",
        amount_rub=Decimal("-10"),
        balance_after_rub=Decimal("990"),
        idempotency_key="uuid-ledger-test",
    )
    db_session.add(ledger)
    await db_session.commit()
    admin = await feed(user=999, callback=f"admin_partner_gens:{owner.id}:0")
    assert generation.id in admin[-1].text
    payments = await feed(user=999, callback=f"admin_partner_payments:{owner.id}:0")
    assert payment.id in payments[-1].text
    history = await feed(callback="history:0")
    assert generation.id in history[-1].text and ledger.id in history[-1].text
    admin_ledger = await feed(user=999, callback=f"admin_partner_ledger:{owner.id}:0")
    assert generation.id in admin_ledger[-1].text and ledger.id in admin_ledger[-1].text


async def test_payment_queue_has_full_uuid_in_message_not_only_button(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    payment = PaymentInvoice(
        partner_id=owner.id,
        requested_rub=Decimal("1000"),
        idempotency_key="queue-full-uuid",
        status="paid_waiting_credit",
    )
    db_session.add(payment)
    await db_session.commit()
    methods = await feed(user=999, callback="admin_payments:0")
    assert payment.id in methods[-1].text
    for row in methods[-1].reply_markup.inline_keyboard:
        for button in row:
            if button.callback_data:
                assert len(button.callback_data.encode()) <= 64


async def test_partner_history_is_scoped_paginated_and_uuid_search_still_works(cabinet, db_session):
    from uuid import uuid4

    feed, _ = cabinet
    owner = await partner(db_session)
    other = await partner(db_session, "456")
    generation = Generation(
        partner_id=owner.id,
        model_id="model",
        model_slug="nano-banana-pro",
        mode="images/edits",
        resolution="1K",
        status="failed",
        prompt="",
        idempotency_key="history-search-uuid",
        partner_price_rub=Decimal("10"),
    )
    db_session.add(generation)
    await db_session.flush()
    own = []
    for index in range(9):
        row = LedgerEntry(
            partner_id=owner.id,
            generation_id=generation.id,
            operation_type="generation_reserve",
            amount_rub=Decimal("-10"),
            balance_after_rub=Decimal("900"),
            idempotency_key=f"uuid-page-{index}",
        )
        db_session.add(row)
        own.append(row)
    hidden_id = str(uuid4())
    hidden = LedgerEntry(
        partner_id=other.id,
        generation_id=hidden_id,
        operation_type="generation_reserve",
        amount_rub=Decimal("-10"),
        balance_after_rub=Decimal("900"),
        idempotency_key="uuid-private",
    )
    db_session.add(hidden)
    await db_session.commit()
    pages = [(await feed(callback=f"history:{page}"))[-1].text for page in (0, 1)]
    for page in pages:
        assert len(page.encode("utf-16-le")) // 2 <= 4096
        assert hidden.id not in page and hidden_id not in page
    assert all(sum(row.id in page for page in pages) == 1 for row in own)
    await feed(callback="search_prompt")
    found = await feed(text=generation.id)
    assert generation.id in found[-1].text
    await feed(user=456, callback="search_prompt")
    denied = await feed(user=456, text=generation.id)
    assert generation.id not in denied[-1].text
