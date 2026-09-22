"""
Нейроныч SaaS — Telegram bot (partner cabinet + admin panel)
"""

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeDefault, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.accounts.models import ApiKey, Partner, PartnerApplication
from app.billing.models import LedgerEntry
from app.billing.safe_to_withdraw import calculate_safe_to_withdraw
from app.infrastructure.config import get_settings
from app.infrastructure.database import get_db_session

_M = "Neyronych SaaS - Cabinet\n\nSelect:"
_A = "Admin Panel\n\nSelect:"


def _kb(partner: Partner):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Balance", callback_data="balance"),
        InlineKeyboardButton(text="History", callback_data="history:0"),
    )
    builder.row(
        InlineKeyboardButton(text="API Keys", callback_data="api_keys:0"),
        InlineKeyboardButton(text="Search", callback_data="search_prompt"),
    )
    builder.row(
        InlineKeyboardButton(text="Docs", callback_data="docs"),
        InlineKeyboardButton(text="Support", callback_data="support"),
    )
    if getattr(partner, "is_admin", False):
        builder.row(InlineKeyboardButton(text="Admin", callback_data="admin_menu"))
    return builder.as_markup()


def _akb():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Apps", callback_data="admin_apps:0"),
        InlineKeyboardButton(text="STW", callback_data="admin_stw"),
    )
    builder.row(
        InlineKeyboardButton(text="Health", callback_data="admin_health"),
        InlineKeyboardButton(text="Back", callback_data="main_menu"),
    )
    return builder.as_markup()


def _back():
    return InlineKeyboardBuilder().button(text="Back", callback_data="main_menu").as_markup()


def _pag(prefix: str, page: int, total: int):
    builder = InlineKeyboardBuilder()
    if page > 0:
        builder.button(text="<", callback_data=f"{prefix}:{page - 1}")
    if page < total - 1:
        builder.button(text=">", callback_data=f"{prefix}:{page + 1}")
    builder.button(text="Back", callback_data="main_menu")
    return builder.adjust(2).as_markup()


def create_bot():
    token = get_settings().telegram_bot_token
    return Bot(token=token) if token else None


async def _db():
    async for session in get_db_session():
        return session
    raise RuntimeError("database_session_unavailable")


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()

    @dispatcher.message(Command("start"))
    async def start(message: types.Message) -> None:
        keyboard = InlineKeyboardBuilder().button(text="Enter", callback_data="main_menu").as_markup()
        await message.answer("Welcome!", reply_markup=keyboard)

    @dispatcher.callback_query(lambda callback: callback.data == "main_menu")
    async def main_menu(callback: types.CallbackQuery) -> None:
        partner = Partner(telegram_id=str(callback.from_user.id))
        await callback.message.edit_text(_M, reply_markup=_kb(partner))
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data == "admin_menu")
    async def admin_menu(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text(_A, reply_markup=_akb())
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data == "balance")
    async def balance(callback: types.CallbackQuery) -> None:
        session = await _db()
        try:
            result = await session.execute(
                select(Partner).where(Partner.telegram_id == str(callback.from_user.id))
            )
            partner = result.scalar_one_or_none()
            text = (
                f"Balance: {partner.balance_rub} RUB\nCoverage: {partner.cost_coverage_rub} RUB"
                if partner
                else "Not found"
            )
            await callback.message.edit_text(text, reply_markup=_back())
        finally:
            await session.close()
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data.startswith("history:"))
    async def history(callback: types.CallbackQuery) -> None:
        page = int(callback.data.split(":")[1]) if ":" in callback.data else 0
        session = await _db()
        try:
            result = await session.execute(
                select(Partner).where(Partner.telegram_id == str(callback.from_user.id))
            )
            partner = result.scalar_one_or_none()
            if not partner:
                await callback.answer()
                return
            rows_result = await session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.partner_id == partner.id)
                .order_by(LedgerEntry.created_at.desc())
                .offset(page * 10)
                .limit(10)
            )
            rows = rows_result.scalars().all()
            lines = [f"History (p.{page + 1})"]
            lines.extend(f"{entry.operation_type}: {entry.amount_rub}RUB" for entry in rows)
            if not rows:
                lines.append("No records")
            await callback.message.edit_text(
                "\n".join(lines),
                reply_markup=_pag("history", page, 100),
            )
        finally:
            await session.close()
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data.startswith("api_keys:"))
    async def api_keys(callback: types.CallbackQuery) -> None:
        session = await _db()
        try:
            result = await session.execute(
                select(Partner).where(Partner.telegram_id == str(callback.from_user.id))
            )
            partner = result.scalar_one_or_none()
            if not partner:
                await callback.answer()
                return
            keys_result = await session.execute(select(ApiKey).where(ApiKey.partner_id == partner.id))
            keys = keys_result.scalars().all()
            lines = ["API Keys"]
            lines.extend(f"{'A' if key.is_active else 'I'} {key.key_prefix}..." for key in keys)
            if not keys:
                lines.append("No keys")
            await callback.message.edit_text("\n".join(lines), reply_markup=_back())
        finally:
            await session.close()
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data == "search_prompt")
    async def search_prompt(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text("Enter UUID:", reply_markup=_back())
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data == "docs")
    async def docs(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text("https://api.neyronych.online/docs", reply_markup=_back())
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data == "support")
    async def support(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text("@support", reply_markup=_back())
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data == "admin_stw")
    async def safe_to_withdraw(callback: types.CallbackQuery) -> None:
        session = await _db()
        try:
            data = await calculate_safe_to_withdraw(session)
            await callback.message.edit_text(
                f"STW: {data['safe_to_withdraw_usdt']:.2f} USDT",
                reply_markup=_back(),
            )
        finally:
            await session.close()
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data == "admin_health")
    async def health(callback: types.CallbackQuery) -> None:
        await callback.message.edit_text("OK", reply_markup=_back())
        await callback.answer()

    @dispatcher.callback_query(lambda callback: callback.data.startswith("admin_apps:"))
    async def applications(callback: types.CallbackQuery) -> None:
        session = await _db()
        try:
            result = await session.execute(
                select(PartnerApplication)
                .where(PartnerApplication.status == "pending")
                .limit(10)
            )
            rows = result.scalars().all()
            lines = ["Applications"]
            lines.extend(application.company_name for application in rows)
            if not rows:
                lines.append("None")
            await callback.message.edit_text("\n".join(lines), reply_markup=_back())
        finally:
            await session.close()
        await callback.answer()

    return dispatcher


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [BotCommand(command="start", description="Main menu")],
        scope=BotCommandScopeDefault(),
    )
