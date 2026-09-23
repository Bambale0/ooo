"""Optional single-instance polling entrypoint; never starts from an API worker."""

import asyncio
from contextlib import suppress

from app.infrastructure.database import engine
from app.infrastructure.logging import configure_logging
from app.infrastructure.production_check import require_production_config
from app.telegram.app import create_bot, create_dispatcher, set_bot_commands
from app.telegram.notifications import notification_loop


async def main() -> None:
    require_production_config()
    configure_logging()
    bot = create_bot()
    if bot is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    notifications = asyncio.create_task(notification_loop(bot))
    try:
        await set_bot_commands(bot)
        await create_dispatcher().start_polling(bot)
    finally:
        notifications.cancel()
        with suppress(asyncio.CancelledError):
            await notifications
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
