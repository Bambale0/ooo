async def test_health_returns_ok(client):
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_is_ready_when_database_works_even_if_redis_degraded(client):
    response = await client.get("/api/v1/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] in {"ok", "degraded"}


async def test_expired_api_major_returns_410(client):
    response = await client.get("/api/v0/health")

    assert response.status_code == 410
    assert response.json()["detail"] == "api_version_expired"
