"""
Нейроныч SaaS — Telegram bot (partner cabinet + admin panel)
"""
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import ApiKey, Partner
from app.billing.models import LedgerEntry
from app.billing.safe_to_withdraw import calculate_safe_to_withdraw
from app.infrastructure.config import get_settings
from app.infrastructure.database import get_db_session

_M = "Neyronych SaaS - Cabinet\n\nSelect:"
_A = "Admin Panel\n\nSelect:"

def _kb(p):
    from aiogram.utils.keyboard import InlineKeyboardBuilder as B
    b = B()
    b.row(InlineKeyboardButton(text="Balance", callback_data="balance"),
          InlineKeyboardButton(text="History", callback_data="history:0"))
    b.row(InlineKeyboardButton(text="API Keys", callback_data="api_keys:0"),
          InlineKeyboardButton(text="Search", callback_data="search_prompt"))
    b.row(InlineKeyboardButton(text="Docs", callback_data="docs"),
          InlineKeyboardButton(text="Support", callback_data="support"))
    if getattr(p, "is_admin", False):
        b.row(InlineKeyboardButton(text="Admin", callback_data="admin_menu"))
    return b.as_markup()

def _akb():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="Apps", callback_data="admin_apps:0"),
          InlineKeyboardButton(text="STW", callback_data="admin_stw"))
    b.row(InlineKeyboardButton(text="Health", callback_data="admin_health"),
          InlineKeyboardButton(text="Back", callback_data="main_menu"))
    return b.as_markup()

def _back():
    return InlineKeyboardBuilder().button(text="Back", callback_data="main_menu").as_markup()

def _pag(prefix, page, total):
    b = InlineKeyboardBuilder()
    if page > 0: b.button(text="<", callback_data=f"{prefix}:{page-1}")
    if page < total-1: b.button(text=">", callback_data=f"{prefix}:{page+1}")
    b.button(text="Back", callback_data="main_menu")
    return b.adjust(2).as_markup()

def create_bot():
    t = get_settings().telegram_bot_token
    return Bot(token=t) if t else None

async def _db():
    async for s in get_db_session():
        return s
def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    @dp.message(Command("start"))
    async def start(m: types.Message) -> None:
        await m.answer("Welcome!", reply_markup=InlineKeyboardBuilder()
                       .button(text="Enter", callback_data="main_menu").as_markup())
    @dp.callback_query(lambda c: c.data == "main_menu")
    async def main_menu(cb: types.CallbackQuery) -> None:
        await cb.message.edit_text(_M, reply_markup=_kb(Partner(telegram_id=str(cb.from_user.id))))
        await cb.answer()
    @dp.callback_query(lambda c: c.data == "admin_menu")
    async def admin_menu(cb: types.CallbackQuery) -> None:
        await cb.message.edit_text(_A, reply_markup=_akb())
        await cb.answer()
    @dp.callback_query(lambda c: c.data == "balance")
    async def balance(cb: types.CallbackQuery) -> None:
        s = await _db()
        try:
            r = await s.execute(select(Partner).where(Partner.telegram_id == str(cb.from_user.id)))
            p = r.scalar_one_or_none()
            t = f"Balance: {p.balance_rub} RUB\nCoverage: {p.cost_coverage_rub} RUB" if p else "Not found"
            await cb.message.edit_text(t, reply_markup=_back())
        finally: await s.close()
        await cb.answer()
    @dp.callback_query(lambda c: c.data.startswith("history:"))
    async def history(cb: types.CallbackQuery) -> None:
        page = int(cb.data.split(":")[1]) if ":" in cb.data else 0
        s = await _db()
        try:
            p = (await s.execute(select(Partner).where(Partner.telegram_id == str(cb.from_user.id)))).scalar_one_or_none()
            if not p: return
            rows = (await s.execute(select(LedgerEntry).where(LedgerEntry.partner_id == p.id)
                .order_by(LedgerEntry.created_at.desc()).offset(page*10).limit(10))).scalars().all()
            lines = [f"History (p.{page+1})"]
            lines.extend(f"{e.operation_type}: {e.amount_rub}RUB" for e in rows)
            if not rows: lines.append("No records")
            await cb.message.edit_text("\n".join(lines), reply_markup=_pag("history", page, 100))
        finally: await s.close()
        await cb.answer()
    @dp.callback_query(lambda c: c.data.startswith("api_keys:"))
    async def api_keys(cb: types.CallbackQuery) -> None:
        s = await _db()
        try:
            p = (await s.execute(select(Partner).where(Partner.telegram_id == str(cb.from_user.id)))).scalar_one_or_none()
            if not p: return
            ks = (await s.execute(select(ApiKey).where(ApiKey.partner_id == p.id))).scalars().all()
            lines = ["API Keys"]
            lines.extend(f"{'A' if k.is_active else 'I'} {k.key_prefix}..." for k in ks)
            if not ks: lines.append("No keys")
            await cb.message.edit_text("\n".join(lines), reply_markup=_back())
        finally: await s.close()
        await cb.answer()
    @dp.callback_query(lambda c: c.data == "search_prompt")
    async def sp(cb: types.CallbackQuery) -> None:
        await cb.message.edit_text("Enter UUID:", reply_markup=_back())
        await cb.answer()
    @dp.callback_query(lambda c: c.data == "docs")
    async def docs(cb: types.CallbackQuery) -> None:
        await cb.message.edit_text("https://api.neyronych.online/docs", reply_markup=_back())
        await cb.answer()
    @dp.callback_query(lambda c: c.data == "support")
    async def support(cb: types.CallbackQuery) -> None:
        await cb.message.edit_text("@support", reply_markup=_back())
        await cb.answer()
    @dp.callback_query(lambda c: c.data == "admin_stw")
    async def stw(cb: types.CallbackQuery) -> None:
        s = await _db()
        try:
            d = await calculate_safe_to_withdraw(s)
            await cb.message.edit_text(f"STW: {d['safe_to_withdraw_usdt']:.2f} USDT", reply_markup=_back())
        finally: await s.close()
        await cb.answer()
    @dp.callback_query(lambda c: c.data == "admin_health")
    async def health(cb: types.CallbackQuery) -> None:
        await cb.message.edit_text("OK", reply_markup=_back())
        await cb.answer()
    @dp.callback_query(lambda c: c.data.startswith("admin_apps:"))
    async def apps(cb: types.CallbackQuery) -> None:
        s = await _db()
        try:
            from app.accounts.models import PartnerApplication
            rows = (await s.execute(select(PartnerApplication).where(PartnerApplication.status == "pending").limit(10))).scalars().all()
            lines = ["Applications"]
            lines.extend(f"{a.company_name}" for a in rows)
            if not rows: lines.append("None")
            await cb.message.edit_text("\n".join(lines), reply_markup=_back())
        finally: await s.close()
        await cb.answer()
    return dp

async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([BotCommand(command="start", description="Main menu")],
                              scope=BotCommandScopeDefault())