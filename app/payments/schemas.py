from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


def normalize_buyer_email(value: str) -> str:
    value = value.strip().lower()
    local, separator, domain = value.rpartition("@")
    if (
        not separator
        or not local
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or len(value) > 320
    ):
        raise ValueError("invalid buyer email")
    return value


class PaymentInvoiceCreate(BaseModel):
    requested_rub: int = Field(ge=1000)
    idempotency_key: str = Field(min_length=8, max_length=160)
    buyer_email: str | None = Field(default=None, min_length=3, max_length=320)

    @field_validator("buyer_email")
    @classmethod
    def validate_buyer_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_buyer_email(value)


class PaymentInvoiceRead(BaseModel):
    id: str
    provider: str
    status: str
    requested_rub: Decimal
    accepted_assets: str
    invoice_url: str | None
    expires_at: datetime | None
    paid_asset: str | None
    paid_amount: Decimal | None

    model_config = {"from_attributes": True}


class PaymentCreditRead(BaseModel):
    payment_id: str
    status: str
    requested_rub: Decimal


class PaymentRefundCreate(BaseModel):
    amount_rub: Decimal = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)


class PaymentRefundRead(BaseModel):
    payment_id: str
    refund_id: str
    amount_rub: Decimal
    refunded_rub_total: Decimal
    payment_status: str


class PaymentReconcileCreate(BaseModel):
    provider_invoice_id: str | int
    reason: str = Field(min_length=10, max_length=2000)
