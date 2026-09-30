import asyncio
import hashlib
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from starlette.datastructures import UploadFile

from app.api.dependencies import DbSession, PartnerAuth, get_partner_auth
from app.billing.service import release_generation_reserves
from app.catalog.models import Model
from app.contracts.registry import MODELS, PROTOCOLS, TEXT_PROTOCOLS, image_reference_limit, validate_request
from app.generations.models import Generation
from app.inference.accounting import settle_actual, token_usage
from app.inference.images import image_usage, inspect_url_images
from app.inference.service import reserve
from app.inference.streams import SSEDecoder, UsageCollector, event_data
from app.infrastructure.config import get_settings
from app.media.models import MediaAsset
from app.media.router import read_media_content
from app.providers.models import ProviderAttempt
from app.providers.service import get_partner_provider_adapter
from app.webhooks.service import ensure_terminal_webhook_event

router = APIRouter()


def public_error(code, status=503, *, generation_id=None, headers=None):
    return JSONResponse(
        {"error": {"type": code, "message": code}, **({"request_id": generation_id} if generation_id else {})},
        status_code=status,
        headers=headers,
    )


def scrub(data, generation_id):
    """Only protocol envelope metadata is private; never rewrite user model content."""
    if not isinstance(data, dict):
        return data
    result = dict(data)
    for field in ("request_id", "provider", "provider_id", "cost", "actual_cost", "billing", "account_cost"):
        result.pop(field, None)
    if "id" in result:
        result["id"] = generation_id
    if isinstance(result.get("usage"), dict):
        result["usage"] = {k: v for k, v in result["usage"].items() if "cost" not in k and "price" not in k}
        usage = result["usage"]
        # Expose the same inclusive output count we settle, so downstream
        # consumers do not undercount converted tool-call reasoning.
        if "total_tokens" in usage:
            protocol = "chat/completions" if "prompt_tokens" in usage else "responses"
            try:
                units = token_usage(protocol, usage)
            except (ValueError, AttributeError):
                # Streaming events can carry partial usage; settlement validates
                # the complete collected usage before releasing the reserve.
                pass
            else:
                output_key = "completion_tokens" if protocol == "chat/completions" else "output_tokens"
                usage[output_key] = units["output_tokens"]
    if isinstance(result.get("response"), dict):
        result["response"] = scrub(result["response"], generation_id)
    if isinstance(result.get("message"), dict) and result.get("type") == "message_start":
        result["message"] = scrub(result["message"], generation_id)
    if result.get("error") or result.get("type") in {"error", "response.failed"}:
        return {"type": "error", "error": {"type": "generation_failed", "message": "generation_failed"}}
    return result


@router.get("/models")
async def models(db: DbSession):
    rows = (await db.execute(select(Model).where(Model.status == "production").order_by(Model.slug))).scalars()
    return {
        "object": "list",
        "data": [{"id": m.slug, "object": "model", "owned_by": "neironych"} for m in rows if m.slug in MODELS],
    }


@router.get("/videos/{generation_id}")
async def video_status(generation_id: str, db: DbSession, auth: PartnerAuth = Depends(get_partner_auth)):
    generation = await db.get(Generation, generation_id)
    if generation is None or generation.partner_id != auth.partner.id:
        raise HTTPException(404, "generation_not_found")
    status = {"completed": "done", "failed": "failed", "timeout": "expired", "cancelled": "failed"}.get(
        generation.status, "pending"
    )
    result = {"request_id": generation.id, "status": status}
    if status == "done":
        result["video"] = {
            "url": get_settings().public_api_base_url.rstrip("/") + "/v1/videos/" + generation.id + "/content"
        }
        if generation.usage_snapshot:
            result["usage"] = {"billed_seconds": generation.usage_snapshot.get("seconds")}
    if generation.public_error_code:
        result["error"] = {"type": generation.public_error_code, "message": generation.public_error_code}
    return result


