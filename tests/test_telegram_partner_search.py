from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, inspect, select, text
from test_telegram_cabinet import cabinet as cabinet_fixture
from test_telegram_cabinet import partner

from app.accounts.models import ApiKey
from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.providers.models import ProviderAttempt, ProviderCredential
from app.telegram.models import BotAction, BotDialog

cabinet = cabinet_fixture


def button_data(methods):
    return [button.callback_data for row in methods[-1].reply_markup.inline_keyboard for button in row]


async def test_partner_picker_lists_paginates_and_selects(cabinet, db_session):
    feed, _ = cabinet
    owners = [await partner(db_session, str(200 + index)) for index in range(6)]
    first = await feed(user=999, callback="admin_form:adjustment")
    first_ids = {value for value in button_data(first) if value.startswith("admin_partner_pick:")}
    assert len(first_ids) == 4
    assert "admin_partner_page:1" in button_data(first)
    assert first[0].__class__.__name__ == "AnswerCallbackQuery"
    second = await feed(user=999, callback="admin_partner_page:1")
    second_ids = {value for value in button_data(second) if value.startswith("admin_partner_pick:")}
    assert len(second_ids) == 2 and not first_ids.intersection(second_ids)
    assert "admin_partner_page:0" in button_data(second)
    selected = await feed(user=999, callback=f"admin_partner_pick:{owners[-1].id}")
    assert "Telegram ID: 205" in selected[-1].text
    assert "Введите сумму" in selected[-1].text
    assert (await db_session.get(BotDialog, "999")).data["values"] == {"partner_id": owners[-1].id}


