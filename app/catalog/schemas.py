from decimal import Decimal

from pydantic import BaseModel, Field


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
    price_rub: Decimal = Field(gt=0)
    provider_cost_usdt: Decimal = Field(gt=0)
    billing_unit: str = Field(default="generation", pattern="^(generation|second|million_tokens)$")


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
