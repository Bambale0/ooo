from pathlib import Path


def _docs_server_block() -> str:
    config = Path("nginx/production.conf").read_text(encoding="utf-8")
    marker = "server_name docs.нейроныч.online;"
    start = config.index(marker)
    block_start = config.rfind("server {", 0, start)
    next_server = config.find("\nserver {", start)
    return config[block_start:] if next_server == -1 else config[block_start:next_server]


def test_docs_domain_proxies_only_minimal_public_guide():
    block = _docs_server_block()

    assert "proxy_pass http://api/docs;" in block
    assert "location = /docs" in block
    assert "location = /guide" in block
    assert "location / {\n        return 404;" in block

    forbidden = (
        "openapi.json",
        "/prices",
        "/api/v1/billing",
        "/api/v1/providers",
        "/internal/metrics",
    )
    for value in forbidden:
        assert value not in block
