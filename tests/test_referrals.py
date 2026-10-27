from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select

from app.accounts.models import Partner
from app.payments.lava_top import LavaTopInvoice
from app.payments.models import PaymentInvoice
from app.payments.service import credit_paid_invoice, record_confirmed_refund
from app.referrals.models import ReferralAttribution, ReferralCommission
from app.referrals.service import bind_referral, get_or_create_profile
from app.telegram.models import BotAction, DevelopmentInvoice, DevelopmentRequest
from tests.test_telegram_cabinet import cabinet as cabinet
from tests.test_telegram_development_requests import complete_intake


async def _partner(db, telegram_id: str) -> Partner:
    partner = Partner(telegram_id=telegram_id, company_name="Test", project_name="Test")
    db.add(partner)
    await db.flush()
    return partner


async def test_referral_binding_is_first_touch_lifetime_and_self_referral_is_rejected(db_session):
    first = await _partner(db_session, "700")
    second = await _partner(db_session, "701")
    first_profile = await get_or_create_profile(db_session, first.id)
    second_profile = await get_or_create_profile(db_session, second.id)

    assert await bind_referral(db_session, referred_telegram_id="123", code=first_profile.code)
    assert not await bind_referral(db_session, referred_telegram_id="123", code=second_profile.code)
    assert not await bind_referral(db_session, referred_telegram_id="700", code=first_profile.code)
    attribution = await db_session.scalar(
        select(ReferralAttribution).where(ReferralAttribution.referred_telegram_id == "123")
    )
    assert attribution.referrer_partner_id == first.id

    assert not await bind_referral(db_session, referred_telegram_id="organic", code=None)
    assert not await bind_referral(db_session, referred_telegram_id="organic", code=first_profile.code)
    organic = await db_session.scalar(
        select(ReferralAttribution).where(ReferralAttribution.referred_telegram_id == "organic")
    )
    assert organic.referrer_partner_id is None


async def test_api_topup_commission_and_refund_reversal_are_exactly_once(db_session):
    referrer = await _partner(db_session, "700")
    referred = await _partner(db_session, "123")
    profile = await get_or_create_profile(db_session, referrer.id)
    assert await bind_referral(db_session, referred_telegram_id=referred.telegram_id, code=profile.code)
    payment = PaymentInvoice(
        partner_id=referred.id,
        status="paid_waiting_credit",
        requested_rub=Decimal("1000.00"),
        idempotency_key="referral-payment",
    )
    db_session.add(payment)
    await db_session.flush()

    await credit_paid_invoice(db_session, payment_id=payment.id)
    await credit_paid_invoice(db_session, payment_id=payment.id)
    commissions = (await db_session.execute(select(ReferralCommission))).scalars().all()
    assert [(row.kind, row.amount_rub) for row in commissions] == [("api_topup", Decimal("70.00"))]

    first_refund, _ = await record_confirmed_refund(
        db_session,
        payment_id=payment.id,
        amount_rub=Decimal("333.33"),
        idempotency_key="refund-1",
        reason="partial",
    )
    await record_confirmed_refund(
        db_session,
        payment_id=payment.id,
        amount_rub=Decimal("666.67"),
        idempotency_key="refund-2",
        reason="remaining",
    )
    repeated, _ = await record_confirmed_refund(
        db_session,
        payment_id=payment.id,
        amount_rub=Decimal("333.33"),
        idempotency_key="refund-1",
        reason="partial",
    )
    assert repeated.id == first_refund.id
    assert await db_session.scalar(select(func.sum(ReferralCommission.amount_rub))) == Decimal("0.00")
    assert await db_session.scalar(select(func.count()).select_from(ReferralCommission)) == 3


