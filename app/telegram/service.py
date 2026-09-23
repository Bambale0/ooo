"""Cabinet persistence. Each mutation is actor-bound and replay safe."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, text

from app.accounts.models import Partner
from app.infrastructure.config import get_settings
from app.telegram.models import BotAction, BotDialog, BotNotification


def is_admin(telegram_id: str) -> bool:
    return telegram_id == get_settings().admin_telegram_id


async def lock_dialog(db, telegram_id: str) -> BotDialog:
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "bot:" + telegram_id})
    dialog = await db.get(BotDialog, telegram_id, with_for_update=True)
    if dialog is None:
        dialog = BotDialog(telegram_id=telegram_id, state="menu", data={})
        db.add(dialog)
        await db.flush()
    return dialog


async def partner_for(db, telegram_id: str) -> Partner | None:
    return (
        await db.execute(select(Partner).where(Partner.telegram_id == telegram_id, Partner.status == "active"))
    ).scalar_one_or_none()


async def new_action(db, telegram_id: str, kind: str, payload: dict) -> BotAction:
    action = BotAction(telegram_id=telegram_id, kind=kind, payload=payload)
    db.add(action)
    await db.flush()
    return action


async def pending_action(db, action_id: str, telegram_id: str) -> BotAction:
    action = await db.get(BotAction, action_id, with_for_update=True)
    if action is None or action.telegram_id != telegram_id:
        raise HTTPException(404, "action_not_found")
    if action.kind.startswith("admin_") and not is_admin(telegram_id):
        raise HTTPException(403, "admin_required")
    created = action.created_at.replace(tzinfo=UTC) if action.created_at.tzinfo is None else action.created_at
    if action.status == "pending" and created < datetime.now(UTC) - timedelta(minutes=15):
        raise HTTPException(409, "confirmation_expired")
    return action


async def notify(db, telegram_id: str | None, text_value: str, key: str) -> None:
    if telegram_id and telegram_id.isdigit():
        existing = (
            await db.execute(select(BotNotification.id).where(BotNotification.dedupe_key == key))
        ).scalar_one_or_none()
        if not existing:
            db.add(BotNotification(telegram_id=telegram_id, text=text_value, dedupe_key=key))
