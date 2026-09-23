import base64
import io

from PIL import Image


def image_usage(body: dict, data: dict) -> dict[str, int]:
    images = data.get("data")
    if not isinstance(images, list) or not images or len(images) > body.get("n", 1):
        raise ValueError("invalid_image_count")
    units = {}
    for item in images:
        # GPT charges by actual rendered pixels, never by requested dimensions.
        if body["model"].startswith("gpt-image"):
            encoded = item.get("b64_json")
            if not isinstance(encoded, str):
                raise ValueError("image_dimensions_unavailable")
            with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as img:
                edge = max(img.size)
            tier = "1K" if edge <= 1024 else "2K" if edge <= 2048 else "4K"
        else:
            tier = str(body.get("resolution", "1K")).upper()
        units[tier] = units.get(tier, 0) + 1
    return units


async def inspect_url_images(body: dict, data: dict) -> dict:
    """Read only enough result bytes to identify dimensions, without storing files."""
    from PIL import ImageFile

    from app.infrastructure.public_http import public_http_client

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
