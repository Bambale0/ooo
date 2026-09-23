from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram import Bot
from sqlalchemy import func, select

from app.accounts.models import ApiKey, ConsentAcceptance, Partner, PartnerApplication
from app.infrastructure.config import get_settings
from app.payments.models import PaymentInvoice
from app.support.models import SupportAttachment, SupportMessage, SupportTicket
from app.telegram.app import create_dispatcher
from app.telegram.models import BotAction, BotDialog, BotNotification
from app.telegram.service import new_action


@pytest.fixture
async def cabinet(db_session, monkeypatch):
    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("app.telegram.app.SessionLocal", session)
    monkeypatch.setattr(get_settings(), "admin_telegram_id", "999")
    bot = Bot(token="123456:TEST_TOKEN")
    bot.session = AsyncMock()
    counter = 0

    async def feed(*, user=123, text=None, callback=None, document=None):
        nonlocal counter
        counter += 1
        sender = {"id": user, "is_bot": False, "first_name": "Test"}
        message = {
            "message_id": counter,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": user, "type": "private"},
            "from": sender,
        }
        update = {"update_id": counter}
        if callback:
            update["callback_query"] = {
                "id": str(counter),
                "from": sender,
                "chat_instance": "test",
                "data": callback,
                "message": message,
            }
        else:
            if text is not None:
                message["text"] = text
            if document:
                message["document"] = document
            update["message"] = message
        bot.session.reset_mock()
        # New dispatcher simulates a process restart: conversation is exclusively in SQL.
        await create_dispatcher().feed_raw_update(bot, update)
        for row in list(db_session.identity_map.values()):
            await db_session.refresh(row)
        return [call.args[1] for call in bot.session.call_args_list]

    yield feed, bot
    await bot.session.close()


async def partner(db, user="123"):
    row = Partner(telegram_id=user, company_name="Test", project_name="Test", balance_rub=Decimal("1000"))
    db.add(row)
    await db.commit()
    return row


async def test_registration_survives_restart_and_records_consents(cabinet, db_session, monkeypatch):
    feed, _ = cabinet
    monkeypatch.setattr(get_settings(), "terms_url", "https://example.org/terms")
    monkeypatch.setattr(get_settings(), "privacy_policy_url", "https://example.org/privacy")
    await feed(callback="register")
    await feed(callback="accept_legal")
    await feed(text="Компания")
    methods = await feed(text="Проект API")
    assert "принята" in methods[-1].text
    row = (await db_session.execute(select(PartnerApplication))).scalar_one()
    assert row.telegram_id == "123"
    assert (await db_session.execute(select(func.count()).select_from(ConsentAcceptance))).scalar() == 2
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 1
    assert (await db_session.get(BotDialog, "123")).state == "menu"


async def test_stale_consent_callback_cannot_register(cabinet, db_session):
    feed, _ = cabinet
    await feed(callback="accept_legal")
    await feed(text="Компания")
    await feed(text="Проект")
    assert (await db_session.execute(select(func.count()).select_from(PartnerApplication))).scalar() == 0


