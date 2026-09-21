import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

Resolver = Callable[[str, int], Awaitable[list[str]]]


def validate_webhook_url(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("webhook_url_must_use_https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("webhook_url_credentials_forbidden")
    host = parsed.hostname
    if not host:
        raise ValueError("webhook_url_host_required")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise ValueError("webhook_url_private_destination")
    port = parsed.port or 443

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host, port
    if not address.is_global:
        raise ValueError("webhook_url_private_destination")
    return host, port


async def resolve_public_addresses(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("webhook_url_unresolvable") from exc
    addresses = sorted({item[4][0] for item in infos})
    return addresses


async def ensure_public_webhook_destination(
    url: str,
    *,
    resolver: Resolver = resolve_public_addresses,
) -> None:
    host, port = validate_webhook_url(url)
    addresses = await resolver(host, port)
    if not addresses:
        raise ValueError("webhook_url_unresolvable")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError("webhook_url_unresolvable") from exc
        if not address.is_global:
            raise ValueError("webhook_url_private_destination")
