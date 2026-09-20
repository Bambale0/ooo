from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.accounts import models as account_models  # noqa: F401
from app.billing import models as billing_models  # noqa: F401
from app.catalog import models as catalog_models  # noqa: F401
from app.generations import models as generation_models  # noqa: F401
from app.infrastructure.config import get_settings
from app.infrastructure.database import Base
from app.media import models as media_models  # noqa: F401
from app.providers import models as provider_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_sync_database_url() -> str:
    return get_settings().database_url.replace("+asyncpg", "").replace("+aiosqlite", "")


def run_migrations_offline() -> None:
    context.configure(
        url=get_sync_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_sync_database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
