import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

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
from app.providers.video_contract import video_request_body


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

    async def native_request(self, protocol, body, *, files=None, headers=None):
        from app.contracts.registry import PROTOCOLS
        from app.providers.rate_limit import get_provider_rate_limiter

        if protocol not in PROTOCOLS | {"media/uploads"}:
            raise ValueError("unknown_native_protocol")
        await get_provider_rate_limiter(self.provider_name, "submit").acquire()
        request_headers = {**(headers or {}), **self._auth_headers()}
        kwargs = {"json": body} if files is None else {"data": body, "files": files}
        request = self._client.build_request(
            "POST",
            "/v1/" + protocol,
            headers=request_headers,
            timeout=httpx.Timeout(get_settings().native_request_timeout_seconds, connect=5, pool=5),
            **kwargs,
        )
        if files is not None:
            # Shared client has JSON defaults; multipart needs the generated boundary.
            request.headers["Content-Type"] = request.stream.get_headers()["Content-Type"]
        return await self._client.send(request, stream=True, follow_redirects=False)

    async def key_usage(self):
        response = await self._client.get("/v1/usage", headers=self._auth_headers())
        response.raise_for_status()
        data = response.json()
        return {key: data[key] for key in ("isValid", "mode", "status", "quota", "remaining", "unit") if key in data}

    async def prepaid_balance_usdt(self) -> Decimal:
        """Only an explicit cash wallet proves funding; a key quota does not."""
        try:
            response = await self._client.get(
                "/v1/usage", headers=self._auth_headers(), timeout=httpx.Timeout(10, connect=5, pool=5)
            )
            response.raise_for_status()
            data = json.loads(response.content, parse_float=Decimal)
            if (
                not isinstance(data, dict)
                or data.get("isValid") is not True
                or data.get("mode") != "unrestricted"
                or data.get("unit") not in ("USD", "USDT")
                or data.get("quota") is not None
                or data.get("subscription") is not None
                or data.get("status", "active") != "active"
            ):
                raise ValueError("unconfirmed_prepaid_wallet")
            value = data.get("balance")
            if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
                raise ValueError("missing_wallet_balance")
            balance = Decimal(value)
            if not balance.is_finite() or balance < 0:
                raise ValueError("invalid_wallet_balance")
            return balance
        except (httpx.HTTPError, ValueError, InvalidOperation) as exc:
            raise ProviderAdapterError("provider_temporarily_unavailable") from exc

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
            # A protected not-found response is the only expected success for this sentinel.
            # Compare with an invalid-key control; a proxy returning 404 for everything
            # must never activate a credential.
            if response.status_code != 404:
                return False
            try:
                data = response.json()
                control = await client.get(
                    "/v1/videos/00000000-0000-0000-0000-000000000000",
                    headers={"Authorization": "Bearer invalid-contract-probe"},
                )
            except (httpx.HTTPError, ValueError):
                return False
            return isinstance(data, dict) and control.status_code == 401

    async def submit_generation(self, payload: ProviderGenerationRequest) -> ProviderSubmitResult:
        if not self.api_key:
            raise ProviderAdapterError(
                public_code="provider_temporarily_unavailable",
                raw_error="ARGOLINK_API_KEY is not configured",
            )
        try:
            request_body = video_request_body(payload)
        except ValueError as exc:
            raise ProviderAdapterError("provider_rejected_request", str(exc), retryable=False) from exc
        async with self._http_client() as client:
            try:
                response = await client.post("/v1/videos/generations", json=request_body, headers=self._auth_headers())
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                error = self._http_error_to_provider_error(exc.response)
                # Without upstream idempotency, server errors can follow acceptance.
                if exc.response.status_code == 408 or exc.response.status_code >= 500:
                    error = ProviderAdapterError(
                        "provider_temporarily_unavailable",
                        "argolink_submit_outcome_unknown",
                        retryable=False,
                    )
                raise error from exc
            except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError) as exc:
                raise ProviderAdapterError(
                    "provider_temporarily_unavailable",
                    type(exc).__name__,
                    retryable=True,
                ) from exc
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError) as exc:
                raise ProviderAdapterError(
                    "provider_temporarily_unavailable",
                    "argolink_submit_outcome_unknown",
                    retryable=False,
                ) from exc
            except httpx.TimeoutException as exc:
                raise ProviderAdapterError(
                    "provider_temporarily_unavailable",
                    "argolink_submit_outcome_unknown",
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
                "invalid_provider_submit_response",
                retryable=False,
            ) from exc
        provider_task_id = self._extract_task_id(data)
        if provider_task_id is None:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "missing_provider_task_id",
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
                public_code="provider_temporarily_unavailable",
                raw_error="ARGOLINK_API_KEY is not configured",
            )
        self._validate_task_id(provider_task_id)
        async with self._http_client() as client:
            try:
                response = await client.get(f"/v1/videos/{provider_task_id}", headers=self._auth_headers())
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise self._http_error_to_provider_error(exc.response) from exc
            except httpx.TimeoutException as exc:
                raise ProviderAdapterError(
                    "provider_temporarily_unavailable",
                    "argolink_timeout",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderAdapterError(
                    "provider_temporarily_unavailable",
                    type(exc).__name__,
                    retryable=True,
                ) from exc
        try:
            # Preserve monetary decimal literals as JSON-safe strings, never float.
            data = json.loads(response.content, parse_float=str)
        except ValueError as exc:
            raise ProviderAdapterError("provider_temporarily_unavailable", "invalid_provider_poll_response") from exc
        status = self._normalize_video_status(data)
        result_url = f"{self.base_url}/v1/videos/{provider_task_id}/content" if status == "completed" else None
        raw_error = self._extract_error(data) if status == "failed" else None
        usage = data.get("usage")
        error = data.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        # Only an explicitly closed, retryable internal failure permits a new
        # paid job. Empty usage is not evidence that the failed job was free.
        retryable_failure = (
            data.get("status") == "failed"
            and isinstance(error, dict)
            and error_code == "internal_error"
            and error.get("retryable") is True
            and data.get("video") in (None, {})
            and usage in (None, {})
        )
        # Grok's live response reports duration in video, unlike Seedance/Wan.
        if status == "completed" and data.get("model") == "grok-imagine-video-1.5":
            duration = (data.get("video") or {}).get("duration")
            if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
                usage = {"billed_seconds": duration, "output_seconds": duration, "reference_video_seconds": 0}
        return ProviderPollResult(
            status=status,
            result_url=result_url,
            raw_error=raw_error,
            usage=usage,
            error_code=error_code if isinstance(error_code, str) else None,
            retryable_failure=retryable_failure,
        )

    async def open_result_stream(
        self,
        provider_content_url: str,
        *,
        range_header: str | None = None,
    ) -> ProviderResultStream:
        if not re.fullmatch(
            re.escape(self.base_url) + r"/v1/videos/[A-Za-z0-9_-]{1,255}/content", provider_content_url
        ):
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
            # The protected content proxy can stall mid-file even when its CDN
            # object is complete. Resolve a fresh signed object URL server-side;
            # keep provider identifiers and credentials out of the public API.
            metadata = await client.get(
                provider_content_url.removesuffix("/content"),
                headers=self._auth_headers(),
                follow_redirects=False,
                auth=None,
            )
            metadata.raise_for_status()
            try:
                data = metadata.json()
            except ValueError:
                data = None
            video = data.get("video") if isinstance(data, dict) and data.get("status") == "done" else None
            cdn_url = video.get("url") if isinstance(video, dict) else None
            if self._is_result_cdn_url(cdn_url):
                # Raw Request bypasses shared-client default headers/cookies.
                # auth=None also bypasses any default client auth handler.
                download_headers = {"Accept-Encoding": "identity"}
                if range_header:
                    download_headers["Range"] = range_header
                request = httpx.Request(
                    "GET",
                    cdn_url,
                    headers=download_headers,
                    extensions={"timeout": httpx.Timeout(self.timeout_seconds, connect=5, pool=5).as_dict()},
                )
            else:
                request = client.build_request("GET", provider_content_url, headers=headers)
            response = await client.send(request, stream=True, follow_redirects=False, auth=None)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if response is not None:
                await response.aclose()
            raise self._http_error_to_provider_error(exc.response) from exc
        except httpx.TimeoutException as exc:
            if response is not None:
                await response.aclose()
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "argolink_timeout",
                retryable=True,
            ) from exc
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

    @staticmethod
    def _is_result_cdn_url(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            url = urlsplit(value)
            return (
                url.scheme == "https"
                and url.hostname == "ark-acg-ap-southeast-1.tos-ap-southeast-1.volces.com"
                and url.port in (None, 443)
                and url.username is None
                and url.password is None
                and not url.fragment
                and url.path.startswith("/")
            )
        except ValueError:
            return False

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
        value = data.get("request_id")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
            return value
        return None

    @staticmethod
    def _validate_task_id(value: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
            raise ProviderAdapterError("provider_rejected_request", "invalid_provider_task_id", retryable=False)

    @staticmethod
    def _http_error_to_provider_error(response: httpx.Response) -> ProviderAdapterError:
        retry_after_seconds = parse_retry_after_seconds(response.headers.get("Retry-After"))
        if response.status_code in {401, 403}:
            return ProviderAdapterError(
                "provider_temporarily_unavailable",
                "argolink_auth_failed",
                retryable=False,
            )
        if response.status_code in {408, 429} or response.status_code >= 500:
            raw_error = "argolink_rate_limited" if response.status_code == 429 else f"argolink_{response.status_code}"
            return ProviderAdapterError(
                "provider_temporarily_unavailable",
                raw_error,
                retryable=True,
                retry_after_seconds=retry_after_seconds,
            )
        return ProviderAdapterError(
            "provider_rejected_request",
            f"argolink_{response.status_code}",
            retryable=False,
        )

    @staticmethod
    def _normalize_video_status(data: Any) -> str:
        if not isinstance(data, dict):
            raise ProviderAdapterError("provider_temporarily_unavailable", "invalid_provider_poll_response")
        raw_status = str(data.get("status", "")).lower()
        if raw_status in {"done", "completed", "succeeded", "success"}:
            return "completed"
        if raw_status in {"failed", "expired", "error"}:
            return "failed"
        if raw_status in {"pending", "queued", "processing", "running"}:
            return "processing"
        raise ProviderAdapterError("provider_temporarily_unavailable", "unknown_provider_status")

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

