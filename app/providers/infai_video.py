"""Seedance inference; management tokens must never reach this adapter."""

import re
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from app.providers.base import (
    ProviderAdapterError,
    ProviderGenerationRequest,
    ProviderPollResult,
    ProviderResultStream,
    ProviderSubmitResult,
)
from app.providers.http_client import get_provider_http_client

INFAI_CREDENTIAL_LABEL = "infai-seedance-1"
MODELS = {
    "seedance-2.0": "doubao-seedance-2-0-260128",
    "seedance-2.5": "doubao-seedance-2-5-260628",
}
BASE = "https://infai.cc"
TASKS = "/api/v3/contents/generations/tasks"
_RATIOS = {"16:9", "9:16", "4:3", "3:4", "1:1", "21:9"}
_NATIVE_FIELDS = {
    "model",
    "prompt",
    "duration",
    "seconds",
    "resolution",
    "aspect_ratio",
    "ratio",
    "size",
    "generate_audio",
    "watermark",
    "omni_reference_task_type",
    "reference_images",
    "image_urls",
    "reference_videos",
    "video_urls",
    "reference_audios",
    "audio_urls",
    "input_references",
    "start_image",
    "image_url",
    "end_image",
    "end_image_url",
    "frame_images",
    "n",
}
_CDN_HOSTS = {
    # Authenticated InfAI task result: public DNS, valid TLS, MP4 Range 206 verified.
    "videos.tpkcur.xyz",
    "ark-acg-cn-beijing.tos-cn-beijing.volces.com",
    "ark-acg-ap-southeast-1.tos-ap-southeast-1.volces.com",
    "ark-content-generation-ap-southeast-1.tos-ap-southeast-1.volces.com",
}


def _effective_ratio(payload: ProviderGenerationRequest) -> str | None:
    # Seedance 2.0/2.5 define an omitted ratio as the provider's adaptive mode.
    # Make that default explicit when routing between providers so the fallback
    # preserves the accepted request instead of being rejected as incompatible.
    return payload.aspect_ratio or "adaptive"


def infai_supports_request(payload: ProviderGenerationRequest) -> bool:
    if payload.model_slug not in MODELS or payload.mode != "videos/generations":
        return False
    native = payload.native_body
    if not isinstance(native, dict) or set(native) - _NATIVE_FIELDS:
        return False
    if native.get("model") != payload.model_slug or native.get("n", 1) != 1:
        return False
    if "generate_audio" in native and not isinstance(native["generate_audio"], bool):
        return False
    ratio = _effective_ratio(payload)
    if payload.model_slug == "seedance-2.5":
        if (
            payload.resolution not in {"480p", "720p"}
            or ratio not in _RATIOS | {"adaptive"}
            or not 4 <= payload.duration_seconds <= 30
            or not payload.prompt.strip()
            or payload.start_image
            or payload.end_image
            or payload.reference_videos
            or payload.reference_audios
            or "watermark" in native
            or native.get("omni_reference_task_type", "reference") != "reference"
        ):
            return False
        limit = 30
    else:
        if (
            payload.resolution not in {"480p", "720p", "1080p", "4k"}
            or ratio not in _RATIOS | {"adaptive"}
            or not 4 <= payload.duration_seconds <= 15
            or native.get("omni_reference_task_type", "auto") not in {"auto", "reference"}
        ):
            return False
        if payload.start_image or payload.end_image:
            if payload.reference_images or payload.reference_videos or payload.reference_audios:
                return False
            if not payload.start_image:
                return False
        elif not payload.prompt.strip() and not (payload.reference_images or payload.reference_videos):
            return False
        if len(payload.reference_videos) > 3 or len(payload.reference_audios) > 3:
            return False
        if payload.reference_audios and not (payload.reference_images or payload.reference_videos):
            return False
        if len(payload.reference_images) + len(payload.reference_videos) + len(payload.reference_audios) > 12:
            return False
        limit = 9
    if len(payload.reference_images) > limit:
        return False
    try:
        from app.contracts.registry import normalized_video

        normalized = normalized_video(native)
    except (AttributeError, TypeError, ValueError):
        return False

    def urls(field):
        items = normalized.get(field, [])
        if not isinstance(items, list):
            raise ValueError
        values = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"url"} or not isinstance(item["url"], str):
                raise ValueError
            values.append(item["url"])
        return tuple(values)

    def media_url(field):
        item = normalized.get(field)
        if item is None:
            return None
        if not isinstance(item, dict) or set(item) != {"url"} or not isinstance(item["url"], str):
            raise ValueError
        return item["url"]

    try:
        return (
            urls("reference_images") == payload.reference_images
            and urls("reference_videos") == payload.reference_videos
            and urls("reference_audios") == payload.reference_audios
            and media_url("start_image") == payload.start_image
            and media_url("end_image") == payload.end_image
        )
    except ValueError:
        return False


