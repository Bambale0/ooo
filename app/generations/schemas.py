from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MediaReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(pattern=r"^https://", max_length=2048)


class GenerationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_slug: str
    prompt: str = Field(min_length=1, max_length=8000)
    idempotency_key: str = Field(min_length=8, max_length=160)
    mode: str = "default"
    resolution: str = "default"
    duration_seconds: int = Field(default=5, ge=1, le=30)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    reference_images: list[MediaReference] = Field(default_factory=list, max_length=30)
    start_image: MediaReference | None = None
    end_image: MediaReference | None = None


class GenerationRead(BaseModel):
    id: str
    model_slug: str
    mode: str
    resolution: str
    duration_seconds: int
    aspect_ratio: str | None
    status: str
    idempotency_key: str
    partner_price_rub: Decimal
    actual_charge_rub: Decimal | None = None
    result_url: str | None
    result_urls: list[str] = Field(default_factory=list)
    public_error_code: str | None

    model_config = {"from_attributes": True}


class ProviderDispatchRead(BaseModel):
    generation_id: str
    status: str
    provider_attempt_status: str
    public_error_code: str | None


class ProviderPollRead(BaseModel):
    generation_id: str
    status: str
    result_url: str | None
    public_error_code: str | None
