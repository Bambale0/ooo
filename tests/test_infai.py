import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.catalog.models import Model, PartnerPrice
from app.infrastructure.config import get_settings
from tests.test_telegram_cabinet import cabinet as cabinet


def upstream(path):
    payloads = {
        "/api/user/self": {
            "id": 123,
            "group": "default",
            "quota": 4934825,
            "used_quota": 65175,
            "request_count": 2,
            "status": 1,
            "email": "private@example.org",
            "access_token": "secret",
        },
        "/api/user/models": ["gpt-test", "unpriced"],
        "/api/user/self/groups": {
            "current_group": "default",
            "data": {"standard": "Standard"},
            "ratios": {"standard": "0.7"},
            "group_ids": {"standard": 8},
            "uptime": {"8": {"status": "healthy", "has_samples": True}},
        },
        "/api/status": {"quota_per_unit": 500000},
    }
    if path == "/api/pricing_new":
        return {
            "success": True,
            "group_ratio": {"standard": "0.7"},
            "group_model_ratio": {"standard": {"gpt-*": "0.4", "gpt-test": "0.3"}},
            "data": [
                {
                    "model_name": "gpt-test",
                    "quota_type": 0,
                    "model_ratio": "1.25",
                    "completion_ratio": "4",
                    "cache_ratio": "0",
                    "enable_groups": ["standard"],
                    "supported_endpoint_types": ["openai"],
                    "access_token": "must-not-leak",
                },
                {
                    "model_name": "video-test",
                    "quota_type": 1,
                    "model_price": "0.01",
                    "enable_groups": ["standard"],
                    "supported_endpoint_types": ["video"],
                },
            ],
        }
    return {"success": True, "data": payloads[path]}


@pytest.fixture
def infai_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "infai_system_token", SecretStr("test-infai-system-token"))
    monkeypatch.setattr(settings, "infai_user_id", 123)
    return settings


async def test_admin_catalog_endpoint_requires_authentication(client):
    response = await client.get("/api/v1/providers/infai/catalog")
    assert response.status_code == 401


async def test_snapshot_preserves_precision_groups_and_missing_prices(infai_settings):
    from app.providers.infai import InfaiClient

    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == "infai.cc"
        if request.url.path == "/api/status":
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == "Bearer test-infai-system-token"
            assert request.headers["new-api-user"] == "123"
        return httpx.Response(200, json=upstream(request.url.path))

    async with httpx.AsyncClient(base_url="https://infai.cc", transport=httpx.MockTransport(handler)) as http:
        result = await InfaiClient(http).snapshot()
    assert len(calls) == 5
    assert result["account"]["balance_usd"] == Decimal("9.86965")
    assert result["counts"] == {"account_models": 2, "priced_models": 2, "unpriced_account_models": 1}
    model = next(row for row in result["models"] if row["id"] == "gpt-test")
    quote = model["group_quotes"][0]
    assert quote["ratio"] == Decimal("0.3")
    assert quote["base_text_rates_usd_per_million"]["input"] == Decimal("0.75")
    assert quote["base_text_rates_usd_per_million"]["output"] == Decimal("3")
    assert quote["base_text_rates_usd_per_million"]["cached_input"] == 0
    assert next(row for row in result["models"] if row["id"] == "unpriced")["pricing"] is None
    video = next(row for row in result["models"] if row["id"] == "video-test")
    assert video["listed_for_account"] is False
    assert video["group_quotes"][0]["base_text_rates_usd_per_million"] is None
    serialized = json.dumps(result, default=str)
    assert "private@example.org" not in serialized and "must-not-leak" not in serialized
    assert "access_token" not in serialized


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {}, "infai_auth_failed"),
        (302, {}, "infai_unexpected_redirect"),
        (200, {"success": False, "message": "secret upstream error"}, "infai_rejected_request"),
        (200, [], "infai_invalid_response"),
    ],
)
async def test_http_failures_are_sanitized(infai_settings, status, body, code):
    from app.providers.infai import InfaiClient, InfaiError

    async with httpx.AsyncClient(
        base_url="https://infai.cc",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body, headers={"Location": "https://example.org"}),
        ),
    ) as http:
        with pytest.raises(InfaiError, match=code) as caught:
            await InfaiClient(http).snapshot()
        assert "secret" not in str(caught.value)


