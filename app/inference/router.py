import asyncio
import hashlib
import json
import re
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from starlette.datastructures import UploadFile

from app.api.dependencies import DbSession, PartnerAuth, get_partner_auth
from app.catalog.models import Model
from app.contracts.registry import MODELS, PROTOCOLS, TEXT_PROTOCOLS, image_reference_limit, validate_request
from app.contracts.validation_errors import validation_reason
from app.generations.models import Generation
from app.inference.accounting import settle_actual, token_usage
from app.inference.admission_receipts import fence_rejected_admission, rejected_admission_response
from app.inference.correlation import derive_client_request_id, generation_client_request_id
from app.inference.diagnostics import NativeRequestTrace, log_rejection
from app.inference.image_results import persist_single_image
from app.inference.images import image_usage, inspect_url_images
from app.inference.outcomes import finalize_native_failure, preserve_unbilled_result
from app.inference.replay import replay_response, save_response
from app.inference.service import InferenceAdmissionError, reserve
from app.inference.streams import SSEDecoder, UsageCollector, event_data
from app.infrastructure.config import get_settings
from app.media.models import MediaAsset
from app.media.router import read_media_content
from app.media.service import build_result_download_url
from app.providers.models import ProviderAttempt
from app.providers.service import get_partner_provider_adapter
from app.webhooks.service import ensure_terminal_webhook_event

router = APIRouter()


def public_error(code, status=503, *, generation_id=None, headers=None):
    return JSONResponse(
        {"error": {"type": code, "message": code}, **({"request_id": generation_id} if generation_id else {})},
        status_code=status,
        headers={**(headers or {}), **({"X-Request-Id": generation_id} if generation_id else {})},
    )


