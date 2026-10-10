"""Connect to the exact validated address; preserve Host and TLS SNI.

No redirects or environment proxies. Disabling idle connection reuse avoids
sharing a TLS connection across different hostnames resolving to the same IP.
"""

import asyncio
import ipaddress
import socket

import httpx

_TRANSLATION_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32",
))


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return bool(
        address.is_global and not address.is_multicast and not address.is_reserved
        and not getattr(address, "is_site_local", False)
        and not any(address in network for network in _TRANSLATION_NETWORKS)
    )


class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__(limits=httpx.Limits(max_connections=40, max_keepalive_connections=0))

    async def handle_async_request(self, request):
        url = request.url
        if url.scheme != "https" or url.username or url.password:
            raise httpx.ConnectError("public_https_required", request=request)
        try:
            infos = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, url.host, url.port or 443, type=socket.SOCK_STREAM),
                5,
            )
            addresses = list(dict.fromkeys(info[4][0] for info in infos))
            if not addresses or not all(_public_address(ip) for ip in addresses):
                raise ValueError("non_public_address")
        except (OSError, ValueError, TimeoutError) as exc:
            raise httpx.ConnectError("public_address_required", request=request) from exc
        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=addresses[0]),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        return await super().handle_async_request(pinned)


def public_http_client(**kwargs):
    return httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False, follow_redirects=False, **kwargs)
