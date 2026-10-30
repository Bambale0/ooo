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
from app.telegram.ui import highlight_pressed, keyboard, show

logger = logging.getLogger(__name__)
ERRORS = {
    "trial_limit_reached": "Оба бесплатных запуска уже использованы. Коммерческие запросы доступны через API.",
    "provider_temporarily_unavailable": "Поставщик временно недоступен. Повторите запрос после восстановления.",
    "required_provider_key_missing": (
        "Откройте заявку в разделе «Заявки» и пришлите ключ поставщика сообщением."
    ),
    "provider_key_invalid": "Не удалось подтвердить ключ у поставщика. Проверьте ключ и пришлите его ещё раз.",
    "provider_key_format_invalid": "Пришлите только ключ поставщика, без пробелов и пояснений.",
    "provider_key_pool_input_invalid": "Пришлите ключи по одному на строке, без пробелов и пояснений.",
    "provider_key_pool_duplicate": "Один из ключей уже есть в пуле или повторяется в сообщении. Ничего не добавлено.",
    "application_not_pending": "Заявка уже рассмотрена. Откройте актуальный список заявок.",
    "negative_balance_delete_forbidden": "Удаление недоступно: сначала погасите задолженность.",
    "confirmation_expired": "Подтверждение устарело. Откройте действие заново.",
    "attachment_too_large": "Файл больше 20 МБ. Отправьте файл меньшего размера.",
    "ticket_closed": "Обращение закрыто. Создайте новое обращение.",
    "payment_not_ready_for_credit": "Провайдер ещё не подтвердил оплату этого счёта.",
    "telegram_id_already_assigned": "Этот Telegram ID уже привязан к другому аккаунту.",
    "payment_provider_temporarily_unavailable": "Платёжный сервис временно недоступен. Проверьте этот счёт позже.",
    "payment_provider_not_configured": "Пополнение временно недоступно. Администратор уже может проверить настройки.",
    "buyer_email_required": "Для оплаты через СБП сначала укажите email для чека.",
    "buyer_email_rejected_by_provider": (
        "Lava.top не принимает этот email для покупки. Email владельца магазина нельзя использовать для оплаты "
        "собственного продукта. Укажите другой email — мы сохраним его для следующих пополнений."
    ),
    "payment_not_cancellable": "Этот счёт нельзя отменить через платёжный API. При необходимости создайте новый.",
    "development_payments_not_configured": "Отдельные чеки за разработку пока не настроены.",
    "development_quote_locked": "Цена уже зафиксирована выставленным счётом и не может быть изменена.",
    "development_quote_required": "Сначала укажите полную цену проекта и email заказчика.",
    "development_advance_not_paid": "Остаток 70% можно выставить только после подтверждённой оплаты аванса.",
    "invalid_development_price": "Введите положительную полную цену проекта целым числом в рублях.",
    "invalid_webhook_url": "Нужен доступный публичный HTTPS-адрес webhook.",
    "consent_required": (
        "Это подтверждение уже неактуально. Откройте /start или раздел «Документы», "
        "ознакомьтесь с текущей версией и нажмите «Принимаю оба документа»."
    ),
}


_TOPUP_BOUNDARY_MARKS = frozenset({"\u200b", "\u2060", "\ufeff"})


def _normalize_topup_text(value: str) -> str:
    """Remove only whitespace and harmless paste marks at the input boundary.

    Never join digit groups or reinterpret signs, fractions, currency suffixes,
    Unicode digits, or format marks between digits as another monetary amount.
    """
    start, end = 0, len(value)
    while start < end and (value[start].isspace() or value[start] in _TOPUP_BOUNDARY_MARKS):
        start += 1
    while end > start and (value[end - 1].isspace() or value[end - 1] in _TOPUP_BOUNDARY_MARKS):
        end -= 1
    return value[start:end]


def _safe_validation_details(exc: Exception) -> dict[str, object]:
    # Do not log str(exc), Pydantic errors/input, traceback source, or locals:
    # they can contain emails, keys, provider payloads, or signed URLs.
    trace = exc.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    return {
        "error_type": type(exc).__name__,
        "error_site_file": trace.tb_frame.f_code.co_filename.rsplit("/", 1)[-1] if trace else None,
        "error_site_line": trace.tb_lineno if trace else None,
        "error_site_function": trace.tb_frame.f_code.co_name if trace else None,
    }


class CabinetSessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not event.from_user:
            return None
        if isinstance(event, CallbackQuery):
            await event.answer()
            await highlight_pressed(event)
        async with SessionLocal() as db:
            dialog_state = None
            input_details = {}
            try:
                dialog = await lock_dialog(db, str(event.from_user.id))
                dialog_state = dialog.state
                if isinstance(event, Message):
                    raw_text = event.text or ""
                    input_details = {
                        "input_chars": len(raw_text),
                        "input_ascii": raw_text.isascii(),
                        "input_digits": raw_text.strip().isdigit(),
                        "input_boundary_marks": any(char in _TOPUP_BOUNDARY_MARKS for char in raw_text),
                    }
                    if dialog_state == "topup" and event.text is not None:
                        normalized = _normalize_topup_text(event.text)
                        if normalized != event.text:
                            # A shallow model copy preserves the bound Bot/session.
                            # The original Update and all other input states stay untouched.
                            event = event.model_copy(update={"text": normalized})
                dialog.telegram_username = (event.from_user.username or "").lower() or None
                data.update(db=db, dialog=dialog)
                result = await handler(event, data)
                await db.commit()
                return result
            except HTTPException as exc:
                await db.rollback()
                detail = str(exc.detail)
                markup = (
                    keyboard(("Изменить email", "payment_email"), back="balance")
                    if detail == "buyer_email_rejected_by_provider"
                    else None
                )
                await show(
                    event,
                    ERRORS.get(detail, "Действие недоступно. Проверьте данные или вернитесь в меню."),
                    markup,
                )
            except (ValueError, ValidationError, InvalidOperation) as exc:
                await db.rollback()
                invalid_amount = dialog_state == "topup" and type(exc) is ValueError and str(exc) == "amount"
                internal_topup_error = dialog_state == "topup" and not invalid_amount
                details = {
                    "dialog_state": dialog_state,
                    "telegram_update_id": getattr(data.get("event_update"), "update_id", None),
                    "error_code": "invalid_topup_amount" if invalid_amount else "validation_processing_error",
                    **input_details,
                    **_safe_validation_details(exc),
                }
                if internal_topup_error:
                    logger.warning("cabinet_input_processing_failed", extra=details)
                    await show(
                        event,
                        "Не удалось подготовить счёт: техническая ошибка. Это не ошибка введённой суммы. "
                        "Вернитесь в раздел «Баланс» и попробуйте снова.",
                        keyboard(back="balance"),
                    )
                else:
                    logger.info("cabinet_input_rejected", extra=details)
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
