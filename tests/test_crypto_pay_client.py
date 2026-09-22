import json

import httpx

from app.payments.crypto_pay import CryptoPayClient


async def test_crypto_pay_client_creates_exact_rub_invoice_with_supported_assets():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Crypto-Pay-API-Token"] == "test-token"
        payload = json.loads(request.read())
        assert payload == {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": "1500",
            "accepted_assets": "USDT,TON",
            "swap_to": "USDT",
            "expires_in": 3600,
            "payload": "local-payment-id",
            "allow_comments": False,
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "invoice_id": 123,
                    "status": "active",
                    "amount": "1500",
                    "bot_invoice_url": "https://t.me/CryptoBot?start=IV123",
                    "payload": "local-payment-id",
                    "expiration_date": "2026-09-22T11:00:00Z",
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://pay.crypt.bot", transport=transport) as http_client:
        client = CryptoPayClient(
            api_token="test-token",
            base_url="https://pay.crypt.bot",
            timeout_seconds=5,
            client=http_client,
        )
        invoice = await client.create_rub_invoice(amount_rub="1500", payload="local-payment-id")

    assert invoice.invoice_id == 123
    assert invoice.status == "active"
    assert invoice.payload == "local-payment-id"
    assert requests[0].url.path == "/api/createInvoice"
