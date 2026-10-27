from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class ReferralProfile(Base):
    __tablename__ = "referral_profiles"
    __table_args__ = (UniqueConstraint("partner_id", name="uq_referral_profiles_partner_id"),)

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    created_at: Mapped[object] = utc_created_at()


class ReferralAttribution(Base):
    """Immutable first-touch attribution keyed by a Telegram identity."""

    __tablename__ = "referral_attributions"
    __table_args__ = (
        UniqueConstraint("referred_partner_id", name="uq_referral_attributions_referred_partner_id"),
        UniqueConstraint("referred_telegram_id", name="uq_referral_attributions_referred_telegram_id"),
    )

    id: Mapped[str] = uuid_pk()
    referrer_partner_id: Mapped[str | None] = mapped_column(ForeignKey("partners.id"), index=True)
    referred_partner_id: Mapped[str | None] = mapped_column(ForeignKey("partners.id"), index=True)
    referred_telegram_id: Mapped[str] = mapped_column(String(64), nullable=False)
    referral_code: Mapped[str | None] = mapped_column(String(24))
    created_at: Mapped[object] = utc_created_at()


class ReferralCommission(Base):
    """Append-only RUB referral earnings and refund reversals."""

    __tablename__ = "referral_commissions"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_referral_commissions_event_key"),
        CheckConstraint("base_amount_rub > 0", name="referral_commission_positive_base"),
        CheckConstraint("rate > 0 AND rate <= 1", name="referral_commission_valid_rate"),
        CheckConstraint("amount_rub <> 0", name="referral_commission_nonzero_amount"),
    )

    id: Mapped[str] = uuid_pk()
    referrer_partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    referred_telegram_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(180), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    base_amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 6), nullable=False)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[object] = utc_created_at()
