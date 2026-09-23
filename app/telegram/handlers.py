"""Cabinet navigation and conversational input. No raw SQL/user markup."""

from uuid import UUID

from aiogram.types import FSInputFile
from fastapi import HTTPException
from sqlalchemy import select

from app.accounts.models import ApiKey, PartnerApplication
from app.accounts.router import submit_application, update_partner_api_key_webhook
from app.accounts.schemas import ApiKeyWebhookUpdate, PartnerApplicationCreate
from app.billing.models import LedgerEntry
from app.billing.safe_to_withdraw import calculate_safe_to_withdraw
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.payments.crypto_pay import get_crypto_pay_client
from app.payments.models import PaymentInvoice
from app.payments.service import apply_paid_provider_invoice, create_or_resume_invoice, get_partner_payment
from app.support.models import SupportAttachment, SupportTicket
from app.support.service import append_message, attachment_for, get_ticket, ticket_messages
from app.telegram.actions import confirm_action
from app.telegram.service import is_admin, new_action, notify, partner_for
from app.telegram.ui import keyboard, show, status_label


def actor(event) -> str:
    return str(event.from_user.id)


def reset(dialog) -> None:
    dialog.state, dialog.data = "menu", {}


async def ask_confirmation(event, db, kind: str, payload: dict, label: str) -> None:
    action = await new_action(db, actor(event), kind, payload)
    await db.commit()
    await show(event, label, keyboard(("Подтвердить", f"confirm:{action.id}")))


async def home(event, db, dialog) -> None:
    reset(dialog)
    partner = await partner_for(db, actor(event))
    if partner:
        buttons = [
            ("Баланс и пополнение", "balance"),
            ("API-ключи", "api_keys:0"),
            ("История и поиск", "history:0"),
            ("Поддержка", "support"),
            ("Документация и аккаунт", "settings"),
        ]
        if is_admin(actor(event)):
            buttons.append(("Администратор", "admin_menu"))
        await show(event, f"Кабинет\nБаланс: {partner.balance_rub:.2f} ₽", keyboard(*buttons, back=None))
    else:
        pending = (
            (
                await db.execute(
                    select(PartnerApplication).where(
                        PartnerApplication.telegram_id == actor(event), PartnerApplication.status == "pending"
                    )
                )
            )
            .scalars()
            .first()
        )
        buttons = [("Подать заявку", "register")]
        if is_admin(actor(event)):
            buttons.append(("Администратор", "admin_menu"))
        await show(
            event,
            "Заявка рассматривается. Сообщим о решении здесь."
            if pending
            else "Для доступа к API подайте заявку на подключение.",
            keyboard(*buttons, back=None),
        )


