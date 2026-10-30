"""Keep active cabinet handlers alive during polling shutdown, within the shutdown budget."""

import asyncio
import logging

from aiogram import BaseMiddleware

from app.infrastructure.config import get_settings

logger = logging.getLogger(__name__)


class InFlightUpdatesMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self.active_tasks: set[asyncio.Task] = set()

    async def __call__(self, handler, event, data):
        task = asyncio.current_task()
        if task is not None:
            self.active_tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            self.active_tasks.discard(task)

    async def drain_updates(self, **kwargs) -> None:
        tasks = {task for task in self.active_tasks if not task.done() and task is not asyncio.current_task()}
        if not tasks:
            return
        logger.info("telegram_update_drain_started", extra={"active_updates": len(tasks)})
        _, pending = await asyncio.wait(tasks, timeout=get_settings().worker_shutdown_grace_seconds)
        if pending:
            # Do not blindly retry provider submission. The persisted creation claim
            # is recovered by the payment sweeper if the process must still exit.
            logger.warning("telegram_update_drain_timed_out", extra={"active_updates": len(pending)})
        else:
            logger.info("telegram_update_drain_completed")
