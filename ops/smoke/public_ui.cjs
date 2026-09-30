/* Run against an isolated preview with public catalog data; never submits paid requests.
 * NODE_PATH=/tmp/ooo-ux-tools/node_modules node ops/smoke/public_ui.cjs
 * Optional: UI_BASE_URL, UI_ARTIFACT_DIR, CHROMIUM_EXECUTABLE, UI_BROWSER=webkit
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium, webkit} = require('playwright');
const base = process.env.UI_BASE_URL || 'http://127.0.0.1:8766';
const output = process.env.UI_ARTIFACT_DIR || '/tmp/ooo-ui-audit';
const browserName = process.env.UI_BROWSER || 'chromium';
fs.mkdirSync(output, {recursive: true});
(async () => {
  const browser = await (browserName === 'webkit' ? webkit : chromium).launch({
    headless: true,
    ...(process.env.CHROMIUM_EXECUTABLE ? {executablePath: process.env.CHROMIUM_EXECUTABLE} : {}),
  });
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const page = await context.newPage();
    const errors = [];
    const failures = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    page.on('response', response => { if (response.status() >= 400) failures.push([response.status(), response.url()]); });
    const checks = [];
    const a11y = [];
    for (const route of ['/', '/docs?lang=ru', '/docs?lang=en', '/guide', '/prices']) {
      assert.equal((await page.goto(base + route)).status(), 200);
      await page.addScriptTag({path: require.resolve('axe-core/axe.min.js')});
      const result = await page.evaluate(async () => {
        // Exercise content in native disclosures as well as the initial screen.
        document.querySelectorAll('details').forEach(element => { element.open = true; });
        return await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'best-practice']}});
      });
      a11y.push({route, violations: result.violations.map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.map(n => n.target)}))});
      for (const width of [320, 360, 375, 390, 414, 768, 1024, 1280, 1440, 1920]) {
        await page.setViewportSize({width, height: width < 500 ? 844 : 1000});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${route}: overflow at ${width}`);
      }
      checks.push(`responsive ${route}: 10 widths`);
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto(base + '/docs');
    await page.screenshot({path: path.join(output, `${browserName}-docs.png`)});
    await page.keyboard.press('Tab');
    assert.equal(await page.locator(':focus').getAttribute('class'), 'skip-link');
    await page.keyboard.press('Enter');
    assert.equal(await page.locator(':focus').getAttribute('id'), 'main');
    await page.locator('.hero-actions a').first().click();
    assert.equal(new URL(page.url()).hash, '#connect');
    for (const anchor of ['models','text','images','video','uploads','python','errors']) {
      await page.locator(`.contents a[href="#${anchor}"]`).click();
      assert.equal(new URL(page.url()).hash, '#' + anchor);
      const top = await page.locator('#' + anchor).evaluate(e => e.getBoundingClientRect().top);
      assert(top >= 86 && top < 500, `Anchor hidden or out of view: ${anchor}: ${top}`);
    }
    await page.locator('[data-language][lang=en]').click();
    assert.equal(new URL(page.url()).hash, '#errors');
    assert.equal(await page.locator('html').getAttribute('lang'), 'en');
    await page.reload();
    assert.equal(new URL(page.url()).hash, '#errors');
    await page.goBack();
    assert.equal(await page.locator('html').getAttribute('lang'), 'ru');
    checks.push('skip link, all section links, primary action, language preserves section, reload/back');
    await page.goto(base + '/docs');
    const expected = await page.locator('pre code').first().textContent();
    if (browserName === 'chromium') {
      await context.grantPermissions(['clipboard-read', 'clipboard-write']);
      await page.locator('.copy-button').first().click();
      await page.waitForFunction(() => document.querySelector('.toast').textContent.includes('Скопировано'));
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), expected);
      checks.push('actual clipboard copy');
    }
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async () => {throw new Error('denied');}}}));
    await page.locator('.copy-button').first().click();
    await page.waitForFunction(() => document.querySelector('.toast').textContent.includes('Код выделен'));
    assert.equal(await page.evaluate(() => getSelection().toString()), expected);
    assert.equal(await page.locator('.copy-button').first().isEnabled(), true);
    checks.push('clipboard permission failure: selected text + recoverable action');
    await page.locator('.primary-nav a').nth(1).click();
    assert.equal(new URL(page.url()).pathname, '/prices');
    const total = await page.locator('#price-table tbody tr').count();
    assert(total > 0, 'Seed the isolated database with public catalog prices');
    await page.locator('#price-search').fill('seedance');
    assert(await page.locator('#price-table tbody tr:visible').count() > 0);
    assert(await page.locator('#price-table tbody tr:visible').count() < total);
    await page.locator('#price-search').fill('NO_MATCH_🧠_<script>');
    assert(await page.locator('#no-results').isVisible());
    await page.locator('#clear-search').click();
    assert.equal(await page.locator('#price-table tbody tr:visible').count(), total);
    assert.equal(await page.locator('#price-search').inputValue(), '');
    await page.screenshot({path: path.join(output, `${browserName}-prices.png`), fullPage: true});
    await page.locator('.hero-actions a').click();
    assert.equal(new URL(page.url()).pathname, '/docs');
    checks.push('pricing navigation, real catalog filter, no results, reset, return to docs');
    await page.setViewportSize({width: 390, height: 844});
    await page.goto(base + '/docs');
    await page.screenshot({path: path.join(output, `${browserName}-mobile.png`)});
    await page.locator('.section-menu summary').click();
    assert.equal(await page.locator('.section-menu').getAttribute('open'), null);
    await page.locator('.section-menu summary').click();
    await page.locator('.contents a[href="#video"]').click();
    assert((await page.locator('#video').boundingBox()).y >= 115);
    await page.locator('.site-footer a').click();
    assert.equal(new URL(page.url()).hash, '#main');
    const noJS = await browser.newContext({javaScriptEnabled: false, viewport: {width: 390,height: 844}});
    const fallback = await noJS.newPage();
    await fallback.goto(base + '/docs');
    assert(await fallback.locator('#connect').isVisible());
    assert(await fallback.locator('pre code').count() > 10);
    await fallback.goto(base + '/prices');
    assert.equal(await fallback.locator('#price-table tbody tr').count(), total);
    assert.equal(await fallback.locator('.price-tools').isVisible(), false);
    await noJS.close();
    checks.push('mobile section toggle, mobile anchor, back to top, no-JS documentation and complete prices');
    fs.writeFileSync(path.join(output, `${browserName}-report.json`), JSON.stringify({browserName, checks, a11y, errors, failures}, null, 2));
    assert.deepEqual(errors, [], 'Console errors');
    assert.deepEqual(failures, [], 'Failed network requests');
    assert(a11y.every(item => item.violations.length === 0), 'Accessibility findings: see JSON report');
    console.log(JSON.stringify({browserName, checks, a11y: 'no violations', console: 'clean', network: 'clean'}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