def infai_price_per_million(payload: ProviderGenerationRequest, no_video_rate: Decimal) -> Decimal:
    """Select the exact Seedance-1 rate; capability stores the no-video row."""
    if payload.model_slug != "seedance-2.0" or not payload.reference_videos:
        return no_video_rate
    factors = {
        "480p": (Decimal(430), Decimal(700)),
        "720p": (Decimal(430), Decimal(700)),
        "1080p": (Decimal(470), Decimal(770)),
        "4k": (Decimal(240), Decimal(400)),
    }
    numerator, denominator = factors[payload.resolution]
    return no_video_rate * numerator / denominator


def infai_cost_ceiling(payload: ProviderGenerationRequest, price_per_million: Decimal) -> Decimal:
    """Conservative admission bound; actual cost uses returned completion_tokens.

    Reviewed 24fps, fixed ratios, no input video. 720p is about 21,600 tokens/s;
    24,000 (480p: 11,000) allows raster/frame rounding. Exceeding this bound
    requires reconciliation, never an additional partner charge.
    """
    tokens_per_second = {"480p": 11000, "720p": 24000, "1080p": 50000, "4k": 200000}[
        payload.resolution
    ]
    billable_seconds = payload.duration_seconds + (15 if payload.reference_videos else 0)
    return price_per_million * Decimal(tokens_per_second * billable_seconds) / Decimal(1_000_000)


