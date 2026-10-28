from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.accounts.models import Partner
from app.infrastructure.retry import utc_now
from app.payments.lava_top import LavaTopBuyerEmailRejected, LavaTopError, LavaTopInvoice
from app.referrals.models import ReferralCommission
from app.referrals.service import (
    accrue_commission,
    bind_referral,
    claim_partner_attribution,
    commission_amount,
    get_or_create_profile,
    referral_stats,
)
from app.telegram.development_billing import (
    create_development_invoice,
    installment_amount,
    set_quote,
    sync_development_invoice,
)
from app.telegram.models import DevelopmentInvoice, DevelopmentRequest


async def _partner(db, telegram_id: str, *, status: str = "active") -> Partner:
    partner = Partner(
        telegram_id=telegram_id,
        company_name="Referral test",
        project_name="Referral test",
        status=status,
    )
    db.add(partner)
    await db.flush()
    return partner


async def _request(
    db,
    telegram_id: str = "customer",
    *,
    price: Decimal | None = Decimal("100000"),
    email: str | None = "buyer@example.test",
) -> DevelopmentRequest:
    request = DevelopmentRequest(
        telegram_id=telegram_id,
        answers={"kind": "Automation"},
        status="in_progress",
        quoted_price_rub=price,
        buyer_email=email,
    )
    db.add(request)
    await db.flush()
    return request


async def _invoice(
    db,
    request: DevelopmentRequest,
    installment: str,
    amount: Decimal,
    *,
    reference: str | None = None,
    total: Decimal = Decimal("100000"),
    status: str = "active",
    settled: bool = False,
) -> DevelopmentInvoice:
    invoice = DevelopmentInvoice(
        development_request_id=request.id,
        installment=installment,
        provider="lava_top",
        provider_reference=reference,
        idempotency_key=f"{request.id}:{installment}",
        status=status,
        total_price_snapshot_rub=total,
        requested_rub=amount,
        buyer_email=request.buyer_email or "buyer@example.test",
        settled_at=utc_now() if settled else None,
    )
    db.add(invoice)
    await db.flush()
    return invoice


def _provider(
    invoice_id: str,
    amount: Decimal,
    *,
    status: str = "active",
    currency: str = "RUB",
    email: str | None = "buyer@example.test",
    paid_at: str | None = None,
) -> LavaTopInvoice:
    return LavaTopInvoice(
        invoice_id=invoice_id,
        status=status,
        amount=str(amount),
        currency=currency,
        payment_url=f"https://pay.example.test/{invoice_id}",
        paid_at=paid_at,
        buyer_email=email,
    )


class CheckoutStub:
    def __init__(self, *, created=None, lookup=None, create_error=None, lookup_error=None):
        self.created = created
        self.lookup = lookup
        self.create_error = create_error
        self.lookup_error = lookup_error
        self.create_calls = 0
        self.lookup_calls = 0

    async def create_sbp_invoice(self, *, amount_rub, buyer_email, offer_id=None):
        self.create_calls += 1
        if self.create_error is not None:
            raise self.create_error
        return self.created

    async def get_invoice(self, invoice_id):
        self.lookup_calls += 1
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.lookup


def _assert_http(error, status_code: int, detail: str) -> None:
    assert error.value.status_code == status_code
    assert error.value.detail == detail


