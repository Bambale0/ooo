from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import ExactNumeric, utc_created_at, uuid_pk


class Generation(Base):
    __tablename__ = "generations"
    __table_args__ = (UniqueConstraint("partner_id", "idempotency_key", name="uq_generations_partner_idem"),)

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_slug: Mapped[str] = mapped_column(String(80), nullable=False)
    mode: Mapped[str] = mapped_column(String(80), nullable=False)
    resolution: Mapped[str] = mapped_column(String(80), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    aspect_ratio: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    partner_price_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    provider_cost_usdt_snapshot: Mapped[Decimal] = mapped_column(
        ExactNumeric(36, 18), nullable=False, default=Decimal("0")
    )
    provider_cost_reserve_usdt: Mapped[Decimal | None] = mapped_column(ExactNumeric(36, 18))
    rub_per_usdt_snapshot: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    provider_cost_reserve_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    request_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)
    actual_charge_rub: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    actual_provider_cost_usdt: Mapped[Decimal | None] = mapped_column(ExactNumeric(36, 18))
    usage_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSON)
    result_url: Mapped[str | None] = mapped_column(Text)
    public_error_code: Mapped[str | None] = mapped_column(String(80))
    webhook_url_snapshot: Mapped[str | None] = mapped_column(Text)
    webhook_secret_encrypted_snapshot: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()

    @property
    def result_urls(self) -> list[str]:
        return list((self.request_payload or {}).get("result_urls", [self.result_url] if self.result_url else []))
