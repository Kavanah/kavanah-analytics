import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

// captures sanitized docs screenshots from the prod demo account.
// One test per manifest entry. Each capture writes TWO artifacts into
// .docs-capture/ (gitignored staging — nothing lands in docs/ from here):
//   <slug>.png   — the screenshot
//   <slug>.json  — sidecar with the page's visible text + capture metadata
// bin/python/tools/publish_doc_captures.py is the only thing that moves
// images into docs/, and only after the sidecars pass the sanitization gate.

// a single `click` only reaches states one interaction deep. The
// User Management modals and every Dashboard Builder panel sit behind two to
// four interactions (open a dashboard, then a modal, then a chart), so an
// entry may instead carry an ordered `steps` list. Each step clicks and then
// waits for the state it just opened, which is what keeps the screenshot off
// a half-rendered panel. `click` stays supported and is exactly `steps` of
// length one.
interface CaptureStep {
  click: string;
  /** Selector that must appear before the next step / the screenshot. */
  wait_for?: string;
  /** Selector that must disappear (a spinner) before continuing. */
  wait_gone?: string;
  /** Fixed pause after this step, for animated panels. */
  settle_ms?: number;
  /** When true, a missing target fails the capture instead of skipping it. */
  required?: boolean;
}

interface CaptureEntry {
  slug: string;
  route: string;
  title: string;
  docs_image: string;
  required_markers: string[];
  settle_ms?: number;
  full_page?: boolean;
  wait_for?: string;
  wait_gone?: string;
  click?: string;
  steps?: CaptureStep[];
}

const manifest = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'capture-manifest.json'), 'utf-8')
) as { captures: CaptureEntry[] };

const OUT_DIR = '.docs-capture';
const enabled = process.env.DOCS_CAPTURE === '1';
test.skip(!enabled, 'docs-capture: set DOCS_CAPTURE=1 to run the  pipeline');

for (const entry of manifest.captures) {
  test(`capture: ${entry.slug}`, async ({ page }) => {
    const prefix = process.env.APP_PREFIX || '';
    await page.goto(prefix + entry.route, { waitUntil: 'domcontentloaded' });
    if (entry.wait_for) {
      await page.waitForSelector(entry.wait_for, { timeout: 15000 });
    }
    if (entry.click) {
      // Open a panel/menu before shooting (e.g. the Dashboards page menu,
      // which is where the workspace context renders on that page).
      await page.click(entry.click, { timeout: 15000 });
    }
    // ordered multi-step walk. A step whose target never appears
    // fails the capture rather than screenshotting whatever is on screen —
    // a silently-skipped click is how you publish a picture of the wrong
    // panel. The publisher then reports this slug as missing (exit 2) and
    // still publishes the clean ones.
    for (const [i, step] of (entry.steps ?? []).entries()) {
      const where = `${entry.slug}: step ${i + 1} (${step.click})`;
      const target = page.locator(step.click).first();
      await target.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {
        throw new Error(
          `${where}: target never became visible. If this state is gated on a ` +
            `role or on tenant data the demo account lacks, drop the entry ` +
            `rather than loosening the gate.`
        );
      });
      await target.click({ timeout: 15000 });
      if (step.wait_for) {
        await page.waitForSelector(step.wait_for, { timeout: 15000 });
      }
      if (step.wait_gone) {
        await page.waitForSelector(step.wait_gone, { state: 'hidden', timeout: 30000 });
      }
      if (step.settle_ms) {
        await page.waitForTimeout(step.settle_ms);
      }
    }
    if (entry.wait_gone) {
      // Wait for a loading indicator to disappear (resolves immediately if
      // it never appeared).
      await page.waitForSelector(entry.wait_gone, { state: 'hidden', timeout: 30000 });
    }
    // Charts/tiles animate in after DOMContentLoaded; a fixed settle beats
    // networkidle here (polling endpoints keep the network busy).
    await page.waitForTimeout(entry.settle_ms ?? 2000);

    // Fail-closed: an unauthenticated or MFA-pending session must never
    // produce a publishable artifact.
    expect(page.url(), `${entry.slug}: bounced to auth — storage state stale?`).not.toMatch(
      /\/(login|mfa)/
    );

    fs.mkdirSync(OUT_DIR, { recursive: true });
    const pageText = await page.evaluate(() => document.body.innerText);
    // some pages (e.g. /dashboard) carry no visible workspace-name
    // text; they expose the resolved workspace in a hidden
    // ``data-docs-workspace`` attribute instead. Capture it separately so the
    // gate can assert the positive demo-workspace marker without the string
    // needing to be visible on screen. The denylist leak-scan still runs on
    // the visible pageText only.
    const anchorText = await page.evaluate(() => {
      const el = document.querySelector('[data-docs-workspace]');
      return el ? el.getAttribute('data-docs-workspace') || '' : '';
    });
    await page.screenshot({
      path: path.join(OUT_DIR, `${entry.slug}.png`),
      fullPage: !!entry.full_page,
    });
    fs.writeFileSync(
      path.join(OUT_DIR, `${entry.slug}.json`),
      JSON.stringify(
        {
          slug: entry.slug,
          route: entry.route,
          final_url: page.url(),
          username: process.env.DEMO_USERNAME || null,
          base_url: process.env.BASE_URL || null,
          page_text: pageText,
          anchor_text: anchorText,
        },
        null,
        2
      )
    );
  });
}
