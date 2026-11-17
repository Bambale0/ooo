"""Pydantic schemas for Partner Dashboard API.

Following API design best practices:
- Clear separation between read/write models
- Comprehensive validation
- Pagination support
- Filter/sort parameters
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, EmailStr, Field, field_validator


# ==================== Profile & Settings ====================


class ProfileRead(BaseModel):
    """Partner profile information."""

    partner_id: str
    company_name: str
    project_name: str
    payment_email: EmailStr | None
    status: str
    is_admin: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ProfileUpdate(BaseModel):
    """Updateable partner profile fields."""

    payment_email: EmailStr | None = Field(
        default=None, description="Email for payment notifications and invoices"
    )

    model_config = {"extra": "forbid"}


# ==================== Balance & Billing ====================


class BalanceDetailRead(BaseModel):
    """Detailed balance information."""

    partner_id: str
    balance_rub: Decimal = Field(description="Current balance in RUB")
    credit_limit_rub: Decimal = Field(description="Credit limit in RUB")
    available_balance_rub: Decimal = Field(description="Available balance (balance + credit_limit) in RUB")
    cost_coverage_rub: Decimal = Field(description="Cost coverage accumulated in RUB")
    currency: str = Field(default="RUB", description="Currency code")


class LedgerEntryType(str, Enum):
    """Ledger entry operation types."""

    TOP_UP = "payment_received"
    GENERATION_CHARGE = "generation_charge"
    GENERATION_REFUND = "generation_refund"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    REFERRAL_BONUS = "referral_bonus"


class LedgerEntryRead(BaseModel):
    """Ledger entry with full details."""

    id: str
    partner_id: str
    operation_type: str
    amount_rub: Decimal
    balance_after_rub: Decimal
    generation_id: str | None
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class LedgerQueryParams(BaseModel):
    """Query parameters for ledger entries."""

    operation_type: str | None = Field(default=None, description="Filter by operation type")
    from_date: datetime | None = Field(default=None, description="Filter entries after this date")
    to_date: datetime | None = Field(default=None, description="Filter entries before this date")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum number of entries to return")
    offset: int = Field(default=0, ge=0, description="Number of entries to skip")


class LedgerResponse(BaseModel):
    """Paginated ledger response."""

    entries: list[LedgerEntryRead]
    total: int
    limit: int
    offset: int
    has_more: bool


class UsageSummary(BaseModel):
    """Usage summary for current period."""

    period_start: datetime
    period_end: datetime
    total_generations: int = Field(description="Total number of generations")
    successful_generations: int = Field(description="Number of successful generations")
    failed_generations: int = Field(description="Number of failed generations")
    total_spent_rub: Decimal = Field(description="Total amount spent in RUB")
    total_video_seconds: Decimal = Field(description="Total video seconds generated")
