"""
SSRF protection utilities — validate webhook URLs are safe before submission.
"""
import ipaddress
from urllib.parse import urlparse

_BLOCKED_HOST_PATTERNS = (
    "169.254.",       # link-local
    "127.",           # loopback
    "10.",            # private Class A
    "172.16.",        # private Class B start
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",        # private Class B end
    "192.168.",       # private Class C
    "0.",             # current network
    "255.",           # broadcast
)
_BLOCKED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "[::1]",
}
_BLOCKED_DOMAINS = {
    "metadata.google.internal",
    "metadata.internal",
    "169.254.169.254",  # metadata endpoint
}


def validate_webhook_url(url: str) -> bool:
    """
    Validate that a webhook URL is safe to call (SSRF protection).
    Returns True if the URL is safe, False otherwise.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.hostname or ""
    host_lower = host.lower()

    if host_lower in _BLOCKED_HOSTS:
        return False
    if host_lower in _BLOCKED_DOMAINS:
        return False
    for pattern in _BLOCKED_HOST_PATTERNS:
        if host_lower.startswith(pattern):
            return False

    # Resolve DNS and check for private IP
    try:
        from socket import gaierror, getaddrinfo
        try:
            addrs = getaddrinfo(host, 80)
            for addr in addrs:
                ip = addr[4][0]
                parsed_ip = ipaddress.ip_address(ip)
                if parsed_ip.is_private or parsed_ip.is_loopback or parsed_ip.is_link_local:
                    return False
        except gaierror:
            # DNS resolution failed — could be a transient issue, block to be safe
            return False
    except Exception:
        # If we can't resolve, block to be safe
        return False

    return True