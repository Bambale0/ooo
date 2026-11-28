"""Cabinet behavior through HTTP; real native billing, isolated upstream only."""
import re
from decimal import Decimal

import httpx
from sqlalchemy import func, select

from app.generations.models import Generation
from app.telegram.ui import home_keyboard
from tests.test_native_inference import setup
from tests.test_web_cabinet import browser as browser  # noqa: F401


def test_home_menus_do_not_offer_trials(monkeypatch):
    from app.infrastructure.config import get_settings
    monkeypatch.setattr(get_settings(), "public_api_base_url", "https://example.org")
    for admin in (False, True):
        buttons = [b for row in home_keyboard(admin=admin).inline_keyboard for b in row]
        assert all(b.callback_data != "trials" and "Бесплатные видеотесты" not in b.text for b in buttons)
        assert any(b.url and b.url.endswith('/cabinet') for b in buttons)


async def signed_browser(browser, monkeypatch, partner):
    from app.dashboard import web
    partner_id = partner.id
    async def read(_):
        return partner_id
    monkeypatch.setattr(web, "_read_session", read)
    browser.cookies.set(web._SESSION_COOKIE, "synthetic-session")
    page = await browser.get('/cabinet?section=playground')
    assert page.status_code == 200
    csrf = re.search(r'name="playground-csrf" content="([^"]+)"', page.text).group(1)
    return {'Origin': 'http://localhost:8000', 'X-CSRF-Token': csrf}


async def test_playground_requires_session(browser):
    response = await browser.get('/cabinet/playground/catalog')
    assert response.status_code == 401


async def test_paid_text_request_and_retry_use_native_billing(browser, db_session, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            'id': 'private-upstream-id',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '<script>bad()</script>'}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 20},
        })
    partner, _, upstream = await setup(db_session, monkeypatch, handler)
    headers = await signed_browser(browser, monkeypatch, partner)
    catalog = await browser.get('/cabinet/playground/catalog')
    assert catalog.status_code == 200
    data = catalog.json()
    assert [m['id'] for m in data['models']] == ['gpt-5.4']
    assert 'procurement' not in catalog.text and 'upstream' not in catalog.text and 'key_hash' not in catalog.text
    payload = {'protocol': 'responses', 'api_key_id': data['keys'][0]['id'],
               'body': {'model': 'gpt-5.4', 'input': 'Hello', 'max_output_tokens': 128},
               'idempotency_key': 'playground-test-operation'}
    quoted = await browser.post('/cabinet/playground/quote', json=payload, headers=headers)
    assert quoted.status_code == 200
    assert Decimal(quoted.json()['estimate_rub']) > 0
    assert calls == []
    payload['quote_token'] = quoted.json()['quote_token']
    response = await browser.post('/cabinet/playground/run', json=payload, headers=headers)
    assert response.status_code == 200, response.text
    generation_id = response.headers['x-request-id']
    again = await browser.post('/cabinet/playground/run', json=payload, headers=headers)
    assert again.status_code == 200 and again.headers['x-request-id'] == generation_id
    assert len(calls) == 1
    row = await db_session.get(Generation, generation_id)
    assert row.actual_charge_rub == Decimal('.03')
    assert (await db_session.scalar(select(func.count()).select_from(Generation))) == 1
    result = await browser.get('/cabinet/playground/results/' + generation_id)
    assert result.json()['charge_rub'] == '0.03'
    assert 'private-upstream-id' not in result.text
    assert response.headers['cache-control'].startswith('private, no-store')
    await upstream.aclose()


async def context(browser, db_session, monkeypatch, **kwargs):
    def handler(request):
        raise AssertionError('Rejected request reached upstream')
    partner, _, upstream = await setup(db_session, monkeypatch, handler, **kwargs)
    headers = await signed_browser(browser, monkeypatch, partner)
    catalog = (await browser.get('/cabinet/playground/catalog')).json()
    payload = {'protocol': 'responses', 'api_key_id': catalog['keys'][0]['id'],
               'body': {'model': 'gpt-5.4', 'input': 'Hello', 'max_output_tokens': 128},
               'idempotency_key': 'playground-rejection-test'}
    return partner, headers, payload, upstream


async def test_csrf_origin_and_quote_binding_fail_closed(browser, db_session, monkeypatch):
    partner, headers, payload, upstream = await context(browser, db_session, monkeypatch)
    for bad in ({}, {**headers, 'Origin': 'https://evil.example'}, {**headers, 'X-CSRF-Token': 'forged'}):
        for action in ('quote', 'run'):
            assert (await browser.post('/cabinet/playground/' + action, json=payload, headers=bad)).status_code == 403
    quoted = await browser.post('/cabinet/playground/quote', json=payload, headers=headers)
    payload['quote_token'] = quoted.json()['quote_token']
    payload['body']['input'] = 'changed after quote'
    assert (await browser.post('/cabinet/playground/run', json=payload, headers=headers)).status_code == 409
    payload['quote_token'] = '1.' + 'a' * 64
    assert (await browser.post('/cabinet/playground/run', json=payload, headers=headers)).status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(Generation)) == 0
    await upstream.aclose()


