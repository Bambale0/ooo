"""Administrator UX for replenishing the encrypted ArgoLink registration pool."""

import json

from aiogram.exceptions import TelegramBadRequest
from fastapi import HTTPException

from app.infrastructure.config import get_settings
from app.infrastructure.security import decrypt_secret, encrypt_secret
from app.providers.key_pool import (
    add_pool_keys,
    parse_admin_key_lines,
    pool_counts,
    validate_new_pool_keys,
)
from app.telegram.service import is_admin, new_action
from app.telegram.ui import keyboard, show


async def show_pool(event, db, dialog) -> None:
    dialog.state, dialog.data = "menu", {}
    free, assigned = await pool_counts(db)
    await show(
        event,
        f"Пул ключей ArgoLink для новых регистраций\n\nСвободно: {free}\nВыдано: {assigned}\n\n"
        "Секреты и префиксы здесь никогда не отображаются.",
        keyboard(("Добавить ключи", "admin_argolink_pool_add"), back="admin_menu"),
    )


async def prompt_add(event, dialog) -> None:
    dialog.state, dialog.data = "admin_argolink_pool_add", {}
    await show(
        event,
        "Отправьте один или несколько заранее выпущенных unrestricted prepaid ключей ArgoLink — "
        "по одному на строке.\n\n"
        "Сообщение будет удалено, каждый ключ проверен без платной генерации, а затем сохранён только зашифрованным. "
        "Если хотя бы один ключ неверен или уже добавлен, весь список будет отклонён.",
        keyboard(back="admin_argolink_pool"),
    )


async def accept_keys(event, db, dialog, value: str) -> None:
    if not is_admin(str(event.from_user.id)):
        raise HTTPException(403, "admin_required")
    try:
        await event.delete()
    except TelegramBadRequest:
        pass
    keys = parse_admin_key_lines(value)
    await validate_new_pool_keys(db, keys)
    master_key = get_settings().provider_credentials_master_key
    if not master_key:
        raise HTTPException(503, "provider_credentials_encryption_not_configured")
    encrypted = encrypt_secret(json.dumps(keys), master_key)
    action = await new_action(
        db,
        str(event.from_user.id),
        "admin_argolink_pool_add",
        {"encrypted_keys": encrypted, "count": len(keys)},
    )
    dialog.state, dialog.data = "menu", {}
    await db.commit()
    await show(
        event,
        f"Проверено ключей: {len(keys)}. Добавить их в пул? Значения ключей не отображаются.",
        keyboard(("Подтвердить", f"confirm:{action.id}"), back="admin_argolink_pool"),
    )


async def execute_add_action(db, action, actor: str) -> int:
    if not is_admin(actor):
        raise HTTPException(403, "admin_required")
    master_key = get_settings().provider_credentials_master_key
    if not master_key:
        raise HTTPException(503, "provider_credentials_encryption_not_configured")
    try:
        keys = json.loads(decrypt_secret(action.payload["encrypted_keys"], master_key))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "provider_key_pool_input_invalid") from exc
    if (
        not isinstance(keys, list)
        or len(keys) != action.payload.get("count")
        or not all(isinstance(x, str) for x in keys)
    ):
        raise HTTPException(422, "provider_key_pool_input_invalid")
    added = await add_pool_keys(db, keys, source="telegram_admin", actor=f"telegram:{actor}")
    # Keep the durable audit count, but discard the now-redundant ciphertext
    # after the pool rows have been staged in the same transaction.
    action.payload = {"count": len(keys), "added": added}
    return added
