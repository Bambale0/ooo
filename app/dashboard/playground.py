"""Session BFF for native inference; billing and execution stay in native services."""

import hashlib
import hmac
import json
import time
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.accounts.models import ApiKey
from app.api.dependencies import DbSession, PartnerAuth
from app.billing.service import require_sufficient_balance
from app.catalog.access import RESTRICTED_STATUS, has_model_grant
from app.catalog.models import Model
from app.catalog.pricing import effective_partner_prices
from app.contracts.registry import MODELS, TEXT_PROTOCOLS, validate_request
from app.dashboard.portal import _check_write, _signed_in
from app.generations.models import Generation
from app.infrastructure.config import get_settings

router = APIRouter(include_in_schema=False)
_MAX_BODY = 1024 * 1024
_HEADERS = {"Cache-Control": "private, no-store, max-age=0", "X-Content-Type-Options": "nosniff"}


class PlaygroundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: str = Field(max_length=40)
    api_key_id: str = Field(max_length=36)
    body: dict
    idempotency_key: str = Field(min_length=8, max_length=130, pattern=r"^[A-Za-z0-9._:-]+$")
    quote_token: str = Field(default="", max_length=100)


async def _payload(request: Request) -> PlaygroundRequest:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > _MAX_BODY:
            raise HTTPException(413, "playground_request_too_large")
    try:
        payload = PlaygroundRequest.model_validate_json(raw)
        if payload.body.get("stream"):
            raise ValueError("stream_not_supported")
        payload.body = validate_request(payload.protocol, payload.body)
        return payload
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        # Do not echo request input, provider details or validation exception text.
        raise HTTPException(422, "invalid_playground_request") from exc


async def _auth(db, partner, key_id: str) -> PartnerAuth:
    key = await db.scalar(select(ApiKey).where(
        ApiKey.id == key_id, ApiKey.partner_id == partner.id, ApiKey.is_active.is_(True),
    ))
    if key is None:
        raise HTTPException(404, "api_key_not_available")
    return PartnerAuth(partner=partner, api_key=key)


async def _model(db, partner, slug: str) -> Model:
    model = await db.scalar(select(Model).where(Model.slug == slug))
    if model is None or model.status not in {"production", RESTRICTED_STATUS}:
        raise HTTPException(404, "model_not_available")
    if model.status == RESTRICTED_STATUS and not await has_model_grant(db, model.id, partner.id):
        raise HTTPException(404, "model_not_available")
    return model


