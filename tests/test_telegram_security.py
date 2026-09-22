from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, User

from app.infrastructure.config import get_settings
from app.telegram.app import create_dispatcher


@pytest.mark.parametrize("action", ["admin_menu", "admin_apps:0", "admin_stw", "admin_health"])
async def test_non_admin_cannot_invoke_admin_callbacks(action, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_telegram_id", "999")
    bot = Bot(token="123456:TEST_TOKEN")
    bot.session = AsyncMock()
    callback = CallbackQuery(
        id="cb1",
        from_user=User(id=123, is_bot=False, first_name="Test"),
        chat_instance="instance",
        data=action,
        message=Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=123, type="private")),
    ).as_(bot)
    await create_dispatcher().feed_raw_update(bot, {"update_id": 1, "callback_query": callback.model_dump(mode="json")})
    methods = [call.args[1] for call in bot.session.call_args_list]
    assert len(methods) == 1
    assert methods[0].text == "Access denied"
    await bot.session.close()


@pytest.mark.parametrize("data,chat_type", [("history:bad", "private"), ("balance", "group"), (None, "group")])
async def test_invalid_callback_or_shared_chat_does_not_reach_account_query(data, chat_type):
    bot = Bot(token="123456:TEST_TOKEN")
    bot.session = AsyncMock()
    callback = CallbackQuery(
        id="cb1",
        from_user=User(id=123, is_bot=False, first_name="Test"),
        chat_instance="instance",
        data=data,
        message=Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=123, type=chat_type)),
    )
    await create_dispatcher().feed_raw_update(bot, {"update_id": 1, "callback_query": callback.model_dump(mode="json")})
    assert bot.session.call_count == 1
    await bot.session.close()
