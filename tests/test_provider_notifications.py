import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.config import get_settings
from app.telegram.models import BotNotification

TOKEN = "test-provider-notification-secret-only"  # pragma: allowlist secret
HEADERS = {"Authorization": "Bearer " + TOKEN}
PAYLOAD = {
    "type": "quota_exceed", "title": "Лимит провайдера",
    "content": "Осталось {{value}}, порог {{value}}", "values": ["$0.99", "$1"], "timestamp": 1739950503,
}


@pytest.fixture
def configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setitem(settings.__dict__, "provider_notification_webhook_secret", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "admin_telegram_id", "123456")
    monkeypatch.setattr(settings, "telegram_bot_token", "test-bot-token")


async def test_authenticated_notification_is_durable_and_duplicate_safe(client, db_session, configured):
    for path, payload in (("/webhook/res/", PAYLOAD), ("/webhook/res", dict(reversed(list(PAYLOAD.items()))))):
        response = await client.post(path, json=payload, headers=HEADERS)
        assert response.status_code == 200
        assert response.json() == {"success": True}
    rows = list((await db_session.execute(select(BotNotification))).scalars())
    assert len(rows) == 1
    assert rows[0].telegram_id == "123456"
    assert "Осталось $0.99, порог $1" in rows[0].text
    assert "quota_exceed" in rows[0].text and "1739950503" in rows[0].text
    assert rows[0].sent_at is None
    assert TOKEN not in rows[0].text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic wrong"}])
async def test_authentication_precedes_payload_processing(client, db_session, configured, headers):
    response = await client.post("/webhook/res/", content=b"not json", headers=headers)
    assert response.status_code == 401
    assert list((await db_session.execute(select(BotNotification))).scalars()) == []


@pytest.mark.parametrize("field,value", [
    ("provider_notification_webhook_secret", None), ("admin_telegram_id", None), ("telegram_bot_token", None),
])
async def test_missing_receiver_configuration_fails_closed(client, configured, monkeypatch, field, value):
    monkeypatch.setattr(get_settings(), field, value)
    response = await client.post("/webhook/res/", json=PAYLOAD, headers=HEADERS)
    assert response.status_code == 503


@pytest.mark.parametrize("payload", [
    {}, {**PAYLOAD, "timestamp": True}, {**PAYLOAD, "timestamp": "123"},
    {**PAYLOAD, "values": "not a list"}, {**PAYLOAD, "type": ""}, {**PAYLOAD, "content": "x" * 12001},
])
async def test_invalid_payload_is_rejected_without_reflecting_input(client, configured, payload):
    response = await client.post("/webhook/res/", json=payload, headers=HEADERS)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_provider_notification"}


async def test_json_errors_and_body_limit(client, configured):
    invalid = await client.post("/webhook/res/", content=b"{", headers=HEADERS)
    assert invalid.status_code == 400
    oversized = await client.post("/webhook/res/", content=b"x" * 65537, headers=HEADERS)
    assert oversized.status_code == 413
    assert (await client.get("/webhook/res/")).status_code == 405


async def test_substitution_does_not_reinterpret_values_and_new_events_are_kept(client, db_session, configured):
    payload = {**PAYLOAD, "values": ["{{value}}", "<b>two</b>"]}
    assert (await client.post("/webhook/res/", json=payload, headers=HEADERS)).status_code == 200
    payload["timestamp"] += 1
    assert (await client.post("/webhook/res/", json=payload, headers=HEADERS)).status_code == 200
    rows = list((await db_session.execute(select(BotNotification))).scalars())
    assert len(rows) == 2
    assert all("Осталось {{value}}, порог <b>two</b>" in row.text for row in rows)


async def test_optional_values_and_long_unicode_messages(client, db_session, configured):
    payload = {key: value for key, value in PAYLOAD.items() if key != "values"}
    payload["content"] = "😀" * 5000
    assert (await client.post("/webhook/res/", json=payload, headers=HEADERS)).status_code == 200
    row = (await db_session.execute(select(BotNotification))).scalar_one()
    assert len(row.text.encode("utf-16-le")) // 2 <= 4096
    assert row.text.endswith("…")


async def test_telegram_outage_keeps_notification_for_retry(client, db_session, configured, monkeypatch):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    from app.telegram.notifications import deliver_notifications

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("app.telegram.notifications.SessionLocal", session)

    class Bot:
        fail = True
        messages = []

        async def send_message(self, chat_id, text, parse_mode):
            assert parse_mode is None and chat_id == 123456
            if self.fail:
                raise TelegramNetworkError(method=SendMessage(chat_id=chat_id, text=text), message="offline")
            self.messages.append(text)

    bot = Bot()
    assert (await client.post("/webhook/res/", json=PAYLOAD, headers=HEADERS)).status_code == 200
    assert await deliver_notifications(bot) == 0
    row = (await db_session.execute(select(BotNotification))).scalar_one()
    assert row.attempts == 1 and row.sent_at is None and row.next_attempt_at is not None
    bot.fail = False
    row.next_attempt_at = None
    await db_session.commit()
    assert await deliver_notifications(bot) == 1
    assert await deliver_notifications(bot) == 0
    assert len(bot.messages) == 1


async def test_commit_failure_is_not_acknowledged(client, db_session, configured, monkeypatch):
    async def fail_commit():
        raise RuntimeError("commit_failed")

    monkeypatch.setattr(db_session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit_failed"):
        await client.post("/webhook/res/", json=PAYLOAD, headers=HEADERS)


@pytest.mark.integration
async def test_postgres_concurrent_duplicates_commit_once(configured):
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    from app.providers.notifications import ProviderNotification, enqueue_notification

    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    payload = ProviderNotification.model_validate({**PAYLOAD, "title": str(uuid4())})

    async def enqueue():
        async with factory() as db:
            created = await enqueue_notification(db, payload, "123456")
            await db.commit()
            return created

    try:
        results = await asyncio.gather(*(enqueue() for _ in range(8)))
        assert sum(results) == 1
    finally:
        async with factory() as db:
            await db.execute(delete(BotNotification).where(BotNotification.dedupe_key == payload.dedupe_key()))
            await db.commit()
        await engine.dispose()
