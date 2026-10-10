"""Unit tests for the IG-toggle worklist planner (pure logic only).

The ES sweep + writes are exercised on staging/prod; these tests pin the
enable/disable/skip decision so a refactor can't silently flip a team's
Instagram toggle the wrong way.
"""

from tools.enable_ig_for_wizard_teams import plan_actions


def _rows():
    return [
        {"team_esid": "t_wiz", "team_title": "Wizard Team", "path": "wizard"},
        {"team_esid": "t_leg", "team_title": "Legacy Team", "path": "legacy"},
        {"team_esid": "t_mix", "team_title": "Mixed Team", "path": "mixed"},
        {"team_esid": "t_none", "team_title": "No Pages Team", "path": "no-pages"},
    ]


def _by_team(actions):
    return {a["team_esid"]: a for a in actions}


def test_should_enable_wizard_and_disable_non_wizard_by_default():
    actions = _by_team(plan_actions(_rows(), wizard_only=False, include_mixed=False))
    assert actions["t_wiz"]["action"] == "enable" and actions["t_wiz"]["enabled"] is True
    assert actions["t_leg"]["action"] == "disable" and actions["t_leg"]["enabled"] is False
    assert actions["t_none"]["action"] == "disable" and actions["t_none"]["enabled"] is False


def test_should_skip_mixed_by_default():
    """A partly-migrated team is neither enabled (has legacy pages) nor
    force-disabled — it needs a human decision."""
    actions = _by_team(plan_actions(_rows(), wizard_only=False, include_mixed=False))
    assert actions["t_mix"]["action"] == "skip" and actions["t_mix"]["enabled"] is None


def test_should_enable_mixed_when_include_mixed():
    actions = _by_team(plan_actions(_rows(), wizard_only=False, include_mixed=True))
    assert actions["t_mix"]["action"] == "enable" and actions["t_mix"]["enabled"] is True


def test_wizard_only_never_disables_anyone():
    """--wizard-only enables wizard teams and leaves every other team untouched
    (for when IG is already off broadly and you only want to re-enable wizard)."""
    actions = _by_team(plan_actions(_rows(), wizard_only=True, include_mixed=False))
    assert actions["t_wiz"]["action"] == "enable"
    assert actions["t_leg"]["action"] == "skip"
    assert actions["t_none"]["action"] == "skip"
    assert actions["t_mix"]["action"] == "skip"
    # No 'disable' action exists at all in wizard-only mode.
    assert all(a["action"] != "disable" for a in actions.values())
