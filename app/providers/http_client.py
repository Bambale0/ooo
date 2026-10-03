from __future__ import annotations

from threading import Lock

import httpx

from app.infrastructure.config import get_settings

_clients: dict[str, httpx.AsyncClient] = {}
_clients_lock = Lock()


def get_provider_http_client(provider: str) -> httpx.AsyncClient:
    with _clients_lock:
        existing = _clients.get(provider)
        if existing is not None and not existing.is_closed:
            return existing

        client = _create_provider_http_client(provider)
        _clients[provider] = client
        return client


async def close_provider_http_clients() -> None:
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()

    for client in clients:
        if not client.is_closed:
            await client.aclose()


def _create_provider_http_client(provider: str) -> httpx.AsyncClient:
    settings = get_settings()
    if provider == "infai":
        # Management credentials are scoped to this verified origin, never redirects.
        return httpx.AsyncClient(
            base_url="https://infai.cc", timeout=httpx.Timeout(20, connect=5, pool=5),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            trust_env=False, follow_redirects=False,
        )
    if provider == "argolink":
        prefix = "argolink"
    elif provider == "asale":
        prefix = "asale"
    else:
        raise ValueError(f"Unsupported provider: {provider}")

    timeout = httpx.Timeout(
        connect=getattr(settings, f"{prefix}_http_connect_timeout_seconds"),
        read=getattr(settings, f"{prefix}_http_read_timeout_seconds"),
        write=getattr(settings, f"{prefix}_http_write_timeout_seconds"),
        pool=getattr(settings, f"{prefix}_http_pool_timeout_seconds"),
    )
    max_connections = getattr(settings, f"{prefix}_http_max_connections")
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=min(
            getattr(settings, f"{prefix}_http_max_keepalive_connections"),
            max_connections,
        ),
        keepalive_expiry=getattr(settings, f"{prefix}_http_keepalive_expiry_seconds"),
    )
    return httpx.AsyncClient(
        base_url=getattr(settings, f"{prefix}_base_url").rstrip("/"),
        headers={"Content-Type": "application/json"},
        timeout=timeout,
        limits=limits,
        trust_env=False,
    )
