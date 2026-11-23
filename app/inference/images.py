import base64
import io

from PIL import Image

from app.contracts.registry import MODELS, image_resolution
from app.infrastructure.config import get_settings


class ImageResultDimensionsMismatch(ValueError):
    """Exact image-contract mismatch, with only safe numeric diagnostics."""

    def __init__(self, actual: tuple[int, int], expected: tuple[int, int]) -> None:
        super().__init__("image_result_invalid_dimensions")
        self.actual = actual
        self.expected = expected


def image_usage(body: dict, data: dict, *, reference_count: int | None = None) -> dict[str, int]:
    images = data.get("data")
    if not isinstance(images, list) or not images or len(images) > body.get("n", 1):
        raise ValueError("invalid_image_count")
    units = {}
    for item in images:
        # A size mismatch is only actionable after full image verification and
        # decode. Otherwise a truncated JPEG could masquerade as recoverable.
        profile = MODELS.get(body["model"], {}).get("image")
        if profile:
            if not isinstance(item, dict) or not isinstance(item.get("b64_json"), str) or not item["b64_json"]:
                raise ValueError("image_result_missing_bytes")
            maximum = get_settings().media_image_max_bytes
            if len(item["b64_json"]) > 4 * ((maximum + 2) // 3):
                raise ValueError("image_result_too_large")
            content = base64.b64decode(item["b64_json"], validate=True)
            if not content or len(content) > maximum:
                raise ValueError("image_result_too_large")
            dimension_mismatch = None
            try:
                with Image.open(io.BytesIO(content)) as image:
                    if (
                        (profile.get("output_format") and image.format != profile["output_format"])
                        or image.width * image.height > get_settings().media_image_max_pixels
                    ):
                        raise ValueError("image_result_invalid_format")
                    if profile["frame"] == "size":
                        if max(image.size) > profile["max_size_edge"]:
                            raise ValueError("image_result_invalid_dimensions")
                    else:
                        resolution = image_resolution(body).lower()
                        ratio = body.get("aspect_ratio", profile["default_aspect_ratio"])
                        dimensions = next(
                            row["pixels"][resolution] for row in profile["output_sizes"] if row["aspect_ratio"] == ratio
                        )
                        expected = tuple(int(side) for side in dimensions.split("x"))
                        if image.size != expected:
                            dimension_mismatch = ImageResultDimensionsMismatch(image.size, expected)
                    image.verify()
                # verify() checks structure, not pixels. Reopen only after the
                # configured byte/pixel limits and exact dimensions are checked.
                with Image.open(io.BytesIO(content)) as decoded:
                    decoded.load()
            except Image.DecompressionBombError as exc:
                raise ValueError("image_result_too_large") from exc
            if dimension_mismatch is not None:
                raise dimension_mismatch
        # GPT charges by actual rendered pixels, never by requested dimensions.
        if body["model"].startswith("gpt-image"):
            encoded = item.get("b64_json")
            if not isinstance(encoded, str):
                raise ValueError("image_dimensions_unavailable")
            with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as img:
                edge = max(img.size)
            tier = "1K" if edge <= 1024 else "2K" if edge <= 2048 else "4K"
        else:
            tier = image_resolution(body)
        units[tier] = units.get(tier, 0) + 1
    if MODELS[body["model"]]["procurement"].get("reference_image_price") is not None:
        count = len(body.get("images", [])) if reference_count is None else reference_count
        if count:
            units["reference_images"] = count
    return units


def recoverable_seedream_dimensions(body: dict, mismatch: ImageResultDimensionsMismatch) -> bool:
    """Allow only verified Seedream JPEGs with near-requested framing.

    Accepted off-size outputs are NOT settled as a billed generation; the
    native response is delivered with a cost reconciliation obligation.
    Limits derive from the published model profile, not a mutable price.
    """
    if body.get("model") != "seedream-5.0-pro":
        return False
    profile = MODELS["seedream-5.0-pro"]["image"]
    ratio = body.get("aspect_ratio", profile["default_aspect_ratio"])
    variants = [row for row in profile["output_sizes"] if row["aspect_ratio"] == ratio]
    if len(variants) != 1:
        return False
    upper = tuple(int(v) for v in variants[0]["pixels"]["2k"].split("x"))
    aw, ah = mismatch.actual
    ew, eh = mismatch.expected
    # The provider may use a different raster size without changing framing.
    # Never accept arbitrary aspect ratios, microscopic thumbnails, or a
    # resolution above the documented 2K profile.
    return (
        0 < aw <= upper[0]
        and 0 < ah <= upper[1]
        and aw * 2 >= ew
        and ah * 2 >= eh
        # ArgoLink's observed 848x1104 raster for a 960x1280 1K request
        # differs in framing by 2.36%. Keep it eligible for the existing
        # verified, UNBILLED response path, without changing strict usage
        # settlement or accepting unrelated crops/aspect ratios.
        and abs(aw * eh - ah * ew) * 100 <= 3 * aw * eh
    )


async def inspect_url_images(body: dict, data: dict) -> dict:
    """Read only enough result bytes to identify dimensions, without storing files."""
    from PIL import ImageFile

    from app.infrastructure.public_http import public_http_client

    if MODELS.get(body["model"], {}).get("image"):
        return await _inspect_profile_urls(body, data)
    if not body["model"].startswith("gpt-image"):
        return data
    copied = {**data, "data": [dict(item) for item in data.get("data", [])]}
    async with public_http_client(timeout=20) as client:
        for item in copied["data"]:
            if item.get("b64_json"):
                continue
            if not isinstance(item.get("url"), str):
                raise ValueError("image_dimensions_unavailable")
            parser = ImageFile.Parser()
            header = bytearray()
            async with client.stream("GET", item["url"], headers={"Range": "bytes=0-1048575"}) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    header.extend(chunk)
                    parser.feed(chunk)
                    if parser.image is not None:
                        # image_usage only reads the header; no pixel decompression.
                        item["b64_json"] = base64.b64encode(header).decode()
                        break
                    if len(header) >= 1048576:
                        raise ValueError("image_dimensions_unavailable")
            if not item.get("b64_json"):
                raise ValueError("image_dimensions_unavailable")
    return copied


async def _inspect_profile_urls(body: dict, data: dict) -> dict:
    """Validate the requested representation, then inspect its bounded bytes."""
    from app.infrastructure.public_http import public_http_client

    items = data.get("data")
    if (
        not isinstance(items, list)
        or not items
        or len(items) > body.get("n", 1)
        or any(not isinstance(item, dict) for item in items)
    ):
        raise ValueError("invalid_image_count")
    requested_format = body.get("response_format", "b64_json")
    if requested_format not in {"b64_json", "url"}:
        raise ValueError("unsupported_response_format")
    if any(not isinstance(item.get(requested_format), str) or not item[requested_format] for item in items):
        raise ValueError("image_result_format_mismatch")
    copied = {**data, "data": [dict(item) for item in items]}
    if requested_format == "b64_json":
        return copied
    maximum = get_settings().media_image_max_bytes
    async with public_http_client(timeout=20) as client:
        for item in copied["data"]:
            # Validate the actual URL delivered to the client, even if the
            # provider also supplies a separate base64 representation.
            content = bytearray()
            async with client.stream("GET", item["url"]) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(content) + len(chunk) > maximum:
                        raise ValueError("image_result_too_large")
                    content.extend(chunk)
            item["b64_json"] = base64.b64encode(content).decode()
    return copied
