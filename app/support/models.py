"""
Support ticket system for partner support within Telegram.
"""

from datetime import datetime
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    closed_at: Mapped[object | None] = mapped_column(default=None)
    created_at: Mapped[object] = utc_created_at()


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[str] = uuid_pk()
    ticket_id: Mapped[str] = mapped_column(ForeignKey("support_tickets.id"), nullable=False, index=True)
    sender_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "partner" or "admin"
    sender_telegram_id: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()


class SupportAttachment(Base):
    __tablename__ = "support_attachments"

    id: Mapped[str] = uuid_pk()
    message_id: Mapped[str] = mapped_column(ForeignKey("support_messages.id"), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = utc_created_at()