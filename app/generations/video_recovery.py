"""Read-only recovery eligibility for known Argo video accounting holds."""

from sqlalchemy import and_, or_

from app.generations.models import Generation
from app.providers.models import ProviderAttempt
from app.providers.video_contract import VIDEO_MODELS

LEGACY_VIDEO_MODES = ("default", "text_to_video", "image_to_video", "reference", "first_frame", "first_last_frame")


def is_video_generation(generation: Generation) -> bool:
    protocol = (generation.request_payload or {}).get("protocol")
    if protocol is not None:
        return protocol == generation.mode == "videos/generations"
    return generation.mode in LEGACY_VIDEO_MODES and generation.model_slug in VIDEO_MODELS


def is_usage_review(generation: Generation, attempt: ProviderAttempt) -> bool:
    return bool(
        generation.status == attempt.status == "reconciliation_required"
        and generation.public_error_code == "usage_reconciliation_required"
        and attempt.provider == "argolink"
        and attempt.provider_task_id
        and is_video_generation(generation)
    )


def usage_review_clause():
    protocol = Generation.request_payload["protocol"].as_string()
    return and_(
        Generation.status == "reconciliation_required",
        ProviderAttempt.status == "reconciliation_required",
        Generation.public_error_code == "usage_reconciliation_required",
        ProviderAttempt.provider == "argolink",
        ProviderAttempt.provider_task_id.is_not(None),
        ProviderAttempt.provider_task_id != "",
        or_(
            and_(protocol == "videos/generations", Generation.mode == "videos/generations"),
            and_(protocol.is_(None), Generation.mode.in_(LEGACY_VIDEO_MODES), Generation.model_slug.in_(VIDEO_MODELS)),
        ),
    )


def active_video_poll_clause():
    return or_(
        and_(
            Generation.status.in_(("sent_to_provider", "processing", "timeout")),
            ProviderAttempt.status.in_(("accepted", "processing", "retry_pending", "timeout")),
            ProviderAttempt.provider_task_id.is_not(None),
        ),
        usage_review_clause(),
    )
