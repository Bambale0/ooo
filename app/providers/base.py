from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderGenerationRequest:
    generation_id: str
    model_slug: str
    mode: str
    resolution: str
    prompt: str
    duration_seconds: int = 1
    aspect_ratio: str | None = None
    reference_images: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderSubmitResult:
    provider_task_id: str
    status: str = "accepted"
    raw_response: dict[str, object] | None = None


@dataclass(frozen=True)
class ProviderPollResult:
    status: str
    result_url: str | None = None
    raw_error: str | None = None


@dataclass(frozen=True)
class ProviderResultStream:
    body: AsyncIterator[bytes]
    status_code: int = 200
    content_type: str | None = None
    content_length: int | None = None
    content_range: str | None = None
    accept_ranges: str | None = None


class ProviderAdapterError(Exception):
    def __init__(
        self,
        public_code: str,
        raw_error: str | None = None,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(public_code)
        self.public_code = public_code
        self.raw_error = raw_error
        self.retry_after_seconds = retry_after_seconds


class ProviderAdapter(Protocol):
    provider_name: str

    async def health_check(self) -> bool: ...

    async def validate_key(self, api_key: str) -> bool: ...

    async def submit_generation(self, payload: ProviderGenerationRequest) -> ProviderSubmitResult: ...

    async def poll_generation(self, provider_task_id: str) -> ProviderPollResult: ...

    async def open_result_stream(
        self,
        provider_content_url: str,
        *,
        range_header: str | None = None,
    ) -> ProviderResultStream: ...

    def normalize_error(self, error: Exception) -> ProviderAdapterError: ...
