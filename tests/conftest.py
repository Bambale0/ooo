import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("ADMIN_API_TOKEN", "test-admin-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("ARGOLINK_API_KEY", "test-argolink-key")
os.environ.setdefault("PROVIDER_CREDENTIALS_MASTER_KEY", "test-provider-credentials-master-key-32")
os.environ.setdefault("CRYPTO_PAY_API_TOKEN", "test-crypto-pay-token")

from app.infrastructure.config import get_settings  # noqa: E402
from app.infrastructure.database import Base, get_db_session  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_http_only(monkeypatch):
    import httpx

    async def reject_network(self, request):
        raise AssertionError("Tests must use MockTransport/ASGITransport, never real provider HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override_db() -> AsyncIterator[AsyncSession]:
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    app.dependency_overrides[get_db_session] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().admin_api_token}"}


@pytest.fixture(autouse=True)
def funded_test_treasury(monkeypatch):
    """Tests use an explicit fake wallet; they never contact Crypto Pay for cash."""
    from decimal import Decimal

    async def available_wallet(db):
        return Decimal("1000000"), 0, "fresh"

    monkeypatch.setattr("app.billing.capital.wallet_balance", available_wallet)
    monkeypatch.setattr(get_settings(), "opening_working_capital_usdt", Decimal("1000000"))


@pytest.fixture(autouse=True)
def fixed_test_fx(monkeypatch):
    async def rate(db):
        return {"rate": get_settings().rub_per_usdt, "source": "manual_fallback", "automatic_at": None}

    monkeypatch.setattr("app.billing.fx.current_fx", rate)
    monkeypatch.setattr("app.billing.incidents.current_fx", rate)
