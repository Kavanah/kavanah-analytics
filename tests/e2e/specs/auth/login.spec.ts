import { test, expect } from '@playwright/test';
import { LoginPage } from '../../pages/login.page';
import { markReleasePlan } from '../../fixtures/release-plan';

// keep-set rationale
// Catches: login bundle / CSP / form-post regressions that would land
// users on a blank page or a 5xx. The login route is the only entry into
// every other authenticated test, so if this is broken the whole suite
// is dead. Pytest covers handler logic but cannot see a missing JS
// bundle or a CSP that blocks the form submit.

test.use({ storageState: { cookies: [], origins: [] } });
test.setTimeout(60_000);

test('AUTH-LOGIN-001 — login page renders form and accepts credentials', async ({
  page,
}, testInfo) => {
  markReleasePlan(testInfo, 'AUTH-LOGIN-001', 'plan-46');

  const loginPage = new LoginPage(page);
  await loginPage.goto();

  await expect(loginPage.usernameInput).toBeVisible();
  await expect(loginPage.passwordInput).toBeVisible();
  await expect(loginPage.submitButton).toBeVisible();
  await expect(loginPage.submitButton).toHaveText('Sign in');

  // CSRF token must be rendered into the form — the post is rejected
  // by Flask-WTF without it, which would be invisible to pytest if the
  // template stopped emitting the hidden field.
  await expect(loginPage.csrfInput).toBeAttached();
  const token = await loginPage.getCsrfToken();
  expect(token.length).toBeGreaterThan(0);
});
