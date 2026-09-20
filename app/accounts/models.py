from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text
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

    id: Mapped[str] = uuid_pk()
    application_id: Mapped[str | None] = mapped_column(ForeignKey("partner_applications.id"))
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    balance_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    created_at: Mapped[object] = utc_created_at()

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="partner")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
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
