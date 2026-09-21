from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("generation_id", name="uq_webhook_events_generation"),)

    id: Mapped[str] = uuid_pk()
    generation_id: Mapped[str] = mapped_column(ForeignKey("generations.id"), nullable=False, index=True)
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    webhook_url: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), index=True)
    claimed_until: Mapped[object | None] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[object] = utc_created_at()


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("event_id", "attempt", name="uq_webhook_delivery_attempt"),)

    id: Mapped[str] = uuid_pk()
    event_id: Mapped[str] = mapped_column(ForeignKey("webhook_events.id"), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()
