from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (UniqueConstraint("generation_id", name="uq_media_assets_generation"),)

    id: Mapped[str] = uuid_pk()
    generation_id: Mapped[str] = mapped_column(ForeignKey("generations.id"), nullable=False, index=True)
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="video")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="provider_ready", index=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_content_url: Mapped[str] = mapped_column(Text, nullable=False)
    storage_backend: Mapped[str | None] = mapped_column(String(80))
    storage_key: Mapped[str | None] = mapped_column(Text)
    public_url: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()
