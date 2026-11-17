"""Additional schemas for API Keys, Generations, Analytics, and Invoices."""

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, field_validator


# ==================== API Keys Management ====================


class ApiKeyRead(BaseModel):
    """API key information (without secret)."""

    id: str
    name: str
    key_prefix: str = Field(description="First 8 characters of the key for identification")
    is_active: bool
    webhook_url: str | None
    has_webhook_secret: bool = Field(description="Whether webhook secret is configured")
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreate(BaseModel):
    """Create new API key."""

    name: str = Field(min_length=2, max_length=120, description="Human-readable name for this API key")

    model_config = {"extra": "forbid"}


class ApiKeyCreated(BaseModel):
    """Response after creating API key - includes the actual secret."""

    id: str
    name: str
    key_prefix: str
    api_key: str = Field(description="Full API key - save it, it won't be shown again!")
    created_at: datetime


class ApiKeyWebhookUpdate(BaseModel):
    """Update webhook configuration for API key."""

    webhook_url: str | None = Field(default=None, max_length=2048, description="Webhook URL or null to disable")
    webhook_secret: str | None = Field(
        default=None, min_length=16, max_length=4096, description="Webhook signing secret"
    )

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: str | None) -> str | None:
        """Ensure webhook URL is HTTPS in production."""
        if v and not v.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("webhook_url must use HTTPS in production")
        return v

    model_config = {"extra": "forbid"}


# ==================== Generations History ====================


class GenerationStatus(str, Enum):
    """Generation status values."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GenerationListItem(BaseModel):
    """Generation summary for list view."""

    id: str
    status: str
    model: str
    mode: str | None
    resolution: str | None
    duration_seconds: Decimal | None
    cost_rub: Decimal
    created_at: datetime
    completed_at: datetime | None
    error_code: str | None = Field(default=None, description="Error code if failed")

    model_config = {"from_attributes": True}


class GenerationsQueryParams(BaseModel):
    """Query parameters for generations list."""

    status: str | None = Field(default=None, description="Filter by status")
    model: str | None = Field(default=None, description="Filter by model")
    from_date: datetime | None = Field(default=None, description="Filter generations after this date")
    to_date: datetime | None = Field(default=None, description="Filter generations before this date")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum number of entries to return")
    offset: int = Field(default=0, ge=0, description="Number of entries to skip")


class GenerationsResponse(BaseModel):
    """Paginated generations response."""

    generations: list[GenerationListItem]
    total: int
    limit: int
    offset: int
    has_more: bool


# ==================== Analytics & Stats ====================


class DailyStats(BaseModel):
    """Daily usage statistics."""

    date: str = Field(description="Date in YYYY-MM-DD format")
    generations_count: int
    successful_count: int
    failed_count: int
    total_cost_rub: Decimal
    total_video_seconds: Decimal


class ModelStats(BaseModel):
    """Per-model usage statistics."""

    model: str
    generations_count: int
    total_cost_rub: Decimal
    average_cost_rub: Decimal


class AnalyticsResponse(BaseModel):
    """Analytics data for dashboard."""

    period_start: datetime
    period_end: datetime
    daily_stats: list[DailyStats]
    model_stats: list[ModelStats]
    total_generations: int
    total_spent_rub: Decimal


class AnalyticsQueryParams(BaseModel):
    """Query parameters for analytics."""

    from_date: datetime | None = Field(default=None, description="Start date for analytics")
    to_date: datetime | None = Field(default=None, description="End date for analytics")
    group_by: str = Field(default="day", description="Grouping: day, week, month")


# ==================== Payments & Invoices ====================


class InvoiceStatus(str, Enum):
    """Payment invoice status."""

    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class InvoiceRead(BaseModel):
    """Payment invoice information."""

    id: str
    partner_id: str
    status: str
    amount_rub: Decimal
    payment_method: str
    payment_url: str | None
    paid_at: datetime | None
    expires_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class InvoicesQueryParams(BaseModel):
    """Query parameters for invoices list."""

    status: str | None = Field(default=None, description="Filter by status")
    from_date: datetime | None = Field(default=None, description="Filter invoices after this date")
    to_date: datetime | None = Field(default=None, description="Filter invoices before this date")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum number of entries to return")
    offset: int = Field(default=0, ge=0, description="Number of entries to skip")


class InvoicesResponse(BaseModel):
    """Paginated invoices response."""

    invoices: list[InvoiceRead]
    total: int
    limit: int
    offset: int
    has_more: bool
