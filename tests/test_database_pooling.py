from app.infrastructure.config import Settings
from app.infrastructure.database import build_engine_kwargs


def test_postgres_engine_pool_settings_are_bounded():
    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@db:5432/app",
        database_pool_size=12,
        database_max_overflow=4,
        database_pool_timeout_seconds=7,
        database_pool_recycle_seconds=900,
    )

    assert build_engine_kwargs(settings) == {
        "pool_pre_ping": True,
        "pool_size": 12,
        "max_overflow": 4,
        "pool_timeout": 7.0,
        "pool_recycle": 900,
    }


def test_sqlite_engine_does_not_receive_queue_pool_only_options():
    settings = Settings(database_url="sqlite+aiosqlite://")

    assert build_engine_kwargs(settings) == {"pool_pre_ping": True}
