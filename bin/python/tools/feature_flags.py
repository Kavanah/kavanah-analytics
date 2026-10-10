"""Admin CLI for the feature-flag system.

The flag registry lives in ``asset_management.web.services.feature_flag_service``;
this tool is the minimum-viable admin toggle for the onboarding
rollout. No web UI yet (deferred — 'admin toggle UI or CLI').

Usage::

    # List every registered flag with its current state.
    python -m bin.python.tools.feature_flags list

    # Show a single flag's stored doc.
    python -m bin.python.tools.feature_flags show ORG_BILLING_PER_TEAM

    # Flip a flag off / on for everyone.
    python -m bin.python.tools.feature_flags set ORG_BILLING_PER_TEAM off
    python -m bin.python.tools.feature_flags set ORG_BILLING_PER_TEAM on

    # Enable for a deterministic percent of users (A/B cohort assignment
    # — same user lands in the same bucket per flag across requests).
    python -m bin.python.tools.feature_flags set ORG_BILLING_PER_TEAM cohort --percent 50

    # Evaluate the flag for a specific user (sanity check before a
    # cohort flip). Prints "enabled" or "disabled" + the bucket value.
    python -m bin.python.tools.feature_flags eval ORG_BILLING_PER_TEAM --user alice
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package. Locally
# this lives at ``<repo>/bin/python/asset_management``; inside the
# ``imports_container`` it lives at ``/app/python/asset_management``. Walk
# the file's parents to find either layout so the CLI works from both.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],  # repo-local: bin/python
    _HERE.parents[2] / "bin" / "python",  # repo root + bin/python
    Path("/app/python"),  # container mirror layout
):
    if (_candidate / "asset_management" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from asset_management.web.services.feature_flag_service import (  # noqa: E402
    FeatureFlagService,
    _REGISTRY,
    _cohort_bucket,
)


def _cmd_list(_args: argparse.Namespace) -> int:
    stored = {f["flag"]: f for f in FeatureFlagService.list_flags() if f.get("flag")}
    rows = []
    for flag, defaults in _REGISTRY.items():
        doc = stored.get(flag) or {}
        rows.append(
            {
                "flag": flag,
                "mode": doc.get("mode", defaults["mode"]),
                "rollout_percent": doc.get("rollout_percent", defaults.get("rollout_percent", 0)),
                "updated_by": doc.get("updated_by", "—"),
                "updated_at": doc.get("updated_at", "—"),
                "stored": flag in stored,
            }
        )
    print(json.dumps(rows, indent=2, default=str))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    doc = FeatureFlagService.get_flag(args.flag)
    if not doc:
        print(
            f"unknown flag {args.flag!r} (registered flags: {sorted(_REGISTRY)})",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(doc, indent=2, default=str))
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    result = FeatureFlagService.set_flag(
        args.flag,
        args.mode,
        rollout_percent=args.percent,
        updated_by=args.actor,
    )
    if not result.get("success"):
        print(f"error: {result.get('error')}", file=sys.stderr)
        return 1
    print(json.dumps(result["flag"], indent=2, default=str))
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    enabled = FeatureFlagService.is_enabled(args.flag, args.user)
    bucket = _cohort_bucket(args.flag, args.user) if args.user else None
    print(
        json.dumps(
            {
                "flag": args.flag,
                "user": args.user,
                "enabled": enabled,
                "bucket": bucket,
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feature_flags",
        description="Admin CLI for the  feature-flag system.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List every registered flag.").set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="Show a single flag's stored doc.")
    p_show.add_argument("flag")
    p_show.set_defaults(func=_cmd_show)

    p_set = sub.add_parser("set", help="Set a flag's mode (and rollout percent).")
    p_set.add_argument("flag")
    p_set.add_argument("mode", choices=("off", "on", "cohort"))
    p_set.add_argument(
        "--percent",
        type=int,
        default=None,
        help="Rollout percent (0–100). Required when mode='cohort'.",
    )
    p_set.add_argument(
        "--actor",
        default="cli",
        help="Operator name for the updated_by audit field.",
    )
    p_set.set_defaults(func=_cmd_set)

    p_eval = sub.add_parser("eval", help="Evaluate the flag for a specific user id.")
    p_eval.add_argument("flag")
    p_eval.add_argument("--user", required=True, help="User id to evaluate against.")
    p_eval.set_defaults(func=_cmd_eval)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
