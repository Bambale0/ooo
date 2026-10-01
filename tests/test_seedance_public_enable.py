"""Public Seedance availability follows authenticated, database-backed enable gates."""

import pytest


@pytest.mark.parametrize("version", ["2.0", "2.5"])
async def test_seedance_self_developed_public_enable_preserves_gates(client, admin_headers, version):
    slug = f"seedance-{version}-self-developed-nsfw"
    name = f"Public Seedance {version} test model"
    path = f"/api/v1/catalog/models/{slug}"
    created = await client.post(
        "/api/v1/catalog/models",
        headers=admin_headers,
        json={"slug": slug, "name": name, "modality": "video", "status": "restricted"},
    )
    assert created.status_code == 201
    assert (await client.get("/v1/models")).json()["data"] == []
    assert (await client.get("/api/v1/catalog/models")).json() == []
    for lang in ("ru", "en"):
        docs = await client.get("/docs", params={"lang": lang})
        assert name not in docs.text
        assert slug not in docs.text

    gates = {
        "has_provider_integration": False,
        "has_public_docs": False,
        "has_successful_smoke": False,
    }
    for suffix, payload in (("enable", None), ("enable-gates", gates)):
        denied = await client.post(f"{path}/{suffix}", json=payload)
        assert denied.status_code == 401
        assert denied.json()["detail"] == "admin_auth_required"

    for field, error in (
        ("has_provider_integration", "provider_integration_gate_missing"),
        ("has_public_docs", "docs_gate_missing"),
        ("has_successful_smoke", "smoke_gate_missing"),
    ):
        denied = await client.post(f"{path}/enable", headers=admin_headers)
        assert denied.status_code == 409
        assert denied.json()["detail"] == error
        gates[field] = True
        updated = await client.post(f"{path}/enable-gates", headers=admin_headers, json=gates)
        assert updated.status_code == 200

    unpriced = await client.post(f"{path}/enable", headers=admin_headers)
    assert unpriced.status_code == 409
    assert unpriced.json()["detail"] == "price_gate_missing"
    priced = await client.put(
        "/api/v1/catalog/pricing",
        headers=admin_headers,
        json={
            "model_slug": slug,
            "mode": "default",
            "resolution": "720p",
            "price_rub": "100.00",
            "provider_cost_usdt": "0.11" if version == "2.0" else "0.17",
            "billing_unit": "second",
        },
    )
    assert priced.status_code == 204
    assert (await client.get("/api/v1/catalog/pricing")).json() == []
    assert name not in (await client.get("/price")).text

    enabled = await client.post(f"{path}/enable", headers=admin_headers)
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "production"
    assert [row["id"] for row in (await client.get("/v1/models")).json()["data"]] == [slug]
    assert [row["slug"] for row in (await client.get("/api/v1/catalog/models")).json()] == [slug]
    pricing = (await client.get("/api/v1/catalog/pricing")).json()
    assert [(row["model_slug"], row["resolution"], row["billing_unit"]) for row in pricing] == [
        (slug, "720p", "second")
    ]
    assert name in (await client.get("/price")).text
    for lang in ("ru", "en"):
        docs = await client.get("/docs", params={"lang": lang})
        assert name in docs.text
        assert f"<td>{slug}</td><td>4–" in docs.text

    # Production models require no per-partner grants; this existing admin path
    # continues to reject grants for any publicly enabled model.
    grant = await client.post(
        f"{path}/grants",
        headers=admin_headers,
        json={"partner_id": "unused-partner", "reason": "public models do not require a grant"},
    )
    assert grant.status_code == 409
    assert grant.json()["detail"] == "model_not_restricted"