async def test_key_and_generation_ownership_and_revocation(browser, db_session, monkeypatch):
    from app.accounts.models import ApiKey, Partner
    partner, headers, payload, upstream = await context(browser, db_session, monkeypatch)
    stranger = Partner(telegram_id='stranger', company_name='Other', project_name='Other', balance_rub=1000)
    db_session.add(stranger)
    await db_session.flush()
    key = ApiKey(partner_id=stranger.id, name='Other secret key', key_hash='other-hash', key_prefix='other')
    db_session.add(key)
    await db_session.flush()
    foreign = Generation(
        partner_id=stranger.id, model_id='foreign', model_slug='gpt-5.4', mode='responses',
        resolution='default', idempotency_key='foreign-request', partner_price_rub=Decimal('1'), prompt='private',
    )
    db_session.add(foreign)
    await db_session.commit()
    foreign_id = foreign.id
    partner_id, other_key_id = partner.id, key.id
    payload['api_key_id'] = other_key_id
    assert (await browser.post('/cabinet/playground/quote', json=payload, headers=headers)).status_code == 404
    assert other_key_id not in (await browser.get('/cabinet/playground/catalog')).text
    own = await db_session.scalar(select(ApiKey).where(ApiKey.partner_id == partner_id))
    payload['api_key_id'] = own.id
    token = (await browser.post('/cabinet/playground/quote', json=payload, headers=headers)).json()['quote_token']
    own.is_active = False
    await db_session.commit()
    assert (await browser.post('/cabinet/playground/run', json={**payload, 'quote_token': token},
                               headers=headers)).status_code == 404
    assert (await browser.get('/cabinet/playground/results/' + foreign_id)).status_code == 404
    await db_session.refresh(partner)
    partner.status = 'suspended'
    await db_session.commit()
    assert (await browser.get('/cabinet/playground/catalog')).status_code == 401
    await upstream.aclose()


async def test_balance_and_invalid_contracts_create_no_generation(browser, db_session, monkeypatch):
    partner, headers, payload, upstream = await context(browser, db_session, monkeypatch)
    partner.balance_rub = Decimal('0')
    await db_session.commit()
    assert (await browser.post('/cabinet/playground/quote', json=payload, headers=headers)).status_code == 402
    partner.balance_rub = Decimal('1000')
    await db_session.commit()
    invalid = [
        {**payload, 'trial_telegram_id': partner.telegram_id},
        {**payload, 'body': {**payload['body'], 'stream': True}},
        {**payload, 'protocol': 'unknown'},
        {**payload, 'body': {'model': 'gpt-5.4'}},
    ]
    for value in invalid:
        assert (await browser.post('/cabinet/playground/quote', json=value, headers=headers)).status_code == 422
    huge = {**payload, 'body': {**payload['body'], 'input': 'x' * (1024 * 1024)}}
    assert (await browser.post('/cabinet/playground/quote', json=huge, headers=headers)).status_code == 413
    assert await db_session.scalar(select(func.count()).select_from(Generation)) == 0
    await upstream.aclose()


async def test_video_uses_durable_queue_and_paid_settlement(browser, db_session, monkeypatch):
    from unittest.mock import AsyncMock

    from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
    from app.providers.argolink import ArgoLinkAdapter
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(202, json={'request_id': 'private-task'})
        return httpx.Response(200, json={'status': 'done', 'video': {'url': 'https://argolink.io/content'},
                                        'usage': {'billed_seconds': 4}})
    partner, _, upstream = await setup(
        db_session, monkeypatch, handler, model='seedance-2.5', category='video',
        rates=[('default', '720p', 'second', Decimal('30'), Decimal('.196'))],
    )
    monkeypatch.setattr('app.generations.service.get_partner_provider_adapter',
                        AsyncMock(return_value=ArgoLinkAdapter(api_key='upstream', client=upstream)))
    headers = await signed_browser(browser, monkeypatch, partner)
    key_id = (await browser.get('/cabinet/playground/catalog')).json()['keys'][0]['id']
    payload = {'protocol': 'videos/generations', 'api_key_id': key_id, 'idempotency_key': 'video-playground-operation',
               'body': {'model': 'seedance-2.5', 'prompt': 'A sunrise', 'duration': 4, 'resolution': '720p'}}
    quoted = await browser.post('/cabinet/playground/quote', json=payload, headers=headers)
    assert quoted.status_code == 200 and quoted.json()['estimate_rub'] == '120.00'
    payload['quote_token'] = quoted.json()['quote_token']
    response = await browser.post('/cabinet/playground/run', json=payload, headers=headers)
    assert response.status_code == 202, response.text
    generation_id = response.json()['request_id']
    row = await db_session.get(Generation, generation_id)
    assert row.status == 'queued' and calls == []
    again = await browser.post('/cabinet/playground/run', json=payload, headers=headers)
    assert again.json()['request_id'] == generation_id
    assert row.partner_price_rub == Decimal('120')
    assert 'trial_telegram_id' not in row.request_payload
    attempt = await dispatch_generation_to_provider(db_session, row)
    attempt.next_poll_at = None
    await db_session.commit()
    await poll_generation_provider(db_session, row)
    assert row.status == 'completed' and row.actual_charge_rub == Decimal('120')
    assert calls == ['POST', 'GET']
    result = await browser.get('/cabinet/playground/results/' + generation_id)
    assert result.status_code == 200 and result.json()['status'] == 'completed'
    assert 'argolink.io' not in result.text and 'private-task' not in result.text
    await upstream.aclose()


