"""Native protocol contracts. Economic dimensions are validated before any charge.

Protocol-specific fields (tools, thinking, structured output, etc.) are preserved:
ArgoLink remains authoritative for the underlying model's semantic validation.
"""

import copy
import json
from decimal import Decimal
from importlib.resources import files
from typing import Any

CATALOG = json.loads(files("app.contracts").joinpath("catalog.json").read_text(), parse_float=Decimal)
OBSERVATIONS = json.loads(files("app.contracts").joinpath("observations.json").read_text())
TEXT_PROTOCOLS = {"responses", "chat/completions", "messages"}
IMAGE_PROTOCOLS = {"images/generations", "images/edits"}
VIDEO_PROTOCOL = "videos/generations"
PROTOCOLS = TEXT_PROTOCOLS | IMAGE_PROTOCOLS | {VIDEO_PROTOCOL}
MODELS = CATALOG["models"]
SEEDANCE = {name for name in MODELS if name.startswith("seedance-")}


def contract(model: str, protocol: str) -> dict[str, Any]:
    entry = MODELS.get(model)
    if entry is None:
        raise ValueError("unknown_model_contract")
    expected = "chat" if protocol in TEXT_PROTOCOLS else "image" if protocol in IMAGE_PROTOCOLS else "video"
    if protocol not in PROTOCOLS or entry["category"] != expected:
        raise ValueError("model_protocol_mismatch")
    return entry


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name}")
    return value


def validate_request(protocol: str, original: dict[str, Any]) -> dict[str, Any]:
    """Return a copy without dropping or injecting provider request controls."""
    body = copy.deepcopy(original)
    model = body.get("model")
    if not isinstance(model, str):
        raise ValueError("model_required")
    contract(model, protocol)
    if protocol in TEXT_PROTOCOLS:
        required = "input" if protocol == "responses" else "messages"
        if required not in body:
            raise ValueError(f"{required}_required")
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            if key in body:
                integer(body[key], key, 1, 10_000_000)
        if protocol == "messages" and "max_tokens" not in body:
            raise ValueError("max_tokens_required")
        if "stream" in body and not isinstance(body["stream"], bool):
            raise ValueError("invalid_stream")
        return body
    if not isinstance(body.get("prompt", ""), str):
        raise ValueError("invalid_prompt")
    if protocol in IMAGE_PROTOCOLS:
        maximum = 7 if model.startswith("gpt-image") else 4 if model.startswith("nano-") else 10
        integer(body.get("n", 1), "n", 1, maximum)
        if not body.get("prompt", "").strip():
            raise ValueError("prompt_required")
        if "images" in body:
            refs = body["images"]
            if not isinstance(refs, list) or not 1 <= len(refs) <= (16 if model.startswith("gpt-image") else 3):
                raise ValueError("invalid_reference_images")
        if model.startswith("nano-"):
            if body.get("response_format", "b64_json") != "b64_json":
                raise ValueError("unsupported_response_format")
            if body.get("aspect_ratio", "1:1") not in {"1:1", "16:9", "9:16", "4:3", "3:4"}:
                raise ValueError("unsupported_aspect_ratio")
        if not model.startswith("gpt-image") and str(body.get("resolution", "1k")).lower() not in {"1k", "2k"}:
            raise ValueError("unsupported_resolution")
        image_tier(body)
        return body
    validate_video(body)
    return body


def image_tier(body: dict[str, Any]) -> str:
    size = body.get("size", "auto")
    if size != "auto":
        try:
            dimensions = [int(part) for part in size.split("x")]
        except (ValueError, AttributeError) as exc:
            raise ValueError("invalid_size") from exc
        if len(dimensions) != 2 or min(dimensions) <= 0:
            raise ValueError("invalid_size")
        edge = max(dimensions)
        return "1K" if edge <= 1024 else "2K" if edge <= 2048 else "4K"
    return str(body.get("resolution", "1K")).upper()


def _alias(body: dict, canonical: str, alias: str) -> Any:
    if canonical in body and alias in body and body[canonical] != body[alias]:
        raise ValueError("conflicting_media_aliases")
    return body.get(canonical, body.get(alias))


def normalized_video(body: dict[str, Any]) -> dict[str, Any]:
    """Resolve aliases only for validation/accounting; forward the original wire body."""
    result = copy.deepcopy(body)
    for canonical, alias in (("duration", "seconds"), ("aspect_ratio", "ratio")):
        value = _alias(body, canonical, alias)
        if value is not None:
            result[canonical] = value
    for canonical, alias in (
        ("reference_images", "image_urls"),
        ("reference_videos", "video_urls"),
        ("reference_audios", "audio_urls"),
    ):
        if canonical in body and alias in body:
            raise ValueError("conflicting_media_aliases")
        if alias in body:
            result[canonical] = [{"url": url} for url in body[alias]]
    for canonical, alias in (("start_image", "image_url"), ("end_image", "end_image_url")):
        if canonical in body and alias in body:
            raise ValueError("conflicting_media_aliases")
        if alias in body:
            result[canonical] = {"url": body[alias]}
    if "frame_images" in body:
        if any(field in result for field in ("start_image", "end_image")):
            raise ValueError("conflicting_media_aliases")
        for frame in body["frame_images"]:
            key = {"first_frame": "start_image", "last_frame": "end_image"}.get(frame.get("frame_type"))
            if key is None or key in result:
                raise ValueError("invalid_frame_images")
            result[key] = {"url": frame.get("url")}
    if "input_references" in body:
        if any(field in result for field in ("reference_images", "reference_videos", "reference_audios")):
            raise ValueError("conflicting_media_aliases")
        for ref in body["input_references"]:
            kind = ref.get("type")
            field = {
                "image_url": "reference_images",
                "video_url": "reference_videos",
                "audio_url": "reference_audios",
            }.get(kind)
            if field is None:
                raise ValueError("invalid_input_references")
            value = ref.get(kind)
            result.setdefault(field, []).append({"url": value if isinstance(value, str) else value.get("url")})
    if "size" in body:
        try:
            w, h = (int(part) for part in body["size"].split("x"))
            if min(w, h) <= 0:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValueError("invalid_size") from exc
        tier = "480p" if min(w, h) < 720 else "720p" if min(w, h) < 1080 else "1080p" if min(w, h) < 2160 else "4k"
        if body.get("model") == "minimax-h3":
            # The provider documents 1366x768, but does not specify the 2k
            # short-side mapping. An explicit tier removes billing ambiguity.
            tier = body.get("resolution", "768p" if min(w, h) == 768 else None)
            if tier is None:
                raise ValueError("explicit_resolution_required_for_minimax_size")
        ratios = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
        ratio = min(ratios, key=lambda r: abs(w / h / (int(r.split(":")[0]) / int(r.split(":")[1])) - 1))
        if abs(w / h / (int(ratio.split(":")[0]) / int(ratio.split(":")[1])) - 1) > 0.03:
            raise ValueError("unsupported_size_ratio")
        if result.get("resolution", tier) != tier or result.get("aspect_ratio", ratio) != ratio:
            raise ValueError("conflicting_size")
        result.update(resolution=tier, aspect_ratio=ratio)
    return result


