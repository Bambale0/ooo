async def test_health_returns_ok(client):
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


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


async def test_internal_metrics_exposes_bounded_api_metrics(client):
    health_response = await client.get("/api/v1/health")
    assert health_response.status_code == 200

    response = await client.get("/internal/metrics")

    assert response.status_code == 200
    body = response.text
    assert "neironych_http_requests_total" in body
    assert 'route="/api/v1/health"' in body
    assert "neironych_http_request_duration_seconds" in body
    assert "neironych_db_pool_checked_out" in body