@router.get("/videos/{generation_id}/content")
async def video_content(
    generation_id: str, request: Request, db: DbSession, auth: PartnerAuth = Depends(get_partner_auth)
):
    asset = (
        await db.execute(
            select(MediaAsset).where(
                MediaAsset.generation_id == generation_id,
                MediaAsset.partner_id == auth.partner.id,
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(404, "content_not_available")
    return await read_media_content(asset.id, request, db, auth.partner)


@router.post("/media/uploads")
async def media_upload(request: Request, db: DbSession, auth: PartnerAuth = Depends(get_partner_auth)):
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type and content_type != "application/json":
        raise HTTPException(415, "media_upload_requires_json")
    try:
        body = await request.json()
    except ValueError:
        # JSONDecodeError and UnicodeDecodeError are both ValueError subclasses.
        # File bytes belong in the later PUT to upload_url, not this ticket request.
        raise HTTPException(422, "invalid_request_contract") from None
    if not isinstance(body, dict) or not isinstance(body.get("model"), str) or body["model"] not in MODELS:
        raise HTTPException(422, "unknown_model_contract")
    adapter = await get_partner_provider_adapter(db, auth.partner.id, "argolink")
    try:
        response = await adapter.native_request("media/uploads", body)
        await response.aread()
        if not response.is_success:
            return public_error("upload_rejected", 422 if response.status_code == 400 else 503)
        data = response.json()
        # Upload tickets are intentionally passed through; bytes go directly to storage.
        return JSONResponse(
            {k: data[k] for k in ("upload_url", "media_url", "upload_expires_at", "expires_at") if k in data},
            status_code=201,
        )
    except (httpx.HTTPError, ValueError):
        return public_error("upload_unavailable")


async def inference(protocol: str, request: Request, db: DbSession, auth: PartnerAuth = Depends(get_partner_auth)):
    if protocol not in PROTOCOLS:
        raise HTTPException(404, "unknown_protocol")
    files = None
    digest = ""
    try:
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            if protocol != "images/edits":
                raise HTTPException(415, "multipart_not_supported_for_protocol")
            async with request.form(max_files=17, max_fields=100) as form:
                body, files = {}, []
                hasher = hashlib.sha256()
                for name, value in form.multi_items():
                    if isinstance(value, UploadFile):
                        content = await value.read()
                        descriptor = json.dumps(
                            [
                                name,
                                value.filename,
                                value.content_type,
                                len(content),
                                hashlib.sha256(content).hexdigest(),
                            ]
                        ).encode()
                        hasher.update(len(descriptor).to_bytes(8, "big") + descriptor)
                        files.append((name, (value.filename, content, value.content_type)))
                        await value.close()
                    else:
                        if name in body:
                            raise ValueError("duplicate_form_field")
                        body[name] = int(value) if name == "n" else value
                digest = hasher.hexdigest()
        else:
            body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("invalid_request_body")
        body = validate_request(protocol, body)
        if files:
            image_count = sum(name in {"image", "image[]", "images", "images[]"} for name, _ in files)
            maximum = image_reference_limit(body["model"], multipart=True)
            if sum(name == "mask" for name, _ in files) > 1:
                raise ValueError("invalid_mask_count")
            if not 1 <= image_count <= maximum:
                raise ValueError("invalid_reference_images")
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "invalid_request_contract") from exc
    idem = request.headers.get("idempotency-key", "")
    if not 8 <= len(idem) <= 160:
        raise HTTPException(422, "invalid_idempotency_key")
    generation, attempt = await reserve(db, auth, protocol, body, idem, files_digest=digest)
    if protocol == "videos/generations":
        return JSONResponse({"request_id": generation.id}, status_code=202)
    if attempt is None:
        return public_error("request_already_submitted", 409, generation_id=generation.id)
    try:
        adapter = await get_partner_provider_adapter(
            db, auth.partner.id, "argolink", credential_id=attempt.credential_id
        )
    except HTTPException:
        await fail(db, generation, attempt, "provider_temporarily_unavailable", definitive=True)
        return public_error("provider_temporarily_unavailable", generation_id=generation.id)
    wire_body = dict(body)
    if previous := body.get("previous_response_id"):
        previous_attempt = (
            await db.execute(
                select(ProviderAttempt)
                .join(Generation, Generation.id == ProviderAttempt.generation_id)
                .where(
                    Generation.id == previous,
                    Generation.partner_id == auth.partner.id,
                    Generation.status == "completed",
                    ProviderAttempt.provider == "argolink",
                    ProviderAttempt.credential_id == attempt.credential_id,
                )
            )
        ).scalar_one_or_none()
        if previous_attempt is None or not previous_attempt.provider_task_id:
            await fail(db, generation, attempt, "invalid_previous_response", definitive=True)
            return public_error("invalid_previous_response", 422)
        wire_body["previous_response_id"] = previous_attempt.provider_task_id
    if protocol == "chat/completions" and body.get("stream"):
        wire_body["stream_options"] = {**body.get("stream_options", {}), "include_usage": True}
    headers = {key: request.headers[key] for key in ("anthropic-version", "anthropic-beta") if key in request.headers}
    await db.commit()
    try:
        response = await adapter.native_request(protocol, wire_body, files=files, headers=headers)
    except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError):
        await fail(db, generation, attempt, "provider_temporarily_unavailable", definitive=True)
        return public_error("provider_temporarily_unavailable", generation_id=generation.id)
    except httpx.HTTPError:
        await fail(db, generation, attempt, "submission_outcome_unknown", definitive=False)
        return public_error("submission_outcome_unknown", generation_id=generation.id)
    if not response.is_success:
        definitive = 400 <= response.status_code < 500 and response.status_code != 408
        code = (
            "provider_rate_limited"
            if response.status_code == 429
            else "provider_rejected_request"
            if definitive
            else "submission_outcome_unknown"
        )
        retry = response.headers.get("retry-after")
        await response.aclose()
        await fail(db, generation, attempt, code, definitive=definitive)
        return public_error(
            code,
            429 if code == "provider_rate_limited" else 422 if response.status_code == 400 else 503,
            generation_id=generation.id,
            headers={"Retry-After": retry} if retry else None,
        )
    if "text/event-stream" in response.headers.get("content-type", ""):
        if not body.get("stream"):
            await response.aclose()
            await fail(db, generation, attempt, "unexpected_provider_stream", definitive=False)
            return public_error("unexpected_provider_stream", generation_id=generation.id)
        return StreamingResponse(
            stream_result(db, generation, attempt, response, protocol),
            media_type="text/event-stream",
            headers={"X-Request-Id": generation.id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        await response.aread()
        data = response.json()
        if not isinstance(data, dict):
            await fail(db, generation, attempt, "provider_response_invalid", definitive=False)
            return public_error("provider_response_invalid", generation_id=generation.id)
        if protocol in TEXT_PROTOCOLS:
            units = token_usage(protocol, data.get("usage"))
            if isinstance(data.get("id"), str):
                attempt.provider_task_id = data["id"][:255]
        else:
            urls = [
                item["url"]
                for item in data.get("data", [])
                if isinstance(item, dict) and isinstance(item.get("url"), str)
            ]
            if urls:
                generation.result_url = urls[0]
                generation.request_payload = {**(generation.request_payload or {}), "result_urls": urls}
            units = image_usage(body, await inspect_url_images(body, data))
        await finish(db, generation, attempt, units)
    except (ValueError, TypeError, KeyError, OSError, httpx.HTTPError):
        await fail(db, generation, attempt, "usage_reconciliation_required", definitive=False)
        # A successful result still belongs to the partner even if its cost needs reconciliation.
        if "data" not in locals():
            return public_error("provider_response_invalid", generation_id=generation.id)
    finally:
        await response.aclose()
    return JSONResponse(scrub(data, generation.id), headers={"X-Request-Id": generation.id})


async def finish(db, generation, attempt, units):
    await db.execute(
        select(Generation)
        .where(Generation.id == generation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await settle_actual(db, generation, units)
    generation.status = attempt.status = "completed"
    await ensure_terminal_webhook_event(db, generation)
    await db.commit()


async def fail(db, generation, attempt, code, *, definitive):
    await db.execute(
        select(Generation)
        .where(Generation.id == generation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if generation.actual_charge_rub is not None:
        return
    generation.status = attempt.status = "failed" if definitive else "reconciliation_required"
    generation.public_error_code = attempt.public_error_code = code
    from app.providers.circuit import observe

    await observe(db, generation)
    if definitive:
        await release_generation_reserves(db, generation, reason="Request definitively rejected")
        await ensure_terminal_webhook_event(db, generation)
    await db.commit()


async def stream_result(db, generation, attempt, response, protocol):
    decoder, usage = SSEDecoder(), UsageCollector(protocol)
    try:
        async for chunk in response.aiter_text():
            for event in decoder.feed(chunk):
                value = event_data(event)
                if value is None:
                    yield event + "\n\n"
                    continue
                usage.accept(value)
                candidate = value.get("response", value.get("message", value))
                if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
                    attempt.provider_task_id = candidate["id"][:255]
                if usage.complete and not usage.failed:
                    await finish(db, generation, attempt, token_usage(protocol, usage.usage))
                prefix = "".join(line + "\n" for line in event.split("\n") if line.startswith("event:"))
                yield prefix + "data: " + json.dumps(scrub(value, generation.id), ensure_ascii=False) + "\n\n"
        if usage.complete and not usage.failed:
            await finish(db, generation, attempt, token_usage(protocol, usage.usage))
        else:
            await fail(db, generation, attempt, "stream_outcome_unknown", definitive=False)
    except (ValueError, httpx.HTTPError):
        await fail(db, generation, attempt, "stream_outcome_unknown", definitive=False)
        yield 'data: {"error":{"type":"stream_interrupted","message":"stream_interrupted"}}\n\n'
    finally:
        # On client cancellation/process death the committed submission intent and
        # full reserve remain visible to reconciliation. No automatic paid replay.
        await asyncio.shield(response.aclose())


def native_endpoint(protocol):
    async def endpoint(request: Request, db: DbSession, auth: PartnerAuth = Depends(get_partner_auth)):
        return await inference(protocol, request, db, auth)

    return endpoint


for native_protocol in sorted(PROTOCOLS):
    router.add_api_route(
        "/" + native_protocol,
        native_endpoint(native_protocol),
        methods=["POST"],
        name="native_" + native_protocol.replace("/", "_"),
        description=(
            "Native protocol. Send the partner key and a unique Idempotency-Key header. "
            "Prices: /api/v1/catalog/pricing."
        ),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["model"],
                            "properties": {"model": {"type": "string"}},
                            "additionalProperties": True,
                        },
                    }
                },
            }
        },
    )