async def handle_callback(event, db, dialog) -> None:
    data = event.data or ""
    user = actor(event)
    if data.startswith("admin_") and not is_admin(user):
        raise HTTPException(403, "admin_required")
    if data == "main_menu":
        await home(event, db, dialog)
        return
    if data.startswith("confirm:"):
        reset(dialog)
        await confirm_action(event, db, user, str(UUID(data.split(":", 1)[1])))
        return
    if data == "register":
        settings = get_settings()
        if not settings.terms_url or not settings.privacy_policy_url:
            await show(event, "Приём заявок ещё не открыт: документы подключения готовятся.")
            return
        dialog.state = "consent"
        dialog.data = {"legal_version": settings.legal_document_version}
        await show(
            event,
            "Ознакомьтесь с условиями и политикой. Нажимая «Принимаю», вы подтверждаете согласие с обоими документами.",
            keyboard(
                ("Условия", settings.terms_url),
                ("Политика конфиденциальности", settings.privacy_policy_url),
                ("Принимаю", "accept_legal"),
            ),
        )
        return
    if data == "accept_legal":
        if dialog.state != "consent" or dialog.data.get("legal_version") != get_settings().legal_document_version:
            raise HTTPException(409, "consent_required")
        dialog.state = "register_company"
        await show(event, "Укажите название компании (2–255 символов).")
        return
    if data.startswith("admin_"):
        await admin_callback(event, db, dialog, data)
        return
    partner = await partner_for(db, user)
    if not partner:
        raise HTTPException(403, "account_unavailable")
    if data == "balance":
        reset(dialog)
        await show(
            event,
            f"Баланс: {partner.balance_rub:.2f} ₽\nПополнение: от 1 000 ₽, USDT или TON. "
            "Зачисление после проверки оплаты администратором.",
            keyboard(("Пополнить", "topup"), ("Мои счета", "payments:0")),
        )
    elif data == "topup":
        dialog.state, dialog.data = "topup", {}
        await show(event, "Введите целую сумму в рублях, от 1 000 ₽.")
    elif data.startswith("payments:"):
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(PaymentInvoice)
                    .where(PaymentInvoice.partner_id == partner.id)
                    .order_by(PaymentInvoice.created_at.desc())
                    .offset(page * 4)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        buttons = [(f"{r.requested_rub:.2f} ₽ · {status_label(r.status)}", f"payment:{r.id}") for r in rows[:4]]
        navigation(buttons, "payments", page, len(rows) > 4)
        await show(event, "Ваши счета" if rows else "Счетов пока нет.", keyboard(*buttons))
    elif data.startswith("payment:"):
        payment = await get_partner_payment(db, payment_id=str(UUID(data.split(":")[1])), partner_id=partner.id)
        if payment.status in {"creating", "creation_unknown"}:
            payment = await create_or_resume_invoice(
                db,
                partner=partner,
                requested_rub=int(payment.requested_rub),
                idempotency_key=payment.idempotency_key,
                client=get_crypto_pay_client(),
            )
        if payment.provider_invoice_id and payment.status in {"active", "expired", "creating"}:
            provider = await get_crypto_pay_client().get_invoice(payment.provider_invoice_id)
            if provider and provider.status == "paid":
                payment = await apply_paid_provider_invoice(db, provider_invoice=provider)
                await notify(
                    db,
                    get_settings().admin_telegram_id,
                    f"Оплачен счёт {payment.id}. Проверьте раздел «Платежи».",
                    f"payment-paid:{payment.id}",
                )
        buttons = [("Обновить", f"payment:{payment.id}")]
        if payment.status == "active":
            if payment.invoice_url:
                buttons.append(("Оплатить", payment.invoice_url))
            buttons.append(("Отменить счёт", f"payment_cancel:{payment.id}"))
        await show(
            event,
            f"Счёт {payment.id}\n{payment.requested_rub:.2f} ₽\n{status_label(payment.status)}",
            keyboard(*buttons),
        )
    elif data.startswith("payment_cancel:"):
        payment = await get_partner_payment(db, payment_id=str(UUID(data.split(":")[1])), partner_id=partner.id)
        await ask_confirmation(
            event,
            db,
            "invoice_cancel",
            {"partner_id": partner.id, "payment_id": payment.id},
            f"Отменить счёт на {payment.requested_rub:.2f} ₽?",
        )
    elif data.startswith("history:"):
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(LedgerEntry)
                    .where(LedgerEntry.partner_id == partner.id)
                    .order_by(LedgerEntry.created_at.desc())
                    .offset(page * 8)
                    .limit(9)
                )
            )
            .scalars()
            .all()
        )
        labels = {
            "generation_reserve": "Резерв",
            "generation_charge": "Генерация",
            "payment_credit": "Пополнение",
            "generation_refund": "Возврат резерва",
        }
        lines = [
            f"{r.created_at:%d.%m %H:%M} · {labels.get(r.operation_type, 'Операция')} · {r.amount_rub:+.2f} ₽"
            for r in rows[:8]
        ]
        buttons = [("Поиск по UUID", "search_prompt")]
        navigation(buttons, "history", page, len(rows) > 8)
        await show(event, "История\n\n" + ("\n".join(lines) or "Операций пока нет."), keyboard(*buttons))
    elif data == "search_prompt":
        dialog.state, dialog.data = "search", {}
        await show(event, "Отправьте UUID генерации или платежа.")
    elif data.startswith("api_keys:"):
        reset(dialog)
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(ApiKey)
                    .where(ApiKey.partner_id == partner.id, ApiKey.is_active.is_(True))
                    .order_by(ApiKey.created_at.desc())
                    .offset(page * 4)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        buttons = [(f"{r.name[:40]} · {r.key_prefix}…", f"key:{r.id}") for r in rows[:4]]
        buttons.append(("Создать ключ", "key_create"))
        navigation(buttons, "api_keys", page, len(rows) > 4)
        await show(event, "Действующие API-ключи" if rows else "Действующих ключей пока нет.", keyboard(*buttons))
    elif data == "key_create":
        dialog.state, dialog.data = "key_name", {}
        await show(event, "Введите название ключа (2–120 символов).")
    elif data.startswith(("key:", "key_revoke:", "key_webhook:")):
        key = await db.get(ApiKey, str(UUID(data.split(":")[1])))
        if not key or key.partner_id != partner.id or not key.is_active:
            raise HTTPException(404, "api_key_not_found")
        if data.startswith("key_revoke:"):
            await ask_confirmation(
                event,
                db,
                "key_revoke",
                {"partner_id": partner.id, "key_id": key.id},
                f"Отозвать ключ «{key.name}»? Приложения с этим ключом потеряют доступ.",
            )
        elif data.startswith("key_webhook:"):
            dialog.state, dialog.data = "webhook", {"key_id": key.id}
            await show(
                event,
                "Отправьте публичный HTTPS-адрес webhook или «выключить». Секрет подписи будет создан автоматически.",
            )
        else:
            await show(
                event,
                f"{key.name}\n{key.key_prefix}…\nWebhook: {key.webhook_url or 'не задан'}",
                keyboard(("Webhook", f"key_webhook:{key.id}"), ("Отозвать", f"key_revoke:{key.id}"), back="api_keys:0"),
            )
    elif data == "settings":
        await show(event, "Документация и аккаунт", keyboard(("Документация", "docs"), ("Удалить аккаунт", "delete")))
    elif data == "docs":
        await show(event, get_settings().public_api_base_url.rstrip("/") + "/guide")
    elif data == "delete":
        await ask_confirmation(
            event,
            db,
            "delete",
            {"partner_id": partner.id},
            f"Удалить аккаунт? Ключи будут отключены, остаток {partner.balance_rub:.2f} ₽ невозвратный. "
            "Выполняющиеся задачи будут завершены и оплачены. История обязательных операций сохраняется.",
        )
    elif data.startswith(("support", "ticket:", "attachment:")):
        await support_callback(event, db, dialog, data, partner)
    else:
        await home(event, db, dialog)


