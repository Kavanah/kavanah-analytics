import { test, expect, skipIfUnauthenticated } from '../../fixtures/auth.fixture';
import { appPrefix } from '../../pages/base.page';
import { markReleasePlan } from '../../fixtures/release-plan';

// keep-set rationale
// Catches: -class session-cookie regressions. Pytest exercises
// the logout handler in isolation; only a real browser proves that
// logout actually clears the cookie the server set during login AND
// that the next request to a protected route hits the unauthenticated
// branch and is bounced to /login. The two halves are integration
// points pytest cannot see together.

const prefix = appPrefix();

test('AUTH-SESSION-001 — logout clears the session and a fresh request lands on /login', async ({
  page,
  context,
}, testInfo) => {
  markReleasePlan(testInfo, 'AUTH-SESSION-001', 'plan-46');
  await skipIfUnauthenticated(context);

  // Confirm we start authenticated (auth.fixture loaded the storage state).
  await page.goto(prefix + '/');
  await expect(page).not.toHaveURL(/\/login/);

  await page.goto(prefix + '/logout');
  await page.waitForLoadState('networkidle', { timeout: 15_000 });
  expect(page.url()).toContain('/login');

  // Verify the cookie was actually cleared by hitting a protected route
  // fresh — a still-set cookie would re-authenticate us.
  await page.goto(prefix + '/');
  await page.waitForLoadState('networkidle', { timeout: 15_000 });
  expect(page.url()).toContain('/login');
});
