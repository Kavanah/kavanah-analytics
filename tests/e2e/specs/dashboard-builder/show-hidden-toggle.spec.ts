import { test, expect, skipIfUnauthenticated } from '../../fixtures/auth.fixture';
import { DashboardBuilderPage } from '../../pages/dashboard-builder.page';
import { markReleasePlan } from '../../fixtures/release-plan';

// "Show hidden dashboards" per-user toggle.
//
// The Builder explorer renders every dashboard, marking ones with
// visibility_mode == 'hide' with a strikethrough label. This toggle
// inverts the default: hidden dashboards are filtered OUT of the
// explorer unless the box is ticked, and the choice persists per-user
// in localStorage (db-show-hidden-dashboards).
//
// Only a real browser proves the localStorage round-trip and the
// DOM-level filter/re-render — there is no Python surface to cover with
// pytest, so this lives as a Playwright e2e spec.

const VISIBLE_TITLE = 'Visible Dashboard';
const HIDDEN_TITLE = 'Hidden Dashboard';
const MENU = 'Reports';

const MOCK_DASHBOARDS = [
  {
    id: 'dash-visible-001',
    title: VISIBLE_TITLE,
    menu: MENU,
    menu_position: 0,
    visibility_mode: 'show_all',
  },
  {
    id: 'dash-hidden-001',
    title: HIDDEN_TITLE,
    menu: MENU,
    menu_position: 1,
    visibility_mode: 'hide',
  },
];

const LS_KEY = 'db-show-hidden-dashboards';

test.describe('Dashboard builder — Show hidden dashboards toggle', () => {
  let builder: DashboardBuilderPage;

  test.beforeEach(async ({ page, context }) => {
    await skipIfUnauthenticated(context);
    builder = new DashboardBuilderPage(page);

    // Deterministic explorer contents: one visible + one hidden dashboard.
    await page.route('**/dashboard-builder/api/dashboards', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ success: true, dashboards: MOCK_DASHBOARDS }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ success: true, id: 'dash-new-001' }),
        });
      }
    });

    // The explorer reads workspace menu order before rendering; pin it so
    // both dashboards land under a known "Reports" group.
    await page.route('**/dashboard-builder/api/menus', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          menus: [{ esid: 'menu-reports-001', title: MENU }],
        }),
      });
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
  });

  test('DB-SHOWHIDDEN-001 — hidden dashboards filtered by default, revealed + persisted when toggled', async ({ page }, testInfo) => {
    markReleasePlan(testInfo, 'DB-SHOWHIDDEN-001', 'plan-69');

    await builder.goto();

    // The checkbox renders below Templates, unchecked by default.
    await expect(builder.showHiddenCheckbox).toBeVisible();
    await expect(builder.showHiddenCheckbox).not.toBeChecked();

    // Unchecked: the visible dashboard is in the explorer; the hidden one
    // is filtered out entirely (not merely collapsed — absent from the DOM).
    // toBeAttached, not toBeVisible: groups render collapsed by default, so
    // we assert DOM presence rather than CSS visibility.
    await expect(builder.explorerItem(VISIBLE_TITLE)).toBeAttached();
    await expect(builder.explorerItem(HIDDEN_TITLE)).toHaveCount(0);

    // Tick the box → the hidden dashboard appears, with the strikethrough
    // (is-hidden) modifier on its label.
    await builder.showHiddenCheckbox.check();
    await expect(builder.explorerItem(HIDDEN_TITLE)).toBeAttached();
    await expect(builder.explorerItem(HIDDEN_TITLE)).toHaveClass(/is-hidden/);
    // The visible dashboard is still present.
    await expect(builder.explorerItem(VISIBLE_TITLE)).toBeAttached();

    // The choice is persisted to localStorage.
    const stored = await page.evaluate((key) => localStorage.getItem(key), LS_KEY);
    expect(stored).toBe('1');

    // Reload: the checkbox stays checked and the hidden dashboard still shows.
    await builder.goto();
    await expect(builder.showHiddenCheckbox).toBeChecked();
    await expect(builder.explorerItem(HIDDEN_TITLE)).toBeAttached();
    await expect(builder.explorerItem(HIDDEN_TITLE)).toHaveClass(/is-hidden/);
  });
});
