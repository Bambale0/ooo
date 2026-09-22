from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.infrastructure.config import get_settings


class CabinetAccessMiddleware(BaseMiddleware):
    """Keep account data in private chats and authorize every admin callback."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            if not isinstance(event.message, Message) or event.message.chat.type != "private":
                await event.answer("Open the cabinet in a private chat", show_alert=True)
                return None
            if (event.data or "").startswith("admin_"):
                if str(event.from_user.id) != get_settings().admin_telegram_id:
                    await event.answer("Access denied", show_alert=True)
                    return None
            for prefix in ("history:", "api_keys:", "admin_apps:"):
                if (event.data or "").startswith(prefix):
                    page = event.data[len(prefix) :]
                    if not page.isascii() or not page.isdigit() or len(page) > 6:
                        await event.answer("Invalid page", show_alert=True)
                        return None
        elif isinstance(event, Message) and event.chat.type != "private":
            return None
        return await handler(event, data)
