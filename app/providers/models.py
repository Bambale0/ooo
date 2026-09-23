from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"

    id: Mapped[str] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text)
    partner_application_id: Mapped[str | None] = mapped_column(
        ForeignKey("partner_applications.id"),
        index=True,
    )
    partner_id: Mapped[str | None] = mapped_column(
        ForeignKey("partners.id"),
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[object] = utc_created_at()


class ProviderModelCapability(Base):
    __tablename__ = "provider_model_capabilities"
    __table_args__ = (UniqueConstraint("provider", "model_id", "mode", "resolution", name="uq_provider_capability"),)

    id: Mapped[str] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(80), nullable=False)
    resolution: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[object] = utc_created_at()


class ProviderAttempt(Base):
    __tablename__ = "provider_attempts"
    __table_args__ = (UniqueConstraint("generation_id", "provider", name="uq_provider_attempt_generation_provider"),)

    id: Mapped[str] = uuid_pk()
    generation_id: Mapped[str] = mapped_column(ForeignKey("generations.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("provider_credentials.id"), index=True)
    provider_task_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    public_error_code: Mapped[str | None] = mapped_column(String(80))
    raw_error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), index=True)
    next_poll_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), index=True)
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()


class ProviderCircuit(Base):
    __tablename__ = "provider_circuits"
    provider: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="closed")
    episode: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    healthy_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    real_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_flight: Mapped[str | None] = mapped_column(String(36))
    probe_started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    recovered_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class ProviderOutcome(Base):
    __tablename__ = "provider_outcomes"
    generation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
