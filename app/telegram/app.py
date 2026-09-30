"""Russian Telegram cabinet with PostgreSQL-backed conversations."""

import logging
from decimal import InvalidOperation

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.types import BotCommand, BotCommandScopeDefault, CallbackQuery, Message
from fastapi import HTTPException
from pydantic import ValidationError

from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.payments.crypto_pay import CryptoPayError
from app.telegram.handlers import handle_callback, handle_message
from app.telegram.security import CabinetAccessMiddleware
from app.telegram.service import lock_dialog
from app.telegram.ui import show

logger = logging.getLogger(__name__)
ERRORS = {
    "trial_limit_reached": "Оба бесплатных запуска уже использованы. Коммерческие запросы доступны через API.",
    "provider_temporarily_unavailable": "Поставщик временно недоступен. Повторите запрос после восстановления.",
    "required_provider_key_missing": (
        "Откройте заявку в разделе «Заявки» и пришлите ключ поставщика сообщением."
    ),
    "provider_key_invalid": "Не удалось подтвердить ключ у поставщика. Проверьте ключ и пришлите его ещё раз.",
    "provider_key_format_invalid": "Пришлите только ключ поставщика, без пробелов и пояснений.",
    "application_not_pending": "Заявка уже рассмотрена. Откройте актуальный список заявок.",
    "negative_balance_delete_forbidden": "Удаление недоступно: сначала погасите задолженность.",
    "confirmation_expired": "Подтверждение устарело. Откройте действие заново.",
    "attachment_too_large": "Файл больше 20 МБ. Отправьте файл меньшего размера.",
    "ticket_closed": "Обращение закрыто. Создайте новое обращение.",
    "payment_not_ready_for_credit": "Провайдер ещё не подтвердил оплату этого счёта.",
    "telegram_id_already_assigned": "Этот Telegram ID уже привязан к другому аккаунту.",
    "payment_provider_temporarily_unavailable": "Платёжный сервис временно недоступен. Проверьте этот счёт позже.",
    "invalid_webhook_url": "Нужен доступный публичный HTTPS-адрес webhook.",
}


class CabinetSessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not event.from_user:
            return None
        if isinstance(event, CallbackQuery):
            await event.answer()
        async with SessionLocal() as db:
            try:
                dialog = await lock_dialog(db, str(event.from_user.id))
                dialog.telegram_username = (event.from_user.username or "").lower() or None
                data.update(db=db, dialog=dialog)
                result = await handler(event, data)
                await db.commit()
                return result
            except HTTPException as exc:
                await db.rollback()
                await show(
                    event, ERRORS.get(str(exc.detail), "Действие недоступно. Проверьте данные или вернитесь в меню.")
                )
            except (ValueError, ValidationError, InvalidOperation):
                await db.rollback()
                await show(event, "Не удалось распознать данные. Проверьте формат и повторите ввод.")
            except CryptoPayError:
                await db.rollback()
                await show(event, "Платёжный сервис временно недоступен. Повторите проверку позже.")
            except Exception:
                await db.rollback()
                logger.exception("cabinet_update_failed")
                await show(
                    event, "Не удалось завершить действие. Откройте меню /start и проверьте результат перед повтором."
                )
        return None


def create_bot() -> Bot | None:
    token = get_settings().telegram_bot_token
    return Bot(token=token) if token else None


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    router = Router(name="cabinet")
    for observer in (router.message, router.callback_query):
        observer.outer_middleware(CabinetAccessMiddleware())
        observer.outer_middleware(CabinetSessionMiddleware())

    @router.callback_query()
    async def callback(event: CallbackQuery, db, dialog) -> None:
        await handle_callback(event, db, dialog)

    @router.message()
    async def message(event: Message, db, dialog) -> None:
        await handle_message(event, db, dialog)

    dispatcher.include_router(router)
    return dispatcher


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [BotCommand(command="start", description="Кабинет"), BotCommand(command="cancel", description="Отменить ввод")],
        scope=BotCommandScopeDefault(),
    )