def video_failure_error(public_code: str, raw_error: str | None) -> dict[str, object]:
    """Return a stable, provider-agnostic public error for video polling."""
    normalized = (raw_error or "").strip().lower()
    if public_code == "provider_rejected_request":
        try:
            diagnostics = json.loads(raw_error or "")
        except (TypeError, ValueError):
            diagnostics = None
        upstream_status = diagnostics.get("upstream_status") if isinstance(diagnostics, dict) else None
        if upstream_status == 400:
            return {
                "type": "provider_rejected_request",
                "code": "invalid_request",
                "message": "The video request was rejected as invalid. Check its parameters and input media.",
                "retryable": False,
            }
        if upstream_status == 402:
            return {
                "type": "provider_rejected_request",
                "code": "provider_temporarily_unavailable",
                "message": (
                    "The video service is temporarily unavailable. "
                    "Submit a new request later with a new idempotency key."
                ),
                "retryable": True,
            }
    if public_code == "provider_generation_failed":
        if "at capacity" in normalized:
            return {
                "type": "provider_generation_failed",
                "code": "model_capacity",
                "message": "The video model is temporarily at capacity. Retry later.",
                "retryable": True,
            }
        if "could not be generated" in normalized or "could not be processed" in normalized:
            return {
                "type": "provider_generation_failed",
                "code": "generation_failed",
                "message": "The video could not be generated. Retry the request or adjust the input.",
                "retryable": True,
            }
        return {
            "type": "provider_generation_failed",
            "code": "generation_failed",
            "message": "Video generation failed.",
            "retryable": False,
        }
    if public_code == "generation_timeout":
        return {
            "type": "generation_timeout",
            "code": "generation_timeout",
            "message": "Video generation timed out before a final result was available.",
            "retryable": True,
        }
    return {
        "type": public_code,
        "code": public_code,
        "message": public_code,
        "retryable": False,
    }


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
    if client_request_id := generation_client_request_id(generation):
        result["client_request_id"] = client_request_id
    if status == "done":
        share_url, expires_at = build_result_download_url(generation)
        result["video"] = {
            "url": share_url,
            "expires_at": expires_at,
            "authenticated_url": (
                get_settings().public_api_base_url.rstrip("/") + "/v1/videos/" + generation.id + "/content"
            ),
        }
        if generation.usage_snapshot:
            result["usage"] = {"billed_seconds": generation.usage_snapshot.get("seconds")}
    if generation.public_error_code:
        attempt = (
            await db.execute(
                select(ProviderAttempt)
                .where(ProviderAttempt.generation_id == generation.id)
                .order_by(ProviderAttempt.created_at.desc(), ProviderAttempt.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        result["error"] = video_failure_error(
            generation.public_error_code,
            attempt.raw_error if attempt is not None else None,
        )
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
    diagnostic_trace_id = str(uuid4())
    files = None
    body = None
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
        body = validate_request(protocol, body, multipart=bool(files))
        if files:
            profile = MODELS[body["model"]].get("image")
            if profile:
                from app.contracts.image_profiles import validate_profile_files

                validate_profile_files(files, profile)
            image_count = sum(name in {"image", "image[]", "images", "images[]"} for name, _ in files)
            maximum = image_reference_limit(body["model"], multipart=True)
            if sum(name == "mask" for name, _ in files) > 1:
                raise ValueError("invalid_mask_count")
            if not 1 <= image_count <= maximum:
                raise ValueError("invalid_reference_images")
    except HTTPException as exc:
        log_rejection(
            trace_id=diagnostic_trace_id,
            partner_id=auth.partner.id,
            api_key_id=auth.api_key.id,
            protocol=protocol,
            model=body.get("model") if isinstance(body, dict) else None,
            failure_stage="request_validation",
            error_code=str(exc.detail),
            http_status=exc.status_code,
        )
        raise HTTPException(
            exc.status_code,
            exc.detail,
            headers={**(exc.headers or {}), "X-Request-Id": diagnostic_trace_id},
        ) from exc
    except (ValueError, TypeError, AttributeError) as exc:
        reason = validation_reason(exc)
        validation_headers = {"X-Request-Id": diagnostic_trace_id}
        if reason is not None:
            validation_headers["X-Validation-Error"] = reason
        log_rejection(
            trace_id=diagnostic_trace_id,
            partner_id=auth.partner.id,
            api_key_id=auth.api_key.id,
            protocol=protocol,
            model=body.get("model") if isinstance(body, dict) else None,
            failure_stage="request_validation",
            error_code="invalid_request_contract",
            http_status=422,
            validation_reason=reason,
        )
        raise HTTPException(
            422,
            "invalid_request_contract",
            headers=validation_headers,
        ) from exc
    idem = request.headers.get("idempotency-key", "")
    if not 8 <= len(idem) <= 160:
        log_rejection(
            trace_id=diagnostic_trace_id,
            partner_id=auth.partner.id,
            api_key_id=auth.api_key.id,
            protocol=protocol,
            model=body.get("model"),
            failure_stage="request_validation",
            error_code="invalid_idempotency_key",
            http_status=422,
        )
        raise HTTPException(422, "invalid_idempotency_key", headers={"X-Request-Id": diagnostic_trace_id})
    client_request_id_header = request.headers.get("x-client-request-id")
    if client_request_id_header is not None and not re.fullmatch(r"[A-Za-z0-9._:/-]{8,160}", client_request_id_header):
        raise HTTPException(422, "invalid_client_request_id", headers={"X-Request-Id": diagnostic_trace_id})
    client_request_id = client_request_id_header or derive_client_request_id(idem)
    partner_id, api_key_id = auth.partner.id, auth.api_key.id
    receipt_requested = request.headers.get("X-Submission-Receipt") == "v1" and client_request_id is not None
    rejection_stage = "admission_replay"
    try:
        generation, attempt = await reserve(
            db,
            auth,
            protocol,
            body,
            idem,
            files_digest=digest,
            client_request_id=client_request_id,
        )
    except HTTPException as exc:
        if (
            receipt_requested
            and isinstance(exc, InferenceAdmissionError)
            and exc.status_code == 503
            and exc.detail == "provider_temporarily_unavailable"
        ):
            rejection_stage = exc.failure_stage
            try:
                # Snapshot IDs above: rollback expires ORM auth objects.
                await db.rollback()
                generation = await fence_rejected_admission(
                    db, partner_id=partner_id, api_key_id=api_key_id,
                    protocol=protocol, body=body, idempotency_key=idem,
                    client_request_id=client_request_id, files_digest=digest,
                )
                attempt = None
            except HTTPException as fence_error:
                log_rejection(
                    trace_id=diagnostic_trace_id, partner_id=partner_id, api_key_id=api_key_id,
                    protocol=protocol, model=body.get("model"), failure_stage="admission_fence",
                    error_code=str(fence_error.detail), http_status=fence_error.status_code,
                )
                raise
            except Exception:
                log_rejection(
                    trace_id=diagnostic_trace_id, partner_id=partner_id, api_key_id=api_key_id,
                    protocol=protocol, model=body.get("model"), failure_stage="admission_fence",
                    error_code="internal_error", http_status=500,
                )
                raise
        else:
            log_rejection(
                trace_id=diagnostic_trace_id, partner_id=partner_id, api_key_id=api_key_id,
                protocol=protocol, model=body.get("model"),
                failure_stage=getattr(exc, "failure_stage", "admission"),
                error_code=str(exc.detail), http_status=exc.status_code,
            )
            raise HTTPException(
                exc.status_code, exc.detail,
                headers={**(exc.headers or {}), "X-Request-Id": diagnostic_trace_id},
            ) from exc
    rejected = rejected_admission_response(
        generation, trace_id=diagnostic_trace_id, receipt_requested=receipt_requested,
    )
    if rejected is not None:
        log_rejection(
            trace_id=diagnostic_trace_id, partner_id=partner_id, api_key_id=api_key_id,
            protocol=protocol, model=body.get("model"), failure_stage=rejection_stage,
            error_code="provider_temporarily_unavailable", http_status=503, generation_id=generation.id,
        )
        return rejected
    if protocol == "videos/generations":
        response_body = {"request_id": generation.id}
        if client_request_id is not None:
            response_body["client_request_id"] = client_request_id
        return JSONResponse(response_body, status_code=202)
    if attempt is None:
        recovered = await replay_response(db, generation)
        if recovered is not None:
            return recovered
        log_rejection(
            trace_id=generation.id,
            partner_id=partner_id,
            api_key_id=api_key_id,
            protocol=protocol,
            model=generation.model_slug,
            failure_stage="idempotency",
            error_code="request_already_submitted",
            http_status=409,
            generation_id=generation.id,
        )
        return public_error("request_already_submitted", 409, generation_id=generation.id)
    try:
        adapter = await get_partner_provider_adapter(
            db, partner_id, "argolink", credential_id=attempt.credential_id
        )
    except HTTPException:
        await fail(db, generation, attempt, "provider_temporarily_unavailable", definitive=True)
        log_rejection(
            trace_id=generation.id,
            partner_id=partner_id,
            api_key_id=api_key_id,
            protocol=protocol,
            model=generation.model_slug,
            failure_stage="provider_credential",
            error_code="provider_temporarily_unavailable",
            http_status=503,
            generation_id=generation.id,
            attempt_id=attempt.id,
        )
        return public_error("provider_temporarily_unavailable", generation_id=generation.id)
    wire_body = dict(body)
    if previous := body.get("previous_response_id"):
        previous_attempt = (
            await db.execute(
                select(ProviderAttempt)
                .join(Generation, Generation.id == ProviderAttempt.generation_id)
                .where(
                    Generation.id == previous,
                    Generation.partner_id == partner_id,
                    Generation.status == "completed",
                    ProviderAttempt.provider == "argolink",
                    ProviderAttempt.credential_id == attempt.credential_id,
                )
            )
        ).scalar_one_or_none()
        if previous_attempt is None or not previous_attempt.provider_task_id:
            await fail(db, generation, attempt, "invalid_previous_response", definitive=True, public_status=422)
            log_rejection(
                trace_id=generation.id,
                partner_id=partner_id,
                api_key_id=api_key_id,
                protocol=protocol,
                model=generation.model_slug,
                failure_stage="request_context",
                error_code="invalid_previous_response",
                http_status=422,
                generation_id=generation.id,
                attempt_id=attempt.id,
            )
            return public_error("invalid_previous_response", 422, generation_id=generation.id)
        wire_body["previous_response_id"] = previous_attempt.provider_task_id
    if protocol == "chat/completions" and body.get("stream"):
        wire_body["stream_options"] = {**body.get("stream_options", {}), "include_usage": True}
    headers = {key: request.headers[key] for key in ("anthropic-version", "anthropic-beta") if key in request.headers}
    await db.commit()
    trace = NativeRequestTrace.begin(generation, attempt, wire_body, files)
    deadline = asyncio.get_running_loop().time() + get_settings().native_request_timeout_seconds
    try:
        async with asyncio.timeout_at(deadline):
            response = await adapter.native_request(protocol, wire_body, files=files, headers=headers)
    except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError) as exc:
        trace.failed(attempt, exc, phase="awaiting_headers")
        await fail(db, generation, attempt, "provider_temporarily_unavailable", definitive=True)
        return public_error("provider_temporarily_unavailable", generation_id=generation.id)
    except (httpx.HTTPError, TimeoutError) as exc:
        trace.failed(attempt, exc, phase="awaiting_headers")
        await fail(db, generation, attempt, "provider_temporarily_unavailable", definitive=False)
        return public_error("provider_temporarily_unavailable", generation_id=generation.id)
    trace.received_headers(attempt, response)
    if not response.is_success:
        definitive = 400 <= response.status_code < 500 and response.status_code != 408
        code = (
            "provider_rate_limited"
            if response.status_code == 429
            else "provider_rejected_request"
            if definitive
            else "provider_temporarily_unavailable"
        )
        retry = response.headers.get("retry-after")
        public_status = 429 if code == "provider_rate_limited" else 422 if response.status_code == 400 else 503
        trace.rejected(error_code=code, http_status=public_status)
        await response.aclose()
        await fail(
            db,
            generation,
            attempt,
            code,
            definitive=definitive,
            public_status=public_status,
            headers={"Retry-After": retry} if retry else None,
        )
        return public_error(
            code,
            public_status,
            generation_id=generation.id,
            headers={"Retry-After": retry} if retry else None,
        )
    if "text/event-stream" in response.headers.get("content-type", ""):
        if not body.get("stream"):
            await response.aclose()
            await fail(db, generation, attempt, "unexpected_provider_stream", definitive=False)
            return public_error("unexpected_provider_stream", generation_id=generation.id)
        return StreamingResponse(
            stream_result(db, generation, attempt, response, protocol, deadline=deadline),
            media_type="text/event-stream",
            headers={"X-Request-Id": generation.id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result_ready = False
    try:
        async with asyncio.timeout_at(deadline):
            chunks, size = [], 0
            async for chunk in response.aiter_bytes(chunk_size=65536):
                size += len(chunk)
                if size > get_settings().native_response_max_bytes:
                    raise ValueError("response_too_large")
                chunks.append(chunk)
            data = json.loads(b"".join(chunks))
            if not isinstance(data, dict):
                raise ValueError("provider_response_invalid")
            if protocol in TEXT_PROTOCOLS:
                # A parsed error envelope or missing result is not a successful delivery.
                result_ready = not data.get("error") and bool(
                    data.get("output") or data.get("choices") or data.get("content")
                )
                if not result_ready:
                    raise ValueError("provider_response_invalid")
                if isinstance(data.get("id"), str):
                    attempt.provider_task_id = data["id"][:255]
                units = token_usage(protocol, data.get("usage"))
            else:
                urls = [
                    item["url"]
                    for item in data.get("data", [])
                    if isinstance(item, dict) and isinstance(item.get("url"), str)
                ]
                if urls:
                    generation.result_url = urls[0]
                    generation.request_payload = {**(generation.request_payload or {}), "result_urls": urls}
                inspected = await inspect_url_images(body, data)
                # Persist and validate bytes BEFORE the independent pricing step.
                if generation.model_slug == "nano-banana-2.1" and len(data.get("data", [])) == 1:
                    await persist_single_image(db, generation, data)
                    result_ready = True
                units = image_usage(body, inspected)
                result_ready = True
        public = JSONResponse(scrub(data, generation.id), headers={"X-Request-Id": generation.id})
        if not await finish(db, generation, attempt, units, response=public):
            return public_error(generation.public_error_code or "request_failed", generation_id=generation.id)
        return public
    except (ValueError, TypeError, KeyError, OSError, httpx.HTTPError) as exc:
        trace.failed(attempt, exc, phase="response_body_or_usage")
        if result_ready:
            public = JSONResponse(scrub(data, generation.id), headers={"X-Request-Id": generation.id})
            if await preserve_unbilled_result(db, generation, attempt, public):
                return public
        return await fail(db, generation, attempt, "provider_response_invalid", definitive=False)
    finally:
        await response.aclose()


async def finish(db, generation, attempt, units, *, response=None):
    await db.execute(
        select(Generation)
        .where(Generation.id == generation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if generation.billing_status in {"not_chargeable", "waived"}:
        return False
    try:
        # Cache, charge and terminal outbox succeed together. A recoverable usage
        # error must not commit half a settlement before delivering a free result.
        async with db.begin_nested():
            if response is not None:
                await save_response(db, generation, response)
            await settle_actual(db, generation, units)
            generation.status = attempt.status = "completed"
            from app.infrastructure.retry import utc_now

            generation.client_finalized_at = generation.client_finalized_at or utc_now()
            generation.provider_cost_hold_usdt = 0
            generation.provider_cost_hold_rub = 0
            generation.request_payload = {**(generation.request_payload or {}), "usage_reconciliation_pending": False}
            attempt.cost_status = "settled"
            attempt.cost_reserve_usdt = 0
            attempt.provider_cost_usdt = generation.actual_provider_cost_usdt
            attempt.usage_snapshot = dict(units)
            await ensure_terminal_webhook_event(db, generation)
    except Exception:
        # Savepoint rollback expires changed ORM fields. Restore them before the
        # error handler decides whether the validated result can be delivered.
        await db.refresh(generation)
        await db.refresh(attempt)
        raise
    await db.commit()
    return True


async def fail(db, generation, attempt, code, *, definitive, public_status=503, headers=None, response=None):
    await finalize_native_failure(db, generation, attempt, code, definitive=definitive)
    response = response or public_error(code, public_status, generation_id=generation.id, headers=headers)
    if generation.billing_status == "not_chargeable":
        await save_response(db, generation, response)
    await db.commit()
    return response


async def stream_result(db, generation, attempt, response, protocol, *, deadline=None):
    decoder, usage = SSEDecoder(), UsageCollector(protocol)
    output = []
    size = 0
    deadline = deadline or asyncio.get_running_loop().time() + get_settings().native_request_timeout_seconds

    def append(frame):
        nonlocal size
        size += len(frame.encode("utf-8"))
        if size > get_settings().native_response_max_bytes:
            raise ValueError("stream_too_large")
        output.append(frame)

    def saved_stream():
        return Response("".join(output), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    try:
        async with asyncio.timeout_at(deadline):
            async for chunk in response.aiter_text():
                if (
                    len(decoder.buffer.encode("utf-8")) + len(chunk.encode("utf-8"))
                    > get_settings().native_response_max_bytes
                ):
                    raise ValueError("stream_frame_too_large")
                for event in decoder.feed(chunk):
                    value = event_data(event)
                    if value is None:
                        frame = event + "\n\n"
                    else:
                        if not isinstance(value, dict):
                            raise ValueError("invalid_stream_event")
                        usage.accept(value)
                        candidate = value.get("response", value.get("message", value))
                        if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
                            attempt.provider_task_id = candidate["id"][:255]
                        prefix = "".join(line + "\n" for line in event.split("\n") if line.startswith("event:"))
                        frame = prefix + "data: " + json.dumps(scrub(value, generation.id), ensure_ascii=False) + "\n\n"
                    append(frame)
                    if usage.failed:
                        raise ValueError("stream_failed")
                    if usage.complete:
                        if protocol == "chat/completions":
                            append("data: [DONE]\n\n")
                            frame += "data: [DONE]\n\n"
                        # Persist before sending the final event: a disconnect
                        # after this commit can recover the identical stream.
                        if not await finish(
                            db, generation, attempt, token_usage(protocol, usage.usage), response=saved_stream()
                        ):
                            raise ValueError("stream_closed")
                        yield frame
                        return
                    yield frame
        raise ValueError("stream_incomplete")
    except (ValueError, TypeError, httpx.HTTPError, TimeoutError):
        frame = 'data: {"error":{"type":"stream_interrupted","message":"stream_interrupted"}}\n\n'
        # Reserve room for the terminal error without exceeding replay bounds.
        if size + len(frame) <= get_settings().native_response_max_bytes:
            append(frame)
            public = saved_stream()
        else:
            public = Response(frame, media_type="text/event-stream")
        await fail(db, generation, attempt, "stream_interrupted", definitive=False, response=public)
        yield frame
    finally:
        # Cancellation/process death is bounded by the durable native deadline
        # sweep; it never creates a second provider job.
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