async def test_missing_configuration_makes_no_request(monkeypatch):
    from app.providers.infai import InfaiClient, InfaiError

    monkeypatch.setattr(get_settings(), "infai_system_token", None)
    with pytest.raises(InfaiError, match="infai_not_configured"):
        await InfaiClient().snapshot()


async def test_catalog_retains_current_retail_and_does_not_write(
    client, admin_headers, db_session, infai_settings, monkeypatch
):
    model = Model(slug="gpt-test", name="Test", modality="llm", status="production")
    db_session.add(model)
    await db_session.flush()
    price = PartnerPrice(
        model_id=model.id,
        mode="input_tokens",
        resolution="default",
        price_rub=Decimal("17.23"),
        provider_cost_usdt=Decimal("0.5"),
        billing_unit="million_tokens",
    )
    db_session.add(price)
    await db_session.commit()
    async with httpx.AsyncClient(
        base_url="https://infai.cc",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=upstream(request.url.path)),
        ),
    ) as http:
        monkeypatch.setattr("app.providers.infai.get_provider_http_client", lambda name: http)
        response = await client.get("/api/v1/providers/infai/catalog", headers=admin_headers)
    assert response.status_code == 200
    payload = response.json()
    match = next(row for row in payload["models"] if row["id"] == "gpt-test")
    assert match["retail_variants"][0]["price_rub"] == "17.23"
    assert payload["retail_prices_changed"] is False
    await db_session.refresh(price)
    assert price.price_rub == Decimal("17.23") and price.provider_cost_usdt == Decimal("0.5")
    assert len((await db_session.execute(select(Model))).scalars().all()) == 1


def test_group_rates_use_exact_then_longest_prefix_and_never_default_unknown_to_one():
    from app.providers.infai import Pricing, group_ratio, ratio_source

    pricing = Pricing.model_validate(
        {
            "data": [],
            "group_ratio": {"a": "0.7", "b": "1.3"},
            "group_model_ratio": {"a": {"gpt-*": "0.4", "gpt-5*": "0.2", "gpt-5-mini": "0"}},
        }
    )
    assert group_ratio(pricing, "a", "gpt-5-mini") == 0
    assert group_ratio(pricing, "a", "gpt-5-pro") == Decimal("0.2")
    assert ratio_source(pricing, "a", "gpt-5-pro") == "pattern:gpt-5*"
    assert group_ratio(pricing, "a", "gpt-4") == Decimal("0.4")
    assert group_ratio(pricing, "a", "other") == Decimal("0.7")
    assert group_ratio(pricing, "b", "gpt-5-mini") == Decimal("1.3")
    assert group_ratio(pricing, "unknown", "gpt-5-mini") is None


async def test_transient_get_retries_and_long_retry_after_is_respected(infai_settings, monkeypatch):
    from app.providers.infai import InfaiClient, InfaiError

    sleeps, calls = [], []

    async def sleep(delay):
        sleeps.append(delay)

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "0"})
            if len(calls) == 1
            else httpx.Response(
                200,
                json={"success": True, "data": []},
            )
        )

    monkeypatch.setattr("app.providers.infai.asyncio.sleep", sleep)
    async with httpx.AsyncClient(base_url="https://infai.cc", transport=httpx.MockTransport(handler)) as http:
        assert (await InfaiClient(http)._get("/api/user/models", {}))["data"] == []
    assert len(calls) == 2 and sleeps == [0]
    async with httpx.AsyncClient(
        base_url="https://infai.cc",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "120"}),
        ),
    ) as http:
        with pytest.raises(InfaiError, match="infai_temporarily_unavailable"):
            await InfaiClient(http)._get("/api/user/models", {})
    assert sleeps == [0]


@pytest.mark.parametrize("body", [b"<html>error</html>", b'{"success":true,"data":NaN}'])
async def test_non_json_and_non_finite_json_rejected(body):
    from app.providers.infai import InfaiClient, InfaiError

    async with httpx.AsyncClient(
        base_url="https://infai.cc",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body),
        ),
    ) as http:
        with pytest.raises(InfaiError, match="infai_invalid_response"):
            await InfaiClient(http)._get("/api/user/models", {})