async def test_telegram_link_dashboard_and_lifetime_binding(cabinet, db_session, monkeypatch):
    from app.infrastructure.config import get_settings

    feed, _ = cabinet
    referrer = await _partner(db_session, "700")
    profile = await get_or_create_profile(db_session, referrer.id)
    monkeypatch.setattr(get_settings(), "telegram_bot_username", "neironych_test_bot")

    dashboard = (await feed(user=700, callback="referrals"))[-1]
    assert "20%" in dashboard.text and "7%" in dashboard.text and "пожизненно" in dashboard.text
    assert f"https://t.me/neironych_test_bot?start=ref_{profile.code}" in dashboard.text

    await feed(text=f"/start ref_{profile.code}")
    attribution = await db_session.scalar(
        select(ReferralAttribution).where(ReferralAttribution.referred_telegram_id == "123")
    )
    assert attribution.referrer_partner_id == referrer.id


async def test_development_commission_waits_for_full_payment_and_accrues_once(db_session):
    from app.telegram.development_billing import sync_development_invoice

    referrer = await _partner(db_session, "700")
    profile = await get_or_create_profile(db_session, referrer.id)
    assert await bind_referral(db_session, referred_telegram_id="123", code=profile.code)
    request = DevelopmentRequest(
        telegram_id="123",
        answers={"kind": "Автоматизация"},
        status="in_progress",
    )
    db_session.add(request)
    await db_session.flush()
    invoice = DevelopmentInvoice(
        development_request_id=request.id,
        installment="advance",
        provider="lava_top",
        provider_reference="development-contract",
        idempotency_key="development-advance",
        status="active",
        total_price_snapshot_rub=Decimal("100000.00"),
        requested_rub=Decimal("30000.00"),
        buyer_email="buyer@example.test",
    )
    db_session.add(invoice)
    await db_session.flush()
    provider = SimpleNamespace(
        invoice_id="development-contract",
        currency="RUB",
        amount=Decimal("30000.00"),
        status="paid",
        buyer_email="buyer@example.test",
        payment_url="https://example.test/pay",
        paid_at="2026-10-27T12:00:00Z",
    )

    await sync_development_invoice(db_session, provider_invoice=provider, invoice_id=invoice.id)
    await sync_development_invoice(db_session, provider_invoice=provider, invoice_id=invoice.id)
    assert await db_session.scalar(select(func.count()).select_from(ReferralCommission)) == 0

    final_invoice = DevelopmentInvoice(
        development_request_id=request.id,
        installment="final",
        provider="lava_top",
        provider_reference="development-final-contract",
        idempotency_key="development-final",
        status="active",
        total_price_snapshot_rub=Decimal("100000.00"),
        requested_rub=Decimal("70000.00"),
        buyer_email="buyer@example.test",
    )
    db_session.add(final_invoice)
    await db_session.flush()
    final_provider = SimpleNamespace(
        invoice_id="development-final-contract",
        currency="RUB",
        amount=Decimal("70000.00"),
        status="paid",
        buyer_email="buyer@example.test",
        payment_url="https://example.test/final-pay",
        paid_at="2026-10-27T13:00:00Z",
    )
    await sync_development_invoice(db_session, provider_invoice=final_provider, invoice_id=final_invoice.id)
    await sync_development_invoice(db_session, provider_invoice=final_provider, invoice_id=final_invoice.id)
    commission = await db_session.scalar(
        select(ReferralCommission).where(ReferralCommission.kind == "development_order")
    )
    assert commission.amount_rub == Decimal("20000.00")
    assert commission.referrer_partner_id == referrer.id

    assert await db_session.scalar(select(func.count()).select_from(ReferralCommission)) == 1


class FakeDevelopmentCheckout:
    def __init__(self):
        self.created = []

    async def create_sbp_invoice(self, *, amount_rub, buyer_email, offer_id=None):
        number = len(self.created) + 1
        reference = f"88888888-8888-4888-8888-{number:012d}"
        self.created.append((amount_rub, buyer_email, offer_id))
        return LavaTopInvoice(
            invoice_id=reference,
            status="active",
            amount=str(amount_rub),
            currency="RUB",
            payment_url=f"https://example.test/{reference}",
            paid_at=None,
            buyer_email=buyer_email,
        )


