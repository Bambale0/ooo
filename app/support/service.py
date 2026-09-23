"""Tenant-scoped support with bounded attachments in operator-owned storage."""

import asyncio
import io
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select

from app.accounts.models import Partner
from app.infrastructure.config import get_settings
from app.support.models import SupportAttachment, SupportMessage, SupportTicket
from app.telegram.service import is_admin, notify

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class BoundedBuffer(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(422, "attachment_too_large")
        return super().write(data)


async def get_ticket(db, ticket_id: str, telegram_id: str) -> SupportTicket:
    ticket = await db.get(SupportTicket, ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(404, "ticket_not_found")
    partner = await db.get(Partner, ticket.partner_id)
    if not is_admin(telegram_id) and (not partner or partner.telegram_id != telegram_id or partner.status != "active"):
        raise HTTPException(404, "ticket_not_found")
    return ticket


async def append_message(db, ticket: SupportTicket, message, *, telegram_id: str) -> None:
    if ticket.status == "closed":
        raise HTTPException(409, "ticket_closed")
    message_id = str(uuid5(NAMESPACE_URL, f"telegram-support:{telegram_id}:{message.message_id}"))
    if await db.get(SupportMessage, message_id):
        return
    attachment = message.document or (message.photo[-1] if message.photo else None)
    text_value = (message.text or message.caption or "").strip()
    if not text_value and not attachment:
        raise HTTPException(422, "support_message_required")
    path = None
    size = 0
    if attachment:
        if (attachment.file_size or 0) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(422, "attachment_too_large")
        buffer = BoundedBuffer()
        await message.bot.download(attachment, destination=buffer)
        content = buffer.getvalue()
        size = len(content)
        root = await asyncio.to_thread(Path(get_settings().support_storage_dir).resolve)
        path = root / f"{message_id}.bin"

        def persist() -> None:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = root / f"{message_id}.tmp"
            temporary.write_bytes(content)
            temporary.chmod(0o600)
            temporary.replace(path)

        await asyncio.to_thread(persist)
    admin = is_admin(telegram_id)
    row = SupportMessage(
        id=message_id,
        ticket_id=ticket.id,
        sender_type="admin" if admin else "partner",
        sender_telegram_id=telegram_id,
        text=text_value,
        attachment_path=str(path) if path else None,
    )
    db.add(row)
    await db.flush()
    if path:
        db.add(
            SupportAttachment(
                message_id=row.id,
                file_name=(getattr(attachment, "file_name", None) or "image.jpg")[:255],
                file_size_bytes=size,
                storage_path=str(path),
            )
        )
    partner = await db.get(Partner, ticket.partner_id)
    recipient = partner.telegram_id if admin else get_settings().admin_telegram_id
    await notify(
        db,
        recipient,
        f"Новое сообщение в обращении {ticket.id}. Откройте раздел «Поддержка».\n{text_value[:1800]}",
        f"support-message:{row.id}",
    )


async def attachment_for(db, attachment_id: str, telegram_id: str) -> SupportAttachment:
    item = await db.get(SupportAttachment, attachment_id)
    if item is None:
        raise HTTPException(404, "attachment_not_found")
    message = await db.get(SupportMessage, item.message_id)
    await get_ticket(db, message.ticket_id, telegram_id)
    root = await asyncio.to_thread(Path(get_settings().support_storage_dir).resolve)
    resolved = await asyncio.to_thread(Path(item.storage_path).resolve)
    if not resolved.is_relative_to(root):
        raise HTTPException(404, "attachment_not_found")
    return item


async def ticket_messages(db, ticket_id: str, offset: int = 0):
    return (
        (
            await db.execute(
                select(SupportMessage)
                .where(SupportMessage.ticket_id == ticket_id)
                .order_by(SupportMessage.created_at.desc(), SupportMessage.id)
                .offset(offset)
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
