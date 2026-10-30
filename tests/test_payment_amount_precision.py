from sqlalchemy import select

from app.telegram.models import BotAction
from tests.test_payment_method_selection import both_methods as methods_fixture
from tests.test_telegram_cabinet import cabinet as cabinet_fixture
from tests.test_telegram_cabinet import partner

both_methods = methods_fixture
cabinet = cabinet_fixture


async def test_crypto_confirmation_keeps_large_integer_amount_exact(cabinet, db_session, both_methods):
    feed, _ = cabinet
    await partner(db_session)
    await feed(callback="topup_method:crypto_pay")
    screen = (await feed(text="9007199254740993"))[-1]
    assert "Создать счёт на 9,007,199,254,740,993 ₽" in screen.text
    action = (await db_session.scalars(select(BotAction))).one()
    assert action.payload["amount"] == "9007199254740993"
    assert not both_methods[0].created and not both_methods[1].created