async def test_referral_helpers_claiming_ledger_and_stats(db_session):
    assert commission_amount(Decimal("0.10"), Decimal("0.05")) == Decimal("0.01")
    with pytest.raises(ValueError, match="base must be positive"):
        commission_amount(Decimal("0"), Decimal("0.07"))

    with pytest.raises(HTTPException) as missing:
        await get_or_create_profile(db_session, "missing")
    _assert_http(missing, 404, "partner_not_found")

    referrer = await _partner(db_session, "referrer")
    profile = await get_or_create_profile(db_session, referrer.id)
    assert (await get_or_create_profile(db_session, referrer.id)).id == profile.id

    # Invalid and inactive first touches are deliberately recorded as organic forever.
    assert not await bind_referral(db_session, referred_telegram_id="invalid", code="bad!")
    inactive = await _partner(db_session, "inactive", status="suspended")
    inactive_profile = await get_or_create_profile(db_session, inactive.id)
    assert not await bind_referral(
        db_session,
        referred_telegram_id="inactive-touch",
        code=inactive_profile.code,
    )
    assert not await bind_referral(
        db_session,
        referred_telegram_id="invalid",
        code=profile.code,
    )

    assert await bind_referral(db_session, referred_telegram_id="future-partner", code=profile.code)
    referred = await _partner(db_session, "future-partner")
    attribution = await claim_partner_attribution(db_session, referred)
    assert attribution is not None
    assert attribution.referred_partner_id == referred.id

    assert await accrue_commission(
        db_session,
        referred_telegram_id="nobody",
        kind="api_topup",
        source_id="none",
        event_key="none",
        base_amount_rub=Decimal("100"),
        rate=Decimal("0.07"),
    ) is None

    earned = await accrue_commission(
        db_session,
        referred_telegram_id=referred.telegram_id,
        referred_partner_id=referred.id,
        kind="api_topup",
        source_id="payment",
        event_key="payment:paid",
        base_amount_rub=Decimal("100"),
        rate=Decimal("0.07"),
        amount_rub=Decimal("1.005"),
    )
    assert earned is not None and earned.amount_rub == Decimal("1.01")
    repeated = await accrue_commission(
        db_session,
        referred_telegram_id=referred.telegram_id,
        kind="api_topup",
        source_id="payment",
        event_key="payment:paid",
        base_amount_rub=Decimal("999"),
        rate=Decimal("0.99"),
    )
    assert repeated.id == earned.id

    reversed_row = await accrue_commission(
        db_session,
        referred_telegram_id=referred.telegram_id,
        kind="api_topup_refund",
        source_id="refund",
        event_key="payment:refund",
        base_amount_rub=Decimal("100"),
        rate=Decimal("0.07"),
        amount_rub=Decimal("1.005"),
        reverse=True,
    )
    assert reversed_row is not None and reversed_row.amount_rub == Decimal("-1.01")
    with pytest.raises(ValueError, match="amount must be positive"):
        await accrue_commission(
            db_session,
            referred_telegram_id=referred.telegram_id,
            kind="api_topup",
            source_id="bad",
            event_key="bad-amount",
            base_amount_rub=Decimal("100"),
            rate=Decimal("0.07"),
            amount_rub=Decimal("0"),
        )

    stats = await referral_stats(db_session, referrer.id)
    assert stats == {"invited": 1, "balance_rub": Decimal("0.00")}


def test_installment_rounding_and_validation():
    assert installment_amount(Decimal("101"), "advance") == Decimal("30")
    assert installment_amount(Decimal("101"), "final") == Decimal("71")
    assert installment_amount(Decimal("102"), "advance") == Decimal("31")
    assert installment_amount(Decimal("102"), "final") == Decimal("71")
    with pytest.raises(ValueError, match="invalid development installment"):
        installment_amount(Decimal("100"), "other")


async def test_quote_validation_normalization_and_locking(db_session):
    with pytest.raises(HTTPException) as missing:
        await set_quote(
            db_session,
            request_id="missing",
            total_price_rub=Decimal("100"),
            buyer_email="buyer@example.test",
            actor="admin",
        )
    _assert_http(missing, 404, "development_request_not_found")

    request = await _request(db_session, price=None, email=None)
    request_id = request.id
    for invalid_price in (Decimal("0"), Decimal("10.50")):
        with pytest.raises(HTTPException) as invalid:
            await set_quote(
                db_session,
                request_id=request.id,
                total_price_rub=invalid_price,
                buyer_email="buyer@example.test",
                actor="admin",
            )
        _assert_http(invalid, 422, "invalid_development_price")

    await db_session.commit()
    with pytest.raises(ValueError, match="invalid buyer email"):
        await set_quote(
            db_session,
            request_id=request.id,
            total_price_rub=Decimal("100"),
            buyer_email="not-an-email",
            actor="admin",
        )
    await db_session.rollback()

    quoted = await set_quote(
        db_session,
        request_id=request_id,
        total_price_rub=Decimal("100001"),
        buyer_email=" Buyer@Example.TEST ",
        actor="telegram:999",
    )
    assert quoted.quoted_price_rub == Decimal("100001")
    assert quoted.buyer_email == "buyer@example.test"
    assert quoted.quoted_by == "telegram:999"
    assert quoted.quoted_at is not None
    await _invoice(db_session, quoted, "advance", Decimal("30000"), total=Decimal("100001"))
    with pytest.raises(HTTPException) as locked:
        await set_quote(
            db_session,
            request_id=request_id,
            total_price_rub=Decimal("200000"),
            buyer_email="other@example.test",
            actor="admin",
        )
    _assert_http(locked, 409, "development_quote_locked")


