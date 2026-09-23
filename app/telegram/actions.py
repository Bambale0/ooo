"""Confirmed mutations; role and ownership are checked again at execution."""

from fastapi import HTTPException

from app.accounts.router import (
    approve_application,
    change_partner_status,
    create_partner_api_key,
    delete_partner,
    reject_application,
    revoke_partner_api_key,
    transfer_telegram,
)
from app.accounts.schemas import (
    ApiKeyCreate,
    DeletePartnerCreate,
    PartnerStatusUpdate,
    RejectApplicationCreate,
    TransferTelegramCreate,
)
from app.payments.crypto_pay import get_crypto_pay_client
from app.payments.service import cancel_active_invoice, create_or_resume_invoice, credit_paid_invoice
from app.support.service import get_ticket
from app.telegram.service import notify, partner_for, pending_action
from app.telegram.ui import keyboard, show, status_label


async def confirm_action(event, db, telegram_id: str, action_id: str) -> None:
    action = await pending_action(db, action_id, telegram_id)
    if action.status != "pending":
        await show(event, "Действие уже выполнено. Новая операция не создавалась.")
        return
    kind, payload = action.kind, action.payload
    partner = await partner_for(db, telegram_id)
    if not kind.startswith("admin_") and (not partner or partner.id != payload.get("partner_id")):
        raise HTTPException(403, "account_unavailable")
    result = "Готово."
    markup = keyboard()
    if kind == "key_create":
        key = await create_partner_api_key(partner.id, ApiKeyCreate(name=payload["name"]), db)
        result = (
            "Ключ создан. Сохраните его сейчас — повторно он не показывается.\n\n"
            f"{key.api_key}\n\nНе передавайте ключ третьим лицам."
        )
    elif kind == "key_revoke":
        await revoke_partner_api_key(partner.id, payload["key_id"], db)
        result = "Ключ отозван. Запросы с ним больше не принимаются."
    elif kind == "delete":
        await delete_partner(partner.id, DeletePartnerCreate(), db)
        result = "Аккаунт удалён. Доступ и ключи отключены. Для новой заявки нажмите /start."
    elif kind == "invoice_create":
        payment = await create_or_resume_invoice(
            db,
            partner=partner,
            requested_rub=int(payload["amount"]),
            idempotency_key=f"telegram:{action.id}",
            client=get_crypto_pay_client(),
        )
        result = f"Счёт {payment.id}\n{payment.requested_rub:.2f} ₽\n{status_label(payment.status)}"
        buttons = [("Проверить оплату", f"payment:{payment.id}")]
        if payment.invoice_url:
            buttons.insert(0, ("Оплатить", payment.invoice_url))
        markup = keyboard(*buttons)
    elif kind == "invoice_cancel":
        await cancel_active_invoice(
            db, payment_id=payload["payment_id"], partner_id=partner.id, client=get_crypto_pay_client()
        )
        result = "Счёт отменён."
    elif kind == "admin_approve":
        approved = await approve_application(payload["application_id"], db)
        await notify(
            db,
            approved.telegram_id,
            "Заявка одобрена. Откройте кабинет: /start",
            f"application-approved:{payload['application_id']}",
        )
    elif kind == "admin_reject":
        app = await reject_application(payload["application_id"], RejectApplicationCreate(reason=payload["reason"]), db)
        await notify(
            db, app.telegram_id, f"Заявка отклонена. Причина: {payload['reason']}", f"application-rejected:{app.id}"
        )
    elif kind == "admin_credit":
        payment = await credit_paid_invoice(db, payment_id=payload["payment_id"])
        from app.accounts.models import Partner

        owner = await db.get(Partner, payment.partner_id)
        await notify(
            db,
            owner.telegram_id,
            f"На баланс зачислено {payment.requested_rub:.2f} ₽. Платёж {payment.id}.",
            f"payment-credited:{payment.id}",
        )
    elif kind == "admin_transfer":
        await transfer_telegram(
            payload["partner_id"],
            TransferTelegramCreate(telegram_id=payload["telegram_id"], reason=payload["reason"]),
            db,
        )
    elif kind == "admin_status":
        await change_partner_status(
            payload["partner_id"], PartnerStatusUpdate(enabled=payload["enabled"], reason=payload["reason"]), db
        )
    elif kind == "admin_fx":
        from decimal import Decimal

        from app.billing.fx import set_manual_fallback

        await set_manual_fallback(
            db,
            rate=Decimal(payload["rate"]) if payload["rate"] else None,
            actor=telegram_id,
            reason=f"telegram-confirmation:{action.id}",
        )
    elif kind == "admin_mute":
        from app.billing.incidents import mute_treasury

        await mute_treasury(db, payload["episode"])
    elif kind == "admin_ticket_close":
        from datetime import UTC, datetime

        from app.accounts.models import Partner

        ticket = await get_ticket(db, payload["ticket_id"], telegram_id)
        ticket.status, ticket.closed_at = "closed", datetime.now(UTC)
        owner = await db.get(Partner, ticket.partner_id)
        await notify(
            db,
            owner.telegram_id,
            f"Обращение {ticket.id} закрыто. Новый вопрос можно отправить через раздел «Поддержка».",
            f"ticket-closed:{ticket.id}",
        )
    else:
        raise HTTPException(422, "unknown_action")
    action.status = "applied"
    # Commit before delivering a secret/result. A lost Telegram response cannot replay a mutation.
    await db.commit()
    await show(event, result, markup)
