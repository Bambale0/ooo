from decimal import Decimal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class PartnerApplicationCreate(BaseModel):
    telegram_id: str = Field(min_length=3, max_length=64)
    company_name: str = Field(min_length=2, max_length=255)
    project_name: str = Field(min_length=2, max_length=255)
    terms_version: str = Field(default="2026-09-19", min_length=1, max_length=40)
    privacy_policy_version: str = Field(default="2026-09-19", min_length=1, max_length=40)
    accepted_terms: bool
    accepted_privacy_policy: bool


class PartnerApplicationRead(BaseModel):
    id: str
    telegram_id: str
    company_name: str
    project_name: str
    status: str
    terms_version: str
    privacy_policy_version: str

    model_config = {"from_attributes": True}


class PartnerRead(BaseModel):
    id: str
    telegram_id: str
    company_name: str
    project_name: str
    status: str
    balance_rub: Decimal

    model_config = {"from_attributes": True}


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    webhook_url: HttpUrl | None = None
    webhook_secret: str | None = Field(default=None, min_length=16, max_length=4096)

    @model_validator(mode="after")
    def validate_webhook_settings(self) -> "ApiKeyCreate":
        if self.webhook_url is not None and self.webhook_url.scheme != "https":
            raise ValueError("webhook_url_must_use_https")
        if self.webhook_secret is not None and self.webhook_url is None:
            raise ValueError("webhook_secret_requires_url")
        return self


class ApiKeyCreated(BaseModel):
    id: str
    name: str
    key_prefix: str
    api_key: str
    webhook_url: str | None = None


class ApiKeyRead(BaseModel):
    id: str
    name: str
    key_prefix: str
    webhook_url: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class RejectApplicationCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class DeletePartnerCreate(BaseModel):
    reason: str = Field(default="partner_requested_delete", max_length=2000)