async def test_invoice_creation_guards(db_session):
    client = CheckoutStub()
    with pytest.raises(HTTPException) as invalid:
        await create_development_invoice(
            db_session,
            request_id="missing",
            installment="other",
            idempotency_key="invalid",
            client=client,
        )
    _assert_http(invalid, 422, "invalid_installment")

    with pytest.raises(HTTPException) as missing:
        await create_development_invoice(
            db_session,
            request_id="missing",
            installment="advance",
            idempotency_key="missing",
            client=client,
        )
    _assert_http(missing, 404, "development_request_not_found")

    unquoted = await _request(db_session, "unquoted", price=None, email=None)
    with pytest.raises(HTTPException) as required:
        await create_development_invoice(
            db_session,
            request_id=unquoted.id,
            installment="advance",
            idempotency_key="unquoted",
            client=client,
        )
    _assert_http(required, 409, "development_quote_required")

    quoted = await _request(db_session, "quoted")
    with pytest.raises(HTTPException) as advance_required:
        await create_development_invoice(
            db_session,
            request_id=quoted.id,
            installment="final",
            idempotency_key="final-first",
            client=client,
        )
    _assert_http(advance_required, 409, "development_advance_not_paid")

    tiny = await _request(db_session, "tiny", price=Decimal("1"))
    with pytest.raises(HTTPException) as tiny_advance:
        await create_development_invoice(
            db_session,
            request_id=tiny.id,
            installment="advance",
            idempotency_key="tiny",
            client=client,
        )
    _assert_http(tiny_advance, 422, "invalid_development_price")
    assert client.create_calls == 0


@pytest.mark.parametrize(
    ("provider_error", "expected_status", "expected_detail"),
    [
        (LavaTopBuyerEmailRejected("rejected"), "failed", "buyer_email_rejected_by_provider"),
        (LavaTopError("connection_lost"), "creation_unknown", "payment_provider_temporarily_unavailable"),
    ],
)
async def test_invoice_creation_errors_are_persisted_without_resubmission(
    db_session,
    provider_error,
    expected_status,
    expected_detail,
):
    request = await _request(db_session)
    client = CheckoutStub(create_error=provider_error)
    with pytest.raises(HTTPException) as failed:
        await create_development_invoice(
            db_session,
            request_id=request.id,
            installment="advance",
            idempotency_key="create-once",
            client=client,
        )
    _assert_http(failed, 422 if expected_status == "failed" else 503, expected_detail)

    invoice = await db_session.scalar(select(DevelopmentInvoice))
    assert invoice.status == expected_status
    assert invoice.creation_claimed_until is None
    if expected_status == "failed":
        assert invoice.reconciliation_snapshot == {
            "source": "create_response",
            "result": "buyer_email_rejected",
        }

    repeated = await create_development_invoice(
        db_session,
        request_id=request.id,
        installment="advance",
        idempotency_key="ignored-on-repeat",
        client=client,
    )
    assert repeated.id == invoice.id
    assert client.create_calls == 1


async def test_existing_invoice_conflict_lookup_failure_and_provider_sync(db_session):
    request = await _request(db_session)
    invoice = await _invoice(
        db_session,
        request,
        "advance",
        Decimal("30000"),
        reference="existing-reference",
    )

    request.quoted_price_rub = Decimal("100001")
    await db_session.flush()
    with pytest.raises(HTTPException) as conflict:
        await create_development_invoice(
            db_session,
            request_id=request.id,
            installment="advance",
            idempotency_key="repeat",
            client=CheckoutStub(),
        )
    _assert_http(conflict, 409, "development_invoice_conflict")

    request.quoted_price_rub = Decimal("100000")
    await db_session.flush()
    failing = CheckoutStub(lookup_error=LavaTopError("timeout"))
    with pytest.raises(HTTPException) as unavailable:
        await create_development_invoice(
            db_session,
            request_id=request.id,
            installment="advance",
            idempotency_key="repeat",
            client=failing,
        )
    _assert_http(unavailable, 503, "payment_provider_temporarily_unavailable")

    missing = CheckoutStub(lookup=None)
    same = await create_development_invoice(
        db_session,
        request_id=request.id,
        installment="advance",
        idempotency_key="repeat",
        client=missing,
    )
    assert same.id == invoice.id

    active = CheckoutStub(lookup=_provider("existing-reference", Decimal("30000")))
    synced = await create_development_invoice(
        db_session,
        request_id=request.id,
        installment="advance",
        idempotency_key="repeat",
        client=active,
    )
    assert synced.invoice_url == "https://pay.example.test/existing-reference"
    assert active.lookup_calls == 1


@pytest.mark.parametrize(
    ("provider", "expected_detail"),
    [
        (_provider("provider", Decimal("30001")), "payment_amount_or_currency_mismatch"),
        (_provider("provider", Decimal("30000"), currency="USD"), "payment_amount_or_currency_mismatch"),
        (_provider("provider", Decimal("30000"), status="paid", email=None), "payment_buyer_mismatch"),
        (_provider("provider", Decimal("30000"), email="other@example.test"), "payment_buyer_mismatch"),
    ],
)
async def test_provider_confirmation_rejects_untrusted_financial_fields(
    db_session,
    provider,
    expected_detail,
):
    request = await _request(db_session)
    invoice = await _invoice(db_session, request, "advance", Decimal("30000"))
    with pytest.raises(HTTPException) as rejected:
        await sync_development_invoice(
            db_session,
            provider_invoice=provider,
            invoice_id=invoice.id,
        )
    _assert_http(rejected, 409, expected_detail)


