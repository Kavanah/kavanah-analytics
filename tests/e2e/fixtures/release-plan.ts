import type { TestInfo } from '@playwright/test';

/**
 * Tag a test with the release-plan markers consumed by
 * `tests/e2e/reporters/release-plan-reporter.ts`.
 * Mirrors the pytest pattern `@pytest.mark.test_id(...)` +
 * `@pytest.mark.release_plan(...)` from `tests/conftest.py`.
 *
 * Call from inside a test body:
 *
 *   test('SEC-CSP-005 — no console errors on dashboard load', async ({ page }, testInfo) => {
 * markReleasePlan(testInfo, 'SEC-CSP-005', 'plan-25');
 *     ...
 *   });
 */
export function markReleasePlan(
  testInfo: TestInfo,
  testId: string,
  ...releasePlans: string[]
): void {
  testInfo.annotations.push({ type: 'test_id', description: testId });
  for (const plan of releasePlans) {
    testInfo.annotations.push({ type: 'release_plan', description: plan });
  }
}