class InfaiVideoAdapter:
    provider_name = "infai"

    def __init__(self, *, api_key: str | None = None, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self._client = client or get_provider_http_client("infai")

    def _headers(self):
        if not self.api_key:
            raise ProviderAdapterError("provider_rejected_request", "infai_inference_key_missing", retryable=False)
        return {"Authorization": f"Bearer {self.api_key}"}

    async def prepaid_balance_usdt(self) -> Decimal:
        raise ProviderAdapterError("provider_temporarily_unavailable", "infai_wallet_not_verified", retryable=False)

    async def validate_key(self, api_key: str) -> bool:
        if not api_key:
            return False
        try:
            response = await self._client.get("/v1/models", headers={"Authorization": f"Bearer {api_key}"})
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def health_check(self) -> bool:
        return await self.validate_key(self.api_key or "")

    @staticmethod
    def _task_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
            raise ProviderAdapterError("provider_temporarily_unavailable", "infai_invalid_task_id", retryable=False)
        return value

    async def _request(self, method: str, path: str, *, body=None):
        submitting = method == "POST"
        try:
            response = await self._client.request(
                method,
                path,
                json=body,
                headers=self._headers(),
                follow_redirects=False,
            )
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as exc:
            raise ProviderAdapterError("provider_temporarily_unavailable", type(exc).__name__, retryable=True) from None
        except httpx.HTTPError:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "infai_transport_error",
                retryable=not submitting,
            ) from None
        if not response.is_success:
            # A gateway 5xx/408/redirect cannot prove a paid POST was not accepted.
            rejected = response.status_code in {400, 401, 402, 403, 404, 422}
            raise ProviderAdapterError(
                "provider_rejected_request" if rejected else "provider_temporarily_unavailable",
                f"infai_http_{response.status_code}",
                retryable=not submitting and not rejected,
            )
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            if "code" in data:
                if data["code"] != 0 or not isinstance(data.get("data"), dict):
                    raise ValueError
                data = data["data"]
            return data
        except ValueError:
            raise ProviderAdapterError(
                "provider_temporarily_unavailable",
                "infai_invalid_response",
                retryable=not submitting,
            ) from None

    async def submit_generation(self, payload: ProviderGenerationRequest) -> ProviderSubmitResult:
        if not infai_supports_request(payload):
            raise ProviderAdapterError("provider_rejected_request", "infai_capability_mismatch", retryable=False)
        content = ([{"type": "text", "text": payload.prompt}] if payload.prompt else [])
        if payload.start_image:
            content.append(
                {"type": "image_url", "image_url": {"url": payload.start_image}, "role": "first_frame"}
            )
            if payload.end_image:
                content.append(
                    {"type": "image_url", "image_url": {"url": payload.end_image}, "role": "last_frame"}
                )
        else:
            content.extend(
                {"type": "image_url", "image_url": {"url": url}, "role": "reference_image"}
                for url in payload.reference_images
            )
            content.extend(
                {"type": "video_url", "video_url": {"url": url}, "role": "reference_video"}
                for url in payload.reference_videos
            )
            content.extend(
                {"type": "audio_url", "audio_url": {"url": url}, "role": "reference_audio"}
                for url in payload.reference_audios
            )
        body = {
            "model": MODELS[payload.model_slug],
            "content": content,
            "duration": payload.duration_seconds,
            "resolution": payload.resolution,
            "ratio": _effective_ratio(payload),
        }
        fields = (
            ("generate_audio", "omni_reference_task_type")
            if payload.model_slug == "seedance-2.5"
            else ("generate_audio", "watermark")
        )
        for field in fields:
            if field in payload.native_body:
                body[field] = payload.native_body[field]
        data = await self._request("POST", TASKS, body=body)
        task_id = self._task_id(data.get("id", data.get("task_id")))
        # Do not retain response bodies that could echo prompts or credentials.
        return ProviderSubmitResult(provider_task_id=task_id, raw_response={"id": task_id})

    async def poll_generation(self, provider_task_id: str) -> ProviderPollResult:
        task_id = self._task_id(provider_task_id)
        data = await self._request("GET", f"{TASKS}/{task_id}")
        status = data.get("status")
        if status in {"queued", "running", "processing", "pending"}:
            return ProviderPollResult(status="processing")
        if status in {"failed", "expired", "cancelled"}:
            error = data.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            code = (
                code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) else "infai_task_failed"
            )
            return ProviderPollResult(status="failed", raw_error=code, error_code=code)
        if status in {"succeeded", "succeed", "completed"}:
            content = data.get("content")
            if not isinstance(content, dict) or not self._cdn_url(content.get("video_url")):
                raise ProviderAdapterError("provider_temporarily_unavailable", "infai_result_unavailable")
            usage = data.get("usage")
            tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
            seconds = data.get("duration")
            return ProviderPollResult(
                status="completed",
                result_url=f"{BASE}{TASKS}/{task_id}/content",
                usage={"completion_tokens": tokens, "billed_seconds": seconds},
            )
        raise ProviderAdapterError("provider_temporarily_unavailable", "infai_unknown_status")

    @staticmethod
    def _cdn_url(value) -> bool:
        if not isinstance(value, str):
            return False
        try:
            url = urlsplit(value)
            return (
                url.scheme == "https"
                and url.hostname in _CDN_HOSTS
                and url.port in {None, 443}
                and url.username is None
                and url.password is None
                and not url.fragment
            )
        except ValueError:
            return False

    async def open_result_stream(self, provider_content_url: str, *, range_header: str | None = None):
        match = re.fullmatch(re.escape(BASE + TASKS) + r"/([A-Za-z0-9_-]{1,255})/content", provider_content_url)
        if not match:
            raise ProviderAdapterError("provider_rejected_request", "unexpected_result_url", retryable=False)
        data = await self._request("GET", f"{TASKS}/{match[1]}")
        content = data.get("content")
        url = content.get("video_url") if isinstance(content, dict) else None
        if not self._cdn_url(url):
            raise ProviderAdapterError("provider_rejected_request", "unexpected_result_url", retryable=False)
        headers = {"Accept-Encoding": "identity"}
        if range_header:
            headers["Range"] = range_header
        response = None
        try:
            # Raw request prevents forwarding API authorization/cookies to storage.
            response = await self._client.send(
                httpx.Request("GET", url, headers=headers), stream=True, follow_redirects=False
            )
            response.raise_for_status()
        except httpx.HTTPError:
            if response is not None:
                await response.aclose()
            raise ProviderAdapterError("provider_temporarily_unavailable", "infai_media_unavailable") from None

        async def body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        length = response.headers.get("Content-Length", "")
        return ProviderResultStream(
            body=body(),
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type"),
            content_length=int(length) if length.isdigit() else None,
            content_range=response.headers.get("Content-Range"),
            accept_ranges=response.headers.get("Accept-Ranges"),
        )

    def normalize_error(self, error: Exception) -> ProviderAdapterError:
        if isinstance(error, ProviderAdapterError):
            return error
        return ProviderAdapterError("provider_temporarily_unavailable", type(error).__name__, retryable=False)
