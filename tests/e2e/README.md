# End-to-end browser tests

This directory holds two distinct test layers. Both are E2E in the sense
that they drive a real browser, but they live in different test runners
and have different scopes.

## 1. Playwright suite — `tests/e2e/specs/*.spec.ts`

**Scope (earlier rationalization):** a skinny smoke set of nine
tests across seven files. Each test pins a regression class no other
layer can see — login-bundle wiring, MFA challenge render, session
cookie invalidation, nginx routing to the three protected pages,
real-browser CSP enforcement, and write-/read-path liveness on
Dashboard Builder + Import Data.

**See `the private docs` (2026-05-12 entry)** for what is
explicitly NOT covered here and why. Before adding a new Playwright
test, read that entry. The ≤10-test cap is intentional.

### Adding a new Playwright test

1. Pick or create a `test_id` of the form `KAN-NNN-<AREA>-NNN` and add
   a row to the open release-plan CSV
   (`the private docs` — the only file without
   a `-done` suffix).
2. Inside the test body, call:
   ```ts
   markReleasePlan(testInfo, 'KAN-NNN-AREA-NNN', 'PR-N');
   ```
3. Add a top-of-file `keep-set rationale` block (or similar)
   explaining what regression class only a real browser can catch
   here. If pytest could cover it, write a pytest test instead.

### Running locally

```bash
npm ci
npx playwright install --with-deps chromium
npx playwright test
```

For staging:

```bash
BASE_URL=https://<staging-host> \
APP_PREFIX=/assets \
E2E_USERNAME=zap-scanner \
E2E_PASSWORD=... \
npx playwright test
```

If a dedicated MFA-enrolled test user has been provisioned, set
`E2E_TOTP_SECRET` to its base32 TOTP secret and `global-setup.ts`
will drive the MFA challenge for you.

### CI wiring

`.github/workflows/e2e.yml` runs the suite on PRs to `main` and via
`workflow_dispatch`. It uses the `zap-scanner` staging user
(MFA-bypassed). `AUTH-MFA-001` skips cleanly in that mode.

## 2. Pytest suite — `tests/e2e/*.py`

Opt-in pytest layer for behaviors that source-pin / unit tests cannot
catch — e.g. modal interactions, JS errors that only fire in a real
DOM, CSS class wiring that affects rendered visibility.

Tests in this directory are tagged with the `e2e` marker. The default
`pytest` run excludes them (`addopts = -m "not e2e"` in `pytest.ini`),
so the pre-push test gate stays fast.

### When to use these

- Behavior that lives entirely in DOM events / CSS classes / live
  network calls: dialog open/close, button visibility tied to async
  API responses, the publish-modal auto-apply flow.
- Regressions that bypass source-pin tests because the bug is
  rendering, not source-text.

### When NOT to use these

- Anything a server-side unit test can prove. E2E is slow — reach
  for it last.
- API-shape pinning (use `pytest-flask` `auth_client`).
- Source-text pinning (use repo-grep patterns under
  `the release-plan tests`).

### Setup

```bash
pip install -r requirements-e2e.txt
playwright install chromium
```

### Running

```bash
pytest -m e2e
pytest tests/e2e/test_publish_dialog_e2e.py::test_publish_modal_opens_on_button_click
pytest                # default: excludes e2e
```

This layer is **not** wired into CI; the pre-push gate runs the
default suite and the test-server runner has no chromium binary.
See `the private docs` for the opt-in-vs-default-on rationale.
