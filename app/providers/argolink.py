from typing import Any

import httpx

from app.infrastructure.config import get_settings
from app.providers.base import (
    ProviderAdapterError,
    ProviderGenerationRequest,
    ProviderPollResult,
    ProviderResultStream,
    ProviderSubmitResult,
)
from app.providers.http_client import get_provider_http_client


class ArgoLinkAdapter:
    provider_name = "argolink"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.argolink_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.argolink_api_key
        self.timeout_seconds = timeout_seconds or settings.argolink_timeout_seconds
        self._client = client or get_provider_http_client(self.provider_name)

    async def health_check(self) -> bool:
        async with self._http_client() as client:
            response = await client.get("/v1/models")
            return response.is_success

    async def validate_key(self, api_key: str) -> bool:
        token = api_key.strip()
        if not token:
            return False
        async with self._http_client() as client:
            try:
                response = await client.get(
                    "/v1/videos/00000000-0000-0000-0000-000000000000",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError:
                return False
            return response.status_code not in {401, 403}

    async def submit_generation(self, payload: ProviderGenerationRequest) -> ProviderSubmitResult:
        if not self.api_key:
            raise ProviderAdapterError(
                public_code="provider_temporarily_unavailable",
                raw_error="ARGOLINK_API_KEY is not configured",
            )
        request_body: dict[str, object] = {
            "model": payload.model_slug,
            "prompt": payload.prompt,
            "duration": payload.duration_seconds,
            "metadata": {
                "generation_id": payload.generation_id,
                "mode": payload.mode,
                "resolution": payload.resolution,
            },
        }
        if payload.resolution != "default":
            request_body["resolution"] = payload.resolution
        if payload.aspect_ratio:
            request_body["aspect_ratio"] = payload.aspect_ratio
        if payload.reference_images:
            request_body["reference_images"] = [{"url": url} for url in payload.reference_images]
        async with self._http_client() as client:
            try:
                response = await client.post("/v1/videos/generations", json=request_body, headers=self._auth_headers())
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise self._http_error_to_provider_error(exc.response) from exc
            except httpx.TimeoutException as exc:
                raise ProviderAdapterError("provider_temporarily_unavailable", "argolink_timeout") from exc
            except httpx.HTTPError as exc:
                raise ProviderAdapterError("provider_temporarily_unavailable", type(exc).__name__) from exc
        data = response.json()
        provider_task_id = self._extract_task_id(data)
        if provider_task_id is None:
            raise ProviderAdapterError("provider_temporarily_unavailable", "missing_provider_task_id")
        return ProviderSubmitResult(
            provider_task_id=provider_task_id,
            status="accepted",
            raw_response=data if isinstance(data, dict) else {"response": data},
        )

    async def poll_generation(self, provider_task_id: str) -> ProviderPollResult:
        if not self.api_key:
            raise ProviderAdapterError(
                public_code="provider_temporarily_unavailable",
                raw_error="ARGOLINK_API_KEY is not configured",
            )
        async with self._http_client() as client:
            try:
                response = await client.get(f"/v1/videos/{provider_task_id}", headers=self._auth_headers())
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise self._http_error_to_provider_error(exc.response) from exc
            except httpx.TimeoutException as exc:
                raise ProviderAdapterError("provider_temporarily_unavailable", "argolink_timeout") from exc
            except httpx.HTTPError as exc:
                raise ProviderAdapterError("provider_temporarily_unavailable", type(exc).__name__) from exc
        data = response.json()
        status = self._normalize_video_status(data)
        result_url = f"{self.base_url}/v1/videos/{provider_task_id}/content" if status == "completed" else None
        raw_error = self._extract_error(data) if status == "failed" else None
        return ProviderPollResult(status=status, result_url=result_url, raw_error=raw_error)

    async def open_result_stream(
        self,
        provider_content_url: str,
        *,
        range_header: str | None = None,
    ) -> ProviderResultStream:
        if not provider_content_url.startswith(f"{self.base_url}/v1/videos/"):
            raise ProviderAdapterError("provider_rejected_request", "unexpected_result_url")
        if not self.api_key:
            raise ProviderAdapterError(
                public_code="provider_temporarily_unavailable",
                raw_error="ARGOLINK_API_KEY is not configured",
            )

        client = self._client
        headers = self._auth_headers()
        if range_header:
            headers["Range"] = range_header

        response: httpx.Response | None = None
        try:
            request = client.build_request("GET", provider_content_url, headers=headers)
            response = await client.send(request, stream=True, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if response is not None:
                await response.aclose()
            raise self._http_error_to_provider_error(exc.response) from exc
        except httpx.TimeoutException as exc:
            if response is not None:
                await response.aclose()
            raise ProviderAdapterError("provider_temporarily_unavailable", "argolink_timeout") from exc
        except httpx.HTTPError as exc:
            if response is not None:
                await response.aclose()
            raise ProviderAdapterError("provider_temporarily_unavailable", type(exc).__name__) from exc

        async def body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        content_length: int | None = None
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
            public_code="provider_temporarily_unavailable",
            raw_error=type(error).__name__,
        )

    def _auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    def _http_client(self) -> "_BorrowedAsyncClient":
        return _BorrowedAsyncClient(self._client)

    @staticmethod
    def _extract_task_id(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("request_id", "id", "task_id", "generation_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        nested = data.get("data")
        if isinstance(nested, dict):
            return ArgoLinkAdapter._extract_task_id(nested)
        return None

    @staticmethod
    def _http_error_to_provider_error(response: httpx.Response) -> ProviderAdapterError:
        if response.status_code in {401, 403}:
            return ProviderAdapterError("provider_temporarily_unavailable", "argolink_auth_failed")
        if response.status_code == 429:
            return ProviderAdapterError("provider_temporarily_unavailable", "argolink_rate_limited")
        if response.status_code >= 500:
            return ProviderAdapterError("provider_temporarily_unavailable", f"argolink_{response.status_code}")
        return ProviderAdapterError("provider_rejected_request", f"argolink_{response.status_code}")

    @staticmethod
    def _normalize_video_status(data: Any) -> str:
        if not isinstance(data, dict):
            return "processing"
        raw_status = str(data.get("status", "")).lower()
        if raw_status in {"done", "completed", "succeeded", "success"}:
            return "completed"
        if raw_status in {"failed", "expired", "error"}:
            return "failed"
        if raw_status in {"pending", "queued", "processing", "running"}:
            return "processing"
        return "processing"

    @staticmethod
    def _extract_error(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        error = data.get("error")
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail") or error.get("code")
            return str(message) if message is not None else str(error)
        return None


class _BorrowedAsyncClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None
