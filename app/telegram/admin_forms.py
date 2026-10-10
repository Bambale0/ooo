"""Guided admin forms with durable, actor-bound confirmations."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.telegram.service import is_admin, new_action
from app.telegram.ui import format_msk, keyboard, show

# field, human prompt; every value is validated again on confirmation.
FORMS = {
    "credential": (
        "Ключ поставщика",
        [
            ("owner_type", "Владелец ключа: application для заявки или partner для партнёра"),
            ("owner_id", "UUID владельца"),
            ("label", "Название ключа"),
            ("api_key", "Ключ ArgoLink. Сообщение будет удалено, ключ сохранится только зашифрованным"),
        ],
    ),
    "revoke_credential": ("Отзыв ключа поставщика", [("credential_id", "UUID ключа поставщика для отзыва")]),
    "gates": (
        "Проверки перед включением модели",
        [
            ("model", "Код модели"),
            ("has_provider_integration", "Интеграция проверена? да / нет"),
            ("has_public_docs", "Публичная документация проверена? да / нет"),
            ("has_successful_smoke", "Все продаваемые режимы прошли live smoke с подтверждённой ценой? да / нет"),
        ],
    ),
    "capability": (
        "Режим поставщика",
        [
            ("model_slug", "Код модели"),
            ("mode", "Режим, например default"),
            ("resolution", "Разрешение"),
            ("is_active", "Включить проверенный режим? да / нет"),
        ],
    ),
    "withdrawal": (
        "Запись вывода / коррекции",
        [
            ("partner_id", "UUID владельца записи"),
            ("amount_usdt", "Сумма USDT; для коррекции отрицательная"),
            ("reason", "Причина (от 10 символов). Записывается уже совершённая операция, деньги бот не переводит"),
            ("correction_for_id", "UUID исправляемой записи или «-»"),
            ("override_reason", "Причина превышения safe-to-withdraw (от 10 символов) или «-»"),
        ],
    ),
    "refund": (
        "Подтверждённый возврат",
        [
            ("payment_id", "UUID платежа"),
            ("amount_rub", "Подтверждённая сумма возврата в рублях"),
            ("reason", "Причина. Фиксируйте только уже подтверждённый возврат; перевод денег здесь не выполняется"),
        ],
    ),
    "adjustment": (
        "Коррекция баланса",
        [
            ("partner_id", "UUID партнёра"),
            ("amount_rub", "Сумма RUB со знаком"),
            ("description", "Причина корректировки"),
        ],
    ),
    "price": (
        "Цена конфигурации",
        [
            ("model_slug", "Код модели, как в каталоге"),
            ("mode", "Режим, например default или input_tokens"),
            ("resolution", "Разрешение, например 720p, 1K или default"),
            ("billing_unit", "Единица: second, generation или million_tokens"),
            ("price_rub", "Розничная цена за единицу в RUB"),
            ("provider_cost_usdt", "Проверенная закупочная цена за ту же единицу в USDT"),
        ],
    ),
    "threshold": (
        "Порог низкой маржи",
        [
            ("model", "Код модели или «-» для глобального порога"),
            ("mode", "Режим или «-» для всей модели / системы"),
            ("resolution", "Разрешение или «-», если выбран общий порог"),
            ("value", "Порог от 0 до 100 процентов или «-» для удаления override"),
        ],
    ),
    "model_status": (
        "Доступность модели",
        [
            ("model", "Код модели"),
            ("status", "production для включения, restricted для скрытой или disabled для отключения"),
        ],
    ),
    "model_grant": (
        "Выдать restricted-модель партнёру",
        [
            ("model", "Код restricted-модели"),
            ("partner_id", "UUID партнёра, которому выдаётся доступ"),
            ("reason", "Основание выдачи (от 10 символов)"),
        ],
    ),
    "model_revoke": (
        "Отозвать restricted-модель",
        [
            ("model", "Код restricted-модели"),
            ("partner_id", "UUID партнёра, у которого отзывается доступ"),
            ("reason", "Основание отзыва (от 10 символов)"),
        ],
    ),
    "invoice_reconcile": (
        "Сверка создания счёта",
        [
            ("payment_id", "UUID нашего платежа"),
            ("provider_invoice_id", "Номер счёта Crypto Pay"),
            ("reason", "Основание сверки (от 10 символов)"),
        ],
    ),
    "generation_reconcile": (
        "Сверка генерации",
        [
            ("generation_id", "UUID нашей генерации"),
            ("outcome", "Результат проверки: completed, not_accepted или attach_video"),
            ("provider_task_id", "ID задачи поставщика для attach_video, иначе «-»"),
            ("units", 'Фактические единицы в JSON, например {"seconds": 6}; для not_accepted / attach_video — «-»'),
            ("reason", "Основание проверки (от 10 символов). Не подтверждайте отсутствие задачи без доказательств"),
        ],
    ),
}


async def menu(event):
    await show(
        event,
        "Операции администратора. Каждое изменение требует отдельного подтверждения.",
        keyboard(
            *[(label, f"admin_form:{name}") for name, (label, _) in FORMS.items()],
            ("Каталог и история цен", "admin_catalog:0"),
            ("История порогов", "admin_threshold_history:0"),
            ("Неопределённые генерации", "admin_reconcile_list"),
            ("Ключи поставщика", "admin_credentials:0"),
            ("Импорт reviewed каталога", "admin_catalog_import"),
            back="admin_menu",
        ),
    )


async def start(event, db, dialog, name):
    if name not in FORMS:
        raise HTTPException(404, "unknown_form")
    if name == "adjustment":
        from app.telegram.admin_partners import start_picker

        await start_picker(event, db, dialog)
        return
    dialog.state, dialog.data = "admin_form_input", {"form": name, "values": {}, "index": 0}
    await show(event, FORMS[name][1][0][1])


async def input_value(event, db, dialog, value):
    name, fields = dialog.data["form"], FORMS[dialog.data["form"]][1]
    index = dialog.data["index"]
    if not 1 <= len(value) <= 2000:
        raise ValueError("invalid_form_value")
    values = {**dialog.data["values"], fields[index][0]: None if value == "-" else value}
    index += 1
    if index < len(fields):
        dialog.data = {"form": name, "values": values, "index": index}
        await show(event, fields[index][1])
        return
    # Validate before displaying a concrete confirmation; financial rules are rechecked at execution.
    validate(name, values, "telegram-preview")
    if name == "credential":
        from aiogram.exceptions import TelegramBadRequest

        from app.infrastructure.config import get_settings
        from app.infrastructure.security import encrypt_secret

        values["api_key"] = encrypt_secret(values["api_key"], get_settings().provider_credentials_master_key)
        try:
            await event.delete()
        except TelegramBadRequest:
            pass
    detail = "\n".join(
        f"{prompt.split(';')[0]}: {'[скрыт]' if key == 'api_key' else values[key] if values[key] is not None else '—'}"
        for key, prompt in fields
    )
    if name == "adjustment":
        from app.telegram.admin_partners import recipient_details

        recipient = await recipient_details(db, values["partner_id"])
        detail = f"{recipient}\nСумма RUB со знаком: {values['amount_rub']}\nПричина: {values['description']}"
    action = await new_action(db, str(event.from_user.id), "admin_form", {"form": name, "values": values})
    dialog.state, dialog.data = "menu", {}
    await db.commit()
    await show(
        event, f"{FORMS[name][0]}\n\n{detail}\n\nПодтвердить?", keyboard(("Подтвердить", f"confirm:{action.id}"))
    )


def validate(name, values, idem):
    import json

    from app.billing.schemas import ManualAdjustmentCreate, ProfitWithdrawalCreate
    from app.catalog.schemas import ModelEnableGateUpdate, PartnerPriceUpsert
    from app.inference.admin import Reconciliation
    from app.payments.schemas import PaymentReconcileCreate, PaymentRefundCreate
    from app.providers.schemas import ProviderCapabilityUpsert, ProviderCredentialCreate

    for key in ("partner_id", "payment_id", "generation_id", "correction_for_id", "owner_id", "credential_id"):
        if values.get(key):
            UUID(values[key])

    def boolean(value):
        if value not in {"да", "нет"}:
            raise ValueError("boolean_required")
        return value == "да"

    if name == "credential":
        if values["owner_type"] not in {"application", "partner"}:
            raise ValueError("invalid_owner")
        owner = "partner_application_id" if values["owner_type"] == "application" else "partner_id"
        return ProviderCredentialCreate(label=values["label"], api_key=values["api_key"], **{owner: values["owner_id"]})
    if name == "revoke_credential":
        return str(UUID(values["credential_id"]))
    if name == "gates":
        return ModelEnableGateUpdate(**{k: boolean(v) for k, v in values.items() if k != "model"})
    if name == "capability":
        return ProviderCapabilityUpsert(**{**values, "is_active": boolean(values["is_active"])})
    if name == "withdrawal":
        return ProfitWithdrawalCreate(**values, idempotency_key=idem)
    if name == "refund":
        return PaymentRefundCreate(**{k: v for k, v in values.items() if k != "payment_id"}, idempotency_key=idem)
    if name == "adjustment":
        if not values.get("description"):
            raise ValueError("reason_required")
        return ManualAdjustmentCreate(**values, idempotency_key=idem)
    if name == "price":
        return PartnerPriceUpsert(**values)
    if name == "threshold":
        from app.billing.schemas import MarginThresholdCreate

        return MarginThresholdCreate(**values)
    if name == "invoice_reconcile":
        return PaymentReconcileCreate(**{k: v for k, v in values.items() if k != "payment_id"})
    if name == "generation_reconcile":
        data = {k: v for k, v in values.items() if k != "generation_id"}
        data["units"] = json.loads(data["units"]) if data["units"] else None
        return Reconciliation(**data)
    if name in {"model_grant", "model_revoke"} and values.get("model") and values.get("partner_id"):
        # A tracked reason is mandatory: access to a restricted model must never
        # be switched on by a bare toggle with no accountable justification.
        if len(values.get("reason") or "") < 10:
            raise ValueError("reason_required")
        return values
    if name == "model_status" and values.get("model") and values["status"] in {"production", "restricted", "disabled"}:
        return values
    raise ValueError("invalid_form")


async def execute(db, action, actor):
    if not is_admin(actor):
        raise HTTPException(403, "admin_required")
    name, values = action.payload["form"], action.payload["values"]
    if name == "credential":
        from app.infrastructure.config import get_settings
        from app.infrastructure.security import decrypt_secret

        values = {
            **values,
            "api_key": decrypt_secret(values["api_key"], get_settings().provider_credentials_master_key),
        }
    payload = validate(name, values, f"telegram:{action.id}")
    if name == "credential":
        from app.providers.router import create_provider_credential

        await create_provider_credential(payload, db)
    elif name == "revoke_credential":
        from app.providers.router import revoke_provider_credential

        await revoke_provider_credential(payload, db)
    elif name == "gates":
        from app.catalog.router import update_model_enable_gates

        await update_model_enable_gates(values["model"], payload, db)
    elif name == "capability":
        from app.providers.router import upsert_provider_capability

        await upsert_provider_capability(payload, db)
    elif name == "withdrawal":
        from app.billing.router import create_profit_withdrawal

        await create_profit_withdrawal(payload, db)
    elif name == "refund":
        from app.payments.router import confirm_refund

        await confirm_refund(values["payment_id"], payload, db)
    elif name == "adjustment":
        from app.billing.router import create_manual_adjustment

        await create_manual_adjustment(payload, db)
    elif name == "price":
        from app.catalog.router import upsert_price

        await upsert_price(payload, db)
    elif name == "threshold":
        from app.billing.margins import set_threshold

        await set_threshold(db, actor=actor, **payload.model_dump())
    elif name == "invoice_reconcile":
        from app.payments.router import reconcile_invoice

        await reconcile_invoice(values["payment_id"], payload, db)
    elif name == "generation_reconcile":
        from app.inference.admin import reconcile

        await reconcile(values["generation_id"], payload, db)
    elif name == "model_status":
        from app.catalog.models import Model
        from app.catalog.router import enable_model, enable_restricted_model

        if values["status"] == "production":
            await enable_model(values["model"], db)
        elif values["status"] == "restricted":
            await enable_restricted_model(values["model"], db)
        else:
            model = (await db.execute(select(Model).where(Model.slug == values["model"]))).scalar_one_or_none()
            if not model:
                raise HTTPException(404, "model_not_found")
            model.status = "disabled"
    elif name in {"model_grant", "model_revoke"}:
        from app.catalog.access import grant_model_access, revoke_model_access

        action = grant_model_access if name == "model_grant" else revoke_model_access
        await action(
            db,
            model_slug=values["model"],
            partner_id=values["partner_id"],
            reason=values["reason"],
            actor=actor,
        )


async def history(event, db, data):
    from app.billing.models import MarginThresholdHistory
    from app.catalog.models import Model, PartnerPrice, PartnerPriceHistory
    from app.telegram.handlers import navigation, page_number

    if data == "admin_reconcile_list":
        from app.inference.admin import pending

        rows = await pending(db)
        await show(
            event,
            "Генерации на сверку:\n"
            + ("\n".join(f"{r['id']} · {r['model']} · {r['status']}" for r in rows[:20]) or "Нет задач."),
            keyboard(("Выполнить сверку", "admin_form:generation_reconcile"), back="admin_ops"),
        )
        return
    page = page_number(data)
    buttons = []
    if data.startswith("admin_credentials:"):
        from app.providers.models import ProviderCredential

        rows = list(
            (
                await db.execute(
                    select(ProviderCredential).order_by(ProviderCredential.created_at.desc()).offset(page * 6).limit(7)
                )
            ).scalars()
        )
        lines = [
            f"{r.id}\n{r.label}: {'активен' if r.is_active else 'отозван'}, "
            f"владелец {r.partner_id or r.partner_application_id}"
            for r in rows[:6]
        ]
        navigation(buttons, "admin_credentials", page, len(rows) > 6)
    elif data.startswith("admin_threshold_history:"):
        rows = list(
            (
                await db.execute(
                    select(MarginThresholdHistory).order_by(MarginThresholdHistory.id.desc()).offset(page * 8).limit(9)
                )
            ).scalars()
        )
        lines = [
            f"{format_msk(r.created_at, '%d.%m %H:%M')} {r.scope}\n{r.old_value} → {r.new_value}, автор {r.actor}"
            for r in rows[:8]
        ]
        navigation(buttons, "admin_threshold_history", page, len(rows) > 8)
    else:
        rows = list((await db.execute(select(Model).order_by(Model.slug).offset(page * 3).limit(4))).scalars())
        lines = []
        for model in rows[:3]:
            prices = (await db.execute(select(PartnerPrice).where(PartnerPrice.model_id == model.id))).scalars()
            lines.append(f"{model.slug}: {model.status}")
            lines.extend(
                f"{p.mode}/{p.resolution}: {p.price_rub} ₽, закупка {p.provider_cost_usdt} USDT/{p.billing_unit}"
                for p in prices
            )
            last = (
                await db.execute(
                    select(PartnerPriceHistory)
                    .where(PartnerPriceHistory.model_id == model.id)
                    .order_by(PartnerPriceHistory.created_at.desc())
                    .limit(3)
                )
            ).scalars()
            lines.extend(
                f"История {format_msk(p.created_at, '%d.%m')}: {p.old_price_rub} → {p.new_price_rub} ₽"
                for p in last
            )
        navigation(buttons, "admin_catalog", page, len(rows) > 3)
    await show(event, "\n".join(lines) or "Записей нет.", keyboard(*buttons, back="admin_ops"))
