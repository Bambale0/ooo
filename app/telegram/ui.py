import asyncio
import logging

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logger = logging.getLogger(__name__)


def keyboard(*buttons: tuple[str, str], back: str | None = "main_menu") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, data in buttons:
        if data.startswith("https://"):
            builder.button(text=label, url=data, style="primary")
        else:
            builder.button(text=label, callback_data=data, style="primary")
    if back:
        builder.button(text="← Назад", callback_data=back, style="primary")
    return builder.adjust(1).as_markup()


async def highlight_pressed(event: CallbackQuery) -> None:
    """Best-effort feedback; a Telegram edit failure must not cancel the action."""
    if not isinstance(event.message, Message) or not event.message.reply_markup or not event.data:
        return
    try:
        # Incoming markup is bound to a live Bot. Copy only Telegram fields,
        # never its private HTTP session / event loop state.
        markup = InlineKeyboardMarkup.model_validate(event.message.reply_markup.model_dump())
        matched = False
        for row in markup.inline_keyboard:
            for button in row:
                pressed = button.callback_data == event.data
                button.style = "success" if pressed else "primary"
                matched |= pressed
        if matched:
            async with asyncio.timeout(1.5):
                await event.message.edit_reply_markup(reply_markup=markup)
    except (TelegramAPIError, TimeoutError):
        pass
    except Exception as exc:
        # Cosmetic feedback must not suppress the requested action. Avoid
        # logging the callback, message content, or raw transport exception.
        logger.warning("cabinet_feedback_failed", extra={"error_type": type(exc).__name__})


async def show(event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup or keyboard(), parse_mode=None)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message:
                raise
    else:
        await event.answer(text, reply_markup=markup or keyboard(), parse_mode=None)


STATUS = {
    "queued": "В очереди",
    "submitting": "Отправляется",
    "processing": "Выполняется",
    "completed": "Готово",
    "failed": "Ошибка",
    "cancelled": "Отменено",
    "reconciliation_required": "Проверяется оператором",
    "active": "Ожидает оплаты",
    "creating": "Счёт создаётся",
    "creation_unknown": "Создание счёта проверяется оператором",
    "paid_waiting_credit": "Оплачен, ожидает зачисления",
    "credited": "Зачислен",
    "expired": "Срок истёк",
    "refunded": "Возвращён",
    "partially_refunded": "Частичный возврат",
    "open": "Открыто",
    "in_progress": "В работе",
    "closed": "Закрыто",
}


def status_label(value: str) -> str:
    return STATUS.get(value, "Проверяется")
