import { test as base, type BrowserContext } from '@playwright/test';

// Authenticated test fixture.
// Uses the stored auth state from global-setup so each test starts with
// an already-authenticated session.
//
// also exports `skipIfUnauthenticated` so tests can bail out
// cleanly when global-setup wrote an empty storage state (e.g. MFA
// enrolled test user with no TOTP secret). Without this, every
// protected-page test fails with a confusing "element not found" that
// hides the real cause.
export const test = base.extend({});

export async function skipIfUnauthenticated(
  context: BrowserContext
): Promise<void> {
  const cookies = await context.cookies();
  if (cookies.length === 0) {
    test.skip(
      true,
      'No session cookies in storage state — global-setup could not ' +
        'authenticate (likely MFA-enrolled test user without ' +
        'E2E_TOTP_SECRET). See global-setup.ts logs.'
    );
  }
}

export { expect } from '@playwright/test';
