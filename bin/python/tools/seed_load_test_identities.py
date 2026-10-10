#!/usr/bin/env python3
"""seed_load_test_identities.py — provision the load-test identity pool.

Creates N throwaway ``loadtest-NN@<TEST_PERSONA_DOMAIN>`` accounts with known
passwords and varied scopes, then emits the identity CSV the locustfile's pool
mode reads (``LOAD_TEST_IDENTITY_FILE``, default
``/app/data/load_tests/identities.csv``).

Deliberately a SEPARATE cohort from ``seed_attack_surface_personas``:
those personas all carry a stamped MFA seed, so a scripted locust ``/login``
lands on ``/mfa/challenge`` and never reaches the dashboard. Load-test
identities get NO force_password_change and a stamped ``bypass_mfa: true``
record (the break-glass, audit-visible as "Bypassed" in User
Management) — staging runs ENFORCE_MFA, which would otherwise force-enroll
every MFA-less account into ``/mfa/challenge``. A plain form login must
succeed headless. (Login rate-limit note: web/auth.py locks a username after
5 FAILED attempts per 15 min; successful logins are unlimited. A stale
password in the CSV therefore burns its identity fast — re-run this tool with
``--reset-passwords`` to resync.)

Also provisions the PROBE account ``loadtest-probe@<TEST_PERSONA_DOMAIN>``
(bypass_mfa, CSV role "probe"): ENFORCE_MFA rule 1 hard-blocks ``elastic``
from the web UI entirely, so the locustfile's default identity can never log
in on an enforcing server. The probe is a REGULAR SCOPED USER — workspace-a
workspace_viewer + Team A1 team viewer, NOT superuser: the dashboards list
is workspace-membership-scoped (a membership-less superuser sees an EMPTY
list — verified on staging 2026-07-05), and the baseline should look like a
real person anyway. run-locust injects this row as LOAD_TEST_USERNAME/
PASSWORD when no explicit env override exists; the identity pool EXCLUDES it
(the probe must not double as background load).

Scopes cycle round-robin over the test structures (workspace-a /
Org A / Team A1 — see tests/security/PERSONAS.md "Manual structure setup"),
plus unscoped users, so the pool exercises per-request allowlist resolution
(UserScope) across role shapes, per the acceptance criteria.

SAFETY
    - Dry-run by default. Pass --apply to write.
    - Refuses prod unless BOTH --prod and LOAD_TEST_ALLOW_PROD=1 are set
      (same belt-and-suspenders pattern as the attack-surface seeder).
    - Runs in-container against ES via lib.es_helpers.

USAGE (inside imports_container)
    # staging dry-run (default)
    python3 -m tools.seed_load_test_identities --base-url https://<staging-host>
    # staging apply, 25 identities
    python3 -m tools.seed_load_test_identities --base-url https://<staging-host> --apply
    # resync passwords into a lost/stale CSV
    python3 -m tools.seed_load_test_identities --base-url ... --apply --reset-passwords
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from lib.prod_hosts import is_prod_host
from lib.test_personas import persona_domain

# Reuse the attack-surface seeder's building blocks (tier metadata mirrors
# app.py:_AUTHZ_TIER_BY_PREFIX; password generator meets
# validate_admin_password_complexity with shell-safe specials).
from tools.seed_attack_surface_personas import (  # noqa: F401
    SeedError,
    _TIER,
    _generate_password,
    _get_es,
)

# Prod hostnames: lib.prod_hosts.
_ALLOW_PROD_ENV = "LOAD_TEST_ALLOW_PROD"
DEFAULT_IDENTITY_FILE = "/app/data/load_tests/identities.csv"
DEFAULT_COUNT = 25
# Domain from TEST_PERSONA_DOMAIN — must match what was seeded.
PROBE_USERNAME = f"loadtest-probe@{persona_domain()}"
PROBE_ROLE_LABEL = "probe"


@dataclass(frozen=True)
class ScopeSpec:
    """One entry in the round-robin scope matrix."""

    label: str  # human label written to the CSV "role" column
    tier: str | None  # "workspace" | "org" | "team" | None (unscoped)
    scope_title: str | None  # title/slug resolved against the structures
    role_esid: str | None  # assignment role; tier default when None


# Cycled loadtest-01, -02, ... so a 25-user pool lands ~6 users per shape.
# Titles MUST match the manually created structures.
SCOPE_MATRIX: list[ScopeSpec] = [
    ScopeSpec(label="unscoped", tier=None, scope_title=None, role_esid=None),
    ScopeSpec(label="workspace_viewer", tier="workspace", scope_title="workspace-a", role_esid="workspace_viewer"),
    ScopeSpec(label="org_viewer", tier="org", scope_title="Org A", role_esid="org_viewer"),
    ScopeSpec(label="team_viewer", tier="team", scope_title="Team A1", role_esid="viewer"),
]

# The probe's scopes: workspace + team so both show_all and show_teams
# dashboards are visible to the baseline user.
PROBE_SCOPES: list[ScopeSpec] = [
    ScopeSpec(label="probe_ws", tier="workspace", scope_title="workspace-a", role_esid="workspace_viewer"),
    ScopeSpec(label="probe_team", tier="team", scope_title="Team A1", role_esid="viewer"),
]


def _log() -> logging.Logger:
    return logging.getLogger("seed_load_test_identities")


def _username(index: int) -> str:
    return f"loadtest-{index:02d}@{persona_domain()}"


def _refuse_prod(base_url: str, prod_flag: bool) -> None:
    """Hard-fail on a prod hostname unless --prod AND LOAD_TEST_ALLOW_PROD=1."""
    lower = base_url.lower()
    if not is_prod_host(lower):
        return
    if prod_flag and os.environ.get(_ALLOW_PROD_ENV) == "1":
        _log().warning("PROD opt-in present (--prod + %s=1). Proceeding against %s.", _ALLOW_PROD_ENV, base_url)
        return
    raise SeedError(
        f"Refusing to seed load-test identities against production ({base_url}). "
        f"To do this deliberately, pass --prod AND set {_ALLOW_PROD_ENV}=1."
    )


def _resolve_scope_esid(es, spec: ScopeSpec) -> str:
    """Resolve a scope title to its esid, failing loudly on a missing structure
    (a silently scope-less identity would make the multi-identity run a false
    exercise of the allowlist path)."""
    if spec.tier is None or spec.scope_title is None:
        raise SeedError(f"scope spec {spec.label!r} has no tier/title to resolve")
    tier = _TIER[spec.tier]
    query = (
        {"term": {tier["scope_field"]: spec.scope_title}}
        if tier["scope_query"] == "term"
        else {"match": {tier["scope_field"]: spec.scope_title}}
    )
    hits = es.search(index=tier["scope_index"], body={"query": query, "size": 1}).get("hits", {}).get("hits", [])
    if not hits:
        raise SeedError(
            f"Scope {spec.scope_title!r} not found in {tier['scope_index']}. "
            f"Create the  structures first (tests/security/PERSONAS.md "
            f"'Manual structure setup')."
        )
    return hits[0]["_id"]


def _create_account(es, username: str, *, apply: bool, reset_passwords: bool, roles: list[str] | None = None) -> str | None:
    """Create (or resync) one load-test account; return its known password.

    None means "account exists and --reset-passwords not passed" — the caller
    keeps the password already recorded in the existing CSV.
    """
    from asset_management.web.services.user_provisioning_service import UserProvisioningService

    exists = UserProvisioningService._username_exists(es, username)

    if not exists:
        password = _generate_password()
        if not apply:
            _log().info("[DRY] would CREATE %s roles=%s", username, roles or [])
            return password
        result = UserProvisioningService.create_user(
            username=username,
            full_name=f"Load-test identity ({username.split('@')[0]})",
            # email MUST equal the username: the web login resolves any
            # @-credential via find_user_by_email, so an empty email can never
            # log in through the form (same trap the seeder documents).
            email=username,
            # Pool accounts get NO system role (scoped access comes entirely
            # from the assignment doc); the probe gets superuser.
            roles=roles or [],
        )
        if not result.get("success"):
            raise SeedError(f"create_user failed for {username}: {result.get('error')}")
        set_res = UserProvisioningService.admin_set_password(
            username=username,
            new_password=password,
            set_by="seed_load_test_identities",
            reason="load-test identity pool",
        )
        if not set_res.get("success"):
            raise SeedError(f"admin_set_password failed for {username}: {set_res.get('error')}")
        _log().info("CREATED %s (password set, force-change cleared)", username)
        return password

    if not reset_passwords:
        _log().info("SKIP %s (exists; pass --reset-passwords to resync)", username)
        return None

    password = _generate_password()
    if not apply:
        _log().info("[DRY] would RESET password for existing %s", username)
        return password
    set_res = UserProvisioningService.admin_set_password(
        username=username,
        new_password=password,
        set_by="seed_load_test_identities",
        reason="load-test identity pool password resync",
    )
    if not set_res.get("success"):
        raise SeedError(f"admin_set_password (reset) failed for {username}: {set_res.get('error')}")
    _log().info("RESET password for %s", username)
    return password


def _ensure_roles(es, username: str, roles: list[str], *, apply: bool) -> None:
    """Force an EXISTING account's ES Security roles to exactly ``roles``
    (preserving full_name/email/metadata). Self-healing: an earlier seeder
    version created the probe with superuser; a throwaway account with a
    recorded password must not keep that role."""
    resp = es.security.get_user(username=username)
    user_data = dict(resp).get(username, {})
    if sorted(user_data.get("roles", [])) == sorted(roles):
        return
    if not apply:
        _log().info("[DRY] would SET roles=%s on %s (was %s)", roles, username, user_data.get("roles"))
        return
    es.security.put_user(
        username=username,
        roles=roles,
        full_name=user_data.get("full_name", ""),
        email=user_data.get("email", username),
        enabled=user_data.get("enabled", True),
        metadata=user_data.get("metadata", {}),
    )
    _log().info("SET roles=%s on %s (was %s)", roles, username, user_data.get("roles"))


def _stamp_mfa_bypass(es, username: str, *, apply: bool) -> None:
    """Write a de_asset_user_mfa doc with bypass_mfa=true (break-glass,
    audit fields included; shows as "Bypassed" in User Management).

    Staging/prod run ENFORCE_MFA, which force-enrolls any MFA-less account
    into /mfa/challenge — a headless locust login would dead-end there.
    Full-doc upsert keyed on username is safe because loadtest-* accounts are
    owned by this tool; do NOT reuse this on real users (it would clobber
    their MFA record).
    """
    doc = {
        "username": username,
        "mfa_enabled": False,
        "bypass_mfa": True,
        "bypass_reason": "load-test identity — headless scripted login",
        "bypass_set_by": "seed_load_test_identities",
        "bypass_set_at": datetime.now(timezone.utc).isoformat(),
    }
    if not apply:
        _log().info("[DRY] would STAMP mfa bypass on %s", username)
        return
    es.index(index="de_asset_user_mfa", id=username, document=doc)
    _log().info("STAMPED mfa bypass on %s", username)


def _write_scope_doc(es, username: str, spec: ScopeSpec, *, apply: bool) -> None:
    """Write the single role/assignment doc (mirrors
    app.py:_provision_new_authz_assignments earlier — same shape the
    attack-surface seeder writes)."""
    if spec.tier is None:
        return  # unscoped: no assignment doc
    tier = _TIER[spec.tier]
    scope_esid = _resolve_scope_esid(es, spec)
    role_esid = spec.role_esid or tier["default_role"]
    assignment_doc = {
        "username": username,
        tier["scope_esid_field"]: scope_esid,
        "role_esid": role_esid,
        "is_primary": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    assignment_id = f"{username}:{scope_esid}:{role_esid}"
    if apply:
        es.index(index=tier["assignment_index"], id=assignment_id, document=assignment_doc)
        _log().info("WROTE role doc %s -> %s", username, assignment_id)
    else:
        _log().info("[DRY] would WRITE role doc %s -> %s", username, assignment_id)


def _read_existing_csv(path: str) -> dict[str, str]:
    """username -> password from an existing identities CSV (so skipped
    accounts keep their recorded password across re-runs)."""
    out: dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            username = (row.get("username") or "").strip()
            password = (row.get("password") or "").strip()
            if username and password:
                out[username] = password
    return out


def _write_csv(path: str, rows: list[dict[str, str]], *, apply: bool) -> None:
    if not apply:
        _log().info("[DRY] would WRITE %s (mode 600, %d identities)", path, len(rows))
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["username", "password", "role", "workspace"])
        writer.writeheader()
        writer.writerows(rows)
    os.chmod(path, 0o600)
    _log().info("WROTE %s (mode 600, %d identities)", path, len(rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the load-test identity pool.")
    parser.add_argument("--base-url", required=True, help="Target base URL (drives the prod guard).")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"Identities to provision (default {DEFAULT_COUNT}).")
    parser.add_argument(
        "--out",
        default=os.environ.get("LOAD_TEST_IDENTITY_FILE", DEFAULT_IDENTITY_FILE),
        help=f"Identity CSV path (default $LOAD_TEST_IDENTITY_FILE or {DEFAULT_IDENTITY_FILE}).",
    )
    parser.add_argument("--apply", action="store_true", help="Actually write. Default is a dry run.")
    parser.add_argument(
        "--reset-passwords",
        action="store_true",
        help="Reset passwords of accounts that already exist and rewrite their CSV row (resync).",
    )
    parser.add_argument("--prod", action="store_true", help=f"Permit a prod hostname (requires {_ALLOW_PROD_ENV}=1 too).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = _log()

    if args.count < 1 or args.count > 100:
        log.error("--count must be between 1 and 100 (run-locust caps users at 100).")
        return 2

    try:
        _refuse_prod(args.base_url, args.prod)
    except SeedError as e:
        log.error("%s", e)
        return 2

    if not args.apply:
        log.info("=== DRY RUN (no writes). Re-run with --apply to commit. ===")

    try:
        es = _get_es()
        existing = _read_existing_csv(args.out)
        rows: list[dict[str, str]] = []
        for i in range(1, args.count + 1):
            username = _username(i)
            spec = SCOPE_MATRIX[(i - 1) % len(SCOPE_MATRIX)]
            log.info("--- %s (%s) ---", username, spec.label)
            password = _create_account(es, username, apply=args.apply, reset_passwords=args.reset_passwords)
            if password is None:
                password = existing.get(username, "")
                if not password:
                    log.warning(
                        "%s exists but has no recorded password in %s — its CSV row will be "
                        "unusable. Re-run with --reset-passwords to resync.",
                        username,
                        args.out,
                    )
            _stamp_mfa_bypass(es, username, apply=args.apply)
            _write_scope_doc(es, username, spec, apply=args.apply)
            rows.append(
                {
                    "username": username,
                    "password": password,
                    "role": spec.label,
                    "workspace": spec.scope_title or "",
                }
            )

        # Probe account: regular scoped viewer + bypass, injected by run-locust
        # as LOAD_TEST_USERNAME (elastic is rule-1 blocked under ENFORCE_MFA;
        # a membership-less superuser sees an EMPTY dashboards list). Both a
        # workspace and a team doc, so show_all AND show_teams dashboards are
        # visible. identity_pool excludes role="probe" rows from the round-robin.
        log.info("--- %s (%s) ---", PROBE_USERNAME, PROBE_ROLE_LABEL)
        probe_password = _create_account(es, PROBE_USERNAME, apply=args.apply, reset_passwords=args.reset_passwords)
        if probe_password is None:
            probe_password = existing.get(PROBE_USERNAME, "")
            if not probe_password:
                log.warning(
                    "%s exists but has no recorded password in %s — re-run with --reset-passwords.",
                    PROBE_USERNAME,
                    args.out,
                )
            # Self-heal roles on the existing account (an earlier seeder
            # version created the probe as superuser).
            _ensure_roles(es, PROBE_USERNAME, [], apply=args.apply)
        _stamp_mfa_bypass(es, PROBE_USERNAME, apply=args.apply)
        for probe_scope in PROBE_SCOPES:
            _write_scope_doc(es, PROBE_USERNAME, probe_scope, apply=args.apply)
        rows.append(
            {
                "username": PROBE_USERNAME,
                "password": probe_password,
                "role": PROBE_ROLE_LABEL,
                "workspace": PROBE_SCOPES[0].scope_title or "",
            }
        )
        _write_csv(args.out, rows, apply=args.apply)
    except SeedError as e:
        log.error("ABORTED: %s", e)
        return 1

    log.info("Done.%s", "" if args.apply else "  (dry run — nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
