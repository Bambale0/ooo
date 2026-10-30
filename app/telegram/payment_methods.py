"""Explicit payment-method selection; legacy confirmations keep their existing path."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.infrastructure.config import get_settings
from app.payments.creation_recovery import expire_lava_creation_claims
from app.payments.models import PaymentInvoice
from app.payments.schemas import normalize_buyer_email
from app.telegram.models import BotAction
from app.telegram.service import new_action, partner_for, pending_action
from app.telegram.ui import keyboard, show, status_label

LABELS = {"lava_top": "СБП · Lava.top", "crypto_pay": "Крипто Бот · USDT / TON"}
ACTION_KIND = "invoice_create_selected"


def available_methods() -> tuple[str, ...]:
    settings = get_settings()
    methods = []
    if settings.lava_top_api_key and settings.lava_top_offer_id and settings.lava_top_webhook_secret:
        methods.append("lava_top")
    if settings.crypto_pay_api_token:
        methods.append("crypto_pay")
    return tuple(methods)


def _require_method(method: str) -> None:
    if method not in available_methods():
        raise HTTPException(503, "payment_provider_not_configured")


async def _begin(event, dialog, partner, method: str) -> None:
    _require_method(method)
    dialog.data = {"payment_provider": method}
    if method == "lava_top" and not partner.payment_email:
        dialog.state = "topup_email"
        await show(event, "Укажите email для чека и оплаты через СБП. Затем попросим сумму.", keyboard(back="topup"))
        return
    dialog.state = "topup"
    email_text = ""
    if method == "lava_top":
        dialog.data = {**dialog.data, "buyer_email": partner.payment_email}
        email_text = f"Email для чека: {partner.payment_email}.\n"
    await show(
        event, f"Способ оплаты: {LABELS[method]}\n\n{email_text}"
        "На какую сумму пополнить баланс? Введите целую сумму от 1 000 ₽, например 3000.",
        keyboard(back="topup"),
    )


async def _show_invoice(event, payment) -> None:
    buttons = [("Обновить", f"payment:{payment.id}")]
    if payment.invoice_url:
        buttons.insert(0, ("Оплатить", payment.invoice_url))
    if payment.status == "creation_unknown":
        details = (
            "Создание счёта не подтверждено. Ссылка на оплату не получена. "
            "Этот счёт автоматически повторно не выставляется; он требует сверки. "
            "Для нового пополнения можно выбрать другой способ оплаты."
        )
        buttons.append(("Выбрать способ оплаты", "topup"))
    else:
        details = "Баланс пополнится автоматически после подтверждения оплаты."
    await show(
        event, f"Счёт {payment.id}\nСпособ: {LABELS.get(payment.provider, payment.provider)}\n"
        f"Сумма пополнения: {payment.requested_rub:.2f} ₽\nК оплате: {payment.amount_due_rub:.2f} ₽\n"
        f"Статус: {status_label(payment.status)}\n\n{details}", keyboard(*buttons, back="balance"),
    )


async def _confirm(event, db, action_id: str, actor: str) -> bool:
    # Leave legacy actions to the existing handler; never reveal another actor's payload.
    selected = await db.scalar(select(BotAction.id).where(
        BotAction.id == action_id, BotAction.telegram_id == actor, BotAction.kind == ACTION_KIND,
    ))
    if selected is None:
        return False
    action = await pending_action(db, action_id, actor)
    if action.status != "pending":
        await show(event, "Действие уже завершено. Новая операция не создавалась.", keyboard(back="balance"))
        return True
    partner = await partner_for(db, actor)
    payload = action.payload
    if partner is None or partner.id != payload.get("partner_id"):
        raise HTTPException(403, "account_unavailable")
    method = payload["payment_provider"]
    _require_method(method)
    idempotency_key = f"telegram:{action.id}"
    existing = await db.scalar(select(PaymentInvoice).where(
        PaymentInvoice.partner_id == partner.id, PaymentInvoice.idempotency_key == idempotency_key,
    ))
    if existing is not None and existing.provider != method:
        raise HTTPException(409, "idempotency_conflict")
    if method == "crypto_pay":
        from app.payments.crypto_pay import get_crypto_pay_client
        from app.payments.service import create_or_resume_invoice

        payment = await create_or_resume_invoice(
            db, partner=partner, requested_rub=int(payload["amount"]),
            idempotency_key=idempotency_key, client=get_crypto_pay_client(),
        )
    else:
        from app.payments.lava_top import get_lava_top_client
        from app.payments.service import create_or_resume_lava_invoice

        payment = await create_or_resume_lava_invoice(
            db, partner=partner, requested_rub=int(payload["amount"]),
            idempotency_key=idempotency_key, buyer_email=payload["buyer_email"], client=get_lava_top_client(),
        )
    action.status = "applied"
    await db.commit()
    await _show_invoice(event, payment)
    return True


async def handle_callback(event, db, dialog) -> bool:
    data = event.data or ""
    actor = str(event.from_user.id)
    if data.startswith("confirm:"):
        return await _confirm(event, db, str(UUID(data.split(":", 1)[1])), actor)
    methods = available_methods()
    if not (data.startswith(("topup_method:", "payment:")) or (data in {"topup", "balance"} and len(methods) > 1)):
        return False
    partner = await partner_for(db, actor)
    if partner is None:
        raise HTTPException(403, "account_unavailable")
    if data.startswith("payment:"):
        payment_id = str(UUID(data.split(":", 1)[1]))
        payment = await db.scalar(select(PaymentInvoice).where(
            PaymentInvoice.id == payment_id, PaymentInvoice.partner_id == partner.id,
        ))
        if payment is None:
            raise HTTPException(404, "payment_not_found")
        if payment.provider != "lava_top" or payment.provider_reference or payment.status not in {
            "creating", "creation_unknown",
        }:
            return False
        await expire_lava_creation_claims(db, payment_id=payment.id, partner_id=partner.id)
        await _show_invoice(event, payment)
        return True
    if data.startswith("topup_method:"):
        await _begin(event, dialog, partner, data.split(":", 1)[1])
        return True
    dialog.state, dialog.data = "menu", {}
    if data == "topup":
        await show(
            event, "Выберите способ пополнения баланса. Сумму укажете на следующем шаге.",
            keyboard(*[(LABELS[method], f"topup_method:{method}") for method in methods], back="balance"),
        )
    else:
        credit = (
            f"Кредитный лимит: {partner.credit_limit_rub:.2f} ₽\n"
            f"Порог для новых запросов: −{partner.credit_limit_rub:.2f} ₽\n" if partner.credit_limit_rub else ""
        )
        email = f"Email для чеков: {partner.payment_email}\n\n" if partner.payment_email else ""
        await show(
            event, f"Баланс: {partner.balance_rub:.2f} ₽\n{credit}"
            f"Доступный баланс: {partner.available_balance_rub:.2f} ₽\n\n{email}"
            "Доступны СБП через Lava.top и Крипто Бот (USDT / TON). "
            "Перед созданием счёта покажем сумму к оплате и сумму зачисления. "
            "Зачисление происходит автоматически после подтверждения оплаты.",
            keyboard(("Пополнить", "topup"), ("Мои счета", "payments:0"), ("Изменить email", "payment_email")),
        )
    return True


async def handle_message(event, db, dialog) -> bool:
    if dialog.state not in {"topup", "topup_email"} or "payment_provider" not in dialog.data:
        return False
    value = (event.text or "").strip()
    if value.startswith("/"):
        return False
    partner = await partner_for(db, str(event.from_user.id))
    if partner is None:
        raise HTTPException(403, "account_unavailable")
    method = dialog.data["payment_provider"]
    _require_method(method)
    if dialog.state == "topup_email":
        partner.payment_email = normalize_buyer_email(value)
        await _begin(event, dialog, partner, method)
        return True
    # Use the persisted money column's precision, not a second business limit.
    amount_type = PaymentInvoice.__table__.c.requested_rub.type
    max_amount = 10 ** (amount_type.precision - amount_type.scale) - 1
    if not value.isascii() or not value.isdigit() or not 1000 <= int(value) <= max_amount:
        raise ValueError("amount")
    amount = int(value)
    buyer_email = dialog.data.get("buyer_email")
    if method == "lava_top":
        from app.payments.lava_top import lava_top_checkout_amount

        due = lava_top_checkout_amount(amount)
    else:
        due = amount
    action = await new_action(db, str(event.from_user.id), ACTION_KIND, {
        "partner_id": partner.id, "amount": str(amount), "buyer_email": buyer_email, "payment_provider": method,
    })
    dialog.state, dialog.data = "menu", {}
    await db.commit()
    await show(
        event, f"Способ оплаты: {LABELS[method]}\nСоздать счёт на {due:,} ₽?\n"
        f"На баланс зачислится {amount:,} ₽."
        + (f"\nEmail для чека: {buyer_email}" if method == "lava_top" else ""),
        keyboard(("Подтвердить", f"confirm:{action.id}"), back="topup"),
    )
    return True
