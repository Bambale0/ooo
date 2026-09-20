from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class ManualAdjustmentCreate(BaseModel):
    partner_id: str
    amount_rub: Decimal
    idempotency_key: str = Field(min_length=8, max_length=160)
    description: str | None = None

    @field_validator("amount_rub")
    @classmethod
    def amount_must_not_be_zero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("amount_rub_must_not_be_zero")
        return value


class BalanceRead(BaseModel):
    partner_id: str
    balance_rub: Decimal


class LedgerEntryRead(BaseModel):
    id: str
    partner_id: str
    operation_type: str
    amount_rub: Decimal
    balance_after_rub: Decimal
    idempotency_key: str
    generation_id: str | None
    description: str | None

    model_config = {"from_attributes": True}
