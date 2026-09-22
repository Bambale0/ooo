"""
Safe-to-withdraw calculation — how much USDT profit can be safely withdrawn.
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.payments.models import PaymentInvoice


async def calculate_safe_to_withdraw(db: AsyncSession) -> dict:
    """
    Calculate safe-to-withdraw in USDT-equivalent.
    
    Returns a dict with all components and the final safe-to-withdraw value.
    """
    # Total retail partner balance (sum of all partner balances)
    total_balance_result = await db.execute(
        select(func.coalesce(func.sum(Partner.balance_rub), 0))
    )
    total_balance = Decimal(str(total_balance_result.scalar()))
    
    # Total cost coverage (sum of all partner cost coverage)
    total_coverage_result = await db.execute(
        select(func.coalesce(func.sum(Partner.cost_coverage_rub), 0))
    )
    total_coverage = Decimal(str(total_coverage_result.scalar()))
    
    # Active generation reservations (queued/sent_to_provider/processing)
    active_reservations_result = await db.execute(
        select(func.count())
        .select_from(Generation)
        .where(Generation.status.in_(["queued", "sent_to_provider", "processing"]))
    )
    active_generations = int(active_reservations_result.scalar())
    
    # Paid but not yet credited payments
    pending_credit_result = await db.execute(
        select(func.coalesce(func.sum(PaymentInvoice.requested_rub), 0))
        .where(PaymentInvoice.status == "paid_waiting_credit")
    )
    pending_credit = Decimal(str(pending_credit_result.scalar()))
    
    # Total retail ledger credits (payments, adjustments)
    total_credits_result = await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_rub), 0))
        .where(LedgerEntry.amount_rub > 0)
    )
    total_credits = Decimal(str(total_credits_result.scalar()))
    
    # Total retail ledger debits (generation charges, refunds)
    total_debits_result = await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_rub), 0))
        .where(LedgerEntry.amount_rub < 0)
    )
    total_debits = Decimal(str(total_debits_result.scalar()))
    
    # Partner obligations = sum of all partner balances (positive = owed to partners)
    partner_obligations = total_balance
    
    # Required provider float = total cost coverage (committed to upstream)
    required_float = total_coverage
    
    # Total working capital required
    working_capital_required = partner_obligations + required_float
    
    # Assume accessible USDT wallet balance (from config / external API)
    # For now, this is a placeholder — in production, this would query Crypto Pay balance
    accessible_wallet_usdt = Decimal("0")
    
    # Net safe-to-withdraw
    safe_to_withdraw_usdt = None  # No trustworthy wallet/FX/reserve snapshot: do not fabricate a monetary value.
    
    return {
        "safe_to_withdraw_usdt": safe_to_withdraw_usdt,
        "components": {
            "accessible_wallet_usdt": accessible_wallet_usdt,
            "partner_obligations_rub": partner_obligations,
            "required_provider_float_rub": required_float,
            "working_capital_required_rub": working_capital_required,
            "active_generations": active_generations,
            "pending_credit_rub": pending_credit,
            "total_credits_rub": total_credits,
            "total_debits_rub": abs(total_debits),
        },
        "freshness": "unavailable",
    }


async def record_profit_withdrawal(
    db: AsyncSession,
    *,
    amount_usdt: Decimal,
    reason: str,
    partner_id: str,
) -> dict:
    """Record an accounting-only profit withdrawal."""
    from app.accounts.models import ProfitWithdrawal
    
    withdrawal = ProfitWithdrawal(
        partner_id=partner_id,
        amount_usdt=amount_usdt,
        reason=reason,
    )
    db.add(withdrawal)
    await db.flush()
    await db.refresh(withdrawal)
    return {
        "id": withdrawal.id,
        "amount_usdt": withdrawal.amount_usdt,
        "reason": withdrawal.reason,
        "created_at": withdrawal.created_at.isoformat(),
    }