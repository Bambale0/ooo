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
    if provider != "argolink":
        raise ValueError(f"Unsupported provider: {provider}")

    settings = get_settings()
    timeout = httpx.Timeout(
        connect=settings.argolink_http_connect_timeout_seconds,
        read=settings.argolink_http_read_timeout_seconds,
        write=settings.argolink_http_write_timeout_seconds,
        pool=settings.argolink_http_pool_timeout_seconds,
    )
    limits = httpx.Limits(
        max_connections=settings.argolink_http_max_connections,
        max_keepalive_connections=min(
            settings.argolink_http_max_keepalive_connections,
            settings.argolink_http_max_connections,
        ),
        keepalive_expiry=settings.argolink_http_keepalive_expiry_seconds,
    )
    return httpx.AsyncClient(
        base_url=settings.argolink_base_url.rstrip("/"),
        headers={"Content-Type": "application/json"},
        timeout=timeout,
        limits=limits,
        trust_env=False,
    )
