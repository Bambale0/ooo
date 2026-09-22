"""
Critical alerts system — detects and notifies about operational/financial incidents.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.payments.models import PaymentInvoice
from app.webhooks.models import WebhookEvent


class AlertSeverity(Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class AlertType(Enum):
    LOW_BALANCE = "low_partner_balance"
    LOW_PROVIDER_FLOAT = "low_provider_float"
    PAYMENT_PENDING_CREDIT = "payment_pending_credit"
    NEGATIVE_SAFE_TO_WITHDRAW = "negative_safe_to_withdraw"
    NEGATIVE_ECONOMICS = "negative_economics"
    WEBHOOK_DELIVERY_FAILED = "webhook_delivery_failed"
    BACKUP_FAILED = "backup_restore_failed"
    PROVIDER_KEY_INVALID = "provider_key_invalid"


@dataclass
class Alert:
    type: AlertType
    severity: AlertSeverity
    message: str
    partner_id: str | None = None
    details: dict | None = None


async def check_alerts(db: AsyncSession) -> list[Alert]:
    """Run all alert checks and return active alerts."""
    alerts: list[Alert] = []
    
    # 1. Low partner balance
    low_balance = await db.execute(
        select(Partner).where(
            Partner.balance_rub < Decimal("100.00"),
            Partner.status == "active",
        )
    )
    for partner in low_balance.scalars().all():
        alerts.append(Alert(
            type=AlertType.LOW_BALANCE,
            severity=AlertSeverity.WARNING,
            message=f"Partner {partner.company_name} balance is {partner.balance_rub:.2f} RUB",
            partner_id=partner.id,
            details={"balance_rub": str(partner.balance_rub)},
        ))
    
    # 2. Payment pending manual credit
    pending = await db.execute(
        select(PaymentInvoice).where(PaymentInvoice.status == "paid_waiting_credit")
    )
    for payment in pending.scalars().all():
        alerts.append(Alert(
            type=AlertType.PAYMENT_PENDING_CREDIT,
            severity=AlertSeverity.WARNING,
            message=f"Payment {payment.id} ({payment.requested_rub:.2f} RUB) waiting credit",
            partner_id=payment.partner_id,
            details={"amount_rub": str(payment.requested_rub), "payment_id": payment.id},
        ))
    
    # 3. Webhook delivery failures
    failed_webhooks = await db.execute(
        select(func.count()).select_from(WebhookEvent).where(
            WebhookEvent.status == "delivery_failed",
        )
    )
    failed_count = int(failed_webhooks.scalar())
    if failed_count > 0:
        alerts.append(Alert(
            type=AlertType.WEBHOOK_DELIVERY_FAILED,
            severity=AlertSeverity.CRITICAL,
            message=f"{failed_count} webhook deliveries failed",
            details={"failed_count": failed_count},
        ))
    
    return alerts


async def send_telegram_alert(alert: Alert, bot=None) -> None:
    """Send an alert to admin Telegram."""
    if bot is None:
        return
    severity_icon = {
        AlertSeverity.CRITICAL: "🚨",
        AlertSeverity.WARNING: "⚠️",
        AlertSeverity.INFO: "ℹ️",
    }.get(alert.severity, "")
    
    text = (
        f"{severity_icon} *{alert.type.value}*\n"
        f"{alert.message}"
    )
    try:
        config = __import__("app.infrastructure.config", fromlist=["get_settings"]).get_settings()
        if config.admin_telegram_id:
            await bot.send_message(chat_id=config.admin_telegram_id, text=text)
    except Exception:
        pass  # Don't crash if alert fails