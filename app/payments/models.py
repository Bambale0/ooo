from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class PaymentInvoice(Base):
    __tablename__ = "payment_invoices"
    __table_args__ = (
        UniqueConstraint("partner_id", "idempotency_key", name="uq_payment_invoice_partner_idempotency"),
        UniqueConstraint("provider_invoice_id", name="uq_payment_invoice_provider_invoice"),
    )

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="crypto_pay")
    provider_invoice_id: Mapped[int | None] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="creating", index=True)
    requested_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    accepted_assets: Mapped[str] = mapped_column(String(120), nullable=False, default="USDT,TON")
    invoice_url: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), index=True)
    creation_claimed_until: Mapped[object | None] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    paid_asset: Mapped[str | None] = mapped_column(String(20))
    paid_amount: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    paid_fiat_rate: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    paid_usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    was_expired_when_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    credited_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    refunded_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    created_at: Mapped[object] = utc_created_at()


class CryptoPayWebhookEvent(Base):
    __tablename__ = "crypto_pay_webhook_events"

    id: Mapped[str] = uuid_pk()
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    update_type: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_invoice_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    created_at: Mapped[object] = utc_created_at()


class PaymentRefund(Base):
    __tablename__ = "payment_refunds"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_payment_refund_idempotency"),)

    id: Mapped[str] = uuid_pk()
    payment_invoice_id: Mapped[str] = mapped_column(
        ForeignKey("payment_invoices.id"),
        nullable=False,
        index=True,
    )
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = utc_created_at()
