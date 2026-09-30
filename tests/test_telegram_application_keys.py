import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_telegram_cabinet import cabinet as cabinet_fixture

from app.accounts.models import Partner, PartnerApplication
from app.infrastructure.config import get_settings
from app.infrastructure.security import decrypt_secret
from app.providers.models import ProviderCredential
from app.providers.router import create_provider_credential
from app.providers.schemas import ProviderCredentialCreate
from app.telegram.models import BotAction, BotDialog, BotNotification
from app.telegram.service import new_action

cabinet = cabinet_fixture
SECRET = "synthetic-provider-key-for-application-tests"


@pytest.fixture
async def application(db_session, monkeypatch):
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", AsyncMock(return_value=True))
    app = PartnerApplication(telegram_id="123", company_name="Компания", project_name="Проект")
    db_session.add(app)
    await db_session.commit()
    return app


async def pending(db):
    return (await db.scalars(select(BotAction).where(BotAction.status == "pending"))).one()


async def test_application_key_input_survives_restart_and_approval_is_atomic_and_idempotent(
    cabinet, db_session, application
):
    feed, _ = cabinet
    await feed(user=999, callback=f"admin_approve:{application.id}")
    methods = await feed(user=999, text=SECRET)
    action = await pending(db_session)
    assert action.payload["application_id"] == application.id
    assert SECRET not in json.dumps(action.payload)
    assert SECRET not in json.dumps((await db_session.get(BotDialog, "999")).data)
    assert all(SECRET not in str(method) for method in methods)
    assert any(method.__api_method__ == "deleteMessage" for method in methods)
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0
    assert application.status == "pending"
    await feed(user=999, callback=f"confirm:{action.id}")
    await feed(user=999, callback=f"confirm:{action.id}")
    credential = (await db_session.scalars(select(ProviderCredential))).one()
    owner = (await db_session.scalars(select(Partner))).one()
    assert credential.partner_application_id == application.id and credential.partner_id == owner.id
    assert decrypt_secret(credential.encrypted_api_key, get_settings().provider_credentials_master_key) == SECRET
    assert application.status == "approved" and owner.balance_rub == 0
    assert await db_session.scalar(select(func.count()).select_from(BotNotification)) == 1


async def test_application_card_accepts_key_without_uuid_form(cabinet, db_session, application):
    feed, _ = cabinet
    methods = await feed(user=999, callback=f"admin_app:{application.id}")
    buttons = [b.callback_data for row in methods[-1].reply_markup.inline_keyboard for b in row]
    assert f"admin_approve:{application.id}" not in buttons
    await feed(user=999, text=SECRET)
    assert (await pending(db_session)).payload["application_id"] == application.id


@pytest.mark.parametrize("value", ["short", "sk-key with spaces", SECRET])
async def test_invalid_key_does_not_create_confirmation_or_store_plaintext(
    cabinet, db_session, application, monkeypatch, value
):
    feed, _ = cabinet
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", AsyncMock(return_value=False))
    await feed(user=999, callback=f"admin_app_key:{application.id}")
    methods = await feed(user=999, text=value)
    assert await db_session.scalar(select(func.count()).select_from(BotAction)) == 0
    dialog = await db_session.get(BotDialog, "999")
    assert dialog.state == "admin_application_key" and value not in json.dumps(dialog.data)
    assert all(value not in str(method) for method in methods)
    assert any(method.__api_method__ == "deleteMessage" for method in methods)


async def test_non_admin_cannot_enter_key_or_confirm_application(cabinet, db_session, application):
    feed, _ = cabinet
    await feed(user=123, callback=f"admin_app_key:{application.id}")
    assert await db_session.get(BotDialog, "123") is None
    await feed(user=999, callback=f"admin_app:{application.id}")
    await feed(user=999, text=SECRET)
    action = await pending(db_session)
    await feed(user=123, callback=f"confirm:{action.id}")
    assert application.status == "pending"
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0