async def test_admin_sets_individual_price_and_issues_30_70_invoices(cabinet, db_session, monkeypatch):
    from app.telegram.development_billing import sync_development_invoice

    feed, _ = cabinet
    fake = FakeDevelopmentCheckout()
    referrer = await _partner(db_session, "700")
    profile = await get_or_create_profile(db_session, referrer.id)
    assert await bind_referral(db_session, referred_telegram_id="123", code=profile.code)
    monkeypatch.setattr("app.payments.lava_top.get_lava_top_client", lambda: fake)

    await complete_intake(feed)
    await feed(callback="dev:submit")
    request = await db_session.scalar(select(DevelopmentRequest))

    await feed(user=999, callback=f"admin_dev_quote:{request.id}")
    await feed(user=999, text="100000")
    preview = (await feed(user=999, text="buyer@example.org"))[-1]
    assert "Аванс 30%: 30000.00 ₽" in preview.text
    quote_action = await db_session.scalar(
        select(BotAction).where(BotAction.kind == "admin_dev_quote")
    )
    await feed(user=999, callback=f"confirm:{quote_action.id}")
    await db_session.refresh(request)
    assert request.quoted_price_rub == Decimal("100000.00")

    await feed(user=999, callback=f"admin_dev_invoice:{request.id}:advance")
    advance_action = await db_session.scalar(
        select(BotAction)
        .where(BotAction.kind == "admin_dev_invoice", BotAction.status == "pending")
        .order_by(BotAction.created_at.desc())
    )
    await feed(user=999, callback=f"confirm:{advance_action.id}")
    advance = await db_session.scalar(
        select(DevelopmentInvoice).where(DevelopmentInvoice.installment == "advance")
    )
    assert advance.requested_rub == Decimal("30000.00")
    assert fake.created == [(30000, "buyer@example.org", None)]

    paid_advance = LavaTopInvoice(
        invoice_id=advance.provider_reference,
        status="paid",
        amount="30000",
        currency="RUB",
        payment_url=advance.invoice_url,
        paid_at="2026-10-27T12:00:00Z",
        buyer_email="buyer@example.org",
    )
    await sync_development_invoice(db_session, provider_invoice=paid_advance, invoice_id=advance.id)
    assert await db_session.scalar(select(func.count()).select_from(ReferralCommission)) == 0
    screen = (await feed(user=999, callback=f"admin_dev_view:{request.id}"))[-1]
    assert any(
        button.callback_data == f"admin_dev_invoice:{request.id}:final"
        for row in screen.reply_markup.inline_keyboard
        for button in row
    )

    await feed(user=999, callback=f"admin_dev_invoice:{request.id}:final")
    final_action = await db_session.scalar(
        select(BotAction)
        .where(BotAction.kind == "admin_dev_invoice", BotAction.status == "pending")
        .order_by(BotAction.created_at.desc())
    )
    await feed(user=999, callback=f"confirm:{final_action.id}")
    final = await db_session.scalar(
        select(DevelopmentInvoice).where(DevelopmentInvoice.installment == "final")
    )
    assert final.requested_rub == Decimal("70000.00")
    assert sum(invoice.requested_rub for invoice in (advance, final)) == request.quoted_price_rub
    paid_final = LavaTopInvoice(
        invoice_id=final.provider_reference,
        status="paid",
        amount="70000",
        currency="RUB",
        payment_url=final.invoice_url,
        paid_at="2026-10-27T13:00:00Z",
        buyer_email="buyer@example.org",
    )
    await sync_development_invoice(db_session, provider_invoice=paid_final, invoice_id=final.id)
    assert await db_session.scalar(select(func.sum(ReferralCommission.amount_rub))) == Decimal("20000.00")
    assert await db_session.scalar(select(func.count()).select_from(ReferralCommission)) == 1