async def test_provider_identity_missing_records_and_status_mapping(db_session):
    with pytest.raises(HTTPException) as missing:
        await sync_development_invoice(
            db_session,
            provider_invoice=_provider("missing", Decimal("30000")),
        )
    _assert_http(missing, 404, "development_invoice_not_found")

    request = await _request(db_session)
    conflicting = await _invoice(
        db_session,
        request,
        "advance",
        Decimal("30000"),
        reference="original",
    )
    with pytest.raises(HTTPException) as identity:
        await sync_development_invoice(
            db_session,
            provider_invoice=_provider("different", Decimal("30000")),
            invoice_id=conflicting.id,
        )
    _assert_http(identity, 409, "payment_provider_id_conflict")

    for status_value in ("active", "failed", "cancelled"):
        separate = await _request(db_session, f"customer-{status_value}")
        invoice = await _invoice(db_session, separate, "advance", Decimal("30000"))
        synced = await sync_development_invoice(
            db_session,
            provider_invoice=_provider(f"provider-{status_value}", Decimal("30000"), status=status_value),
            invoice_id=invoice.id,
        )
        assert synced.status == status_value

    orphan = DevelopmentInvoice(
        development_request_id="missing-request",
        installment="advance",
        provider="lava_top",
        idempotency_key="orphan",
        status="active",
        total_price_snapshot_rub=Decimal("100000"),
        requested_rub=Decimal("30000"),
        buyer_email="buyer@example.test",
    )
    db_session.add(orphan)
    await db_session.flush()
    with pytest.raises(HTTPException) as missing_request:
        await sync_development_invoice(
            db_session,
            provider_invoice=_provider("orphan-provider", Decimal("30000")),
            invoice_id=orphan.id,
        )
    _assert_http(missing_request, 404, "development_request_not_found")


@pytest.mark.parametrize("integrity_case", ["snapshot", "sum"])
async def test_completed_order_requires_consistent_30_70_contract(db_session, integrity_case):
    request = await _request(db_session)
    advance_total = Decimal("100001") if integrity_case == "snapshot" else Decimal("100000")
    advance_amount = Decimal("30001") if integrity_case == "sum" else Decimal("30000")
    await _invoice(
        db_session,
        request,
        "advance",
        advance_amount,
        total=advance_total,
        reference="advance",
        status="paid",
        settled=True,
    )
    final = await _invoice(
        db_session,
        request,
        "final",
        Decimal("70000"),
        reference="final",
    )
    with pytest.raises(HTTPException) as conflict:
        await sync_development_invoice(
            db_session,
            provider_invoice=_provider("final", Decimal("70000"), status="paid"),
            invoice_id=final.id,
        )
    _assert_http(conflict, 409, "development_invoice_conflict")
    assert await db_session.scalar(select(func.count()).select_from(ReferralCommission)) == 0


async def test_fully_paid_organic_order_does_not_create_commission(db_session):
    request = await _request(db_session, "organic")
    await _invoice(
        db_session,
        request,
        "advance",
        Decimal("30000"),
        reference="advance",
        status="paid",
        settled=True,
    )
    final = await _invoice(db_session, request, "final", Decimal("70000"), reference="final")
    synced = await sync_development_invoice(
        db_session,
        provider_invoice=_provider(
            "final",
            Decimal("70000"),
            status="paid",
            paid_at="not-a-date",
        ),
        invoice_id=final.id,
    )
    assert synced.settled_at is not None
    assert synced.paid_at is not None
    assert await db_session.scalar(select(func.count()).select_from(ReferralCommission)) == 0


async def test_development_reconciliation_recovers_payment_without_duplicate_checkout(db_session):
    from app.payments.reconciliation import reconcile_development_payments_once

    request = await _request(db_session)
    invoice = await _invoice(
        db_session,
        request,
        "advance",
        Decimal("30000"),
        reference="reconcile-reference",
        status="creation_unknown",
    )
    await db_session.commit()
    client = CheckoutStub(
        lookup=_provider(
            "reconcile-reference",
            Decimal("30000"),
            status="paid",
            paid_at="2026-10-28T10:00:00",
        )
    )
    cursor = await reconcile_development_payments_once(
        session_factory=async_sessionmaker(db_session.bind, expire_on_commit=False),
        client=client,
        limit=20,
    )
    assert cursor is None
    await db_session.refresh(invoice)
    assert invoice.status == "paid"
    assert invoice.settled_at is not None
    # SQLite drops timezone metadata; the stored provider wall time is still exact.
    assert invoice.paid_at.isoformat() == "2026-10-28T10:00:00"
    assert client.lookup_calls == 1
    assert client.create_calls == 0
