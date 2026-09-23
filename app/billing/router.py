from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.accounts.models import Partner
from app.api.dependencies import DbSession, require_admin
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.billing.schemas import (
    BalanceRead,
    CoverageAdjustmentCreate,
    CoverageLedgerEntryRead,
    CoverageRead,
    LedgerEntryRead,
    ManualAdjustmentCreate,
    MarginThresholdCreate,
    ProfitWithdrawalCreate,
)
from app.billing.service import apply_cost_coverage_change, apply_partner_balance_change
from app.infrastructure.security import hash_secret

router = APIRouter()


@router.post("/margin-thresholds", dependencies=[Depends(require_admin)])
async def update_margin_threshold(payload: MarginThresholdCreate, db: DbSession):
    from app.billing.margins import set_threshold

    row = await set_threshold(db, actor="admin_api", **payload.model_dump())
    return {"id": row.id, "scope": row.scope, "old_value": row.old_value, "new_value": row.new_value}


@router.get("/margin-thresholds", dependencies=[Depends(require_admin)])
async def read_margin_threshold_history(db: DbSession, before_id: int | None = None):
    from app.billing.models import MarginThresholdHistory

    query = select(MarginThresholdHistory).order_by(MarginThresholdHistory.id.desc()).limit(100)
    if before_id is not None:
        query = query.where(MarginThresholdHistory.id < before_id)
    return list((await db.execute(query)).scalars())


@router.post("/manual-adjustments", response_model=LedgerEntryRead, dependencies=[Depends(require_admin)])
async def create_manual_adjustment(payload: ManualAdjustmentCreate, db: DbSession) -> LedgerEntry:
    partner = await db.get(Partner, payload.partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    return await apply_partner_balance_change(
        db=db,
        partner=partner,
        amount_rub=payload.amount_rub,
        operation_type="manual_adjustment",
        idempotency_key=f"manual:{partner.id}:{hash_secret(payload.idempotency_key)}",
        description=payload.description,
    )


@router.post(
    "/coverage-adjustments",
    response_model=CoverageLedgerEntryRead,
    dependencies=[Depends(require_admin)],
)
async def create_coverage_adjustment(
    payload: CoverageAdjustmentCreate,
    db: DbSession,
) -> CoverageLedgerEntry:
    partner = await db.get(Partner, payload.partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    return await apply_cost_coverage_change(
        db=db,
        partner=partner,
        amount_rub=payload.amount_rub,
        operation_type="manual_coverage_adjustment",
        idempotency_key=f"coverage-manual:{partner.id}:{hash_secret(payload.idempotency_key)}",
        description=payload.reason,
    )


@router.get("/partners/{partner_id}/balance", response_model=BalanceRead, dependencies=[Depends(require_admin)])
async def read_partner_balance(partner_id: str, db: DbSession) -> BalanceRead:
    partner = await db.get(Partner, partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    return BalanceRead(partner_id=partner.id, balance_rub=partner.balance_rub)


@router.get(
    "/partners/{partner_id}/coverage",
    response_model=CoverageRead,
    dependencies=[Depends(require_admin)],
)
async def read_partner_coverage(partner_id: str, db: DbSession) -> CoverageRead:
    partner = await db.get(Partner, partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    return CoverageRead(partner_id=partner.id, cost_coverage_rub=partner.cost_coverage_rub)


@router.get(
    "/partners/{partner_id}/ledger",
    response_model=list[LedgerEntryRead],
    dependencies=[Depends(require_admin)],
)
async def list_partner_ledger(partner_id: str, db: DbSession) -> list[LedgerEntry]:
    result = await db.execute(
        select(LedgerEntry).where(LedgerEntry.partner_id == partner_id).order_by(LedgerEntry.created_at.desc())
    )
    return list(result.scalars().all())


@router.get(
    "/partners/{partner_id}/coverage-ledger",
    response_model=list[CoverageLedgerEntryRead],
    dependencies=[Depends(require_admin)],
)
async def list_partner_coverage_ledger(partner_id: str, db: DbSession) -> list[CoverageLedgerEntry]:
    result = await db.execute(
        select(CoverageLedgerEntry)
        .where(CoverageLedgerEntry.partner_id == partner_id)
        .order_by(CoverageLedgerEntry.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/safe-to-withdraw", dependencies=[Depends(require_admin)])
async def get_safe_to_withdraw(db: DbSession) -> dict:
    from app.billing.safe_to_withdraw import calculate_safe_to_withdraw

    return await calculate_safe_to_withdraw(db)


@router.post("/profit-withdrawals", dependencies=[Depends(require_admin)])
async def create_profit_withdrawal(payload: ProfitWithdrawalCreate, db: DbSession) -> dict:
    from app.billing.safe_to_withdraw import record_profit_withdrawal

    return await record_profit_withdrawal(db, **payload.model_dump())
