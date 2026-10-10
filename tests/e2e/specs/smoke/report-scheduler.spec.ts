import { test, expect, skipIfUnauthenticated } from '../../fixtures/auth.fixture';
import { appPrefix } from '../../pages/base.page';
import { markReleasePlan } from '../../fixtures/release-plan';

// Report Scheduler rework smoke.
//
// pytest pins the service logic (FREQ-WINDOW-001 frequency→window,
// EMAIL-TYPE-NO-IMPORT-001 email-type dispatches no import) but cannot
// see nginx + the rendered template + the dialog JS. This spec drives the
// deployed page end-to-end to prove the new two-type UI actually mounts:
//   * both list sections render ('Scheduled imports' + 'Scheduled emails');
//   * both create buttons render ('New schedule' + 'New email schedule');
//   * the new Email dialog opens with its email-only fields and none of the
//     import dialog's connector/snapshot plumbing;
//   * the IMPORT dialog no longer carries a Date Range or email block
//.
//
// Skips cleanly when global-setup couldn't authenticate (no E2E creds).

const prefix = appPrefix();

test('SMOKE-EMAIL-SCHED-001 — Report Scheduler shows both schedule types', async ({
  page,
  context,
}, testInfo) => {
  markReleasePlan(testInfo, 'SMOKE-EMAIL-SCHED-001', 'plan-96');
  await skipIfUnauthenticated(context);

  await page.goto(prefix + '/report-scheduler/');
  await expect(page).not.toHaveURL(/\/login/);

  // Both list sections render (renamed "Scheduled reports" →
  // "Scheduled imports"; added "Scheduled emails").
  await expect(page.getByText('Scheduled imports', { exact: true })).toBeVisible();
  await expect(page.getByText('Scheduled emails', { exact: true })).toBeVisible();

  // Both create buttons render.
  await expect(page.locator('#rs-new-btn')).toBeVisible();
  await expect(page.locator('#rs-new-email-btn')).toBeVisible();

  // The Email dialog opens and is purpose-built. It always has the required
  // Recipients field and NONE of the import dialog's connector/snapshot
  // plumbing. The "Dashboards to email" picker is shown ONLY when the user has
  // dashboards; with none #3 blocks creation — the picker is hidden,
  // a "no dashboards" warning shows, and Save is disabled. Assert whichever
  // state applies so the smoke is robust to the runner user's data.
  //
  // made this a MULTI-select checkbox tree (#esm-dashboard-tree),
  // replacing the single #esm-dashboard <select>. Probing the removed id made
  // isVisible() always false, so this took the "no dashboards" branch even
  // when the runner had dashboards and then failed on the correctly-hidden
  // warning. Assert the two states are mutually exclusive rather than trusting
  // one locator's absence to imply the other's presence.
  await page.locator('#rs-new-email-btn').click();
  await expect(page.locator('#esm-overlay')).toBeVisible();
  await expect(page.locator('#esm-recipients')).toBeVisible();
  // No save-snapshot / connector controls in the email dialog.
  await expect(page.locator('#esm-overlay #rsm-save-snapshot')).toHaveCount(0);

  const dashboardTree = page.locator('#esm-dashboard-tree');
  // The picker must exist in the DOM either way — only its visibility varies.
  await expect(dashboardTree).toHaveCount(1);
  if (await dashboardTree.isVisible()) {
    // Has dashboards: the tree is shown and Save is enabled.
    await expect(page.locator('#esm-no-dashboard-warn')).toBeHidden();
    await expect(page.locator('#esm-save')).toBeEnabled();
  } else {
    // No dashboards: #3 blocks creation — warning shown, Save disabled.
    await expect(page.locator('#esm-no-dashboard-warn')).toBeVisible();
    await expect(page.locator('#esm-save')).toBeDisabled();
  }

  await page.locator('#esm-close').click();
  await expect(page.locator('#esm-overlay')).toBeHidden();

  // The IMPORT dialog no longer carries a Date Range select or an email block
  // (moved emailing out of the import schedule).
  await page.locator('#rs-new-btn').click();
  await expect(page.locator('#rsm-overlay')).toBeVisible();
  await expect(page.locator('#rsm-range')).toHaveCount(0);
  await expect(page.locator('#rsm-email-enabled')).toHaveCount(0);
});

// Backfill smoke. pytest pins the cap + scoped dispatch + the
// Meta route-reject, but cannot see the rendered
// card button, the dialog mount, or the Meta button's disabled/tooltip state —
// those live entirely in the template + dialog JS. This drives the deployed
// page to prove the backfill UI actually mounts end-to-end.
//
// Data-tolerant: the runner user may have zero import cards, or only Meta cards,
// or only non-connector schedules. Each branch asserts whatever applies so the
// smoke is robust to the runner user's data (mirrors the email-dialog branch
// above) — but it always proves the backfill dialog itself can mount.
test('SMOKE-BACKFILL-001 — Report Scheduler backfill button + dialog mount', async ({
  page,
  context,
}, testInfo) => {
  markReleasePlan(testInfo, 'SMOKE-BACKFILL-001', 'plan-96');
  await skipIfUnauthenticated(context);

  await page.goto(prefix + '/report-scheduler/');
  await expect(page).not.toHaveURL(/\/login/);

  // Wait for the import-schedule list to settle (it renders async from /api/list).
  await expect(page.locator('#rs-list')).toBeVisible();

  // a Meta (meta_ads) card's Backfill button is now ENABLED (smart
  // backfill: rate-limit bail-out lets it run without an approved FB app). The old
  // "Coming soon" disabled state is gone; a Meta button is disabled ONLY while a
  // run is already in flight. Assert it's not the old permanent-disable tooltip.
  const metaBackfill = page
    .locator('.rs-card', { hasText: 'meta_ads' })
    .locator('button[data-act="backfill"]');
  if (await metaBackfill.count()) {
    await expect(metaBackfill.first()).not.toHaveAttribute('title', /Coming soon/i);
  }

  // an ENABLED Backfill button opens the dialog with a from/to date
  // range (replacing the old 1–365 days field). If the user has no such card, fall
  // through to a capability check that still proves the dialog markup deployed.
  const enabledBackfill = page.locator('button[data-act="backfill"]:not([disabled])');
  if (await enabledBackfill.count()) {
    await enabledBackfill.first().click();
    await expect(page.locator('#rbm-overlay')).toBeVisible();
    await expect(page.locator('#rbm-from')).toBeVisible();
    await expect(page.locator('#rbm-to')).toBeVisible();
    await expect(page.locator('#rbm-from')).toHaveAttribute('type', 'date');
    await expect(page.locator('#rbm-to')).toHaveAttribute('type', 'date');
    // Start + Cancel controls present.
    await expect(page.locator('#rbm-ok')).toBeVisible();
    await page.locator('#rbm-cancel').click();
    await expect(page.locator('#rbm-overlay')).toBeHidden();
  } else {
    // No backfillable card for this user — at minimum prove the backfill dialog
    // markup deployed (the overlay + date range exist in the DOM), so a regression
    // that drops the dialog is still caught.
    await expect(page.locator('#rbm-overlay')).toHaveCount(1);
    await expect(page.locator('#rbm-from')).toHaveCount(1);
    await expect(page.locator('#rbm-to')).toHaveCount(1);
  }
});