def page_number(data: str) -> int:
    value = data.rsplit(":", 1)[1]
    if not value.isascii() or not value.isdigit() or len(value) > 6:
        raise ValueError("invalid page")
    return int(value)


def navigation(buttons: list, prefix: str, page: int, more: bool) -> None:
    if page:
        buttons.append(("← Предыдущие", f"{prefix}:{page - 1}"))
    if more:
        buttons.append(("Следующие →", f"{prefix}:{page + 1}"))


async def support_callback(event, db, dialog, data: str, partner=None) -> None:
    admin = is_admin(actor(event)) and data.startswith("admin_")
    if data in {"support", "admin_support"} or data.startswith(("support_page:", "admin_support_page:")):
        page = page_number(data) if ":" in data else 0
        query = select(SupportTicket)
        if not admin:
            query = query.where(SupportTicket.partner_id == partner.id)
        rows = (
            (await db.execute(query.order_by(SupportTicket.created_at.desc()).offset(page * 4).limit(5)))
            .scalars()
            .all()
        )
        buttons = [
            (f"{status_label(t.status)} · {t.subject[:38]}", f"{'admin_' if admin else ''}ticket:{t.id}:0")
            for t in rows[:4]
        ]
        if not admin:
            buttons.append(("Новое обращение", "support_new"))
        navigation(buttons, "admin_support_page" if admin else "support_page", page, len(rows) > 4)
        reset(dialog)
        await show(event, "Обращения в поддержку" if rows else "Обращений пока нет.", keyboard(*buttons))
    elif data == "support_new":
        dialog.state, dialog.data = "support_subject", {}
        await show(
            event,
            "Кратко опишите тему обращения (2–200 символов). Далее можно отправить сообщения и файлы до 20 МБ каждый.",
        )
    elif data.startswith(("ticket:", "admin_ticket:")):
        _, ticket_id, page_value = data.split(":")
        ticket = await get_ticket(db, str(UUID(ticket_id)), actor(event))
        page = page_number("page:" + page_value)
        rows = await ticket_messages(db, ticket.id, page * 4)
        lines = [f"Обращение {ticket.id}\n{ticket.subject}\n{status_label(ticket.status)}"]
        buttons = []
        for row in reversed(rows[:4]):
            lines.append(f"{'Поддержка' if row.sender_type == 'admin' else 'Вы'}: {row.text[:650]}")
            attachments = (
                (await db.execute(select(SupportAttachment).where(SupportAttachment.message_id == row.id)))
                .scalars()
                .all()
            )
            buttons.extend(
                (f"Файл: {item.file_name[:35]}", f"{'admin_' if is_admin(actor(event)) else ''}attachment:{item.id}")
                for item in attachments
            )
        prefix = "admin_ticket" if is_admin(actor(event)) else "ticket"
        if page:
            buttons.append(("Новые сообщения", f"{prefix}:{ticket.id}:{page - 1}"))
        if len(rows) > 4:
            buttons.append(("Ранее", f"{prefix}:{ticket.id}:{page + 1}"))
        if ticket.status != "closed":
            dialog.state, dialog.data = (
                "admin_support_reply" if is_admin(actor(event)) else "support_reply",
                {"ticket_id": ticket.id},
            )
            lines.append("Отправьте сообщение или файл, чтобы ответить.")
            if is_admin(actor(event)):
                buttons.append(("Закрыть обращение", f"admin_ticket_close:{ticket.id}"))
        else:
            reset(dialog)
        await show(
            event, "\n\n".join(lines), keyboard(*buttons, back="admin_support" if is_admin(actor(event)) else "support")
        )
    elif data.startswith(("attachment:", "admin_attachment:")):
        attachment = await attachment_for(db, str(UUID(data.split(":")[1])), actor(event))
        await event.message.answer_document(FSInputFile(attachment.storage_path, filename=attachment.file_name))
    elif data.startswith("admin_ticket_close:"):
        ticket = await get_ticket(db, str(UUID(data.split(":")[1])), actor(event))
        await ask_confirmation(
            event,
            db,
            "admin_ticket_close",
            {"ticket_id": ticket.id},
            "Закрыть обращение? Для следующего вопроса потребуется новое.",
        )


