from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_infai_fallback import PrimaryFailure, seed

from app.accounts.models import ApiKey, Partner
from app.generations.models import Generation
from app.generations.service import poll_generation_provider
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.infrastructure.security import encrypt_secret, hash_secret
from app.media.models import MediaAsset
from app.media.service import create_provider_ready_asset
from app.providers.base import ProviderPollResult, ProviderResultStream, ProviderSubmitResult
from app.providers.models import ProviderAttempt, ProviderCredential
from app.providers.service import get_active_provider_credential, get_partner_provider_adapter
from app.workers.generation_worker import process_generation_work_concurrently_once


def credential(key, *, partner_id=None, label="infai-seedance-1", provider="infai"):
    return ProviderCredential(
        provider=provider,
        partner_id=partner_id,
        label=label,
        key_hash=hash_secret(key),
        key_prefix=key[:12],
        encrypted_api_key=encrypt_secret(key, get_settings().provider_credentials_master_key),
        created_at=utc_now() + timedelta(seconds=1),
    )


async def test_infai_only_group_global_key_is_eligible_and_argolink_stays_tenant_scoped(db_session, monkeypatch):
    partner, _, _, original = await seed(db_session)
    tenant_key = credential("tenant-infai", partner_id=partner.id)
    management_key = credential("management-infai", label="management")
    foreign_argo = credential("foreign-argolink", provider="argolink", partner_id="other-partner")
    db_session.add_all([tenant_key, management_key, foreign_argo])
    await db_session.commit()
    monkeypatch.setattr("app.providers.service.get_provider_adapter", lambda provider, api_key: api_key)

    selected = await get_active_provider_credential(db_session, partner.id, "infai")
    assert selected.id == original.id
    assert await get_partner_provider_adapter(db_session, partner.id, "infai") == "infai-test-key"
    for rejected in (tenant_key, management_key):
        with pytest.raises(HTTPException) as error:
            await get_partner_provider_adapter(db_session, partner.id, "infai", credential_id=rejected.id)
        assert error.value.status_code == 503
    with pytest.raises(HTTPException) as error:
        await get_partner_provider_adapter(db_session, partner.id, "argolink", credential_id=foreign_argo.id)
    assert error.value.status_code == 503
    original.is_active = False
    await db_session.commit()
    assert await get_active_provider_credential(db_session, partner.id, "infai") is None


async def test_infai_worker_reloads_durable_route_and_original_key_without_resubmit(db_session, monkeypatch):
    partner, generation, _, original = await seed(db_session)
    calls = []

    class Adapter(PrimaryFailure):
        async def submit_generation(self, request):
            calls.append(("submit", request.generation_id))
            return ProviderSubmitResult(provider_task_id="guard-infai-job")

        async def poll_generation(self, task_id):
            calls.append(("poll", task_id))
            return ProviderPollResult(
                status="completed",
                usage={"billed_seconds": 15, "completion_tokens": 324000},
                result_url="https://infai.cc/api/v3/contents/generations/tasks/guard-infai-job/content",
            )

    async def primary_adapter(*args, **kwargs):
        return PrimaryFailure()

    with monkeypatch.context() as scoped:
        scoped.setattr("app.generations.service.get_partner_provider_adapter", primary_adapter)
        await poll_generation_provider(db_session, generation)
        await db_session.commit()
    generation_id, partner_id, original_id = generation.id, partner.id, original.id
    db_session.add(credential("rotated-infai-key"))
    await db_session.commit()

    def factory(provider, *, api_key=None):
        assert provider == "infai"
        assert api_key == "infai-test-key"
        return Adapter()

    monkeypatch.setattr("app.providers.service.get_provider_adapter", factory)
    factory_sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    first = await process_generation_work_concurrently_once(session_factory=factory_sessions)
    assert first.dispatched == 1
    async with factory_sessions() as fresh:
        attempt = await fresh.scalar(select(ProviderAttempt).where(ProviderAttempt.provider == "infai"))
        assert attempt.credential_id == original_id
        attempt.next_poll_at = utc_now()
        old = await fresh.get(ProviderCredential, original_id)
        old.is_active = False
        await fresh.commit()
    second = await process_generation_work_concurrently_once(session_factory=factory_sessions)
    assert second.dispatched == 0 and second.polled == 1
    third = await process_generation_work_concurrently_once(session_factory=factory_sessions)
    assert not third.did_work
    assert calls == [("submit", generation_id), ("poll", "guard-infai-job")]
    async with factory_sessions() as fresh:
        completed = await fresh.get(Generation, generation_id)
        owner = await fresh.get(Partner, partner_id)
        asset = await fresh.scalar(select(MediaAsset).where(MediaAsset.generation_id == generation_id))
        assert completed.status == "completed"
        assert completed.actual_charge_rub == Decimal("327")
        assert owner.balance_rub == Decimal("673")
        assert asset.provider == "infai"