async def test_playground_static_assets_are_served(browser):
    for name, kind in [('playground.js', 'javascript'), ('playground.css', 'text/css')]:
        response = await browser.get('/ui/' + name)
        assert response.status_code == 200 and kind in response.headers['content-type']


async def test_image_json_uses_native_result_and_actual_charge(browser, db_session, monkeypatch):
    import base64
    import io

    from PIL import Image

    image = io.BytesIO()
    Image.new('RGB', (1024, 1024)).save(image, format='PNG')
    encoded = base64.b64encode(image.getvalue()).decode()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'data': [{'b64_json': encoded}]})
    partner, _, upstream = await setup(
        db_session, monkeypatch, handler, model='nano-banana-2.1', category='image',
        rates=[('default', tier, 'generation', Decimal('10'), Decimal('.02')) for tier in ('1K', '2K', '4K')],
    )
    headers = await signed_browser(browser, monkeypatch, partner)
    key_id = (await browser.get('/cabinet/playground/catalog')).json()['keys'][0]['id']
    payload = {'protocol': 'images/generations', 'api_key_id': key_id, 'idempotency_key': 'image-playground-test',
               'body': {'model': 'nano-banana-2.1', 'prompt': 'A sunrise', 'resolution': '1K'}}
    quote = await browser.post('/cabinet/playground/quote', json=payload, headers=headers)
    assert quote.status_code == 200 and quote.json()['estimate_rub'] == '10.00'
    payload['quote_token'] = quote.json()['quote_token']
    response = await browser.post('/cabinet/playground/run', json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['data'][0]['b64_json'] == encoded
    result = await browser.get('/cabinet/playground/results/' + response.headers['x-request-id'])
    assert result.json()['charge_rub'] == '10.00'
    assert len(calls) == 1
    await upstream.aclose()


async def test_restricted_models_and_price_changes_respect_control_plane(browser, db_session, monkeypatch):
    from app.catalog.models import Model, PartnerModelGrant, PartnerPriceSnapshot
    partner, headers, payload, upstream = await context(browser, db_session, monkeypatch)
    model = await db_session.scalar(select(Model).where(Model.slug == 'gpt-5.4'))
    model.status = 'restricted'
    await db_session.commit()
    partner_id, model_id = partner.id, model.id
    assert (await browser.get('/cabinet/playground/catalog')).json()['models'] == []
    assert (await browser.post('/cabinet/playground/quote', json=payload, headers=headers)).status_code == 404
    db_session.add(PartnerModelGrant(model_id=model_id, partner_id=partner_id, reason='test', granted_by='test'))
    await db_session.commit()
    assert len((await browser.get('/cabinet/playground/catalog')).json()['models']) == 1
    quoted = await browser.post('/cabinet/playground/quote', json=payload, headers=headers)
    assert quoted.status_code == 200
    payload['quote_token'] = quoted.json()['quote_token']
    rates = (await db_session.scalars(select(PartnerPriceSnapshot))).all()
    for price in rates:
        price.price_rub *= 2
    await db_session.commit()
    assert (await browser.post('/cabinet/playground/run', json=payload, headers=headers)).status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(Generation)) == 0
    await upstream.aclose()


async def test_quote_cannot_be_forged_with_browser_visible_csrf(browser, db_session, monkeypatch):
    import hashlib
    import hmac
    import json
    import time

    _, headers, payload, upstream = await context(browser, db_session, monkeypatch)
    quoted = (await browser.post('/cabinet/playground/quote', json=payload, headers=headers)).json()
    expires = int(time.time()) + 300
    from app.accounts.models import ApiKey

    partner_id = await db_session.scalar(select(ApiKey.partner_id).where(ApiKey.id == payload['api_key_id']))
    message = json.dumps(
        ['synthetic-session', partner_id, payload['protocol'], payload['api_key_id'],
         payload['body'], payload['idempotency_key'], quoted['estimate_rub'], expires],
        sort_keys=True, separators=(',', ':'), ensure_ascii=True,
    )
    forged = hmac.new(headers['X-CSRF-Token'].encode(), message.encode(), hashlib.sha256).hexdigest()
    payload['quote_token'] = f'{expires}.{forged}'
    assert (await browser.post('/cabinet/playground/run', json=payload, headers=headers)).status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(Generation)) == 0
    await upstream.aclose()
