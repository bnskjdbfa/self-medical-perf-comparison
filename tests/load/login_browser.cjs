/* Run through run_login_concurrency.py against its isolated local server. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
let stage = 'launch';

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    headless: true,
  });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    await cdp.send('Network.enable');
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false, latency: 200, downloadThroughput: 100000, uploadThroughput: 50000,
    });
    const base = process.env.LOGIN_TEST_BASE_URL;
    let codePosts = 0;
    let identityPosts = 0;
    let failNext = false;
    let unavailableNext = false;
    await page.route('**/surveys/preview/v2/**', async route => {
      const request = route.request();
      if (request.method() !== 'POST') return route.continue();
      if (request.url().endsWith('/code/')) codePosts++;
      if (request.url().endsWith('/identity/')) identityPosts++;
      await new Promise(resolve => setTimeout(resolve, 600));
      if (failNext) { failNext = false; return route.abort('failed'); }
      if (unavailableNext) {
        unavailableNext = false;
        return route.fulfill({status: 503, contentType: 'application/json', body: '{"error":"temporarily_unavailable"}'});
      }
      return route.continue();
    });
    const burst = async () => page.evaluate(() => {
      const form = document.querySelector('[data-login-form]');
      const button = form.querySelector('[type="submit"]');
      for (let n = 0; n < 10; n++) { button.click(); form.requestSubmit(); }
    });
    const restored = async () => {
      await page.waitForFunction(() => {
        const form = document.querySelector('[data-login-form]');
        return form && !form.hasAttribute('aria-busy') && !form.querySelector('[type="submit"]').disabled;
      });
    };
    stage = 'code-validation';
    await page.goto(base + '/surveys/preview/v2/code/');
    await page.locator('[name="team_code"]').fill('invalid');
    await burst();
    for (let n = 0; n < 5; n++) await page.keyboard.press('Enter');
    assert.equal(await page.locator('[type="submit"]').isDisabled(), true);
    assert.equal(await page.locator('form').getAttribute('aria-busy'), 'true');
    await page.waitForSelector('.v2-inline-errors');
    await restored();
    assert.equal(codePosts, 1);
    stage = 'code-success';
    await page.locator('[name="team_code"]').fill('LOGIN-RACE');
    await burst();
    await page.waitForURL('**/identity/');
    assert.equal(codePosts, 2);
    stage = 'identity-validation';
    await page.locator('[name="name"]').fill('Incorrect fixture');
    await page.locator('[name="birth_date"]').fill('900101');
    await page.locator('[name="gender"]').selectOption('M');
    await burst();
    await page.waitForSelector('#identity-form-error');
    await restored();
    assert.equal(identityPosts, 1);
    await page.locator('[name="name"]').fill('Race Fixture');
    stage = 'network-retry';
    failNext = true;
    await burst();
    await page.waitForFunction(() => document.querySelector('[data-login-status]').textContent.includes('다시 시도'));
    await restored();
    assert.equal(identityPosts, 2);
    stage = '503-retry';
    unavailableNext = true;
    await page.locator('[name="birth_date"]').press('Enter');
    await page.waitForFunction(() => document.querySelector('[data-login-status]').textContent.includes('다시 시도'));
    await restored();
    assert.equal(identityPosts, 3);
    stage = 'identity-success';
    await burst();
    await page.waitForURL('**/home/');
    assert.equal(identityPosts, 4);
    await page.reload();
    assert.ok(page.url().endsWith('/home/'));
    stage = 'back-navigation';
    await page.goBack();
    if (page.url().endsWith('/identity/')) await restored();
    // Explicit pageshow recovery also covers browsers which do not use BFCache
    // for POST/form pages in automation.
    // GET must not reset authentication.
    stage = 'reset-get-rejected';
    const resetGet = await page.request.get(base + '/surveys/preview/v2/login/reset/');
    assert.equal(resetGet.status(), 405);
    assert.equal((await page.request.get(base + '/surveys/preview/v2/home/')).status(), 200);
    stage = 'pageshow';
    const fresh = await browser.newPage();
    await fresh.goto(base + '/surveys/preview/v2/code/');
    await fresh.goto('about:blank');
    await fresh.goBack();
    await fresh.waitForSelector('[data-login-form]');
    assert.equal(await fresh.locator('[type="submit"]').isDisabled(), false);
    await fresh.evaluate(() => {
      const form = document.querySelector('[data-login-form]');
      form.setAttribute('aria-busy', 'true');
      form.querySelector('[type="submit"]').disabled = true;
      window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
    });
    assert.equal(await fresh.locator('[type="submit"]').isDisabled(), false);
    console.log('PASS browser: slow network, click/Enter bursts, validation retry, network/503 retry, session persistence, pageshow recovery');
    await context.close();
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error('FAIL browser login regression stage=' + stage + ' type=' + error.name);
  process.exitCode = 1;
});
