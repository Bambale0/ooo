"""Opt-in image limits from the reviewed model contract, not provider guesses."""

import base64
import re
from urllib.parse import urlsplit


def validate_profile_request(protocol, body, profile, *, multipart=False):
    if set(body) - set(profile["request_fields"]):
        raise ValueError("unsupported_generation_control")
    if profile["frame"] == "size":
        size = body.get("size", "auto")
        if size != "auto":
            if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]{0,3}x[1-9][0-9]{0,3}", size):
                raise ValueError("invalid_size")
            if max(int(side) for side in size.split("x")) > profile["max_size_edge"]:
                raise ValueError("unsupported_size")
        if body.get("quality", profile["default_quality"]) not in profile["quality"]:
            raise ValueError("unsupported_quality")
    elif body.get("aspect_ratio", profile["default_aspect_ratio"]) not in profile["aspect_ratios"]:
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
    # Some cards publish only a reference count. Do not invent a supplier byte
    # limit; the gateway's total request bound still applies at ingress.
    maximum = profile.get("max_reference_mb")
    maximum = maximum * 1024 * 1024 if maximum is not None else None
    for ref in refs or []:
        if not isinstance(ref, dict) or set(ref) != {"image_url"} or not isinstance(ref["image_url"], str):
            raise ValueError("invalid_reference_images")
        url = ref["image_url"]
        if url.startswith("data:image/"):
            header, separator, encoded = url.partition(",")
            if (
                not separator or not header.endswith(";base64")
                or (maximum is not None and len(encoded) > 4 * ((maximum + 2) // 3))
            ):
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
        if maximum is not None and total > maximum:
            raise ValueError("reference_limit_exceeded")
    # HTTPS bytes remain subject to the upstream aggregate reference-size cap.
    # Do not download arbitrary input URLs inside an admission transaction.


def validate_profile_files(files, profile):
    allowed = {"image", "image[]", "images", "images[]"}
    if any(name not in allowed for name, _ in files):
        raise ValueError("invalid_reference_images")
    if any(not content for _, (_, content, _) in files):
        raise ValueError("invalid_reference_images")
    maximum = profile.get("max_reference_mb")
    if maximum is not None and sum(len(content) for _, (_, content, _) in files) > maximum * 1024 * 1024:
        raise ValueError("reference_limit_exceeded")
