import { test as setup } from '@playwright/test';
import * as OTPAuth from 'otpauth';
import * as fs from 'fs';

// primes.auth/demo-user.json with an authenticated session for the
// PROD demo account (username `kavanah`, scoped to the kavanah-demo
// workspace). Mirrors tests/e2e/global-setup.ts but:
//   - reads DEMO_USERNAME / DEMO_PASSWORD / DEMO_TOTP_SECRET (never the
//     E2E_* staging credentials — captures must come from the demo account),
//   - HARD-FAILS instead of writing an empty state when login does not
//     produce a session. A silent empty state would let the capture run
//     proceed unauthenticated and produce login-page screenshots.
// The demo account is MFA-bypassed; the TOTP branch
// exists only in case that changes.

const enabled = process.env.DOCS_CAPTURE === '1';
setup.skip(!enabled, 'docs-capture: set DOCS_CAPTURE=1 to run the  pipeline');

setup('authenticate demo account', async ({ page }) => {
  const baseURL = process.env.BASE_URL || 'http://localhost:8084';
  const username = process.env.DEMO_USERNAME;
  const password = process.env.DEMO_PASSWORD;
  const totpSecret = process.env.DEMO_TOTP_SECRET;

  if (!username || !password) {
    throw new Error(
      '[demo-auth] DOCS_CAPTURE=1 but DEMO_USERNAME / DEMO_PASSWORD are not set. ' +
      'See the private docs'
    );
  }

  const appPrefix = process.env.APP_PREFIX || '';
  await page.goto(`${baseURL}${appPrefix}/login`);
  await page.waitForSelector('input[name="username"]');
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');

  await page.waitForURL((url) => !url.pathname.endsWith('/login'), { timeout: 10000 });
  await page.waitForLoadState('domcontentloaded', { timeout: 10000 });

  if (page.url().includes('/mfa/challenge')) {
    if (!totpSecret) {
      throw new Error(
        '[demo-auth] Demo account was redirected to /mfa/challenge but ' +
        'DEMO_TOTP_SECRET is not set. The demo account is expected to be ' +
        'MFA-bypassed — investigate before capturing.'
      );
    }
    const totp = new OTPAuth.TOTP({
      secret: OTPAuth.Secret.fromBase32(totpSecret),
      digits: 6,
      period: 30,
    });
    await page.fill('input[name="code"]', totp.generate());
    await page.click('button[type="submit"]');
    await page.waitForURL(/^(?!.*\/mfa\/)/, { timeout: 10000 });
  }

  const cookies = await page.context().cookies();
  const hasSessionCookie = cookies.some((c) => c.name === 'session' || c.name === 'user_session');
  if (!hasSessionCookie || page.url().includes('/login')) {
    throw new Error(
      `[demo-auth] Demo login did not produce an authenticated session ` +
      `(url=${page.url()}, session cookie=${hasSessionCookie}).`
    );
  }

  fs.mkdirSync('.auth', { recursive: true });
  await page.context().storageState({ path: '.auth/demo-user.json' });
});
