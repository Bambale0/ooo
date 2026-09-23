from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.infrastructure.config import get_settings


class CryptoPayError(RuntimeError):
    pass


@dataclass(frozen=True)
class CryptoPayInvoice:
    invoice_id: int
    status: str
    amount: str
    bot_invoice_url: str
    payload: str | None
    expiration_date: str | None
    paid_at: str | None
    paid_asset: str | None
    paid_amount: str | None
    paid_fiat_rate: str | None
    paid_usd_rate: str | None


class CryptoPayClient:
    def __init__(
        self,
        *,
        api_token: str,
        base_url: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._borrowed_client = client
        self._owned_client: httpx.AsyncClient | None = None

    async def create_rub_invoice(self, *, amount_rub: str, payload: str) -> CryptoPayInvoice:
        data = await self._request(
            "createInvoice",
            {
                "currency_type": "fiat",
                "fiat": "RUB",
                "amount": amount_rub,
                "accepted_assets": "USDT,TON",
                "swap_to": "USDT",
                "expires_in": 3600,
                "payload": payload,
                "allow_comments": False,
            },
        )
        return _parse_invoice(data)

    async def get_invoice(self, invoice_id: int) -> CryptoPayInvoice | None:
        data = await self._request("getInvoices", {"invoice_ids": str(invoice_id), "count": 1})
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CryptoPayError("invalid_invoice_list")
        for item in items:
            if isinstance(item, dict) and int(item.get("invoice_id", 0)) == invoice_id:
                return _parse_invoice(item)
        return None

    async def find_invoice_by_payload(self, payload: str) -> CryptoPayInvoice | None:
        data = await self._request("getInvoices", {"count": 1000})
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CryptoPayError("invalid_invoice_list")
        matches = [_parse_invoice(item) for item in items if isinstance(item, dict) and item.get("payload") == payload]
        return max(matches, key=lambda invoice: invoice.invoice_id, default=None)

    async def get_available_usdt(self) -> Decimal:
        rows = await self._request("getBalance", {})
        if not isinstance(rows, list):
            raise CryptoPayError("invalid_wallet_balance")
        for row in rows:
            if not isinstance(row, dict):
                raise CryptoPayError("invalid_wallet_balance")
            if row.get("currency_code") == "USDT":
                try:
                    amount = Decimal(str(row["available"]))
                except (KeyError, InvalidOperation) as exc:
                    raise CryptoPayError("invalid_wallet_balance") from exc
                if not amount.is_finite() or amount < 0:
                    raise CryptoPayError("invalid_wallet_balance")
                return amount
        raise CryptoPayError("usdt_balance_missing")

    async def get_rub_per_usdt(self) -> Decimal:
        rows = await self._request("getExchangeRates", {})
        if isinstance(rows, list):
            for row in rows:
                if (
                    isinstance(row, dict)
                    and row.get("source") == "USDT"
                    and row.get("target") == "RUB"
                    and row.get("is_valid") is True
                    and isinstance(row.get("rate"), str)
                ):
                    try:
                        rate = Decimal(row["rate"])
                    except InvalidOperation as exc:
                        raise CryptoPayError("invalid_exchange_rate") from exc
                    if rate.is_finite() and 0 < rate < Decimal("1000000000000"):
                        return rate
        raise CryptoPayError("exchange_rate_unavailable")

    async def delete_invoice(self, invoice_id: int) -> None:
        await self._request("deleteInvoice", {"invoice_id": invoice_id})

    async def close(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    async def _request(self, method: str, payload: dict[str, object]) -> Any:
        try:
            response = await self._client().post(
                f"/api/{method}",
                json=payload,
                headers={"Crypto-Pay-API-Token": self.api_token},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CryptoPayError(type(exc).__name__) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise CryptoPayError("invalid_provider_response") from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            error = data.get("error") if isinstance(data, dict) else None
            raise CryptoPayError(str(error or "crypto_pay_request_failed"))
        return data.get("result")

    def _client(self) -> httpx.AsyncClient:
        if self._borrowed_client is not None:
            return self._borrowed_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                trust_env=False,
            )
        return self._owned_client


_client: CryptoPayClient | None = None


def get_crypto_pay_client() -> CryptoPayClient:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.crypto_pay_api_token:
            raise CryptoPayError("crypto_pay_not_configured")
        _client = CryptoPayClient(
            api_token=settings.crypto_pay_api_token,
            base_url=settings.crypto_pay_base_url,
            timeout_seconds=settings.crypto_pay_timeout_seconds,
        )
    return _client


async def close_crypto_pay_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def _parse_invoice(data: Any) -> CryptoPayInvoice:
    if not isinstance(data, dict):
        raise CryptoPayError("invalid_crypto_pay_invoice")
    return CryptoPayInvoice(
        invoice_id=int(data["invoice_id"]),
        status=str(data.get("status", "")),
        amount=str(data.get("amount", "0")),
        bot_invoice_url=str(data.get("bot_invoice_url") or data.get("pay_url") or ""),
        payload=str(data["payload"]) if data.get("payload") is not None else None,
        expiration_date=str(data["expiration_date"]) if data.get("expiration_date") else None,
        paid_at=str(data["paid_at"]) if data.get("paid_at") else None,
        paid_asset=str(data["paid_asset"]) if data.get("paid_asset") else None,
        paid_amount=str(data["paid_amount"]) if data.get("paid_amount") else None,
        paid_fiat_rate=str(data["paid_fiat_rate"]) if data.get("paid_fiat_rate") else None,
        paid_usd_rate=str(data["paid_usd_rate"]) if data.get("paid_usd_rate") else None,
    )
