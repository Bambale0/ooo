from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.catalog.procurement import supports_free_rate


class ModelCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=2, max_length=160)
    modality: str = Field(pattern="^(video|image|llm)$")
    status: str = Field(default="draft", pattern="^(draft|admin_only|production|disabled)$")
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