async def admin_callback(event, db, dialog, data: str) -> None:
    if data == "admin_menu":
        reset(dialog)
        await show(
            event,
            "Администрирование",
            keyboard(
                ("Заявки", "admin_apps:0"),
                ("Платежи", "admin_payments:0"),
                ("Казначейство", "admin_stw"),
                ("Поддержка", "admin_support"),
                ("Управление аккаунтом", "admin_account"),
                ("Состояние", "admin_health"),
            ),
        )
    elif data.startswith("admin_apps:"):
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(PartnerApplication)
                    .where(PartnerApplication.status == "pending")
                    .order_by(PartnerApplication.created_at)
                    .offset(page * 4)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        buttons = [(r.company_name[:45], f"admin_app:{r.id}") for r in rows[:4]]
        navigation(buttons, "admin_apps", page, len(rows) > 4)
        await show(
            event, "Заявки на подключение" if rows else "Новых заявок нет.", keyboard(*buttons, back="admin_menu")
        )
    elif data.startswith(("admin_app:", "admin_approve:", "admin_reject:")):
        app = await db.get(PartnerApplication, str(UUID(data.split(":")[1])))
        if not app or app.status != "pending":
            raise HTTPException(404, "application_not_found")
        if data.startswith("admin_approve:"):
            await ask_confirmation(
                event,
                db,
                "admin_approve",
                {"application_id": app.id},
                f"Подключить {app.company_name}? Ключ поставщика должен быть заранее привязан к заявке.",
            )
        elif data.startswith("admin_reject:"):
            dialog.state, dialog.data = "admin_reject", {"application_id": app.id}
            await show(event, "Введите причину отказа — она будет отправлена заявителю.")
        else:
            await show(
                event,
                f"Заявка {app.id}\n{app.company_name}\n{app.project_name}\nTelegram ID: {app.telegram_id}",
                keyboard(
                    ("Одобрить", f"admin_approve:{app.id}"),
                    ("Отклонить", f"admin_reject:{app.id}"),
                    back="admin_apps:0",
                ),
            )
    elif data.startswith("admin_payments:"):
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(PaymentInvoice)
                    .where(PaymentInvoice.status == "paid_waiting_credit")
                    .order_by(PaymentInvoice.created_at)
                    .offset(page * 4)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        buttons = [(f"{r.requested_rub:.2f} ₽ · {r.id[:8]}", f"admin_credit:{r.id}") for r in rows[:4]]
        navigation(buttons, "admin_payments", page, len(rows) > 4)
        await show(
            event,
            "Оплаченные счета на зачисление" if rows else "Нет счетов на зачисление.",
            keyboard(*buttons, back="admin_menu"),
        )
    elif data.startswith("admin_credit:"):
        payment = await db.get(PaymentInvoice, str(UUID(data.split(":")[1])))
        if not payment:
            raise HTTPException(404, "payment_not_found")
        await ask_confirmation(
            event,
            db,
            "admin_credit",
            {"payment_id": payment.id},
            f"Зачислить {payment.requested_rub:.2f} ₽?\nПартнёр: {payment.partner_id}\n"
            f"Платёж: {payment.id}\n{status_label(payment.status)}",
        )
    elif data == "admin_stw":
        state = await calculate_safe_to_withdraw(db)
        value = state["safe_to_withdraw_usdt"]
        freshness = "актуальные данные" if state["freshness"] == "fresh" else "данные устарели или недоступны"
        amount = format(value, ".2f") + " USDT" if value is not None else "нет подтверждённых данных"
        await show(
            event,
            f"Доступно к выводу: {amount}\n"
            f"Кошелёк: {freshness}\n"
            f"Возраст: {int(state.get('wallet_age_seconds') or 0)} с",
            keyboard(("Курс RUB/USDT", "admin_fx"), ("Заглушить инцидент", "admin_mute"), back="admin_menu"),
        )
    elif data == "admin_fx":
        from app.billing.fx import current_fx

        fx = await current_fx(db)
        dialog.state, dialog.data = "admin_fx", {}
        await show(
            event,
            f"Курс: {fx['rate']} ₽/USDT. Источник: {fx['source']}.\n"
            "Введите ручной fallback-курс или «выключить». Автоматический курс имеет приоритет.",
        )
    elif data == "admin_mute":
        from app.billing.models import FinancialIncident

        incident = await db.get(FinancialIncident, "treasury")
        if not incident:
            await show(event, "Открытых инцидентов нет.")
        else:
            await ask_confirmation(
                event,
                db,
                "admin_mute",
                {"episode": incident.episode},
                "Остановить уведомления по текущему инциденту? При новом дефиците они включатся снова.",
            )
    elif data == "admin_health":
        import json

        from app.health.router import readiness

        result = json.loads((await readiness(db)).body)
        await show(
            event,
            "Состояние сервиса\n" + "\n".join(f"{k}: {v}" for k, v in result["checks"].items()),
            keyboard(back="admin_menu"),
        )
    elif data == "admin_account":
        dialog.state, dialog.data = "admin_account", {}
        await show(event, "Введите UUID партнёра для изменения доступа или переноса Telegram ID.")
    elif data.startswith(("admin_disable:", "admin_enable:", "admin_transfer:")):
        partner_id = str(UUID(data.split(":")[1]))
        dialog.state, dialog.data = (
            "admin_transfer" if data.startswith("admin_transfer:") else "admin_status",
            {"partner_id": partner_id, "enabled": data.startswith("admin_enable:")},
        )
        await show(
            event,
            "Введите новый Telegram ID и причину переноса через пробел. Подтвердите личность владельца до переноса."
            if dialog.state == "admin_transfer"
            else "Введите причину изменения доступа.",
        )
    elif data.startswith(("admin_support", "admin_ticket", "admin_attachment")):
        await support_callback(event, db, dialog, data)


