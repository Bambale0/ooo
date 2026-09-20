from pydantic import BaseModel, Field


class ProviderCredentialCreate(BaseModel):
    provider: str = Field(default="argolink", min_length=2, max_length=80)
    label: str = Field(min_length=2, max_length=120)
    api_key: str = Field(min_length=8, max_length=4096)


class ProviderCredentialRead(BaseModel):
    id: str
    provider: str
    label: str
    key_prefix: str
    is_active: bool

    model_config = {"from_attributes": True}


class ProviderCapabilityUpsert(BaseModel):
    provider: str = Field(default="argolink", min_length=2, max_length=80)
    model_slug: str
    mode: str
    resolution: str
    is_active: bool = True


class ProviderCapabilityRead(BaseModel):
    id: str
    provider: str
    model_id: str
    mode: str
    resolution: str
    is_active: bool

    model_config = {"from_attributes": True}
