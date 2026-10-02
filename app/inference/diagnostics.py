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

    def failed(self, attempt: ProviderAttempt, error: Exception, *, phase: str) -> None:
        event = {**self._event(phase), "exception_class": type(error).__name__}
        attempt.raw_error = json.dumps(event, separators=(",", ":"))
        # No exception string or exc_info: they may contain signed URLs or keys.
        logger.warning("native_inference_transport_or_decode_failed", extra=event)