def validate_video(original: dict[str, Any]) -> None:
    from urllib.parse import urlsplit

    body = normalized_video(original)
    model = body["model"]
    seedance = model in SEEDANCE
    minimax = model == "minimax-h3"
    url_video = seedance or minimax
    wan = model == "wan-3"
    maximum = 30 if model in {"seedance-2.5", "wan-3"} else 15
    minimum = 4 if url_video else 2 if wan else 1
    edit = body.get("omni_reference_task_type") == "edit"
    if edit and (model != "seedance-2.5" or "duration" in body):
        raise ValueError("invalid_edit_request")
    if not edit:
        integer(body.get("duration", 5), "duration", minimum, maximum)
    integer(body.get("n", 1), "n", 1, 1)
    resolutions = {t["label"] for t in MODELS[model]["procurement"]["tiers"]}
    if body.get("resolution", "768p" if minimax else "720p") not in resolutions:
        raise ValueError("unsupported_resolution")
    ref_fields = ("reference_images", "reference_videos", "reference_audios")
    counts = []
    limits = (
        (30, 10, 10, 50)
        if model == "seedance-2.5"
        else (9, 3, 3, 15)
        if minimax
        else (9, 3, 3, 12)
        if seedance
        else (10, 5, 5, 20)
        if wan
        else (7, 0, 0, 7)
    )
    refs = []
    for field, limit in zip(ref_fields, limits, strict=False):
        items = body.get(field, [])
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError("reference_limit_exceeded")
        counts.append(len(items))
        refs.extend(items)
    if sum(counts) > limits[3]:
        raise ValueError("reference_limit_exceeded")
    if url_video and model != "seedance-2.5" and counts[2] and not (counts[0] or counts[1]):
        raise ValueError("audio_requires_visual_reference")
    start = body.get("start_image", body.get("image"))
    end = body.get("end_image")
    if (start and refs) or (end and not start) or (edit and (start or not counts[1])):
        raise ValueError("conflicting_media_inputs")
    if url_video and start and "aspect_ratio" in body:
        raise ValueError("frame_aspect_ratio_is_derived_from_input")
    if not url_video and not wan and counts[0] and body.get("resolution") == "1080p":
        raise ValueError("reference_resolution_not_supported")
    ratios = {"1:1", "16:9", "9:16", "4:3", "3:4"}
    ratios |= {"21:9"} if url_video else set() if wan else {"3:2", "2:3"}
    if minimax and refs and not start:
        ratios.add("adaptive")
    if "aspect_ratio" in body and body["aspect_ratio"] not in ratios:
        raise ValueError("unsupported_aspect_ratio")
    prompt = body.get("prompt", "")
    if (not prompt.strip() and not (refs or start)) or ((edit or wan or minimax) and not prompt.strip()):
        raise ValueError("prompt_required")
    if (seedance and len(prompt.encode()) > 40000) or (wan and len(prompt) > 4500):
        raise ValueError("prompt_too_long")
    if url_video:
        if len(json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode()) >= 1024 * 1024:
            raise ValueError("video_body_too_large")
        if "seed" in body or body.get("watermark") is True or body.get("generate_audio") is False:
            raise ValueError("unsupported_generation_control")
        if body.get("omni_reference_task_type", "auto") not in {"auto", "reference", "edit"}:
            raise ValueError("unsupported_task_type")
        for ref in [*refs, *([start] if start else []), *([end] if end else [])]:
            if not isinstance(ref, dict) or not isinstance(ref.get("url"), str):
                raise ValueError("invalid_media_reference")
            url = urlsplit(ref["url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("invalid_media_reference")


def video_reserve_seconds(original: dict[str, Any]) -> int:
    body = normalized_video(original)
    model = body["model"]
    refs = body.get("reference_videos", [])
    output = 30 if body.get("omni_reference_task_type") == "edit" else int(body.get("duration", 5))
    # Provider validates actual media lengths. Reserve the documented upper bound,
    # never trust a client-supplied duration for billable input media.
    ref_bound = 30 if model == "seedance-2.5" else 15
    return min(30, output + ref_bound) if model == "wan-3" and refs else output + (ref_bound if refs else 0)
