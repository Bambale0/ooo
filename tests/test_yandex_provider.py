from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.dashboard import yandex
from app.infrastructure.config import get_settings


@pytest.mark.parametrize(
    "failure",
    [None, "token_400", "info_500", "wrong_client", "bad_subject", "bad_token", "bad_json", "timeout", "redirect"],
)
async def test_yandex_exchange_uses_pkce_and_header_token(monkeypatch, failure):
    s = get_settings()
    monkeypatch.setattr(s, "yandex_client_id", "client-id")
    monkeypatch.setattr(s, "yandex_client_secret", SecretStr("private-client-secret"))
    monkeypatch.setattr(s, "yandex_callback_url", "https://testserver/cabinet/yandex/callback")
    original_client = httpx.AsyncClient
    seen = []

    def provider(request):
        seen.append(request)
        if request.url.path == "/token":
            assert request.url == "https://oauth.yandex.ru/token"
            assert request.method == "POST"
            fields = parse_qs(request.content.decode())
            assert fields == {
                "grant_type": ["authorization_code"],
                "code": ["provider-code"],
                "client_id": ["client-id"],
                "client_secret": ["private-client-secret"],
                "redirect_uri": [s.yandex_callback_url],
                "code_verifier": ["verifier"],
            }
            if failure == "timeout":
                raise httpx.ReadTimeout("provider unavailable")
            if failure == "token_400":
                return httpx.Response(400, json={"error": "invalid_grant"})
            if failure == "bad_json":
                return httpx.Response(200, text="not json")
            if failure == "redirect":
                return httpx.Response(302, headers={"location": "https://attacker.example/token"})
            return httpx.Response(200, json={"access_token": None if failure == "bad_token" else "private-token"})
        assert request.url == "https://login.yandex.ru/info?format=json"
        assert request.headers["authorization"] == "OAuth private-token"
        assert "private-token" not in str(request.url)
        return httpx.Response(
            500 if failure == "info_500" else 200,
            json={
                "id": "bad-subject" if failure == "bad_subject" else "1234567",
                "client_id": "other-client" if failure == "wrong_client" else "client-id",
                "default_email": "irrelevant@yandex.ru",
            },
        )

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 10
        return original_client(**kwargs, transport=httpx.MockTransport(provider))

    monkeypatch.setattr(yandex.httpx, "AsyncClient", client)
    if failure:
        with pytest.raises((httpx.HTTPError, ValueError)):
            await yandex._subject("provider-code", "verifier")
    else:
        assert await yandex._subject("provider-code", "verifier") == "1234567"
        assert len(seen) == 2


def test_production_config_validates_oauth_even_with_explicit_settings(monkeypatch):
    from app.infrastructure.config import Settings
    from app.infrastructure.production_check import validate_production_config

    s = Settings(app_env="production", yandex_client_id="client", _env_file=None)
    monkeypatch.setattr("app.infrastructure.production_check.get_settings", lambda: s)
    assert any("YANDEX" in error for error in validate_production_config())