@pytest.mark.parametrize("identifier", ["123", "@CaSe_User", "CASE_USER"])
async def test_search_recipient_then_confirmation_credits_exactly_once(cabinet, db_session, identifier):
    feed, _ = cabinet
    owner = await partner(db_session)
    other = await partner(db_session, "456")
    await feed(username="Case_User", text="/start")
    await feed(user=999, callback="admin_form:adjustment")
    selected = await feed(user=999, text=identifier)
    if identifier != "123":
        selected = await feed(user=999, callback=f"admin_partner_pick:{owner.id}")
    assert "Telegram ID: 123" in selected[-1].text
    assert "@case_user" in selected[-1].text
    await feed(user=999, text="+500")
    preview = await feed(user=999, text="Начисление по распоряжению администратора")
    assert "Telegram ID: 123" in preview[-1].text and "@case_user" in preview[-1].text
    action = (await db_session.execute(select(BotAction))).scalar_one()
    assert action.payload["values"]["partner_id"] == owner.id
    assert owner.balance_rub == Decimal("1000")
    await feed(user=456, callback=f"confirm:{action.id}")
    assert owner.balance_rub == Decimal("1000")
    await feed(user=999, callback=f"confirm:{action.id}")
    await feed(user=999, callback=f"confirm:{action.id}")
    assert owner.balance_rub == Decimal("1500") and other.balance_rub == Decimal("1000")
    assert (await db_session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one() == 1


async def test_partner_picker_empty_unknown_and_retry(cabinet, db_session):
    feed, _ = cabinet
    empty = await feed(user=999, callback="admin_form:adjustment")
    assert "Партнёров на этой странице нет" in empty[-1].text
    await partner(db_session)
    missing = await feed(user=999, text="@unknown_user")
    assert "Партнёр не найден" in missing[-1].text
    assert (await db_session.get(BotDialog, "999")).state == "admin_partner_pick"
    all_partners = await feed(user=999, callback="admin_partner_list")
    assert any(value.startswith("admin_partner_pick:") for value in button_data(all_partners))
    selected = await feed(user=999, text="123")
    assert "Telegram ID: 123" in selected[-1].text
    assert (await db_session.execute(select(func.count()).select_from(BotAction))).scalar_one() == 0


async def test_username_refresh_and_removal_do_not_keep_old_search_results(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    await feed(username="Old_Name", text="/start")
    await feed(username="New_Name", callback="balance")
    await feed(user=999, callback="admin_form:adjustment")
    old = await feed(user=999, text="@old_name")
    assert "Партнёр не найден" in old[-1].text
    new = await feed(user=999, text="@new_name")
    assert f"admin_partner_pick:{owner.id}" in button_data(new)
    new = await feed(user=999, callback=f"admin_partner_pick:{owner.id}")
    assert "Telegram ID: 123" in new[-1].text
    await feed(text="/start")
    await feed(user=999, callback="admin_form:adjustment")
    removed = await feed(user=999, text="@new_name")
    assert "Партнёр не найден" in removed[-1].text
    assert (await db_session.get(BotDialog, "123")).telegram_username is None


async def test_duplicate_cached_username_requires_explicit_selection(cabinet, db_session):
    feed, _ = cabinet
    owners = [await partner(db_session, value) for value in ("123", "456")]
    # Telegram usernames can be reassigned while an old owner has not contacted the bot.
    for owner in owners:
        db_session.add(BotDialog(telegram_id=owner.telegram_id, telegram_username="reused_name"))
    await db_session.commit()
    await feed(user=999, callback="admin_form:adjustment")
    results = await feed(user=999, text="@REUSED_NAME")
    assert "Проверьте Telegram ID" in results[-1].text
    assert (await db_session.get(BotDialog, "999")).state == "admin_partner_pick"
    assert {value for value in button_data(results) if value.startswith("admin_partner_pick:")} == {
        f"admin_partner_pick:{owner.id}" for owner in owners
    }
    assert {
        button.text.split(" · ", 1)[0]
        for row in results[-1].reply_markup.inline_keyboard
        for button in row
        if button.callback_data.startswith("admin_partner_pick:")
    } == {"123", "456"}
    assert (await db_session.execute(select(func.count()).select_from(BotAction))).scalar_one() == 0


@pytest.mark.parametrize(
    "callback", ["admin_form:adjustment", "admin_partner_list", "admin_partner_search", "admin_partner_page:0"]
)
async def test_partner_picker_denies_nonadmin_without_disclosing_recipients(cabinet, db_session, callback):
    feed, _ = cabinet
    owner = await partner(db_session)
    methods = await feed(user=456, callback=callback)
    assert all(owner.telegram_id not in (getattr(method, "text", "") or "") for method in methods)
    assert (await db_session.get(BotDialog, "456")) is None


async def test_nonadmin_forged_picker_state_and_stale_selection_are_rejected(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    db_session.add(BotDialog(telegram_id="456", state="admin_partner_pick", data={"form": "adjustment"}))
    await db_session.commit()
    await feed(user=456, text="123")
    assert (await db_session.execute(select(func.count()).select_from(BotAction))).scalar_one() == 0
    await feed(user=999, callback="admin_form:adjustment")
    await feed(user=999, text="/cancel")
    stale = await feed(user=999, callback=f"admin_partner_pick:{owner.id}")
    assert "устарело" in stale[-1].text
    assert (await db_session.get(BotDialog, "999")).state == "menu"


@pytest.mark.parametrize("value", ["not a username", "@' OR 1=1 --", "a" * 2001])
async def test_malformed_search_can_be_corrected(cabinet, db_session, value):
    feed, _ = cabinet
    await partner(db_session)
    await feed(user=999, callback="admin_form:adjustment")
    response = await feed(user=999, text=value)
    assert "Введите числовой Telegram ID" in response[-1].text
    assert (await db_session.get(BotDialog, "999")).state == "admin_partner_pick"


def test_username_migration_preserves_dialog_and_can_be_rolled_back():
    path = Path(__file__).parents[1] / "alembic/versions/20260929_0021_telegram_username.py"
    spec = spec_from_file_location("username_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE bot_dialogs (telegram_id VARCHAR(64) PRIMARY KEY, state VARCHAR(80), data JSON)")
        )
        connection.execute(text("INSERT INTO bot_dialogs VALUES ('123', 'admin_form_input', '{}')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(text("SELECT telegram_id, state, telegram_username FROM bot_dialogs")).one() == (
                "123",
                "admin_form_input",
                None,
            )
            assert any(
                index["name"] == "ix_bot_dialogs_telegram_username"
                for index in inspect(connection).get_indexes("bot_dialogs")
            )
            migration.downgrade()
            assert connection.execute(text("SELECT telegram_id, state FROM bot_dialogs")).one() == (
                "123",
                "admin_form_input",
            )
            assert "telegram_username" not in {
                column["name"] for column in inspect(connection).get_columns("bot_dialogs")
            }
    engine.dispose()


async def test_admin_partner_management_lists_searches_and_opens_card(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    owner.company_name = "Acme Studio"
    owner.project_name = "Video Factory"
    db_session.add(BotDialog(telegram_id=owner.telegram_id, telegram_username="acme_owner"))
    await db_session.commit()

    menu = await feed(user=999, callback="admin_menu")
    assert "admin_partners:0" in button_data(menu)

    listing = await feed(user=999, callback="admin_partners:0")
    assert f"admin_partner_view:{owner.id}" in button_data(listing)
    assert "Поиск" in listing[-1].text

    await feed(user=999, callback="admin_partners_search")
    card = await feed(user=999, text="Acme Studio")
    assert owner.id in card[-1].text
    assert "Video Factory" in card[-1].text
    assert "@acme_owner" in card[-1].text
    assert "Баланс: 1000.00 ₽" in card[-1].text
    assert f"admin_partner_adjust:{owner.id}" in button_data(card)
    assert f"admin_disable:{owner.id}" in button_data(card)


async def test_admin_partner_card_exposes_operational_views_without_secrets(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    db_session.add(
        ApiKey(
            partner_id=owner.id,
            name="Production",
            key_hash="a" * 64,
            key_prefix="nr_live_123",
            webhook_url="https://example.org/hook",
        )
    )
    db_session.add(
        ProviderCredential(
            provider="argolink",
            label="Primary",
            key_hash="b" * 64,
            key_prefix="argo_test",
            encrypted_api_key="ENCRYPTED_SECRET_MUST_NOT_RENDER",
            partner_id=owner.id,
        )
    )
    await db_session.commit()

    card = await feed(user=999, callback=f"admin_partner_view:{owner.id}")
    assert "API-ключей: 1" in card[-1].text
    assert "ключей поставщика: 1" in card[-1].text

    keys = await feed(user=999, callback=f"admin_partner_keys:{owner.id}")
    assert "Production" in keys[-1].text
    assert "nr_live_123" in keys[-1].text
    assert "a" * 64 not in keys[-1].text

    credentials = await feed(user=999, callback=f"admin_partner_creds:{owner.id}")
    assert "argolink" in credentials[-1].text
    assert "argo_test" in credentials[-1].text
    assert "ENCRYPTED_SECRET_MUST_NOT_RENDER" not in credentials[-1].text
    assert "b" * 64 not in credentials[-1].text


async def test_generation_and_partner_history_show_full_correlation_uuids(cabinet, db_session):
    feed, _ = cabinet
    owner = await partner(db_session)
    generation = Generation(
        partner_id=owner.id,
        model_id="00000000-0000-0000-0000-000000000111",
        model_slug="nano-banana-pro",
        mode="images/edits",
        resolution="1K",
        duration_seconds=1,
        idempotency_key="uuid-visibility-generation",
        partner_price_rub=Decimal("10.00"),
        prompt="trace",
        status="failed",
    )
    db_session.add(generation)
    await db_session.flush()
    attempt = ProviderAttempt(generation_id=generation.id, provider="argolink", status="failed")
    ledger = LedgerEntry(
        partner_id=owner.id,
        operation_type="generation_charge",
        amount_rub=Decimal("-10.00"),
        balance_after_rub=Decimal("990.00"),
        idempotency_key="uuid-visibility-ledger",
        generation_id=generation.id,
    )
    db_session.add_all([attempt, ledger])
    await db_session.commit()

    admin_view = await feed(user=999, callback=f"admin_partner_gens:{owner.id}")
    assert generation.id in admin_view[-1].text
    assert attempt.id in admin_view[-1].text
    assert generation.id[:8] + " · " not in admin_view[-1].text

    partner_view = await feed(user=int(owner.telegram_id), callback="history:0")
    assert ledger.id in partner_view[-1].text
    assert generation.id in partner_view[-1].text


@pytest.mark.parametrize(
    "callback",
    [
        "admin_partners:0",
        "admin_partners_search",
        "admin_partner_view:00000000-0000-0000-0000-000000000001",
        "admin_partner_keys:00000000-0000-0000-0000-000000000001",
    ],
)
async def test_partner_management_denies_nonadmin(cabinet, db_session, callback):
    feed, _ = cabinet
    owner = await partner(db_session)
    methods = await feed(user=456, callback=callback)
    assert all(owner.telegram_id not in (getattr(method, "text", "") or "") for method in methods)
    assert (await db_session.get(BotDialog, "456")) is None
