import { defineConfig } from '@playwright/test';

// The DEAM app runs under /assets/ prefix on staging (behind nginx).
// Set APP_PREFIX=/assets when testing against staging servers.
// Locally (without nginx), leave it empty.
export default defineConfig({
  testDir: './tests/e2e/specs',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [
    // 'list' streams per-test progress to stdout — without it, CI logs
    // are silent for the entire run, leaving exit-1 with no diagnostic.
    ['list'],
    ['html', { outputFolder: 'playwright-report' }],
    // emits release-plan JSON when RELEASE_PLAN_REPORT is set.
    // No-op when the env var is unset, so it's safe to enable globally.
    ['./tests/e2e/reporters/release-plan-reporter.ts'],
  ],
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:8084',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'setup',
      testMatch: /global-setup\.ts/,
      testDir: './tests/e2e',
    },
    {
      name: 'chromium',
      use: {
        browserName: 'chromium',
        storageState: '.auth/user.json',
      },
      dependencies: ['setup'],
    },
    // docs-screenshot pipeline. Both projects are statically
    // skipped unless DOCS_CAPTURE=1, so a plain `npx playwright test`
    // (dev/CI e2e runs) never touches them or the prod demo account.
    {
      name: 'demo-setup',
      testDir: './tests/e2e/docs-capture',
      testMatch: /demo-auth\.setup\.ts/,
    },
    {
      name: 'docs-capture',
      testDir: './tests/e2e/docs-capture',
      testMatch: /capture\.spec\.ts/,
      use: {
        browserName: 'chromium',
        storageState: '.auth/demo-user.json',
        viewport: { width: 1440, height: 900 },
        deviceScaleFactor: 2,
      },
      dependencies: ['demo-setup'],
    },
  ],
});
