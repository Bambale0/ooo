"""Restricted models are invisible in the public catalog and usable only with a live
per-partner grant. Granting requires reason text; revoking works immediately while
keeping history; re-granting restores the existing row instead of duplicating it.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text

from app.catalog.models import PartnerModelGrant
from app.infrastructure.config import get_settings


def admin_token_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().admin_api_token}"}


async def _restricted_model(client, admin_headers: dict) -> dict:
    response = await client.post(
        "/api/v1/catalog/models",
        headers=admin_headers,
        json={
            "slug": "seedance-2.5-self-developed-nsfw",
            "name": "Seedance 2.5 Self-Developed NSFW",
            "modality": "video",
            "status": "restricted",
        },
    )
    assert response.status_code == 201
    return response.json()


async def test_restricted_model_grant_lifecycle(client, db_session, admin_headers):
    model = await _restricted_model(client, admin_headers)
    assert model["status"] == "restricted"

    price = await client.put(
        "/api/v1/catalog/pricing",
        headers=admin_headers,
        json={
            "model_slug": "seedance-2.5-self-developed-nsfw",
            "mode": "default",
            "resolution": "720p",
            "price_rub": "20.00",
            "provider_cost_usdt": "0.170000",
            "billing_unit": "second",
        },
    )
    assert price.status_code == 204

    gates = await client.post(
        "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/enable-gates",
        headers=admin_headers,
        json={"has_provider_integration": True, "has_public_docs": False, "has_successful_smoke": True},
    )
    assert gates.status_code == 200

    enabled = await client.post(
        "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/enable-restricted",
        headers=admin_headers,
    )
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "restricted"

    # Grants require tracked reason text paid attention to, not a silent toggle.
    empty_reason = await client.post(
        "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants",
        headers=admin_headers,
        json={"partner_id": "partner-1", "reason": "short"},
    )
    assert empty_reason.status_code == 422

    from app.accounts.models import Partner

    db_session.add(
        Partner(telegram_id="700100", company_name="Restricted", project_name="NSFW grant", status="active")
    )
    await db_session.flush()
    partner = (await db_session.execute(select(Partner).where(Partner.telegram_id == "700100"))).scalar_one()

    grant = await client.post(
        "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants",
        headers=admin_headers,
        json={"partner_id": partner.id, "reason": "verified partner, explicit adult-content request"},
    )
    assert grant.status_code == 201
    body = grant.json()
    assert body["model_slug"] == "seedance-2.5-self-developed-nsfw"
    assert body["partner_id"] == partner.id
    assert body["revoked_at"] is None

    listed = await client.get(
        "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    assert [row["partner_id"] for row in listed.json()] == [partner.id]

    # Without a grant the model is invisible everywhere public...
    public_models = await client.get("/api/v1/catalog/models")
    assert "seedance-2.5-self-developed-nsfw" not in [m["slug"] for m in public_models.json()]
    public_pricing = await client.get("/api/v1/catalog/pricing")
    assert "seedance-2.5-self-developed-nsfw" not in [p["model_slug"] for p in public_pricing.json()]

    # ...so the flow stops before any money is reserved for strangers.
    from app.catalog.access import has_model_grant

    assert await has_model_grant(db_session, model["id"], partner.id) is True
    assert await has_model_grant(db_session, model["id"], "partner-without-grant") is False

    revoked = await client.delete(
        f"/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants/{partner.id}",
        headers=admin_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert await has_model_grant(db_session, model["id"], partner.id) is False
    assert (await client.get(
        "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants",
        headers=admin_headers,
    )).json() == []

    # Re-granting restores the same row, never a duplicate line in the ledger.
    again = await client.post(
        "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants",
        headers=admin_headers,
        json={"partner_id": partner.id, "reason": "verified partner, explicit adult-content request"},
    )
    assert again.status_code == 201
    assert again.json()["id"] == grant.json()["id"]
    rows = (
        await db_session.execute(
            select(PartnerModelGrant).where(PartnerModelGrant.model_id == model["id"])
        )
    ).scalars()
    assert len(list(rows)) == 1


async def test_enable_restricted_requires_contract_gates(client, admin_headers):
    response = await client.post(
        "/api/v1/catalog/models",
        headers=admin_headers,
        json={"slug": "unknown-model", "name": "Unknown", "modality": "video", "status": "restricted"},
    )
    assert response.status_code == 201
    denied = await client.post(
        "/api/v1/catalog/models/unknown-model/enable-restricted",
        headers=admin_headers,
    )
    assert denied.status_code == 409
    assert denied.json()["detail"] == "model_contract_not_supported"

    # A contracted restricted model rejects an unknown partner before writing a row.
    await _restricted_model(client, admin_headers)
    grant = await client.post(
        "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants",
        headers=admin_headers,
        json={"partner_id": "does-not-exist", "reason": "verified partner, explicit adult-content request"},
    )
    assert grant.status_code == 404
    assert grant.json()["detail"] == "partner_not_found"

    # An unknown model never reaches the partner lookup at all.
    unknown_grant = await client.post(
        "/api/v1/catalog/models/no-such-model/grants",
        headers=admin_headers,
        json={"partner_id": "does-not-exist", "reason": "verified partner, explicit adult-content request"},
    )
    assert unknown_grant.status_code == 404
    assert unknown_grant.json()["detail"] == "model_not_found"


async def test_production_model_cannot_be_granted(client, admin_headers):
    """Grants exist for restricted models only; a public model stays open to all."""
    await _restricted_model(client, admin_headers)
    created = await client.post(
        "/api/v1/catalog/models",
        headers=admin_headers,
        json={"slug": "seedance-2.5", "name": "Seedance 2.5", "modality": "video", "status": "production"},
    )
    assert created.status_code == 409

    await client.post(
        "/api/v1/catalog/models",
        headers=admin_headers,
        json={"slug": "draft-model", "name": "Draft", "modality": "video", "status": "draft"},
    )
    denied = await client.post(
        "/api/v1/catalog/models/draft-model/grants",
        headers=admin_headers,
        json={"partner_id": "partner-1", "reason": "verified partner, explicit adult-content request"},
    )
    assert denied.status_code == 409
    assert denied.json()["detail"] == "model_not_restricted"


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/enable-restricted"),
        ("get", "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants"),
        (
            "post",
            "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants",
        ),
        ("delete", "/api/v1/catalog/models/seedance-2.5-self-developed-nsfw/grants/partner-1"),
    ],
)
async def test_grant_endpoints_require_admin_auth(client, method, path):
    """Granting access to a restricted model must never be reachable without admin auth."""
    await _restricted_model(client, admin_token_header())
    call = client.delete if method == "delete" else getattr(client, method)
    response = await call(path)
    assert response.status_code == 401
    assert response.json()["detail"] == "admin_auth_required"


def test_grant_migration_enforces_uniqueness_and_rolls_back():
    """The grant table must reject duplicate (model, partner) pairs and drop cleanly."""
    path = Path(__file__).parents[1] / "alembic/versions/20260930_0022_partner_model_grants.py"
    spec = spec_from_file_location("partner_model_grants_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.execute(
            text(
                "INSERT INTO partner_model_grants"
                " (id, model_id, partner_id, granted_by, reason, created_at)"
                " VALUES ('g1', 'm1', 'p1', 'admin', 'request', '2026-09-30 00:00:00')"
            )
        )
        indexes = {index["name"] for index in inspect(connection).get_indexes("partner_model_grants")}
        assert {
            "ix_partner_model_grants_model_id",
            "ix_partner_model_grants_partner_id",
            "ix_partner_model_grants_revoked_at",
        } <= indexes
        from sqlalchemy.exc import IntegrityError

        try:
            with connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO partner_model_grants"
                        " (id, model_id, partner_id, granted_by, reason, created_at)"
                        " VALUES ('g2', 'm1', 'p1', 'admin', 'duplicate', '2026-09-30 00:00:01')"
                    )
                )
        except IntegrityError:
            pass
        else:
            raise AssertionError("duplicate (model_id, partner_id) must be rejected")
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "partner_model_grants" not in inspect(connection).get_table_names()
    engine.dispose()