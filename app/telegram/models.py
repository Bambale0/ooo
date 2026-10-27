"""Durable cabinet conversations, confirmations and notification outbox."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class BotDialog(Base):
    __tablename__ = "bot_dialogs"
    telegram_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Last observed Telegram username, normalized for exact case-insensitive search.
    telegram_username: Mapped[str | None] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(80), default="menu", nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class BotAction(Base):
    __tablename__ = "bot_actions"
    id: Mapped[str] = uuid_pk()
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    created_at: Mapped[object] = utc_created_at()


class BotNotification(Base):
    __tablename__ = "bot_notifications"
    id: Mapped[str] = uuid_pk()
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[object] = utc_created_at()


class DevelopmentRequest(Base):
    """Durable custom-bot inquiry, independent of API-partner registration."""

    __tablename__ = "development_requests"
    __table_args__ = (
        CheckConstraint(
            "quoted_price_rub IS NULL OR quoted_price_rub > 0",
            name="development_request_positive_quote",
        ),
    )

    id: Mapped[str] = uuid_pk()
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(32))
    answers: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False, index=True)
    quoted_price_rub: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    buyer_email: Mapped[str | None] = mapped_column(String(320))
    quoted_by: Mapped[str | None] = mapped_column(String(64))
    quoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[object] = utc_created_at()


class DevelopmentInvoice(Base):
    """A separate development installment that never credits API balance."""

    __tablename__ = "development_invoices"
    __table_args__ = (
        UniqueConstraint("development_request_id", "installment", name="uq_development_invoice_installment"),
        UniqueConstraint("provider_reference", name="uq_development_invoice_provider_reference"),
        UniqueConstraint("idempotency_key", name="uq_development_invoice_idempotency"),
        CheckConstraint("requested_rub > 0", name="development_invoice_positive_amount"),
        CheckConstraint("installment IN ('advance', 'final')", name="development_invoice_installment_kind"),
    )

    id: Mapped[str] = uuid_pk()
    development_request_id: Mapped[str] = mapped_column(
        ForeignKey("development_requests.id"), nullable=False, index=True
    )
    installment: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="lava_top")
    provider_reference: Mapped[str | None] = mapped_column(String(80), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="creating", index=True)
    total_price_snapshot_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    requested_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    buyer_email: Mapped[str] = mapped_column(String(320), nullable=False)
    invoice_url: Mapped[str | None] = mapped_column(Text)
    creation_claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_snapshot: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[object] = utc_created_at()


class TrialEntitlement(Base):
    __tablename__ = "trial_entitlements"
    # Intentionally independent of Partner: deletion/re-registration never resets it.
    telegram_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
