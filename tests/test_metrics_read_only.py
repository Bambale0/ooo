from sqlalchemy import func, select

from app.billing.models import WalletSnapshot


async def test_metrics_scrape_never_refreshes_financial_wallet(client, db_session, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Metrics must not call a financial API or mutate wallet snapshots")

    monkeypatch.setattr("app.billing.capital.wallet_balance", forbidden)
    before = await db_session.scalar(select(func.count()).select_from(WalletSnapshot))
    response = await client.get("/internal/metrics")
    assert response.status_code == 200
    assert "neironych_" in response.text
    assert await db_session.scalar(select(func.count()).select_from(WalletSnapshot)) == before
