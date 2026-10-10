"""E2E demo test for the publish dialog.

Pins behaviors that source-pin tests in
`the release-plan tests` cannot catch:

  - The Publish button actually appears in the rendered DOM (not just
    the source) when the user has the capability and the dashboard is
    eligible.
  - Clicking it actually opens the modal (no JS errors block the
    handler).
  - The modal contains the expiry input + never-expires checkbox.

These are stub-quality. A future expansion would also test the
auto-apply debounce, scope-warning yellow state, and the published
pill toggling — but those need an authenticated session that this
scaffold does not yet provide (see `conftest.py` TODO).

The test currently asserts only that the dashboards page loads.
Anything authenticated lives behind the SSO flow and needs a real
session — flagged as expected-skip until KAN-FOLLOWUP wires it up.
"""

import pytest


@pytest.mark.e2e
def test_dashboards_page_loads_without_js_errors(authed_page, base_url):
    """The dashboards page renders to the redirect-to-login point with no
    JavaScript errors. Cheapest possible smoke test — proves Playwright
    + the live server are talking to each other and our JS additions for
     do not blow up at parse time."""
    js_errors: list[str] = []
    authed_page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    authed_page.goto(base_url + "/assets/dashboards", wait_until="domcontentloaded")
    # Whether we land on the login page or the dashboards page, our new
    # JS (publish helpers, badge logic) must parse cleanly.
    assert not js_errors, f"JavaScript errors during page load: {js_errors}"


@pytest.mark.e2e
@pytest.mark.skip(reason="needs an authenticated session — KAN-FOLLOWUP wires this up")
def test_publish_modal_opens_on_button_click(authed_page, base_url):
    """When an Impact Report dashboard is shown and the user has
    publish_reports, clicking #publish-btn must open the publish modal
    and render the expiry input."""
    authed_page.goto(base_url + "/assets/dashboards", wait_until="networkidle")
    # Publish lives inside the "..." toolbar overflow menu now —
    # open the menu first, then click the item.
    authed_page.locator("#dashboard-overflow-btn").click()
    publish_btn = authed_page.locator("#publish-btn")
    publish_btn.wait_for(state="visible")
    publish_btn.click()
    overlay = authed_page.locator("#publish-modal-overlay")
    assert "hidden" not in (overlay.get_attribute("class") or "")
    assert authed_page.locator("#publish-expires-days-input").is_visible()
    assert authed_page.locator("#publish-never-expires-cb").is_visible()
