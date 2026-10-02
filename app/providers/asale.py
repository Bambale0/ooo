import json
import re
from decimal import Decimal
from typing import Any

import httpx

from app.infrastructure.config import get_settings
from app.infrastructure.retry import parse_retry_after_seconds
from app.providers.base import (
    ProviderAdapterError,
    ProviderGenerationRequest,
    ProviderPollResult,
    ProviderResultStream,
    ProviderSubmitResult,
)
from app.providers.http_client import get_provider_http_client

_ASALE_VIDEO_RESOLUTIONS = {
    "seedance-2.0": {"480p", "720p", "1080p", "4k"},
    "seedance-2.5": {"480p", "720p"},
}
_ASALE_SEEDANCE_20_RATIOS = {"1:1", "3:4", "9:16", "4:3", "16:9", "21:9"}
_ASALE_SEEDANCE_25_RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9"}


def asale_supports_request(payload: ProviderGenerationRequest) -> bool:
    """Reviewed fallback subset with no semantic degradation."""
    resolutions = _ASALE_VIDEO_RESOLUTIONS.get(payload.model_slug)
    if resolutions is None or payload.resolution.lower() not in resolutions:
        return False
    if payload.mode not in {"default", "text_to_video", "videos/generations"}:
        return False
    if not payload.prompt.strip():
        return False
    if payload.mode == "videos/generations":
        native = payload.native_body
        if not isinstance(native, dict):
            return False
        # The adapter currently forwards only the fields below. Reject every
        # other native control instead of silently dropping provider semantics.
        safe_native_fields = {
            "model",
            "prompt",
            "duration",
            "seconds",
            "resolution",
            "aspect_ratio",
            "ratio",
            "size",
            "n",
            "generate_audio",
        }
        if set(native) - safe_native_fields:
            return False
        if native.get("model") != payload.model_slug or native.get("n", 1) != 1:
            return False
        if native.get("generate_audio", False):
            return False
    # Frame/reference inputs are intentionally fail-closed until their exact
    # Asale wire contract has a production smoke test.
    if payload.reference_images or payload.start_image or payload.end_image:
        return False
    if payload.model_slug == "seedance-2.0":
        if not 4 <= payload.duration_seconds <= 15:
            return False
        ratios = _ASALE_SEEDANCE_20_RATIOS
    else:
        if not 4 <= payload.duration_seconds <= 30:
            return False
        ratios = _ASALE_SEEDANCE_25_RATIOS
    return payload.aspect_ratio is None or payload.aspect_ratio in ratios


