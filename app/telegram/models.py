"""Durable cabinet conversations, confirmations and notification outbox."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class BotDialog(Base):
    __tablename__ = "bot_dialogs"
    telegram_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Last observed Telegram username, normalized for exact case-insensitive search.
    telegram_username: Mapped[str | None] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(80), default="menu", nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class BotAction(Base):
    __tablename__ = "bot_actions"
    id: Mapped[str] = uuid_pk()
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    created_at: Mapped[object] = utc_created_at()


class BotNotification(Base):
    __tablename__ = "bot_notifications"
    id: Mapped[str] = uuid_pk()
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[object] = utc_created_at()


class TrialEntitlement(Base):
    __tablename__ = "trial_entitlements"
    # Intentionally independent of Partner: deletion/re-registration never resets it.
    telegram_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
