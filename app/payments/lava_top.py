from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.infrastructure.config import get_settings


class LavaTopError(RuntimeError):
    pass


class LavaTopBuyerEmailRejected(LavaTopError):
    """Lava.top definitively rejected the buyer email before creating an invoice."""


LAVA_TOP_SURCHARGE_RATE = Decimal("0.10")
WHOLE_RUBLE = Decimal("1")


def lava_top_checkout_amount(requested_rub: Decimal | int) -> Decimal:
    """Return the immutable Lava checkout gross for an API-balance top-up."""
    requested = Decimal(requested_rub)
    return (requested * (Decimal(1) + LAVA_TOP_SURCHARGE_RATE)).quantize(
        WHOLE_RUBLE,
        rounding=ROUND_HALF_UP,
    )


@dataclass(frozen=True)
class LavaTopInvoice:
    invoice_id: str
    status: str
    amount: str
    currency: str
    payment_url: str | None
    paid_at: str | None
    buyer_email: str | None


class LavaTopClient:
    def __init__(
        self,
        *,
        api_key: str,
        offer_id: str,
        base_url: str,
        timeout_seconds: float,
        successful_return_url: str | None = None,
        failure_return_url: str | None = None,
        cancel_return_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.offer_id = offer_id
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.successful_return_url = successful_return_url
        self.failure_return_url = failure_return_url
        self.cancel_return_url = cancel_return_url
        self._borrowed_client = client
        self._owned_client: httpx.AsyncClient | None = None

    async def create_sbp_invoice(
        self,
        *,
        amount_rub: int,
        buyer_email: str,
        offer_id: str | None = None,
    ) -> LavaTopInvoice:
        payload: dict[str, object] = {
            "email": buyer_email,
            "offerId": offer_id or self.offer_id,
            "currency": "RUB",
            "amount": amount_rub,
            "paymentProvider": "PAY2ME",
            "paymentMethod": "SBP",
        }
        for key, value in (
            ("successful_return_url", self.successful_return_url),
            ("failure_return_url", self.failure_return_url),
            ("cancel_return_url", self.cancel_return_url),
        ):
            if value:
                payload[key] = value
        data = await self._request("POST", "/api/v3/invoice", payload=payload)
        return _parse_invoice(data, created=True)

    async def get_invoice(self, invoice_id: str) -> LavaTopInvoice | None:
        data = await self._request("GET", f"/api/v1/invoices/{invoice_id}", allow_not_found=True)
        return None if data is None else _parse_invoice(data, created=False)

    async def close(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        try:
            response = await self._client().request(
                method,
                path,
                json=payload,
                headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
            )
            if allow_not_found and response.status_code == 404:
                return None
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                try:
                    provider_error = exc.response.json().get("error")
                except (AttributeError, ValueError):
                    provider_error = None
                if str(provider_error or "").strip().casefold() == "incorrect email to purchase":
                    raise LavaTopBuyerEmailRejected("buyer_email_rejected") from exc
            raise LavaTopError(f"provider_http_{exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise LavaTopError(type(exc).__name__) from exc
        try:
            return json.loads(response.text, parse_float=Decimal)
        except (ValueError, TypeError) as exc:
            raise LavaTopError("invalid_provider_response") from exc

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


_client: LavaTopClient | None = None


def get_lava_top_client() -> LavaTopClient:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.lava_top_api_key or not settings.lava_top_offer_id:
            raise LavaTopError("lava_top_not_configured")
        _client = LavaTopClient(
            api_key=settings.lava_top_api_key.get_secret_value(),
            offer_id=settings.lava_top_offer_id,
            base_url=settings.lava_top_base_url,
            timeout_seconds=settings.lava_top_timeout_seconds,
            successful_return_url=settings.lava_top_successful_return_url,
            failure_return_url=settings.lava_top_failure_return_url,
            cancel_return_url=settings.lava_top_cancel_return_url,
        )
    return _client


async def close_lava_top_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def _parse_invoice(data: Any, *, created: bool) -> LavaTopInvoice:
    if not isinstance(data, dict):
        raise LavaTopError("invalid_lava_top_invoice")
    amount_data = data.get("amountTotal") if created else data.get("receipt")
    if not isinstance(amount_data, dict):
        raise LavaTopError("invalid_lava_top_invoice")
    try:
        amount = Decimal(str(amount_data["amount"]))
        invoice_id = str(UUID(str(data["id"])))
        currency = str(amount_data["currency"])
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise LavaTopError("invalid_lava_top_invoice") from exc
    if not invoice_id or not amount.is_finite() or amount <= 0:
        raise LavaTopError("invalid_lava_top_invoice")
    raw_status = str(data.get("status", "")).lower().replace("_", "-")
    status = {
        "new": "active",
        "in-progress": "active",
        "completed": "paid",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(raw_status, raw_status)
    buyer = data.get("buyer")
    buyer_email = str(buyer.get("email")) if isinstance(buyer, dict) and buyer.get("email") else None
    payment_url = data.get("paymentUrl")
    if payment_url:
        parsed_url = urlsplit(str(payment_url))
        if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            raise LavaTopError("invalid_lava_top_invoice")
    paid_at = str(data.get("datetime")) if status == "paid" and data.get("datetime") else None
    return LavaTopInvoice(
        invoice_id=invoice_id,
        status=status,
        amount=str(amount),
        currency=currency,
        payment_url=str(payment_url) if payment_url else None,
        paid_at=paid_at,
        buyer_email=buyer_email,
    )
