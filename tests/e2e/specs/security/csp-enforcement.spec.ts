import { test, expect, skipIfUnauthenticated } from '../../fixtures/auth.fixture';
import { appPrefix } from '../../pages/base.page';
import { markReleasePlan } from '../../fixtures/release-plan';

// keep-set rationale
// Catches: -class real-browser CSP regressions. The pytest tests
// in `tests/flask/test_security.py` pin the CSP header string but
// cannot see whether the rendered page's inline scripts, CDN fetches,
// or Leaflet sourcemap requests actually get past the policy in a
// real browser. The dashboard-builder page is the right surface: it
// eagerly pulls the Leaflet bundle and inline scripts on first render,
// so any tightened directive trips here first.

const prefix = appPrefix();

interface ConsoleViolation {
  text: string;
  location: string;
}

function attachConsoleViolationListener(
  page: import('@playwright/test').Page
): ConsoleViolation[] {
  const violations: ConsoleViolation[] = [];
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    if (/Refused to|violat(ed|es) the following Content Security Policy/i.test(text)) {
      violations.push({ text, location: msg.location().url || '' });
    }
  });
  return violations;
}

test('SEC-CSP-001 — dashboard builder loads with zero CSP violations', async ({
  page,
  context,
}, testInfo) => {
  markReleasePlan(testInfo, 'SEC-CSP-001', 'plan-46');
  await skipIfUnauthenticated(context);

  const violations = attachConsoleViolationListener(page);

  await page.goto(prefix + '/dashboard-builder/');
  await page.waitForLoadState('networkidle', { timeout: 20_000 });

  expect(
    violations,
    `Unexpected CSP violations on dashboard builder:\n${violations
      .map((v) => `  ${v.text} (${v.location})`)
      .join('\n')}`
  ).toEqual([]);
});
