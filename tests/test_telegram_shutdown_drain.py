import asyncio

from app.infrastructure.config import get_settings
from app.telegram.app import create_dispatcher


def _tracker():
    dispatcher = create_dispatcher()
    handlers = [item.callback for item in dispatcher.shutdown.handlers if item.callback.__name__ == "drain_updates"]
    assert len(handlers) == 1, "Polling shutdown must drain in-flight cabinet updates before closing the Bot session"
    return handlers[0].__self__


async def test_shutdown_waits_for_confirmation_to_finish(monkeypatch):
    tracker = _tracker()
    monkeypatch.setattr(get_settings(), "worker_shutdown_grace_seconds", 1.0)
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(event, data):
        started.set()
        await release.wait()
        return "persisted"

    update = asyncio.create_task(tracker(handler, None, {}))
    try:
        await started.wait()
        draining = asyncio.create_task(tracker.drain_updates())
        await asyncio.sleep(0)
        assert not draining.done()
        release.set()
        await asyncio.wait_for(draining, 1)
        assert await update == "persisted"
        assert not tracker.active_tasks
    finally:
        release.set()
        await update


async def test_shutdown_timeout_is_bounded_and_does_not_cancel_live_handler(monkeypatch, caplog):
    tracker = _tracker()
    monkeypatch.setattr(get_settings(), "worker_shutdown_grace_seconds", 0.01)
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(event, data):
        started.set()
        await release.wait()

    update = asyncio.create_task(tracker(handler, None, {}))
    try:
        await started.wait()
        await asyncio.wait_for(tracker.drain_updates(), 1)
        assert not update.done()
        assert "telegram_update_drain_timed_out" in caplog.text
    finally:
        release.set()
        await update
    assert not tracker.active_tasks
