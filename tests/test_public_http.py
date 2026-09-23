import socket

import httpx
import pytest

from app.infrastructure.public_http import PublicHTTPTransport


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "224.0.0.1"])
async def test_private_addresses_never_connect(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 443))])
    async with httpx.AsyncClient(transport=PublicHTTPTransport()) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://attacker.example/result")


async def test_dns_is_pinned_and_original_tls_name_preserved(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.215.14", 443))])

    async def send(self, request):
        calls.append(request)
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    async with httpx.AsyncClient(transport=PublicHTTPTransport()) as client:
        await client.get("https://partner.example/hook")
    assert calls[0].url.host == "93.184.215.14"
    assert calls[0].headers["Host"] == "partner.example"
    assert calls[0].extensions["sni_hostname"] == "partner.example"
