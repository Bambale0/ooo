from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder


def keyboard(*buttons: tuple[str, str], back: str | None = "main_menu") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, data in buttons:
        if data.startswith("https://"):
            builder.button(text=label, url=data)
        else:
            builder.button(text=label, callback_data=data)
    if back:
        builder.button(text="← Назад", callback_data=back)
    return builder.adjust(1).as_markup()


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
