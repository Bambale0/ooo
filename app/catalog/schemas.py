from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.catalog.procurement import supports_free_rate


class ModelCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=2, max_length=160)
    modality: str = Field(pattern="^(video|image|llm)$")
    status: str = Field(default="draft", pattern="^(draft|admin_only|restricted|production|disabled)$")
    has_provider_integration: bool = False
    has_public_docs: bool = False
    has_successful_smoke: bool = False


class ModelRead(BaseModel):
    id: str
    slug: str
    name: str
    modality: str
    status: str
    has_provider_integration: bool
    has_public_docs: bool
    has_successful_smoke: bool

    model_config = {"from_attributes": True}


class PartnerPriceUpsert(BaseModel):
    model_slug: str
    mode: str = "default"
    resolution: str = "default"
    price_rub: Decimal = Field(ge=0, allow_inf_nan=False)
    provider_cost_usdt: Decimal = Field(ge=0, allow_inf_nan=False)
    billing_unit: str = Field(default="generation", pattern="^(generation|second|million_tokens)$")

    @model_validator(mode="after")
    def require_reviewed_zero_rate(self) -> "PartnerPriceUpsert":
        if (self.price_rub == 0 or self.provider_cost_usdt == 0) and not supports_free_rate(
            self.model_slug, self.mode, self.resolution, self.billing_unit
        ):
            raise ValueError("zero_price_requires_reviewed_free_token_rate")
        return self


class PricingRead(BaseModel):
    model_slug: str
    model_name: str
    modality: str
    mode: str
    resolution: str
    price_rub: Decimal
    billing_unit: str


class ModelEnableGateUpdate(BaseModel):
    has_provider_integration: bool
    has_public_docs: bool
    has_successful_smoke: bool


class ModelGrantCreate(BaseModel):
    partner_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=10, max_length=2000)


class ModelGrantRead(BaseModel):
    id: str
    model_slug: str
    partner_id: str
    granted_by: str
    reason: str
    revoked_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PartnerPriceOverrideUpsert(BaseModel):
    model_slug: str = Field(min_length=2, max_length=80)
    mode: str = Field(default="default", min_length=1, max_length=80)
    resolution: str = Field(default="default", min_length=1, max_length=80)
    price_rub: Decimal = Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    reason: str = Field(min_length=5, max_length=500)


class PartnerPriceOverrideRead(BaseModel):
    partner_id: str
    model_slug: str
    mode: str
    resolution: str
    price_rub: Decimal
    billing_unit: str
    is_custom: bool
