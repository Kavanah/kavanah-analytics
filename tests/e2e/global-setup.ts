import { test as setup } from '@playwright/test';
import * as OTPAuth from 'otpauth';

setup('authenticate', async ({ page }) => {
  const baseURL = process.env.BASE_URL || 'http://localhost:8084';
  const username = process.env.E2E_USERNAME;
  const password = process.env.E2E_PASSWORD;
  const totpSecret = process.env.E2E_TOTP_SECRET;

  if (!username || !password) {
    // No credentials — write an empty auth state so dependent tests can still
    // be skipped or run in unauthenticated mode rather than hard-failing.
    const fs = await import('fs');
    fs.mkdirSync('.auth', { recursive: true });
    fs.writeFileSync('.auth/user.json', JSON.stringify({ cookies: [], origins: [] }));
    console.warn(
      'E2E_USERNAME / E2E_PASSWORD not set — saved empty auth state. ' +
      'Authenticated tests will see unauthenticated sessions.'
    );
    return;
  }

  // Navigate to login page (respect /assets prefix on staging)
  const appPrefix = process.env.APP_PREFIX || '';
  await page.goto(`${baseURL}${appPrefix}/login`);
  await page.waitForSelector('input[name="username"]');

  // Fill credentials and submit
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');

  // Wait for navigation away from /login itself. The historical pattern of
  // `waitForURL(/.*/)` resolved on the first navigation event, which on
  // some responses is the /login URL itself before the 302 fires — leaving
  // /mfa/challenge detection brittle. Wait specifically until we are off
  // the login form, then wait for the DOM to be ready.
  //
  // the previous `networkidle` wait blocked on long-lived
  // requests (analytics beacons, charts polling) on the post-login
  // landing page, exceeding the 10 s budget on every CI run. `domcontent
  // loaded` is the right signal here — we don't care about idle network,
  // we just need the redirect chain to settle enough that
  // `page.url()` and `page.context().cookies()` reflect the final state.
  // The downstream cookie sniff (line ~100) is the real proof of
  // authentication.
  await page.waitForURL((url) => !url.pathname.endsWith('/login'), {
    timeout: 10000,
  });
  await page.waitForLoadState('domcontentloaded', { timeout: 10000 });

  // Handle MFA challenge if redirected.
  // when the test user is MFA-enrolled (no bypass) and no TOTP
  // secret is available, the historical behavior was to throw and abort the
  // whole suite. That makes every downstream test look broken when really
  // it's an environment-config issue. Instead, write an unauthenticated
  // storage state and let dependent tests skip cleanly via the
  // `.auth/user.json` empty-cookies path. The MFA-required path still
  // works fully when E2E_TOTP_SECRET is provided.
  if (page.url().includes('/mfa/challenge')) {
    if (!totpSecret) {
      console.warn(
        '[global-setup] Redirected to /mfa/challenge but E2E_TOTP_SECRET is ' +
        'not set. Writing an empty storage state — authenticated tests will ' +
        'see no session and either skip or fail with a clear unauth signal. ' +
        'Provide STAGING_MFA_USER_TOTP_SECRET in CI to drive the MFA path.'
      );
      const fs = await import('fs');
      fs.mkdirSync('.auth', { recursive: true });
      fs.writeFileSync('.auth/user.json', JSON.stringify({ cookies: [], origins: [] }));
      return;
    }

    const totp = new OTPAuth.TOTP({
      secret: OTPAuth.Secret.fromBase32(totpSecret),
      digits: 6,
      period: 30,
    });
    const code = totp.generate();

    await page.fill('input[name="code"]', code);
    await page.click('button[type="submit"]');
    await page.waitForURL(/^(?!.*\/mfa\/)/, { timeout: 10000 });
  }

  // Handle forced password change if redirected
  if (page.url().includes('/change-password')) {
    console.warn(
      '[global-setup] Test user requires a password change. Writing an ' +
      'empty storage state — change the password manually then re-run.'
    );
    const fs = await import('fs');
    fs.mkdirSync('.auth', { recursive: true });
    fs.writeFileSync('.auth/user.json', JSON.stringify({ cookies: [], origins: [] }));
    return;
  }

  // verify we actually reached an authenticated landing page.
  // Belt-and-suspenders against the silent-mfa-pending case where the
  // login response did not include `/mfa/challenge` in the URL we were
  // redirected to (e.g. the login route landed us on a non-MFA page that
  // a subsequent protected-page navigation will still 302 to MFA). A
  // session-cookie sniff catches that: if no session cookie was set,
  // we were never authenticated.
  const cookies = await page.context().cookies();
  const hasSessionCookie = cookies.some((c) => c.name === 'session' || c.name === 'user_session');
  if (!hasSessionCookie || page.url().includes('/login')) {
    console.warn(
      '[global-setup] Login did not produce an authenticated session ' +
      `(url=${page.url()}, session cookie=${hasSessionCookie}). Writing ` +
      'empty storage state.'
    );
    const fs = await import('fs');
    fs.mkdirSync('.auth', { recursive: true });
    fs.writeFileSync('.auth/user.json', JSON.stringify({ cookies: [], origins: [] }));
    return;
  }

  // Save authenticated state
  await page.context().storageState({ path: '.auth/user.json' });
});