async def test_response_body_bound(monkeypatch):
    from app.providers.infai import InfaiClient, InfaiError

    monkeypatch.setattr("app.providers.infai.MAX_RESPONSE_BYTES", 10)
    async with httpx.AsyncClient(
        base_url="https://infai.cc",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 11),
        ),
    ) as http:
        with pytest.raises(InfaiError, match="infai_response_too_large"):
            await InfaiClient(http)._get("/api/user/models", {})


def test_csv_export_keeps_all_group_rows_and_is_not_a_formula(tmp_path):
    from app.providers.infai_export import write_report

    report = {
        "models": [
            {
                "id": "=unsafe",
                "listed_for_account": True,
                "pricing": None,
                "group_quotes": [{"group": "a", "ratio": Decimal("0.7")}, {"group": "b", "ratio": Decimal("1.3")}],
            }
        ]
    }
    target = tmp_path / "report"
    write_report(report, target)
    import csv

    with (target / "prices-by-group.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2 and {row["ratio"] for row in rows} == {"0.7", "1.3"}
    assert all(row["model"] == "'=unsafe" for row in rows)
    with pytest.raises(FileExistsError):
        write_report(report, target)


async def test_telegram_infai_overview_is_admin_only(cabinet, infai_settings, monkeypatch):
    from aiogram.methods import AnswerCallbackQuery

    feed, _ = cabinet
    async with httpx.AsyncClient(base_url="https://infai.cc", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=upstream(request.url.path)),
    )) as http:
        monkeypatch.setattr("app.providers.infai.get_provider_http_client", lambda name: http)
        denied = await feed(callback="admin_infai")
        assert len(denied) == 1 and isinstance(denied[0], AnswerCallbackQuery)
        allowed = (await feed(user=999, callback="admin_infai"))[-1]
    assert "$9.86965" in allowed.text and "коэффициент" in allowed.text.lower()
    assert "test-infai-system-token" not in allowed.text and "private@example.org" not in allowed.text


async def test_telegram_infai_shows_progress_before_provider_request(cabinet, monkeypatch):
    from aiogram.methods import EditMessageText

    from app.providers.infai import InfaiError

    feed, bot = cabinet
    progress = []

    async def unavailable(db):
        progress.extend(call.args[1].text for call in bot.session.call_args_list
                        if isinstance(call.args[1], EditMessageText))
        raise InfaiError("infai_temporarily_unavailable", 503)

    monkeypatch.setattr("app.telegram.infai.catalog_with_retail", unavailable)
    await feed(user=999, callback="admin_infai")
    assert any("Запрашиваем" in text for text in progress)


async def test_telegram_infai_stalled_request_releases_navigation(cabinet, monkeypatch):
    import asyncio

    feed, _ = cabinet
    cancelled = False

    async def stalled(db):
        nonlocal cancelled
        try:
            await asyncio.Future()
        finally:
            cancelled = True

    monkeypatch.setattr("app.telegram.infai.catalog_with_retail", stalled)
    monkeypatch.setattr("app.telegram.infai.OVERVIEW_TIMEOUT_SECONDS", 0.01, raising=False)
    async with asyncio.timeout(1):
        methods = await feed(user=999, callback="admin_infai")
    assert cancelled
    assert "не успел" in methods[-1].text
    assert any(b.callback_data == "admin_menu" for row in methods[-1].reply_markup.inline_keyboard for b in row)
    assert "Администрирование" in (await feed(user=999, callback="admin_menu"))[-1].text


async def test_invalid_upstream_pricing_is_not_reported_as_free(infai_settings):
    from app.providers.infai import InfaiClient, InfaiError

    def handler(request):
        payload = upstream(request.url.path)
        if request.url.path == "/api/pricing_new":
            payload["group_ratio"]["standard"] = "-1"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(base_url="https://infai.cc", transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(InfaiError, match="infai_invalid_response"):
            await InfaiClient(http).snapshot()


def test_system_token_is_redacted_in_logs(infai_settings, capsys):
    import logging

    from app.infrastructure.logging import configure_logging

    root = logging.getLogger()
    original, level = root.handlers[:], root.level
    try:
        configure_logging()
        logging.getLogger("infai-test").error("upstream %s", infai_settings.infai_system_token.get_secret_value())
        output = capsys.readouterr().err
        assert "test-infai-system-token" not in output and "[REDACTED]" in output
    finally:
        root.handlers, root.level = original, level
