"""Standalone browser smoke with synthetic session/DB and isolated provider.

Install playwright separately, then run:
PYTHONPATH=. python tests/browser_playground.py
Requires a local Chromium executable; never contacts a real provider or Telegram.
"""
import asyncio
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import uvicorn
from playwright.async_api import async_playwright
from pytest import MonkeyPatch
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.catalog.models import Model
from app.dashboard import web
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.database import Base, get_db_session
from app.main import app
from tests.test_native_inference import setup


async def main():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            'id': 'private-task', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': '<img src=x onerror="window.compromised=true">'}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 20},
        })
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    patch = MonkeyPatch()
    settings = get_settings()
    patch.setattr(settings, 'public_api_base_url', 'https://localhost:8765')
    patch.setattr(settings, 'provider_credentials_master_key', 'browser-synthetic-master-key-32')
    patch.setattr(settings, 'telegram_bot_token', 'browser-synthetic-bot-token')
    patch.setattr(settings, 'opening_working_capital_usdt', Decimal('1000000'))
    patch.setattr('app.billing.capital.wallet_balance', AsyncMock(return_value=(Decimal('1000000'), 0, 'fresh')))
    patch.setattr('app.providers.argolink.ArgoLinkAdapter.prepaid_balance_usdt',
                  AsyncMock(return_value=Decimal('1000000')))
    fx = AsyncMock(return_value={'rate': Decimal('100'), 'source': 'manual_fallback', 'automatic_at': None})
    patch.setattr('app.billing.fx.current_fx', fx)
    patch.setattr('app.billing.incidents.current_fx', fx)
    async with factory() as db:
        partner, _, upstream = await setup(db, patch, handler)
        partner_id = partner.id
        db.add_all([
            Model(slug='gpt-image-2.6-flare', name='Test image', modality='image', status='production'),
            Model(slug='seedance-2.5', name='Test video', modality='video', status='production'),
        ])
        await db.commit()
    async def read_session(token):
        return partner_id if token == 'synthetic-browser-session' else None
    patch.setattr(web, '_read_session', read_session)
    async def database():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    app.dependency_overrides[get_db_session] = database
    output = Path(tempfile.mkdtemp(prefix='ooo-playground-browser-'))
    cert = output / 'localhost.crt'
    key = output / 'localhost.key'
    process = await asyncio.create_subprocess_exec(
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    assert await process.wait() == 0
    ready = asyncio.Event()
    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()
    server = Server(uvicorn.Config(app, host='127.0.0.1', port=8765, lifespan='off', log_level='error',
                                  ssl_keyfile=str(key), ssl_certfile=str(cert)))
    server_task = asyncio.create_task(server.serve())
    await asyncio.wait_for(ready.wait(), timeout=10)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                               args=['--no-sandbox'])
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000},
                                                 ignore_https_errors=True)
            await context.add_cookies([{'name': web._SESSION_COOKIE, 'value': 'synthetic-browser-session',
                                       'url': 'https://localhost:8765', 'secure': True, 'sameSite': 'Lax'}])
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto('https://localhost:8765/cabinet?section=playground')
            await page.wait_for_load_state('networkidle')
            await page.locator('#pg-app').wait_for(state='visible')
            await page.locator('#pg-model').select_option('gpt-image-2.6-flare')
            assert await page.locator('[data-parameter=resolution]').count() == 0
            assert await page.locator('[data-parameter=size]').count() == 1
            await page.locator('#pg-model').select_option('seedance-2.5')
            assert await page.locator('[data-parameter=duration]').count() == 1
            await page.locator('#pg-model').select_option('gpt-5.4')
            await page.locator('#pg-prompt').fill("Hello ' $(touch /tmp/do-not-run) `example`")
            await page.locator('#pg-quote').click()
            await page.locator('#pg-run').wait_for(state='visible')
            await page.wait_for_function('!document.getElementById("pg-run").disabled')
            # Lose the first successful response, then retry in the actual UI.
            lost = False
            async def lose_once(route):
                nonlocal lost
                if not lost:
                    lost = True
                    await route.fetch()
                    await route.abort('failed')
                else:
                    await route.continue_()
            await page.route('**/cabinet/playground/run', lose_once)
            await page.locator('#pg-run').click()
            await page.get_by_role('status').filter(has_text='Ответ не получен').wait_for()
            await page.locator('#pg-quote').click()
            await page.wait_for_function('!document.getElementById("pg-run").disabled')
            await page.locator('#pg-run').click()
            await page.wait_for_function(
                'document.getElementById("pg-result-status").textContent.includes("completed")')
            assert await page.locator('#pg-response').inner_text()
            assert not await page.evaluate('Boolean(window.compromised)')
            assert await page.locator('#pg-preview img').count() == 0
            assert '$NEIRONYCH_API_KEY' in await page.locator('#pg-curl').inner_text()
            assert 'partner-native-key' not in await page.content()
            assert len(calls) == 1
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(Generation)) == 1
                generation = await db.scalar(select(Generation))
                assert generation.actual_charge_rub == Decimal('.03')
            await page.evaluate('window.scrollTo(0, 0)')
            await page.screenshot(path=str(output / 'desktop.png'), full_page=True)
            for width in (320, 375, 390, 430):
                await page.set_viewport_size({'width': width, 'height': 900})
                await page.wait_for_function(
                    'document.getElementById("portal-sidebar").getBoundingClientRect().right <= 0')
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                await page.screenshot(path=str(output / f'mobile-{width}.png'), full_page=True)
            await page.reload()
            await page.wait_for_load_state('networkidle')
            await page.wait_for_function(
                'document.getElementById("pg-result-status").textContent.includes("completed")')
            assert len(calls) == 1 and errors == []
            await browser.close()
        (output / 'evidence.json').write_text(json.dumps({
            'provider_calls': len(calls), 'generations': 1, 'charge_rub': '0.03',
            'lost_response_retry': 'passed', 'reload_status': 'passed', 'xss': 'passed',
            'widths': [1440, 320, 375, 390, 430], 'page_errors': errors,
        }, indent=2))
        print(output)
    finally:
        server.should_exit = True
        await server_task
        await upstream.aclose()
        app.dependency_overrides.clear()
        key.unlink(missing_ok=True)
        patch.undo()
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
