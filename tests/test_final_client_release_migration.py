import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20261009_0028_final_client_reserve_release.py"
    spec = importlib.util.spec_from_file_location("final_client_release_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_policy_migration_has_no_enrollment_backfill_and_round_trips(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE generations (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO generations(id) VALUES ('pre-existing-customer-job')"))
        module = migration()
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
        module.upgrade()
        row = connection.execute(sa.text(
            "SELECT client_release_policy, client_release_due_at, client_reserve_released_at FROM generations"
        )).one()
        assert row == (None, None, None)
        columns = {item["name"]: item for item in sa.inspect(connection).get_columns("generations")}
        for name in ("client_release_policy", "client_release_due_at", "client_reserve_released_at"):
            assert columns[name]["nullable"] is True
            assert columns[name]["default"] is None
        assert sa.inspect(connection).get_indexes("generations")[0]["name"] == "ix_generations_client_release_due_at"
        module.downgrade()
        assert [item["name"] for item in sa.inspect(connection).get_columns("generations")] == ["id"]
    engine.dispose()


def test_downgrade_cannot_erase_final_financial_marker(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE generations (id VARCHAR(36) PRIMARY KEY)"))
        module = migration()
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
        module.upgrade()
        connection.execute(sa.text(
            "INSERT INTO generations(id, client_reserve_released_at) VALUES ('final-job', '2026-10-09 13:00:00')"
        ))
        with pytest.raises(RuntimeError, match="after a final release"):
            module.downgrade()
        assert connection.execute(sa.text("SELECT client_reserve_released_at FROM generations")).scalar() is not None
    engine.dispose()
