"""
Нейроныч SaaS — Telegram bot (partner cabinet + admin panel)
"""

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.accounts.models import Partner
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now

_MAIN_MENU_TEXT = (
    "🤖 *Нейроныч SaaS — Кабинет*\n\n"
    "Выберите раздел:"
)

_ADMIN_MENU_TEXT = (
    "🔧 *Панель администратора*\n\n"
    "Выберите раздел:"
)


def _partner_kb(partner: Partner) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💰 Баланс", callback_data="balance"),
        InlineKeyboardButton(text="📊 История", callback_data="history"),
    )
    builder.row(
        InlineKeyboardButton(text="🔑 API-ключи", callback_data="api_keys"),
        InlineKeyboardButton(text="🔍 Поиск", callback_data="search_gen"),
    )
    builder.row(
        InlineKeyboardButton(text="📄 Документация", callback_data="docs"),
        InlineKeyboardButton(text="❓ Поддержка", callback_data="support"),
    )
    if partner.is_admin:
        builder.row(InlineKeyboardButton(text="🔧 Админка", callback_data="admin_menu"))
    return builder.as_markup()


def _admin_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Заявки", callback_data="admin_applications"),
        InlineKeyboardButton(text="👥 Партнёры", callback_data="admin_partners"),
    )
    builder.row(
        InlineKeyboardButton(text="💳 Биллинг", callback_data="admin_billing"),
        InlineKeyboardButton(text="🏥 Health", callback_data="admin_health"),
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"),
    )
    return builder.as_markup()


def create_bot() -> Bot | None:
    token = get_settings().telegram_bot_token
    if not token:
        return None
    return Bot(token=token)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message) -> None:
        await message.answer(
            "👋 Добро пожаловать в *Нейроныч SaaS*!\n\n"
            "Используйте кнопки ниже для навигации.",
            reply_markup=InlineKeyboardBuilder()
            .button(text="🔑 Войти в кабинет", callback_data="main_menu")
            .as_markup(),
        )

    @dp.callback_query(lambda c: c.data == "main_menu")
    async def main_menu(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text(
            _MAIN_MENU_TEXT,
            reply_markup=_partner_kb(Partner(telegram_id=str(callback.from_user.id))),
        )
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "admin_menu")
    async def admin_menu(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text(
            _ADMIN_MENU_TEXT,
            reply_markup=_admin_kb(),
        )
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "balance")
    async def show_balance(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text(
            "💰 *Баланс*\n\nВаш баланс: *0.00 ₽*\nДоступно для покрытия: *0.00 ₽*",
            reply_markup=InlineKeyboardBuilder()
            .button(text="🔙 Назад", callback_data="main_menu")
            .as_markup(),
        )
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "history")
    async def show_history(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text(
            "📊 *История*\n\nЗдесь будет отображаться история операций.",
            reply_markup=InlineKeyboardBuilder()
            .button(text="🔙 Назад", callback_data="main_menu")
            .as_markup(),
        )
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "api_keys")
    async def show_api_keys(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text(
            "🔑 *API-ключи*\n\nЗдесь будет отображаться список API-ключей.",
            reply_markup=InlineKeyboardBuilder()
            .button(text="🔙 Назад", callback_data="main_menu")
            .as_markup(),
        )
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "docs")
    async def show_docs(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text(
            "📄 *Документация*\n\n"
            "OpenAPI: https://api.нейроныч.online/docs\n"
            "Документация: https://docs.нейроныч.online",
            reply_markup=InlineKeyboardBuilder()
            .button(text="🔙 Назад", callback_data="main_menu")
            .as_markup(),
        )
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "support")
    async def show_support(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text(
            "❓ *Поддержка*\n\n"
            "Для связи с поддержкой напишите: @support\n"
            "Или создайте тикет через команду /ticket",
            reply_markup=InlineKeyboardBuilder()
            .button(text="🔙 Назад", callback_data="main_menu")
            .as_markup(),
        )
        await callback.answer()

    return dp


async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Главное меню"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