class AsaleAdapter:
    provider_name = "asale"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.asale_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.asale_api_key
        self.timeout_seconds = timeout_seconds or settings.asale_timeout_seconds
        self._client = client or get_provider_http_client(self.provider_name)

    async def prepaid_balance_usdt(self) -> Decimal:
        # The inference gateway does not expose a verified wallet balance.
        raise ProviderAdapterError(
            "provider_temporarily_unavailable",
            "asale_wallet_balance_not_exposed_to_inference_key",
            retryable=False,
        )

    async def health_check(self) -> bool:
        if not self.api_key:
            return False
        try:
            response = await self._client.get("/v1/models", headers=self._auth_headers())
            return response.is_success
        except httpx.HTTPError:
            return False

    async def validate_key(self, api_key: str) -> bool:
        token = api_key.strip()
        if not token:
            return False
        try:
            response = await self._client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def submit_generation(self, payload: ProviderGenerationRequest) -> ProviderSubmitResult:
        if not self.api_key:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "ASALE_API_KEY is not configured",
                retryable=False,
            )
        if not asale_supports_request(payload):
            raise ProviderAdapterError("provider_rejected_request", "asale_capability_mismatch", retryable=False)

        body: dict[str, object] = {
            "model": payload.model_slug,
            "prompt": payload.prompt,
            "duration": payload.duration_seconds,
            "resolution": "4K" if payload.resolution.lower() == "4k" else payload.resolution.lower(),
        }
        if payload.aspect_ratio:
            body["aspect_ratio"] = payload.aspect_ratio

        try:
            response = await self._client.post("/v1/videos", json=body, headers=self._auth_headers())
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._http_error_to_provider_error(exc.response) from exc
        except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError) as exc:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                type(exc).__name__,
                retryable=True,
            ) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError) as exc:
            # Paid POST outcome can be ambiguous after bytes were written.
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "asale_submit_outcome_unknown",
                retryable=False,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                type(exc).__name__,
                retryable=False,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "invalid_asale_submit_response",
                retryable=False,
            ) from exc
        provider_task_id = self._extract_task_id(data)
        if provider_task_id is None:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "missing_asale_task_id",
                retryable=False,
            )
        return ProviderSubmitResult(
            provider_task_id=provider_task_id,
            status="accepted",
            raw_response=data if isinstance(data, dict) else {"response": data},
        )

    async def poll_generation(self, provider_task_id: str) -> ProviderPollResult:
        if not self.api_key:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "ASALE_API_KEY is not configured",
                retryable=False,
            )
        self._validate_task_id(provider_task_id)
        try:
            response = await self._client.get(
                f"/v1/videos/{provider_task_id}",
                headers=self._auth_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._http_error_to_provider_error(exc.response) from exc
        except httpx.TimeoutException as exc:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "asale_poll_timeout",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                type(exc).__name__,
                retryable=True,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "invalid_asale_poll_response",
            ) from exc

        status = self._normalize_video_status(data)
        billed_seconds = data.get("billedSeconds") if isinstance(data, dict) else None
        usage = None
        if isinstance(billed_seconds, int) and not isinstance(billed_seconds, bool) and billed_seconds > 0:
            usage = {"billed_seconds": billed_seconds}
        raw_error = self._extract_error(data) if status == "failed" else None
        error = data.get("error") if isinstance(data, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        return ProviderPollResult(
            status=status,
            result_url=f"{self.base_url}/v1/videos/{provider_task_id}/content" if status == "completed" else None,
            raw_error=raw_error,
            usage=usage,
            error_code=error_code if isinstance(error_code, str) else None,
            retryable_failure=False,
        )

    async def open_result_stream(
        self,
        provider_content_url: str,
        *,
        range_header: str | None = None,
    ) -> ProviderResultStream:
        if not re.fullmatch(
            re.escape(self.base_url) + r"/v1/videos/[A-Za-z0-9_-]{1,255}/content",
            provider_content_url,
        ):
            raise ProviderAdapterError("provider_rejected_request", "unexpected_result_url", retryable=False)

        headers = self._auth_headers()
        if range_header:
            headers["Range"] = range_header
        response: httpx.Response | None = None
        try:
            request = self._client.build_request("GET", provider_content_url, headers=headers)
            response = await self._client.send(request, stream=True, follow_redirects=False)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if response is not None:
                await response.aclose()
            raise self._http_error_to_provider_error(exc.response) from exc
        except httpx.HTTPError as exc:
            if response is not None:
                await response.aclose()
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                type(exc).__name__,
                retryable=True,
            ) from exc

        async def body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        content_length = None
        raw_content_length = response.headers.get("content-length")
        if raw_content_length and raw_content_length.isdigit():
            content_length = int(raw_content_length)
        return ProviderResultStream(
            body=body(),
            status_code=response.status_code,
            content_type=response.headers.get("content-type"),
            content_length=content_length,
            content_range=response.headers.get("content-range"),
            accept_ranges=response.headers.get("accept-ranges"),
        )

    def normalize_error(self, error: Exception) -> ProviderAdapterError:
        if isinstance(error, ProviderAdapterError):
            return error
        return ProviderAdapterError(
            "provider_temporarily_unavailable",
            type(error).__name__,
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @staticmethod
    def _extract_task_id(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        value = data.get("id")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
            return value
        return None

    @staticmethod
    def _validate_task_id(value: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
            raise ProviderAdapterError("provider_rejected_request", "invalid_provider_task_id", retryable=False)

    @staticmethod
    def _normalize_video_status(data: Any) -> str:
        if not isinstance(data, dict):
            raise ProviderAdapterError("provider_temporarily_unavailable", "invalid_asale_poll_response")
        raw_status = str(data.get("status", "")).lower()
        if raw_status in {"completed", "done", "succeeded", "success"}:
            return "completed"
        if raw_status in {"failed", "error", "cancelled"}:
            return "failed"
        if raw_status in {"pending", "queued", "processing", "running"}:
            return "processing"
        raise ProviderAdapterError("provider_temporarily_unavailable", "unknown_asale_video_status")

    @staticmethod
    def _extract_error(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:4000]
            code = error.get("code")
            if isinstance(code, str):
                return code[:4000]
        if isinstance(error, str):
            return error[:4000]
        return None

    @staticmethod
    def _http_error_to_provider_error(response: httpx.Response) -> ProviderAdapterError:
        retry_after_seconds = parse_retry_after_seconds(response.headers.get("Retry-After"))
        code = None
        try:
            payload = json.loads(response.content)
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
        except (ValueError, TypeError):
            pass
        raw_error = f"asale_{code}" if isinstance(code, str) else f"asale_{response.status_code}"

        if response.status_code in {429, 502, 503}:
            return ProviderAdapterError(
                "provider_temporarily_unavailable",
                raw_error,
                retryable=True,
                retry_after_seconds=retry_after_seconds,
            )
        if response.status_code in {401, 402, 403}:
            return ProviderAdapterError(
                "provider_rejected_request",
                raw_error,
                retryable=False,
            )
        return ProviderAdapterError(
            "provider_rejected_request",
            raw_error,
            retryable=False,
        )