async def test_key_confirmation_cannot_be_stolen_or_replayed(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    await partner(db_session, "456")
    await feed(callback="key_create")
    await feed(text="Основной")
    action = (await db_session.execute(select(BotAction))).scalar_one()
    await feed(user=456, callback=f"confirm:{action.id}")
    assert (await db_session.execute(select(func.count()).select_from(ApiKey))).scalar() == 0
    first = await feed(callback=f"confirm:{action.id}")
    assert "Сохраните" in first[-1].text
    again = await feed(callback=f"confirm:{action.id}")
    assert "уже выполнено" in again[-1].text
    key = (await db_session.execute(select(ApiKey))).scalar_one()
    assert key.partner_id == owner.id
    assert "api_key" not in action.payload
    await feed(callback=f"key_revoke:{key.id}")
    revoke = (await db_session.execute(select(BotAction).where(BotAction.kind == "key_revoke"))).scalar_one()
    await feed(callback=f"confirm:{revoke.id}")
    assert key.is_active is False


async def test_search_is_tenant_scoped_and_cancel_clears_state(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    other = await partner(db_session, "456")
    payment = PaymentInvoice(
        partner_id=other.id, idempotency_key="search", requested_rub=Decimal(1000), status="active"
    )
    db_session.add(payment)
    await db_session.commit()
    await feed(callback="search_prompt")
    methods = await feed(text=payment.id)
    assert "такой операции нет" in methods[-1].text
    payment.partner_id = owner.id
    await db_session.commit()
    methods = await feed(text=payment.id)
    assert "1000.00" in methods[-1].text
    await feed(text="/cancel")
    assert (await db_session.get(BotDialog, "123")).state == "menu"


async def test_admin_confirmation_rechecks_role(cabinet, db_session, monkeypatch):
    feed, _ = cabinet
    owner = await partner(db_session)
    action = await new_action(
        db_session, "999", "admin_status", {"partner_id": owner.id, "enabled": False, "reason": "Проверка"}
    )
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "admin_telegram_id", "888")
    await feed(user=999, callback=f"confirm:{action.id}")
    assert owner.status == "active"
    assert action.status == "pending"


async def test_support_attachments_limit_and_cross_tenant_access(cabinet, db_session, monkeypatch, tmp_path):
    feed, bot = cabinet
    await partner(db_session)
    await partner(db_session, "456")
    monkeypatch.setattr(get_settings(), "support_storage_dir", str(tmp_path))
    await feed(callback="support_new")
    await feed(text="Не проходит запрос")
    ticket = (await db_session.execute(select(SupportTicket))).scalar_one()
    await feed(text="Проверьте UUID")
    large = {"file_id": "test", "file_unique_id": "unique", "file_size": 20 * 1024 * 1024 + 1, "file_name": "large.dat"}
    result = await feed(document=large)
    assert "20 МБ" in result[-1].text

    async def download(file, destination):
        destination.write(b"attachment")

    monkeypatch.setattr(Bot, "download", AsyncMock(side_effect=download))
    await feed(document={**large, "file_size": 10, "file_name": "../../secret.txt"})
    attachment = (await db_session.execute(select(SupportAttachment))).scalar_one()
    assert str(tmp_path) in attachment.storage_path and "secret.txt" not in attachment.storage_path
    result = await feed(user=456, callback=f"attachment:{attachment.id}")
    assert not any(type(m).__name__ == "SendDocument" for m in result)
    await feed(user=999, callback=f"admin_ticket:{ticket.id}:0")
    await feed(user=999, text="Ответ поддержки")
    assert (await db_session.execute(select(func.count()).select_from(SupportMessage))).scalar() == 3
    await feed(user=999, callback=f"admin_ticket_close:{ticket.id}")
    action = (await db_session.execute(select(BotAction).where(BotAction.kind == "admin_ticket_close"))).scalar_one()
    await feed(user=999, callback=f"confirm:{action.id}")
    assert ticket.status == "closed"
    await feed(text="Ещё вопрос")
    assert (await db_session.execute(select(func.count()).select_from(SupportMessage))).scalar() == 3


async def test_topup_requires_confirmation_and_duplicate_does_not_recreate(cabinet, db_session, monkeypatch):
    from tests.test_crypto_payments import FakeCryptoPayClient

    feed, _ = cabinet
    await partner(db_session)
    fake = FakeCryptoPayClient()
    monkeypatch.setattr("app.telegram.actions.get_crypto_pay_client", lambda: fake)
    await feed(callback="topup")
    await feed(text="999")
    assert not fake.created
    await feed(text="1500")
    action = (await db_session.execute(select(BotAction))).scalar_one()
    await feed(callback=f"confirm:{action.id}")
    await feed(callback=f"confirm:{action.id}")
    assert len(fake.created) == 1
    assert (await db_session.execute(select(func.count()).select_from(PaymentInvoice))).scalar() == 1


async def test_delete_allows_new_registration_and_transfer_preserves_account(cabinet, db_session):
    from app.accounts.router import transfer_telegram
    from app.accounts.schemas import TransferTelegramCreate

    feed, _ = cabinet
    owner = await partner(db_session)
    old_id = owner.id
    await transfer_telegram(owner.id, TransferTelegramCreate(telegram_id="456", reason="Identity verified"), db_session)
    await db_session.commit()
    assert owner.id == old_id and owner.balance_rub == Decimal("1000")
    result = await feed(callback="balance")
    assert "1000.00" not in result[-1].text
    await feed(user=456, callback="delete")
    action = (await db_session.execute(select(BotAction))).scalar_one()
    await feed(user=456, callback=f"confirm:{action.id}")
    assert owner.status == "deleted" and owner.telegram_id.startswith("deleted:")
    replacement = await partner(db_session, "456")
    assert replacement.id != old_id


async def test_unknown_uuid_and_admin_dialog_do_not_leak(cabinet, db_session):
    feed, _ = cabinet
    await partner(db_session)
    dialog = BotDialog(telegram_id="123", state="admin_account", data={})
    db_session.add(dialog)
    await db_session.commit()
    result = await feed(text=str(uuid4()))
    assert "Действие недоступно" in result[-1].text