async def handle_message(event, db, dialog) -> None:
    value = (event.text or "").strip()
    if value.split(" ", 1)[0].split("@", 1)[0] in {"/start", "/cancel"}:
        await home(event, db, dialog)
        return
    user = actor(event)
    state = dialog.state
    if state.startswith("admin_") and not is_admin(user):
        reset(dialog)
        raise HTTPException(403, "admin_required")
    if state == "register_company":
        if not 2 <= len(value) <= 255:
            raise ValueError("company length")
        dialog.data = {**dialog.data, "company": value}
        dialog.state = "register_project"
        await show(event, "Укажите название и назначение проекта (2–255 символов).")
        return
    if state == "register_project":
        if not 2 <= len(value) <= 255:
            raise ValueError("project length")
        app = await submit_application(
            PartnerApplicationCreate(
                telegram_id=user,
                company_name=dialog.data["company"],
                project_name=value,
                accepted_terms=True,
                accepted_privacy_policy=True,
                terms_version=dialog.data["legal_version"],
                privacy_policy_version=dialog.data["legal_version"],
            ),
            db,
        )
        await notify(
            db,
            get_settings().admin_telegram_id,
            f"Новая заявка {app.id}. Откройте раздел «Заявки».",
            f"application:{app.id}",
        )
        reset(dialog)
        await db.commit()
        await show(event, f"Заявка {app.id} принята. Решение придёт в этот чат.")
        return
    partner = await partner_for(db, user)
    if not partner and not state.startswith("admin_"):
        await home(event, db, dialog)
        return
    if state == "search":
        identifier = str(UUID(value))
        row = (
            await db.execute(select(Generation).where(Generation.id == identifier, Generation.partner_id == partner.id))
        ).scalar_one_or_none()
        if row:
            charge = row.actual_charge_rub if row.actual_charge_rub is not None else row.partner_price_rub
            await show(
                event,
                f"Генерация {row.id}\n{status_label(row.status)}\nСумма: {charge:.2f} ₽",
            )
        else:
            payment = (
                await db.execute(
                    select(PaymentInvoice).where(
                        PaymentInvoice.id == identifier, PaymentInvoice.partner_id == partner.id
                    )
                )
            ).scalar_one_or_none()
            await show(
                event,
                f"Платёж {payment.id}\n{status_label(payment.status)}\n{payment.requested_rub:.2f} ₽"
                if payment
                else "В вашем аккаунте такой операции нет.",
            )
    elif state == "topup":
        if not value.isascii() or not value.isdigit() or not 1000 <= int(value) <= 9999999999999999:
            raise ValueError("amount")
        reset(dialog)
        await ask_confirmation(
            event,
            db,
            "invoice_create",
            {"partner_id": partner.id, "amount": value},
            f"Создать счёт на {int(value):,} ₽? Срок оплаты — 1 час.",
        )
    elif state == "key_name":
        if not 2 <= len(value) <= 120:
            raise ValueError("name")
        reset(dialog)
        await ask_confirmation(
            event, db, "key_create", {"partner_id": partner.id, "name": value}, f"Создать API-ключ «{value}»?"
        )
    elif state == "webhook":
        import secrets

        secret = None if value.lower() == "выключить" else secrets.token_urlsafe(32)
        await update_partner_api_key_webhook(
            partner.id,
            dialog.data["key_id"],
            ApiKeyWebhookUpdate(webhook_url=value if secret else None, webhook_secret=secret),
            db,
        )
        reset(dialog)
        await db.commit()
        await show(event, f"Webhook настроен. Сохраните секрет подписи:\n{secret}" if secret else "Webhook отключён.")
    elif state == "support_subject":
        if not 2 <= len(value) <= 200:
            raise ValueError("subject")
        ticket = SupportTicket(partner_id=partner.id, subject=value)
        db.add(ticket)
        await db.flush()
        dialog.state, dialog.data = "support_reply", {"ticket_id": ticket.id}
        await notify(
            db, get_settings().admin_telegram_id, f"Новое обращение {ticket.id}: {value}", f"ticket:{ticket.id}"
        )
        await db.commit()
        await show(
            event,
            f"Обращение {ticket.id} создано. Отправьте сообщение, скриншот или файл (до 20 МБ каждый).",
            keyboard(back="support"),
        )
    elif state in {"support_reply", "admin_support_reply"}:
        ticket = await get_ticket(db, dialog.data["ticket_id"], user)
        await append_message(db, ticket, event, telegram_id=user)
        await db.commit()
        await show(
            event,
            "Сообщение добавлено. Можно отправить ещё одно сообщение или файл.",
            keyboard(back="admin_support" if is_admin(user) else "support"),
        )
    elif state == "admin_fx":
        from decimal import Decimal

        rate = None if value.lower() == "выключить" else Decimal(value.replace(",", "."))
        if rate is not None and (not rate.is_finite() or not 0 < rate < Decimal("1000000000000")):
            raise ValueError("invalid rate")
        reset(dialog)
        await ask_confirmation(
            event,
            db,
            "admin_fx",
            {"rate": str(rate) if rate is not None else None},
            f"Изменить резервный курс: {rate if rate is not None else 'выключен'}?",
        )
    elif state == "admin_reject":
        if not 1 <= len(value) <= 2000:
            raise ValueError("reason")
        payload = {**dialog.data, "reason": value}
        reset(dialog)
        await ask_confirmation(event, db, "admin_reject", payload, f"Отклонить заявку? Причина: {value}")
    elif state == "admin_account":
        from app.accounts.models import Partner

        owner = await db.get(Partner, str(UUID(value)))
        if not owner or owner.status == "deleted":
            raise HTTPException(404, "partner_not_found")
        reset(dialog)
        await show(
            event,
            f"{owner.company_name}\n{owner.id}\nTelegram: {owner.telegram_id}\nБаланс: {owner.balance_rub:.2f} ₽",
            keyboard(
                (
                    "Отключить" if owner.status == "active" else "Включить",
                    f"admin_{'disable' if owner.status == 'active' else 'enable'}:{owner.id}",
                ),
                ("Перенести Telegram ID", f"admin_transfer:{owner.id}"),
            ),
        )
    elif state in {"admin_transfer", "admin_status"}:
        payload = dict(dialog.data)
        if state == "admin_transfer":
            from app.accounts.schemas import TransferTelegramCreate

            target, reason = value.split(" ", 1)
            valid = TransferTelegramCreate(telegram_id=target, reason=reason)
            payload.update(valid.model_dump())
        else:
            if not 3 <= len(value) <= 2000:
                raise ValueError("reason")
            payload["reason"] = value
        reset(dialog)
        await ask_confirmation(
            event, db, state, payload, f"Подтвердить изменение аккаунта {payload['partner_id']}?\n{value}"
        )
    else:
        await home(event, db, dialog)
