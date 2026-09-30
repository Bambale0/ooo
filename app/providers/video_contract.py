"""Supported ArgoLink video subset, checked against /en/docs on 2026-09-23.

Video/audio references and editing require additional billable-input accounting
and are deliberately not accepted by the public schema yet.
"""

from urllib.parse import urlsplit

from app.providers.base import ProviderGenerationRequest

SEEDANCE_MODELS = {"seedance-2.0", "seedance-2.0-mini", "seedance-2.0-fast", "seedance-2.5"}
# Seedance 2.5 and its self-developed variants share one limit profile: 4-30s,
# 30 reference images and adaptive-only ratios with a start frame. Membership is
# what decides the profile, never an exact slug, so a new variant cannot silently
# inherit the narrower 2.0 limits.
SEEDANCE_25_FAMILY = {"seedance-2.5", "seedance-2.5-self-developed-nsfw"}
SEEDANCE_20_4K_FAMILY = {"seedance-2.0", "seedance-2.0-self-developed-nsfw"}
SEEDANCE_MODELS |= SEEDANCE_25_FAMILY | SEEDANCE_20_4K_FAMILY
VIDEO_MODELS = SEEDANCE_MODELS | {"grok-imagine-video-1.5", "wan-3"}
SEEDANCE_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
GROK_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"}


def validate_video_request(payload: ProviderGenerationRequest) -> None:
    """Reject unsupported inputs before reserving funds or contacting the provider."""
    seedance = payload.model_slug in SEEDANCE_MODELS
    if payload.model_slug not in VIDEO_MODELS:
        raise ValueError("model_contract_not_supported")
    minimum, maximum = (4, 30 if payload.model_slug in SEEDANCE_25_FAMILY else 15) if seedance else (1, 15)
    if not minimum <= payload.duration_seconds <= maximum:
        raise ValueError("unsupported_duration")
    resolutions = {"480p", "720p", "1080p"}
    if payload.model_slug in SEEDANCE_20_4K_FAMILY:
        resolutions = {"480p", "720p", "1080p", "4k"}
    elif payload.model_slug in {"seedance-2.0-mini", "seedance-2.0-fast"}:
        resolutions = {"480p", "720p"}
    if payload.resolution not in resolutions:
        raise ValueError("unsupported_resolution")
    ratios = SEEDANCE_RATIOS if seedance else GROK_RATIOS
    if seedance and payload.start_image:
        ratios = {"adaptive"} if payload.model_slug in SEEDANCE_25_FAMILY else ratios | {"adaptive"}
    if payload.aspect_ratio and payload.aspect_ratio not in ratios:
        raise ValueError("unsupported_aspect_ratio")
    max_images = (30 if payload.model_slug in SEEDANCE_25_FAMILY else 9) if seedance else 7
    if len(payload.reference_images) > max_images:
        raise ValueError("too_many_reference_images")
    if payload.start_image and payload.reference_images:
        raise ValueError("frames_and_references_are_exclusive")
    if payload.end_image and (not seedance or not payload.start_image):
        raise ValueError("unsupported_end_image")
    if not seedance and payload.reference_images and payload.resolution == "1080p":
        raise ValueError("reference_resolution_not_supported")
    modes = {"default", "text_to_video", "image_to_video", "reference", "first_frame", "first_last_frame"}
    if payload.mode not in modes:
        raise ValueError("unsupported_mode")
    if payload.mode == "text_to_video" and (payload.reference_images or payload.start_image):
        raise ValueError("text_mode_does_not_accept_images")
    if payload.mode in {"image_to_video", "first_frame"} and (not payload.start_image or payload.end_image):
        raise ValueError("first_frame_required")
    if payload.mode == "first_last_frame" and not (payload.start_image and payload.end_image):
        raise ValueError("first_and_last_frame_required")
    if payload.mode == "reference" and payload.start_image:
        raise ValueError("reference_mode_does_not_accept_frames")
    if not payload.prompt.strip() or len(payload.prompt.encode("utf-8")) > 40000:
        raise ValueError("invalid_prompt")
    for url in (*payload.reference_images, payload.start_image, payload.end_image):
        if url is not None:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("invalid_media_url")


def video_request_body(payload: ProviderGenerationRequest) -> dict[str, object]:
    if payload.native_body is not None:
        from app.contracts.registry import validate_request

        return validate_request("videos/generations", payload.native_body)
    validate_video_request(payload)
    body: dict[str, object] = {
        "model": payload.model_slug,
        "prompt": payload.prompt,
        "duration": payload.duration_seconds,
        "resolution": payload.resolution,
    }
    if payload.aspect_ratio:
        body["aspect_ratio"] = payload.aspect_ratio
    if payload.reference_images:
        body["reference_images"] = [{"url": url} for url in payload.reference_images]
    if payload.start_image:
        body["start_image" if payload.model_slug in SEEDANCE_MODELS else "image"] = {"url": payload.start_image}
    if payload.end_image:
        body["end_image"] = {"url": payload.end_image}
    return body
