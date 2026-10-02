from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


class ProviderCredentialCreate(BaseModel):
    provider: str = Field(default="argolink", min_length=2, max_length=80)
    label: str = Field(min_length=2, max_length=120)
    api_key: str = Field(min_length=8, max_length=4096)
    partner_application_id: str | None = None
    partner_id: str | None = None

    @model_validator(mode="after")
    def exactly_one_owner(self) -> "ProviderCredentialCreate":
        owners = [self.partner_application_id is not None, self.partner_id is not None]
        if sum(owners) != 1:
            raise ValueError("exactly_one_credential_owner_required")
        return self


class ProviderCredentialRead(BaseModel):
    id: str
    provider: str
    label: str
    key_prefix: str
    partner_application_id: str | None
    partner_id: str | None
    is_active: bool
    provider_cost_ceiling_usdt: Decimal | None
    billing_unit: str | None

    model_config = {"from_attributes": True}


class ProviderCapabilityUpsert(BaseModel):
    provider: str = Field(default="argolink", min_length=2, max_length=80)
    model_slug: str
    mode: str
    resolution: str
    is_active: bool = True
    provider_cost_ceiling_usdt: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    billing_unit: str | None = Field(default=None, pattern="^(second|generation)$")

    @model_validator(mode="after")
    def cost_fields_are_paired(self) -> "ProviderCapabilityUpsert":
        if (self.provider_cost_ceiling_usdt is None) != (self.billing_unit is None):
            raise ValueError("provider_cost_ceiling_and_billing_unit_must_be_paired")
        return self


class ProviderCapabilityRead(BaseModel):
    id: str
    provider: str
    model_id: str
    mode: str
    resolution: str
    is_active: bool

    model_config = {"from_attributes": True}
