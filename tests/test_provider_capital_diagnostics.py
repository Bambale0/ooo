from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.billing.capital import provider_capital_state, require_provider_capital
from app.providers.base import ProviderAdapterError


async def test_capital_read_failure_preserves_status_without_secrets(db_session, monkeypatch, caplog):
    request = httpx.Request("GET", "https://provider.invalid/usage?secret=do-not-log")
    cause = httpx.HTTPStatusError(
        "sensitive body do-not-log", request=request, response=httpx.Response(502, request=request)
    )
    error = ProviderAdapterError("provider_temporarily_unavailable", "do-not-log")
    error.__cause__ = cause
    adapter = SimpleNamespace(prepaid_balance_usdt=AsyncMock(side_effect=error))
    monkeypatch.setattr("app.billing.capital.get_partner_provider_adapter", AsyncMock(return_value=adapter))
    with pytest.raises(HTTPException):
        await provider_capital_state(db_session, "test-partner")
    record = next(r for r in caplog.records if r.message == "provider_capital_balance_unavailable")
    assert record.upstream_status == 502 and record.partner_id == "test-partner"
    assert record.error_type == "HTTPStatusError"
    assert "do-not-log" not in str(record.__dict__)


async def test_insufficient_capital_has_distinct_internal_reason(db_session, monkeypatch, caplog):
    monkeypatch.setattr(
        "app.billing.capital.provider_capital_state",
        AsyncMock(
            return_value={
                "balance": Decimal("1"),
                "active": Decimal(".9"),
                "pending": Decimal(0),
                "float": Decimal(0),
                "available": Decimal(".1"),
            }
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await require_provider_capital(db_session, Decimal(".2"), partner_id="test-partner")
    assert exc.value.detail == "provider_temporarily_unavailable"
    record = next(r for r in caplog.records if r.message == "provider_capital_insufficient")
    assert record.required_usdt == "0.2" and record.available_usdt == "0.1"
