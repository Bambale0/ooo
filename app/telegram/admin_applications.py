"""Provider key entry scoped to an application, followed by one approval."""

from aiogram.exceptions import TelegramAPIError
from fastapi import HTTPException
from sqlalchemy import select

from app.accounts.models import Partner, PartnerApplication
from app.accounts.router import approve_application
from app.infrastructure.config import get_settings
from app.infrastructure.security import decrypt_secret, encrypt_secret
from app.providers.models import ProviderCredential
from app.providers.registry import get_provider_adapter
from app.providers.router import create_provider_credential
from app.providers.schemas import ProviderCredentialCreate
from app.telegram.models import BotAction, BotDialog
from app.telegram.service import is_admin, new_action
from app.telegram.ui import keyboard, show


async def application_for(db, application_id: str, telegram_id: str) -> PartnerApplication:
    if not is_admin(telegram_id):
        raise HTTPException(403, "admin_required")
    application = await db.get(PartnerApplication, application_id, with_for_update=True)
    if not application or application.status != "pending":
        raise HTTPException(409, "application_not_pending")
    return application


async def credential_for(db, application_id: str) -> ProviderCredential | None:
    return (
        await db.scalars(
            select(ProviderCredential).where(
                ProviderCredential.partner_application_id == application_id,
                ProviderCredential.provider == "argolink",
                ProviderCredential.is_active.is_(True),
                ProviderCredential.encrypted_api_key.is_not(None),
            )
        )
    ).one_or_none()


async def cancel_approvals(db, telegram_id: str, application_id: str) -> None:
    actions = await db.scalars(
        select(BotAction).where(
            BotAction.telegram_id == telegram_id,
            BotAction.kind == "admin_approve",
            BotAction.status == "pending",
        )
    )
    for action in actions:
        if action.payload.get("application_id") == application_id:
            action.status = "cancelled"
            action.payload = {"application_id": application_id}


def key_dialog(dialog: BotDialog, application_id: str) -> None:
    dialog.state, dialog.data = "admin_application_key", {"application_id": application_id}


async def request_key(event, db, dialog: BotDialog, application: PartnerApplication) -> None:
    await cancel_approvals(db, str(event.from_user.id), application.id)
    key_dialog(dialog, application.id)
    await show(
        event,
        f"Заявка: {application.company_name}\nПроект: {application.project_name}\n"
        f"Telegram ID: {application.telegram_id}\n\n"
        "Пришлите ключ поставщика отдельным сообщением. Бот проверит его и предложит подтвердить подключение. "
        "Сообщение с ключом будет удалено; ключ хранится только зашифрованным.",
        keyboard(back=f"admin_app:{application.id}"),
    )


async def confirmation(event, db, dialog: BotDialog, application: PartnerApplication, payload: dict) -> None:
    await cancel_approvals(db, str(event.from_user.id), application.id)
    key_dialog(dialog, application.id)
    action = await new_action(
        db, str(event.from_user.id), "admin_approve", {"application_id": application.id, **payload}
    )
    await db.commit()
    await show(
        event,
        f"Подключить {application.company_name}?\nПроект: {application.project_name}\n"
        f"Telegram ID: {application.telegram_id}\n\n"
        "Ключ поставщика проверен. После подтверждения он будет привязан к заявке и аккаунту партнёра. "
        "Для замены пришлите другой ключ.",
        keyboard(("Подтвердить", f"confirm:{action.id}"), back=f"admin_app:{application.id}"),
    )


async def begin_approval(event, db, dialog: BotDialog, application: PartnerApplication) -> None:
    credential = await credential_for(db, application.id)
    if credential is None:
        await request_key(event, db, dialog, application)
    else:
        await confirmation(event, db, dialog, application, {"credential_id": credential.id})


async def receive_key(event, db, dialog: BotDialog, value: str) -> None:
    application = await application_for(db, dialog.data["application_id"], str(event.from_user.id))
    # Delete even invalid secrets; never put message text in dialog/action/error output.
    try:
        await event.delete()
    except TelegramAPIError:
        pass
    if not 8 <= len(value) <= 4096 or any(char.isspace() for char in value):
        raise HTTPException(422, "provider_key_format_invalid")
    settings = get_settings()
    if not settings.provider_credentials_master_key:
        raise HTTPException(503, "provider_credentials_encryption_not_configured")
    adapter = get_provider_adapter("argolink", api_key=value)
    if not await adapter.validate_key(value):
        raise HTTPException(409, "provider_key_invalid")
    encrypted = encrypt_secret(value, settings.provider_credentials_master_key)
    await confirmation(event, db, dialog, application, {"encrypted_api_key": encrypted})


async def approve_with_key(event, db, action: BotAction, telegram_id: str) -> Partner | None:
    application = await application_for(db, action.payload["application_id"], telegram_id)
    encrypted = action.payload.get("encrypted_api_key")
    if encrypted:
        credential = await create_provider_credential(
            ProviderCredentialCreate(
                label="Основной ключ",
                api_key=decrypt_secret(encrypted, get_settings().provider_credentials_master_key),
                partner_application_id=application.id,
            ),
            db,
        )
    else:
        credential = await credential_for(db, application.id)
        if credential is None:
            # Buttons created by the previous bot version lead into key entry too.
            dialog = await db.get(BotDialog, telegram_id)
            await request_key(event, db, dialog, application)
            return None
        if action.payload.get("credential_id", credential.id) != credential.id:
            raise HTTPException(409, "confirmation_expired")
    approved = await approve_application(application.id, db)
    # Once applied, keep the credential reference rather than a second encrypted copy.
    action.payload = {"application_id": application.id, "credential_id": credential.id}
    return approved
