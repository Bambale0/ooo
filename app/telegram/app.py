from aiogram import Bot, Dispatcher

from app.infrastructure.config import get_settings


def create_dispatcher() -> Dispatcher:
    return Dispatcher()


def create_bot() -> Bot | None:
    token = get_settings().telegram_bot_token
    if not token:
        return None
    return Bot(token=token)
