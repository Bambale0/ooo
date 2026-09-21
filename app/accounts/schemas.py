from decimal import Decimal

from pydantic import BaseModel, Field


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


class ApiKeyCreated(BaseModel):
    id: str
    name: str
    key_prefix: str
    api_key: str


class ApiKeyRead(BaseModel):
    id: str
    name: str
    key_prefix: str
    is_active: bool

    model_config = {"from_attributes": True}


class RejectApplicationCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class DeletePartnerCreate(BaseModel):
    reason: str = Field(default="partner_requested_delete", max_length=2000)