async def _estimate(db, partner, payload) -> str:
    from app.billing.fx import current_fx
    from app.inference.accounting import charges
    from app.inference.service import quote

    model = await _model(db, partner, payload.body["model"])
    prices = await effective_partner_prices(db, partner_id=partner.id, model_id=model.id)
    fx = await current_fx(db)
    rates, units, _ = quote(payload.protocol, payload.body, prices, fx=fx["rate"])
    amount, _ = charges(rates, units, divisor=Decimal(1000000) if payload.protocol in TEXT_PROTOCOLS else Decimal(1))
    return str(amount.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def _quote_signature(session_id, partner_id, payload, amount, expires):
    secret = get_settings().provider_credentials_master_key
    if not secret:
        raise HTTPException(503, "playground_quote_unavailable")
    # Domain-separated server secret; the browser-visible CSRF token is NOT a signing key.
    key = hmac.new(secret.encode(), b"cabinet-playground-quote-v1", hashlib.sha256).digest()
    message = json.dumps(
        [session_id, partner_id, payload.protocol, payload.api_key_id, payload.body,
         payload.idempotency_key, amount, expires],
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


@router.get('/cabinet/playground/catalog')
async def catalog(request: Request, db: DbSession):
    partner, _ = await _signed_in(request, db)
    models = []
    rows = (await db.scalars(select(Model).where(
        Model.status.in_(['production', RESTRICTED_STATUS]),
    ).order_by(Model.slug))).all()
    for model in rows:
        entry = MODELS.get(model.slug)
        if entry is None or (model.status == RESTRICTED_STATUS and not await has_model_grant(db, model.id, partner.id)):
            continue
        # Only public request shape. Procurement/provider metadata never leaves BFF.
        image = entry.get('image', {})
        models.append({
            'id': model.slug, 'name': model.name, 'category': entry['category'],
            'protocols': sorted(TEXT_PROTOCOLS) if entry['category'] == 'chat' else (
                ['images/generations', 'images/edits'] if entry['category'] == 'image' else ['videos/generations']
            ),
            'default_protocol': entry['endpoint'].removeprefix('/v1/'),
            'resolutions': [t['label'] for t in entry['procurement'].get('tiers', [])],
            'image': {k: image[k] for k in (
                'aspect_ratios', 'default_aspect_ratio', 'quality', 'default_quality', 'response_formats',
                'frame', 'default_resolution', 'request_fields',
            ) if k in image},
        })
    keys = (await db.scalars(select(ApiKey).where(
        ApiKey.partner_id == partner.id, ApiKey.is_active.is_(True),
    ).order_by(ApiKey.created_at))).all()
    return JSONResponse({'models': models, 'keys': [{'id': k.id, 'name': k.name} for k in keys]}, headers=_HEADERS)


@router.post('/cabinet/playground/quote')
async def estimate(request: Request, db: DbSession):
    partner, session_id = await _signed_in(request, db)
    _check_write(request, session_id, request.headers.get('x-csrf-token', ''))
    payload = await _payload(request)
    await _auth(db, partner, payload.api_key_id)
    await require_sufficient_balance(partner, Decimal('.01'))
    amount = await _estimate(db, partner, payload)
    expires = int(time.time()) + 300
    token = f'{expires}.' + _quote_signature(session_id, partner.id, payload, amount, expires)
    return JSONResponse({
        'estimate_rub': amount, 'quote_token': token, 'expires_at': expires,
        'notice': 'Предварительная оценка. Итоговое списание — по фактическому usage действующего API.',
    }, headers=_HEADERS)


@router.post('/cabinet/playground/run')
async def run(request: Request, db: DbSession):
    from app.inference.router import inference

    partner, session_id = await _signed_in(request, db)
    _check_write(request, session_id, request.headers.get('x-csrf-token', ''))
    payload = await _payload(request)
    auth = await _auth(db, partner, payload.api_key_id)
    amount = await _estimate(db, partner, payload)
    try:
        expires_text, signature = payload.quote_token.split('.')
        expires = int(expires_text)
    except ValueError as exc:
        raise HTTPException(409, 'playground_quote_required') from exc
    expected = _quote_signature(session_id, partner.id, payload, amount, expires)
    if not int(time.time()) <= expires <= int(time.time()) + 300 or not hmac.compare_digest(expected, signature):
        raise HTTPException(409, 'playground_quote_expired_or_changed')
    # Same native HTTP handler, explicit server-authenticated identity. No internal
    # HTTP loop, fake Bearer key, trial flag, alternative queue or settlement path.
    wire = json.dumps(payload.body).encode()
    scope = {**request.scope, 'headers': [
        (b'content-type', b'application/json'),
        (b'idempotency-key', ('playground:' + payload.idempotency_key).encode()),
    ]}
    async def receive():
        return {'type': 'http.request', 'body': wire, 'more_body': False}
    response = await inference(payload.protocol, Request(scope, receive), db, auth)
    response.headers.update(_HEADERS)
    return response


@router.get('/cabinet/playground/results/{generation_id}')
async def result(generation_id: str, request: Request, db: DbSession):
    from app.inference.replay import replay_response
    from app.inference.router import video_status

    partner, _ = await _signed_in(request, db)
    generation = await db.scalar(select(Generation).where(
        Generation.id == generation_id, Generation.partner_id == partner.id,
    ))
    if generation is None:
        raise HTTPException(404, 'generation_not_found')
    data = {'request_id': generation.id, 'status': generation.status,
            'charge_rub': str(generation.actual_charge_rub) if generation.actual_charge_rub is not None else None}
    if generation.mode == 'videos/generations':
        key = await db.get(ApiKey, (generation.request_payload or {}).get('api_key_id'))
        data['response'] = await video_status(generation.id, db, PartnerAuth(partner=partner, api_key=key))
    else:
        replay = await replay_response(db, generation)
        if replay is not None:
            data['response'] = json.loads(replay.body)
    return JSONResponse(data, headers=_HEADERS)
