import { test, expect, skipIfUnauthenticated } from '../../fixtures/auth.fixture';
import { DashboardBuilderPage } from '../../pages/dashboard-builder.page';
import { markReleasePlan } from '../../fixtures/release-plan';

// keep-set rationale
// Catches: dashboard-builder write-path liveness — the page renders, the
// canvas mounts in its empty state, and the Save button is wired into
// the DOM. Pytest covers the API contract and the template render;
// only a real browser proves the JS bundle hydrates the canvas without
// throwing. The "save" button is a load-bearing affordance for every
// builder user — a regression that hides it would be invisible to
// pytest. API mocked so the test does not depend on ES contents.

test.describe('Dashboard builder liveness', () => {
  let builder: DashboardBuilderPage;

  test.beforeEach(async ({ page, context }) => {
    await skipIfUnauthenticated(context);
    builder = new DashboardBuilderPage(page);

    await page.route('**/dashboard-builder/api/dashboards', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ success: true, dashboards: [] }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ success: true, id: 'test-dash-001' }),
        });
      }
    });

    await page.route('**/dashboard-builder/api/data-views', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data_views: [{ id: 'test_fb_insights', title: 'Test FB Insights' }],
        }),
      });
    });

    await builder.goto();
  });

  test('DB-CREATE-001 — canvas mounts in empty state and Save button is present', async ({}, testInfo) => {
    markReleasePlan(testInfo, 'DB-CREATE-001', 'plan-46');

    await expect(builder.canvas).toBeVisible();
    await expect(builder.canvasEmptyState).toBeVisible();
    await expect(builder.saveButton).toBeVisible();
  });
});
