from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class PartnerApplication(Base):
    __tablename__ = "partner_applications"

    id: Mapped[str] = uuid_pk()
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    terms_version: Mapped[str] = mapped_column(String(40), nullable=False, default="2026-09-19")
    privacy_policy_version: Mapped[str] = mapped_column(String(40), nullable=False, default="2026-09-19")
    created_at: Mapped[object] = utc_created_at()


class Partner(Base):
    __tablename__ = "partners"
    __table_args__ = (CheckConstraint("credit_limit_rub >= 0", name="partner_credit_limit_nonnegative"),)

    id: Mapped[str] = uuid_pk()
    application_id: Mapped[str | None] = mapped_column(ForeignKey("partner_applications.id"))
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    payment_email: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    balance_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    credit_limit_rub: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0.00"), server_default="0.00"
    )
    cost_coverage_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    is_admin: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[object] = utc_created_at()

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="partner")

    @property
    def available_balance_rub(self) -> Decimal:
        return Decimal(self.balance_rub) + Decimal(self.credit_limit_rub or 0)


class PartnerCreditLimitHistory(Base):
    __tablename__ = "partner_credit_limit_history"
    __table_args__ = (
        CheckConstraint("old_limit_rub >= 0 AND new_limit_rub >= 0", name="credit_limit_history_nonnegative"),
    )

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    old_limit_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    new_limit_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = utc_created_at()


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    webhook_url: Mapped[str | None] = mapped_column(Text)
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()

    partner: Mapped[Partner] = relationship(back_populates="api_keys")


class ConsentAcceptance(Base):
    __tablename__ = "consent_acceptances"

    id: Mapped[str] = uuid_pk()
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    partner_application_id: Mapped[str | None] = mapped_column(ForeignKey("partner_applications.id"))
    partner_id: Mapped[str | None] = mapped_column(ForeignKey("partners.id"))
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    document_version: Mapped[str] = mapped_column(String(40), nullable=False)
    accepted: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[object] = utc_created_at()


class PartnerAccountStateHistory(Base):
    __tablename__ = "partner_account_state_history"

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()


class ProfitWithdrawal(Base):
    __tablename__ = "profit_withdrawals"

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    override_reason: Mapped[str | None] = mapped_column(Text)
    correction_for_id: Mapped[str | None] = mapped_column(ForeignKey("profit_withdrawals.id"))
    created_at: Mapped[object] = utc_created_at()
