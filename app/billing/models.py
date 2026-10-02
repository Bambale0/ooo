from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import ExactNumeric, utc_created_at, uuid_pk


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_ledger_entries_idempotency_key"),)

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    balance_after_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    generation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()


class CoverageLedgerEntry(Base):
    __tablename__ = "coverage_ledger_entries"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_coverage_ledger_entries_idempotency_key"),)

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    coverage_after_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    generation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()


class WalletSnapshot(Base):
    __tablename__ = "wallet_snapshots"
    __table_args__ = (Index("ix_wallet_snapshots_created_at", "created_at"),)

    id: Mapped[str] = uuid_pk()
    available_usdt: Mapped[Decimal] = mapped_column(ExactNumeric(36, 18), nullable=False)
    created_at: Mapped[object] = utc_created_at()


class FxRateSnapshot(Base):
    __tablename__ = "fx_rate_snapshots"
    id: Mapped[str] = uuid_pk()
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class FxFallbackSetting(Base):
    __tablename__ = "fx_fallback_settings"
    id: Mapped[str] = uuid_pk()
    rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    automatic_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class FinancialIncident(Base):
    __tablename__ = "financial_incidents"
    kind: Mapped[str] = mapped_column(String(200), primary_key=True)
    episode: Mapped[str] = mapped_column(String(36), nullable=False)
    recovered: Mapped[bool] = mapped_column(default=False, nullable=False)
    muted: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_alert_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str] = mapped_column(Text, nullable=False)


class MarginThresholdHistory(Base):
    __tablename__ = "margin_threshold_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    old_value: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    new_value: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[object] = utc_created_at()