async def test_replacing_pending_key_invalidates_older_confirmation(cabinet, db_session, application):
    feed, _ = cabinet
    await feed(user=999, callback=f"admin_app:{application.id}")
    await feed(user=999, text=SECRET)
    old = await pending(db_session)
    await feed(user=999, text=SECRET + "-replacement")
    current = await pending(db_session)
    assert old.status == "cancelled" and "encrypted_api_key" not in old.payload
    await feed(user=999, callback=f"confirm:{old.id}")
    assert application.status == "pending"
    await feed(user=999, callback=f"confirm:{current.id}")
    credential = (await db_session.scalars(select(ProviderCredential))).one()
    assert decrypt_secret(credential.encrypted_api_key, get_settings().provider_credentials_master_key) == (
        SECRET + "-replacement"
    )


@pytest.mark.parametrize("back", ["/cancel", "admin_apps:0", "main_menu"])
async def test_leaving_application_cancels_key_confirmation(cabinet, db_session, application, back):
    feed, _ = cabinet
    await feed(user=999, callback=f"admin_app:{application.id}")
    await feed(user=999, text=SECRET)
    action = await pending(db_session)
    await feed(user=999, **({"text": back} if back.startswith("/") else {"callback": back}))
    assert (await db_session.get(BotDialog, "999")).state == "menu"
    await feed(user=999, callback=f"confirm:{action.id}")
    assert application.status == "pending"
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0


async def test_old_approval_button_requests_missing_key(cabinet, db_session, application):
    feed, _ = cabinet
    action = await new_action(db_session, "999", "admin_approve", {"application_id": application.id})
    await db_session.commit()
    await feed(user=999, callback=f"confirm:{action.id}")
    assert (await db_session.get(BotDialog, "999")).data == {"application_id": application.id}
    await feed(user=999, text=SECRET)
    assert (await pending(db_session)).id != action.id


async def test_changed_application_is_not_approved_or_given_a_key(cabinet, db_session, application):
    feed, _ = cabinet
    await feed(user=999, callback=f"admin_app:{application.id}")
    await feed(user=999, text=SECRET)
    action = await pending(db_session)
    application.status = "rejected"
    await db_session.commit()
    await feed(user=999, callback=f"confirm:{action.id}")
    assert application.status == "rejected"
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0


async def test_key_revoked_before_confirmation_does_not_approve(cabinet, db_session, application, monkeypatch):
    feed, _ = cabinet
    await feed(user=999, callback=f"admin_app:{application.id}")
    await feed(user=999, text=SECRET)
    action = await pending(db_session)
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", AsyncMock(return_value=False))
    await feed(user=999, callback=f"confirm:{action.id}")
    assert application.status == "pending" and action.status == "pending"
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0


async def test_already_bound_key_can_be_approved_without_reentry(cabinet, db_session, application):
    feed, _ = cabinet
    credential = await create_provider_credential(
        ProviderCredentialCreate(label="Основной", api_key=SECRET, partner_application_id=application.id), db_session
    )
    await db_session.commit()
    await feed(user=999, callback=f"admin_approve:{application.id}")
    action = await pending(db_session)
    assert action.payload["credential_id"] == credential.id
    await feed(user=999, callback=f"confirm:{action.id}")
    assert application.status == "approved" and credential.partner_id is not None
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 1


async def test_approval_failure_rolls_back_key_binding(cabinet, db_session, application, monkeypatch):
    from fastapi import HTTPException

    feed, _ = cabinet
    await feed(user=999, callback=f"admin_app:{application.id}")
    await feed(user=999, text=SECRET)
    action = await pending(db_session)
    monkeypatch.setattr(
        "app.telegram.admin_applications.approve_application", AsyncMock(side_effect=HTTPException(409, "conflict"))
    )
    await feed(user=999, callback=f"confirm:{action.id}")
    assert application.status == "pending" and action.status == "pending"
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0
