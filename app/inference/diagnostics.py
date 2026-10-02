"""Allowlisted native-call diagnostics; never record request/response content."""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import httpx

from app.generations.models import Generation
from app.providers.models import ProviderAttempt

logger = logging.getLogger(__name__)


def log_rejection(
    *,
    trace_id: str,
    partner_id: str,
    api_key_id: str,
    protocol: str,
    failure_stage: str,
    error_code: str,
    http_status: int,
    model: str | None = None,
    generation_id: str | None = None,
    attempt_id: str | None = None,
    upstream_status: int | None = None,
) -> None:
    """One safe, queryable event for every public inference rejection."""

    event: dict[str, str | int] = {
        "trace_id": trace_id,
        "partner_id": partner_id,
        "api_key_id": api_key_id,
        "protocol": protocol,
        "failure_stage": failure_stage,
        "error_code": error_code,
        "http_status": http_status,
    }
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", model):
        event["model"] = model
    if generation_id:
        event["generation_id"] = generation_id
    if attempt_id:
        event["attempt_id"] = attempt_id
    if isinstance(upstream_status, int):
        event["upstream_status"] = upstream_status
    logger.warning("native_inference_rejected", extra=event)



@dataclass
class NativeRequestTrace:
    context: dict[str, str | int]
    started: float = field(default_factory=time.monotonic)
    response_context: dict[str, str | int] = field(default_factory=dict)

    @classmethod
    def begin(cls, generation: Generation, attempt: ProviderAttempt, body: dict, files=None):
        prompt = body.get("prompt")
        images = body.get("images")
        references = len(images) if isinstance(images, list) else 0
        references += sum(name in {"image", "image[]", "images", "images[]"} for name, _ in files or ())
        trace = cls(
            {
                "trace_id": generation.id,
                "generation_id": generation.id,
                "attempt_id": attempt.id,
                "partner_id": generation.partner_id,
                "api_key_id": (generation.request_payload or {}).get("api_key_id"),
                "model_id": generation.model_id,
                "provider": attempt.provider,
                "model": generation.model_slug,
                "protocol": generation.mode,
                "prompt_chars": len(prompt) if isinstance(prompt, str) else 0,
                "image_reference_count": references,
            }
        )
        logger.info("native_inference_submit_started", extra=trace.context)
        return trace

    def _event(self, phase: str) -> dict:
        # Includes adapter rate-limiter wait; this is not an origin-render timer.
        return {
            **self.context,
            **self.response_context,
            "phase": phase,
            "observed_at": datetime.now(UTC).isoformat(),
            "submit_elapsed_ms": round(max(0, time.monotonic() - self.started) * 1000, 3),
        }

    def received_headers(self, attempt: ProviderAttempt, response: httpx.Response) -> None:
        self.response_context = {"upstream_status": response.status_code}
        # Strict shapes prevent arbitrary upstream text/credentials entering logs.
        ray = response.headers.get("cf-ray", "")
        if re.fullmatch(r"[a-fA-F0-9]{16,32}-[A-Z]{3}", ray):
            self.response_context["cf_ray"] = ray
        request_id = response.headers.get("x-request-id", "")
        if len(request_id) == 36:
            try:
                parsed = UUID(request_id)
            except ValueError:
                pass
            else:
                if str(parsed) == request_id.lower():
                    self.response_context["upstream_request_id"] = str(parsed)
        event = self._event("response_headers")
        if not response.is_success:
            attempt.raw_error = json.dumps(event, separators=(",", ":"))
        logger.log(
            logging.INFO if response.is_success else logging.WARNING, "native_inference_upstream_headers", extra=event
        )

    def rejected(self, *, error_code: str, http_status: int) -> None:
        log_rejection(
            trace_id=str(self.context["trace_id"]),
            partner_id=str(self.context["partner_id"]),
            api_key_id=str(self.context["api_key_id"]),
            protocol=str(self.context["protocol"]),
            model=str(self.context["model"]),
            failure_stage="provider_response",
            error_code=error_code,
            http_status=http_status,
            generation_id=str(self.context["generation_id"]),
            attempt_id=str(self.context["attempt_id"]),
            upstream_status=(
                int(self.response_context["upstream_status"])
                if "upstream_status" in self.response_context
                else None
            ),
        )

    def failed(self, attempt: ProviderAttempt, error: Exception, *, phase: str) -> None:
        event = {**self._event(phase), "exception_class": type(error).__name__}
        attempt.raw_error = json.dumps(event, separators=(",", ":"))
        # No exception string or exc_info: they may contain signed URLs or keys.
        logger.warning("native_inference_transport_or_decode_failed", extra=event)
