"""At-least-once notification delivery; domain mutations are never retried here."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import or_, select

from app.infrastructure.database import SessionLocal
from app.telegram.models import BotNotification

logger = logging.getLogger(__name__)


async def deliver_notifications(bot) -> int:
    sent = 0
    async with SessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(BotNotification)
                    .where(
                        BotNotification.sent_at.is_(None),
                        or_(
                            BotNotification.next_attempt_at.is_(None),
                            BotNotification.next_attempt_at <= datetime.now(UTC),
                        ),
                    )
                    .order_by(BotNotification.created_at)
                    .limit(20)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.attempts += 1
            try:
                await bot.send_message(int(row.telegram_id), row.text, parse_mode=None)
            except TelegramAPIError:
                row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(3600, 15 * 2 ** min(row.attempts, 8)))
            else:
                row.sent_at = datetime.now(UTC)
                sent += 1
        await db.commit()
    return sent


async def notification_loop(bot) -> None:
    last_financial_tick = None
    while True:
        try:
            if last_financial_tick is None or datetime.now(UTC) - last_financial_tick >= timedelta(seconds=60):
                from app.billing.incidents import financial_tick

                async with SessionLocal() as db:
                    await financial_tick(db)
                    await db.commit()
                last_financial_tick = datetime.now(UTC)
        except Exception:
            logger.exception("financial_tick_failed")
        try:
            await deliver_notifications(bot)
        except Exception:
            logger.exception("notification_iteration_failed")
        await asyncio.sleep(3)
