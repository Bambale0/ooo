from decimal import Decimal

from sqlalchemy import JSON, DateTime, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class Model(Base):
    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("slug", name="uq_models_slug"),)

    id: Mapped[str] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    modality: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    has_provider_integration: Mapped[bool] = mapped_column(default=False, nullable=False)
    has_public_docs: Mapped[bool] = mapped_column(default=False, nullable=False)
    has_successful_smoke: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[object] = utc_created_at()


class PartnerModelGrant(Base):
    """Per-partner access to a model that is deliberately absent from the public catalog.

    A `restricted` model is never listed in /models, /pricing or the public docs.
    It becomes usable only for partners holding a non-revoked row here, so access
    is granted explicitly per partner instead of being published to everyone.
    """

    __tablename__ = "partner_model_grants"
    __table_args__ = (UniqueConstraint("model_id", "partner_id", name="uq_partner_model_grant"),)

    id: Mapped[str] = uuid_pk()
    model_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    partner_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    granted_by: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[object] = utc_created_at()


class PartnerPrice(Base):
    __tablename__ = "partner_prices"
    __table_args__ = (
        UniqueConstraint("model_id", "mode", "resolution", "partner_id", name="uq_partner_prices_variant"),
    )

    id: Mapped[str] = uuid_pk()
    model_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    partner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    resolution: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    price_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    provider_cost_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    billing_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="generation")
    created_at: Mapped[object] = utc_created_at()


class PartnerPriceHistory(Base):
    fx_snapshot: Mapped[dict | None] = mapped_column(JSON)
    __tablename__ = "partner_price_history"

    id: Mapped[str] = uuid_pk()
    model_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    partner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    resolution: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    old_price_rub: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    new_price_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    old_provider_cost_usdt: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    new_provider_cost_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    billing_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="generation")
    rub_per_usdt_snapshot: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    created_at: Mapped[object] = utc_created_at()
