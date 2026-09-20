from decimal import Decimal

from sqlalchemy import Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class Model(Base):
    __tablename__ = "models"

    id: Mapped[str] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    modality: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    has_provider_integration: Mapped[bool] = mapped_column(default=False, nullable=False)
    has_public_docs: Mapped[bool] = mapped_column(default=False, nullable=False)
    has_successful_smoke: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[object] = utc_created_at()


class PartnerPrice(Base):
    __tablename__ = "partner_prices"
    __table_args__ = (UniqueConstraint("model_id", "mode", "resolution", name="uq_partner_prices_variant"),)

    id: Mapped[str] = uuid_pk()
    model_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    resolution: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    price_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    provider_cost_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    billing_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="generation")
    created_at: Mapped[object] = utc_created_at()


class PartnerPriceHistory(Base):
    __tablename__ = "partner_price_history"

    id: Mapped[str] = uuid_pk()
    model_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    resolution: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    old_price_rub: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    new_price_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    old_provider_cost_usdt: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    new_provider_cost_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    billing_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="generation")
    rub_per_usdt_snapshot: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    created_at: Mapped[object] = utc_created_at()