async def test_infai_native_api_and_media_hide_routing_and_enforce_ownership(client, db_session, monkeypatch):
    owner, generation, primary, original = await seed(db_session)
    outsider = Partner(telegram_id="infai-outsider", company_name="Other", project_name="Other", status="active")
    db_session.add(outsider)
    await db_session.flush()
    for partner_id, token in ((owner.id, "owner-key"), (outsider.id, "outsider-key")):
        db_session.add(ApiKey(partner_id=partner_id, name="test", key_hash=hash_secret(token), key_prefix=token))
    generation.status = "completed"
    generation.usage_snapshot = {"seconds": 15, "completion_tokens": 324000}
    primary.status = "failed"
    original.is_active = False
    db_session.add(credential("replacement-infai"))
    db_session.add(
        ProviderAttempt(
            generation_id=generation.id,
            provider="infai",
            credential_id=original.id,
            provider_task_id="private-provider-job",
            status="completed",
        )
    )
    asset = await create_provider_ready_asset(
        db=db_session,
        generation=generation,
        provider="infai",
        provider_content_url="https://infai.cc/api/v3/contents/generations/tasks/private-provider-job/content",
    )
    await db_session.commit()
    generation_id, asset_id, original_id = generation.id, asset.id, original.id
    accesses = []

    class Media:
        async def open_result_stream(self, url, *, range_header=None):
            accesses.append((url, range_header))

            async def body():
                yield b"video"

            return ProviderResultStream(
                body=body(),
                status_code=206,
                content_type="video/mp4",
                content_length=5,
                content_range="bytes 0-4/5",
                accept_ranges="bytes",
            )

    def factory(provider, *, api_key=None):
        assert provider == "infai" and api_key == "infai-test-key"
        return Media()

    monkeypatch.setattr("app.providers.service.get_provider_adapter", factory)
    for path in (
        f"/v1/videos/{generation_id}",
        f"/v1/videos/{generation_id}/content",
        f"/api/v1/media/{asset_id}/content",
        f"/api/v1/generations/{generation_id}",
    ):
        response = await client.get(path, headers={"Authorization": "Bearer outsider-key"})
        assert response.status_code == 404
    assert accesses == []
    response = await client.get(f"/v1/videos/{generation_id}", headers={"Authorization": "Bearer owner-key"})
    assert response.status_code == 200 and response.json()["status"] == "done"
    assert response.json()["usage"] == {"billed_seconds": 15}
    for private in ("infai", "private-provider-job", "completion_tokens", original_id):
        assert private not in response.text
    streamed = await client.get(
        f"/v1/videos/{generation_id}/content",
        headers={"Authorization": "Bearer owner-key", "Range": "bytes=0-4"},
    )
    assert streamed.status_code == 206 and streamed.content == b"video"
    assert streamed.headers["content-range"] == "bytes 0-4/5"
    assert len(accesses) == 1 and accesses[0][1] == "bytes=0-4"
