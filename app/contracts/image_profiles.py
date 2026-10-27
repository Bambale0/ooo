"""Opt-in image limits from the reviewed model contract, not provider guesses."""

import base64
from urllib.parse import urlsplit


def validate_profile_request(protocol, body, profile, *, multipart=False):
    if set(body) - set(profile["request_fields"]):
        raise ValueError("unsupported_generation_control")
    if body.get("aspect_ratio", profile["default_aspect_ratio"]) not in profile["aspect_ratios"]:
        raise ValueError("unsupported_aspect_ratio")
    if body.get("response_format", "b64_json") not in profile["response_formats"]:
        raise ValueError("unsupported_response_format")
    refs = body.get("images")
    if protocol == "images/generations" and refs is not None:
        raise ValueError("invalid_reference_images")
    if multipart:
        if refs is not None:
            raise ValueError("conflicting_media_inputs")
        return
    if protocol == "images/edits" and not refs:
        raise ValueError("invalid_reference_images")
    total = 0
    maximum = profile["max_reference_mb"] * 1024 * 1024
    for ref in refs or []:
        if not isinstance(ref, dict) or set(ref) != {"image_url"} or not isinstance(ref["image_url"], str):
            raise ValueError("invalid_reference_images")
        url = ref["image_url"]
        if url.startswith("data:image/"):
            header, separator, encoded = url.partition(",")
            if not separator or not header.endswith(";base64") or len(encoded) > 4 * ((maximum + 2) // 3):
                raise ValueError("invalid_reference_images")
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("invalid_reference_images") from exc
            if not content:
                raise ValueError("invalid_reference_images")
            total += len(content)
        else:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("invalid_reference_images")
        if total > maximum:
            raise ValueError("reference_limit_exceeded")
    # HTTPS bytes remain subject to the upstream aggregate reference-size cap.
    # Do not download arbitrary input URLs inside an admission transaction.


def validate_profile_files(files, profile):
    allowed = {"image", "image[]", "images", "images[]"}
    if any(name not in allowed for name, _ in files):
        raise ValueError("invalid_reference_images")
    if any(not content for _, (_, content, _) in files):
        raise ValueError("invalid_reference_images")
    if sum(len(content) for _, (_, content, _) in files) > profile["max_reference_mb"] * 1024 * 1024:
        raise ValueError("reference_limit_exceeded")
