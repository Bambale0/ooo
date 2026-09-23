from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class PaymentInvoiceCreate(BaseModel):
    requested_rub: int = Field(ge=1000)
    idempotency_key: str = Field(min_length=8, max_length=160)


class PaymentInvoiceRead(BaseModel):
    id: str
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
    provider_invoice_id: int = Field(gt=0)
    reason: str = Field(min_length=10, max_length=2000)
