"""Audit which teams use the new KA (unified wizard) Meta app, then scope the
Instagram import toggle to only those teams.

Context: the per-team Instagram source toggle
(``de_asset_field_catalog`` docs, ``team__<team_esid>__instagram``) resolves
**team-override-else-ON** — a team with no toggle doc still imports Instagram.
So "enable IG for only the new-KA-app teams" means, when nothing is toggled yet:

  * wizard teams  -> write ``instagram: enabled=true``  (durable ON, not default-ON)
  * non-wizard    -> write ``instagram: enabled=false`` (turn OFF)

"New KA app" = the unified wizard Meta app (``WIZARD_META_APP_ID``). A team is on
it iff its pages carry ``wizard_connection_esid`` — the same classification the
``fb_legacy_team_health`` audit uses (path == "wizard"). This tool reuses that
audit's team-classification so the two never diverge.

DRY-RUN BY DEFAULT. Nothing is written unless ``--apply`` is passed. Read the
printed worklist first.

Usage (inside imports_container)::

    # 1) Audit only — see the plan, write nothing:
    docker exec -w /app/python imports_container python3 \\
        -m tools.enable_ig_for_wizard_teams

    # 2) JSON worklist (pipe-friendly):
    docker exec -w /app/python imports_container python3 \\
        -m tools.enable_ig_for_wizard_teams --json

    # 3) Apply — write the toggle docs (after reviewing the dry run):
    docker exec -w /app/python imports_container python3 \\
        -m tools.enable_ig_for_wizard_teams --apply --username <you>

Flags:
  --apply                 Actually write toggle docs (default: dry run).
  --username NAME         Stamp changed_by/updated_by (required with --apply).
  --wizard-only           Only touch wizard teams (write enabled=true); do NOT
                          turn OFF non-wizard teams. Use this if IG is already
                          off broadly and you just want to (re-)enable wizard.
  --include-mixed         Treat path=="mixed" teams as wizard (default: they are
                          reported but NOT enabled, since some pages are legacy).
  --json                  JSON output instead of the table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Three-candidate resolver — matches the other tools in this directory.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "lib" / "es_helpers.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from tools.fb_legacy_team_health import build_report  # noqa: E402


def plan_actions(rows: list[dict[str, Any]], *, wizard_only: bool, include_mixed: bool) -> list[dict[str, Any]]:
    """Turn audit rows into an IG-toggle worklist.

    Pure function (no ES writes) so it's unit-testable. Each action is
    ``{team_esid, team_title, path, action, enabled}`` where ``action`` is
    ``enable`` / ``disable`` / ``skip`` and ``enabled`` is the value we'd write.
    """
    wizard_paths = {"wizard"} | ({"mixed"} if include_mixed else set())
    actions: list[dict[str, Any]] = []
    for r in rows:
        path = r.get("path")
        base = {"team_esid": r["team_esid"], "team_title": r["team_title"], "path": path}
        if path in wizard_paths:
            actions.append({**base, "action": "enable", "enabled": True})
        elif wizard_only:
            # Only touching wizard teams — leave everyone else alone.
            actions.append({**base, "action": "skip", "enabled": None})
        elif path == "mixed":
            # Not enabling (some legacy pages) and not force-disabling a partly
            # migrated team — surface it for a human decision.
            actions.append({**base, "action": "skip", "enabled": None})
        else:
            # legacy / no-pages -> turn IG OFF (it's on-by-default otherwise).
            actions.append({**base, "action": "disable", "enabled": False})
    return actions


def _apply(actions: list[dict[str, Any]], username: str) -> list[dict[str, Any]]:
    """Write the toggle docs for enable/disable actions. Returns results."""
    from asset_management.web.services.field_catalog_service import (
        FieldCatalogError,
        FieldCatalogService,
    )

    results: list[dict[str, Any]] = []
    for a in actions:
        if a["action"] == "skip":
            results.append({**a, "result": "skipped"})
            continue
        try:
            # allowed_workspaces=None -> superuser/see-all (a server-side script).
            FieldCatalogService.set_source_enabled(
                a["team_esid"], "instagram", a["enabled"], username, allowed_workspaces=None
            )
            results.append({**a, "result": "written"})
        except (FieldCatalogError, ValueError) as e:
            results.append({**a, "result": f"error: {e}"})
    return results


def _print_plan(actions: list[dict[str, Any]], *, applied: bool) -> None:
    verb = "APPLIED" if applied else "DRY RUN (no writes)"
    enable = [a for a in actions if a["action"] == "enable"]
    disable = [a for a in actions if a["action"] == "disable"]
    skip = [a for a in actions if a["action"] == "skip"]
    print(f"IG toggle scope to new-KA-app (wizard) teams — {verb}")
    print(f"  enable (wizard, IG on):   {len(enable)}")
    print(f"  disable (non-wizard, off):{len(disable)}")
    print(f"  skip (mixed/wizard-only): {len(skip)}")
    print()

    headers = ("Action", "Path", "Team", "team_esid", "Result")
    table = [
        (
            a["action"],
            a.get("path") or "",
            (a["team_title"] or "")[:28],
            a["team_esid"],
            a.get("result", ""),
        )
        for a in actions
    ]
    if not table:
        print("(no ds_facebook teams found)")
        return
    widths = [max(len(headers[i]), *(len(row[i]) for row in table)) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in table:
        print(fmt.format(*row))
    print()
    print(
        "enable  = write de_asset_field_catalog team__<esid>__instagram enabled=true\n"
        "disable = write the same doc enabled=false (IG is on-by-default without a doc)\n"
        "skip    = left untouched (mixed path, or --wizard-only mode)\n"
        "Re-run with --apply --username <you> to write."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="enable_ig_for_wizard_teams",
        description="Audit new-KA-app (wizard) teams and scope the Instagram import toggle to them.",
    )
    parser.add_argument("--apply", action="store_true", help="Write toggle docs (default: dry run).")
    parser.add_argument("--username", default="", help="changed_by/updated_by stamp (required with --apply).")
    parser.add_argument("--wizard-only", action="store_true", help="Only enable wizard teams; don't disable others.")
    parser.add_argument("--include-mixed", action="store_true", help="Treat mixed-path teams as wizard.")
    parser.add_argument("--json", action="store_true", help="JSON output.")
    parser.add_argument("--stale-days", type=int, default=3, help="Passed through to the health audit.")
    args = parser.parse_args(argv)

    if args.apply and not args.username.strip():
        print("--apply requires --username <you> (stamped on every toggle doc).", file=sys.stderr)
        return 2

    rows = build_report(stale_days=args.stale_days, real_window_days=14, ads_only=True)
    actions = plan_actions(rows, wizard_only=args.wizard_only, include_mixed=args.include_mixed)

    if args.apply:
        actions = _apply(actions, args.username.strip())

    if args.json:
        print(json.dumps(actions, indent=2, default=str))
    else:
        _print_plan(actions, applied=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
