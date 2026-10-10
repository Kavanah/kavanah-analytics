import { test, expect, skipIfUnauthenticated } from '../../fixtures/auth.fixture';
import { ImportDataPage } from '../../pages/import-data.page';
import { markReleasePlan } from '../../fixtures/release-plan';

// keep-set rationale
// Catches: import-data read-path liveness — the page renders and the
// status section mounts. Pytest covers the route, the data-source
// status endpoint contract, and the date-range guards; only
// a real browser proves the JS bundle hydrates and the status panel
// is reachable end-to-end. Existing read endpoints stubbed so the
// test does not depend on a configured FB import on the test server.

test.describe('Import data liveness', () => {
  let importPage: ImportDataPage;

  test.beforeEach(async ({ page, context }) => {
    await skipIfUnauthenticated(context);
    importPage = new ImportDataPage(page);

    await page.route('**/api/import/data-source-status*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          document_count: 0,
          last_pulled: null,
          monthly_summary: [],
        }),
      });
    });

    await page.route('**/api/import/data-view-teams*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, teams_with_data: [] }),
      });
    });

    await page.route('**/dashboard-builder/api/data-views', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          data_views: [{ id: 'test_fb_insights', label: 'Test FB Insights' }],
        }),
      });
    });

    await importPage.goto();
  });

  test('IMPORT-LIVENESS-001 — status section mounts', async ({}, testInfo) => {
    markReleasePlan(testInfo, 'IMPORT-LIVENESS-001', 'plan-46');

    await expect(importPage.statusSection).toBeVisible();
  });
});
