from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import func, select

from app.accounts.models import ConsentAcceptance, PartnerApplication
from app.infrastructure.config import get_settings
from app.telegram.models import BotDialog
from app.telegram.ui import keyboard
from tests.test_telegram_cabinet import cabinet as cabinet
from tests.test_telegram_cabinet import partner


async def test_start_shows_documents_before_collecting_application(cabinet, db_session, monkeypatch):
    feed, _ = cabinet
    settings = get_settings()
    monkeypatch.setattr(settings, "terms_url", "https://example.org/terms")
    monkeypatch.setattr(settings, "privacy_policy_url", "https://example.org/privacy")
    methods = await feed(text="/start")
    screen = methods[-1]
    assert "Добро пожаловать" in screen.text
    assert settings.legal_document_version in screen.text
    buttons = [button for row in screen.reply_markup.inline_keyboard for button in row]
    assert {button.url for button in buttons if button.url} == {settings.terms_url, settings.privacy_policy_url}
    assert any(button.callback_data == "accept_legal" and "оба документа" in button.text for button in buttons)
    assert (await db_session.get(BotDialog, "123")).state == "consent"
    await feed(text="Компания без согласия")
    assert (await db_session.execute(select(func.count()).select_from(PartnerApplication))).scalar() == 0
    await feed(callback="accept_legal")
    assert (await db_session.get(BotDialog, "123")).state == "register_company"
    await feed(text="Компания")
    await feed(text="Проект")
    assert (await db_session.execute(select(func.count()).select_from(PartnerApplication))).scalar() == 1
    assert (await db_session.execute(select(func.count()).select_from(ConsentAcceptance))).scalar() == 2
    pending = (await feed(text="/start"))[-1]
    assert "повторно" in pending.text
    assert all(button.callback_data not in {"register", "accept_legal"}
               for row in pending.reply_markup.inline_keyboard for button in row)


async def test_documents_changed_during_application_require_new_acceptance(cabinet, db_session, monkeypatch):
    feed, _ = cabinet
    monkeypatch.setattr(get_settings(), "terms_url", "https://example.org/terms")
    monkeypatch.setattr(get_settings(), "privacy_policy_url", "https://example.org/privacy")
    await feed(callback="register")
    await feed(callback="accept_legal")
    await feed(text="Компания")
    monkeypatch.setattr(get_settings(), "legal_document_version", "new-version")
    await feed(text="Проект")
    assert (await db_session.execute(select(func.count()).select_from(PartnerApplication))).scalar() == 0
    assert (await db_session.get(BotDialog, "123")).state == "consent"


def test_all_buttons_are_blue_including_links_and_back():
    markup = keyboard(("Действие", "action"), ("Документы", "https://example.org"))
    assert all(button.style == "primary" for row in markup.inline_keyboard for button in row)


@pytest.mark.parametrize("api_failure", [False, True])
async def test_click_feedback_is_green_without_mutating_original_or_blocking_action(api_failure):
    from app.telegram.ui import highlight_pressed

    bot = Bot(token="123456:TEST_TOKEN")
    bot.session = AsyncMock()
    markup = keyboard(("Первый", "first"), ("Второй", "second"))
    event = CallbackQuery(
        id="click", from_user=User(id=123, is_bot=False, first_name="Test"), chat_instance="test", data="second",
        message=Message(
            message_id=1, date=datetime.now(UTC), chat=Chat(id=123, type="private"), reply_markup=markup,
        ).as_(bot),
    ).as_(bot)
    if api_failure:
        bot.session.side_effect = TelegramBadRequest(
            method=EditMessageReplyMarkup(chat_id=123, message_id=1), message="message is not modified",
        )
    try:
        await highlight_pressed(event)
        method = bot.session.call_args.args[1]
        assert isinstance(method, EditMessageReplyMarkup)
        buttons = [button for row in method.reply_markup.inline_keyboard for button in row]
        assert [button.style for button in buttons] == ["primary", "success", "primary"]
        assert all(button.style == "primary" for row in markup.inline_keyboard for button in row)
    finally:
        await bot.session.close()


@pytest.mark.parametrize("callback, screen_text", [
    ("trials", "тест"), ("legal_documents", "Документы вашего подключения"),
    ("balance", "Баланс"), ("api_keys:0", "ключей"), ("history:0", "История"),
    ("support", "обращени"), ("settings", "Документация и аккаунт"), ("admin_menu", "Администрирование"),
])
async def test_bound_menu_buttons_navigate(cabinet, db_session, monkeypatch, callback, screen_text):
    from contextvars import copy_context

    feed, bot = cabinet
    # Incoming buttons are bound to this bot, including the live async context.
    bot.session.active_context = copy_context()
    await partner(db_session, user="999")
    monkeypatch.setattr(get_settings(), "terms_url", "https://example.org/terms")
    monkeypatch.setattr(get_settings(), "privacy_policy_url", "https://example.org/privacy")
    home = (await feed(user=999, text="/start"))[-1]
    methods = await feed(user=999, callback=callback, reply_markup=home.reply_markup)
    assert screen_text.lower() in methods[-1].text.lower()
    assert any(isinstance(method, EditMessageReplyMarkup) for method in methods)


@pytest.mark.parametrize("failure", ["telegram", "timeout", "serialization"])
async def test_feedback_failure_still_opens_requested_screen(cabinet, db_session, monkeypatch, failure):
    import asyncio

    from aiogram.types import InlineKeyboardMarkup

    feed, bot = cabinet
    await partner(db_session)
    markup = keyboard(("Баланс", "balance"))

    async def request(bot, method, **kwargs):
        if isinstance(method, EditMessageReplyMarkup):
            if failure == "telegram":
                raise TelegramBadRequest(method=method, message="message is not modified")
            if failure == "timeout":
                await asyncio.Future()
        return True

    bot.session.side_effect = request
    if failure == "serialization":
        original = InlineKeyboardMarkup.model_dump

        def dump(self, **kwargs):
            if self.bot is not None:
                raise TypeError("synthetic markup serialization failure")
            return original(self, **kwargs)

        monkeypatch.setattr(InlineKeyboardMarkup, "model_dump", dump)
    async with asyncio.timeout(3):
        methods = await feed(callback="balance", reply_markup=markup)
    assert "баланс" in methods[-1].text.lower()


async def test_click_feedback_handles_incoming_markup_bound_to_live_bot(monkeypatch):
    from app.telegram.ui import highlight_pressed

    bot = Bot(token="123456:TEST_TOKEN")
    # A real session contains async state that cannot be deep-copied. No network I/O.
    await bot.session.create_session()
    request = AsyncMock()
    monkeypatch.setattr(bot.session, "make_request", request)
    event = CallbackQuery.model_validate({
        "id": "click", "from": {"id": 123, "is_bot": False, "first_name": "Test"},
        "chat_instance": "test", "data": "second",
        "message": {
            "message_id": 1, "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": 123, "type": "private"},
            "reply_markup": keyboard(("Первый", "first"), ("Второй", "second")).model_dump(),
        },
    }, context={"bot": bot})
    try:
        assert event.message.reply_markup.bot is bot
        await highlight_pressed(event)
        method = request.call_args.args[1]
        assert [b.style for row in method.reply_markup.inline_keyboard for b in row] == [
            "primary", "success", "primary",
        ]
        assert all(b.style == "primary" for row in event.message.reply_markup.inline_keyboard for b in row)
    finally:
        await bot.session.close()
