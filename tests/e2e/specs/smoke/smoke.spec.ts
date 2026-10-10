import { test, expect, skipIfUnauthenticated } from '../../fixtures/auth.fixture';
import { appPrefix } from '../../pages/base.page';
import { markReleasePlan } from '../../fixtures/release-plan';

// keep-set rationale
// Catches: -class nginx-routing regressions where a protected
// page returns 404 from the wrong location block, or where the
// PrefixMiddleware is misconfigured so the page URL exists but the
// JS bundle resolves to a 404. Pytest cannot see nginx; this is the
// integration check that proves the deployed app responds end-to-end
// for the three core authenticated surfaces.

const prefix = appPrefix();

test('SMOKE-EXPLORER-001 — Asset Explorer loads when authenticated', async ({
  page,
  context,
}, testInfo) => {
  markReleasePlan(testInfo, 'SMOKE-EXPLORER-001', 'plan-46');
  await skipIfUnauthenticated(context);

  await page.goto(prefix + '/');
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('body')).toBeVisible();
});

test('SMOKE-BUILDER-001 — Dashboard Builder canvas mounts when authenticated', async ({
  page,
  context,
}, testInfo) => {
  markReleasePlan(testInfo, 'SMOKE-BUILDER-001', 'plan-46');
  await skipIfUnauthenticated(context);

  await page.goto(prefix + '/dashboard-builder/');
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#db-canvas')).toBeVisible();
});

test('SMOKE-IMPORT-001 — Import Data status section mounts when authenticated', async ({
  page,
  context,
}, testInfo) => {
  markReleasePlan(testInfo, 'SMOKE-IMPORT-001', 'plan-46');
  await skipIfUnauthenticated(context);

  await page.goto(prefix + '/import-data');
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#section-status')).toBeVisible();
});
