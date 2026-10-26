"""Telegram development leads: resumable intake, isolation and admin workflow."""

from sqlalchemy import func, select

from app.accounts.models import Partner
from app.telegram.models import BotDialog, BotNotification, DevelopmentRequest
from tests.test_telegram_cabinet import cabinet as cabinet
from tests.test_telegram_cabinet import partner


async def complete_intake(feed):
    await feed(callback="dev:start")
    await feed(callback="dev:choose:automation")
    await feed(callback="dev:choose:telegram")
    await feed(text="Хочу автоматизировать приём и распределение заявок клиентов.")
    await feed(callback="dev:choose:crm")
    await feed(callback="dev:choose:month")
    await feed(callback="dev:choose:50_150")
    return await feed(callback="dev:choose:telegram")


async def test_home_has_obvious_order_action_and_compact_copy(cabinet, db_session):
    feed, _ = cabinet
    await partner(db_session)
    screen = (await feed(text="/start"))[-1]
    assert "Баланс:" in screen.text
    assert len(screen.text) < 400
    rows = screen.reply_markup.inline_keyboard
    assert any(button.callback_data == "dev:start" for row in rows for button in row)
    assert any(button.callback_data == "trials" for row in rows for button in row)
    assert all(len(row) <= 2 for row in rows)


async def test_guest_can_submit_lead_once_and_admin_is_notified(cabinet, db_session):
    feed, _ = cabinet
    screen = (await feed(text="/start"))[-1]
    assert any(button.callback_data == "dev:start" for row in screen.reply_markup.inline_keyboard for button in row)
    preview = (await complete_intake(feed))[-1]
    assert "Проверьте заявку" in preview.text
    assert "приём и распределение" in preview.text
    sent = (await feed(callback="dev:submit"))[-1]
    assert "Заявка" in sent.text and "отправлена" in sent.text
    row = (await db_session.execute(select(DevelopmentRequest))).scalar_one()
    assert row.telegram_id == "123"
    assert row.status == "new"
    assert row.answers["kind"] == "Автоматизация"
    assert row.answers["platform"] == "Telegram"
    assert row.answers["goal"].startswith("Хочу автоматизировать")
    assert (await db_session.execute(select(func.count()).select_from(Partner))).scalar() == 0
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 1
    await feed(callback="dev:submit")
    assert (await db_session.execute(select(func.count()).select_from(DevelopmentRequest))).scalar() == 1
    assert (await db_session.get(BotDialog, "123")).state == "menu"


async def test_back_validation_and_cancel_preserve_user_control(cabinet, db_session):
    feed, _ = cabinet
    await feed(callback="dev:start")
    await feed(callback="dev:choose:ai")
    await feed(callback="dev:choose:telegram")
    back = (await feed(callback="dev:back"))[-1]
    assert "2 из 7" in back.text
    await feed(callback="dev:choose:web")
    bad = (await feed(text="кратко"))[-1]
    assert "15" in bad.text
    assert (await db_session.get(BotDialog, "123")).data["step"] == 2
    await feed(text="Нужен чат-бот для консультаций посетителей сайта.")
    assert (await db_session.get(BotDialog, "123")).data["step"] == 3
    await feed(callback="dev:cancel")
    await feed(callback="dev:submit")
    assert (await db_session.execute(select(func.count()).select_from(DevelopmentRequest))).scalar() == 0


async def test_admin_review_and_status_notifications_are_role_bound(cabinet, db_session):
    feed, _ = cabinet
    await complete_intake(feed)
    await feed(callback="dev:submit")
    row = (await db_session.execute(select(DevelopmentRequest))).scalar_one()
    unauthorized = await feed(user=456, callback=f"dev:view:{row.id}")
    assert not any("распределение" in str(getattr(m, "text", "")) for m in unauthorized)
    blocked = await feed(user=456, callback=f"admin_dev_view:{row.id}")
    assert not any("распределение" in str(getattr(m, "text", "")) for m in blocked)
    admin_list = (await feed(user=999, callback="admin_dev:0"))[-1]
    assert "Заявки на разработку" in admin_list.text
    details = (await feed(user=999, callback=f"admin_dev_view:{row.id}"))[-1]
    assert "распределение" in details.text
    await feed(user=999, callback=f"admin_dev_status:{row.id}:in_progress")
    await db_session.refresh(row)
    assert row.status == "in_progress"
    await feed(user=999, callback=f"admin_dev_status:{row.id}:in_progress")
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 2
    mine = (await feed(callback="dev:my"))[-1]
    assert "В работе" in mine.text
