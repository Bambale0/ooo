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
