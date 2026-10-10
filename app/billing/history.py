"""Read-only partner transaction projection; the full ledger remains internal.

Group generation reserve/release/late-charge/settlement rows before pagination.
Never report an unsettled hold or the quoted partner_price_rub as a final debit.
No rows are created, removed or repriced by this presentation layer.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import String, and_, case, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import LedgerEntry
from app.generations.models import Generation

GENERATION_ENTRIES = (
    "generation_reserve",
    "generation_reserve_release",
    "generation_late_charge",
    "generation_usage_adjustment",
    "generation_charge",
    "generation_refund",
)
CASH_ENTRIES = ("payment_credit", "payment_refund_adjustment", "manual_adjustment")


@dataclass(frozen=True)
class PartnerTransaction:
    id: str
    generation_id: str | None
    model_slug: str | None
    operation_type: str
    amount_rub: Decimal
    created_at: datetime


def final_generation_rows(partner_id: str):
    """Net finalized ledger debit, including successful fixed-price legacy jobs.

    Native settlement has an explicit actual_charge_rub. Legacy success (or an
    explicit generation_charge) can instead prove finality. A failed, timed-out
    or reconciling reserve alone cannot. Both sides of the join are tenant scoped.
    """
    net = func.sum(LedgerEntry.amount_rub)
    direct_charge = func.max(case((LedgerEntry.operation_type == "generation_charge", 1), else_=0))
    final = or_(
        Generation.actual_charge_rub > 0,
        and_(Generation.actual_charge_rub == 0, net == 0),
        and_(Generation.actual_charge_rub.is_(None), or_(Generation.status == "completed", direct_charge == 1)),
    )
    return (
        select(
            Generation.id.label("id"),
            Generation.id.label("generation_id"),
            Generation.model_slug.label("model_slug"),
            literal("generation_charge").label("operation_type"),
            net.label("amount_rub"),
            func.max(LedgerEntry.created_at).label("created_at"),
        )
        .join(
            LedgerEntry,
            and_(LedgerEntry.generation_id == Generation.id, LedgerEntry.partner_id == Generation.partner_id),
        )
        .where(
            Generation.partner_id == partner_id,
            LedgerEntry.partner_id == partner_id,
            LedgerEntry.operation_type.in_(GENERATION_ENTRIES),
        )
        .group_by(Generation.id, Generation.model_slug, Generation.status, Generation.actual_charge_rub)
        .having(final, net <= 0)
    )


def partner_history_query(partner_id: str, *, page: int = 0, page_size: int = 8):
    if page < 0 or not 1 <= page_size <= 100:
        raise ValueError("invalid_history_page")
    generation = final_generation_rows(partner_id).subquery()
    debits = select(generation).where(generation.c.amount_rub < 0)
    cash = select(
        LedgerEntry.id.label("id"),
        literal(None, String()).label("generation_id"),
        literal(None, String()).label("model_slug"),
        LedgerEntry.operation_type,
        LedgerEntry.amount_rub,
        LedgerEntry.created_at,
    ).where(
        LedgerEntry.partner_id == partner_id, LedgerEntry.operation_type.in_(CASH_ENTRIES), LedgerEntry.amount_rub != 0
    )
    transactions = union_all(debits, cash).subquery()
    return (
        select(transactions)
        .order_by(transactions.c.created_at.desc(), transactions.c.operation_type, transactions.c.id)
        .offset(page * page_size)
        .limit(page_size + 1)
    )


async def partner_transactions(db: AsyncSession, partner_id: str, *, page: int = 0) -> list[PartnerTransaction]:
    rows = (await db.execute(partner_history_query(partner_id, page=page))).mappings()
    return [PartnerTransaction(**row) for row in rows]


async def final_generation_charge(db: AsyncSession, generation: Generation) -> Decimal | None:
    if generation.actual_charge_rub == 0:
        return Decimal("0.00")  # Free final result need not have any ledger rows.
    row = (
        (await db.execute(final_generation_rows(generation.partner_id).where(Generation.id == generation.id)))
        .mappings()
        .one_or_none()
    )
    return -row["amount_rub"] if row is not None else None
