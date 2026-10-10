"""Scheduled read-only credential validity sweep.

Answers "is every stored Meta credential still usable?" **without waiting for an
import to fail**. Prod recon on 2026-08-27 found the gap this closes: nothing in
the system watches ``data_access_expires_at``, and a batch of wizard connections
loses data access around 2026-11-02 with no signal at all.

What it walks
-------------
Both credential families, because both matter and neither alone is enough:

* ``de_asset_fb_app_users`` — the legacy chain. **All 38 ``ds_facebook`` teams
  import through it.** Every token there is a non-expiring SYSTEM_USER token.
* ``de_connection`` (provider=facebook) — the wizard cohort. Drives no team
  imports today, but carries real 67–88 day data-access clocks.

Heartbeat canary
----------------
CLAUDE.md #24: a background worker added without a canary is a silent-failure
surface waiting to happen — the KM dashboard lost its uptime monitor for 23 days
exactly this way, and its rollup for four months. So this job stamps
``de_credential_health`` with a ``__heartbeat__`` row on every run, and
``--check-heartbeat`` fails loudly when that row is older than the sweep's
cadence. Wire the check into whatever surface an operator actually looks at; a
worker that can stop without anyone noticing is worse than no worker, because it
reads as "all clear".

Per-asset probes
---------------------------
Each credential is probed against the assets it is expected to reach — see
``credential_asset_scope`` for the two joins. A legacy app-user token gets the
pages / IG accounts (``Pages`` role) or the ad accounts pinned to it (``Ads``
role) across the teams whose page chain reaches its app; a
wizard connection gets its ``de_connection_asset`` rows, its teams' configured
assets, and every IG doc stamped with its ``wizard_connection_esid``. The
resulting per-asset verdicts are what IG importability reads.

Read-only against Meta and against every source index — the only writes are to
``de_credential_health``.

Usage::

    python3 /app/python/asset_management/jobs/probe_credential_health.py
    python3 .../probe_credential_health.py --dry-run       # probe, print, persist nothing
    python3 .../probe_credential_health.py --check-heartbeat --max-age-hours 30
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/python")

import lib.logging  # noqa: E402,F401 — configures the shared log format

from asset_management.jobs.credential_asset_scope import AssetScope  # noqa: E402
from lib import credential_health as ch  # noqa: E402
from lib.credential_prober import probe_assets, probe_token  # noqa: E402
from lib.encryption import decrypt  # noqa: E402
from lib.es_helpers import (  # noqa: E402
    get_document_by_id,
    get_elasticsearch_client,
)

HEARTBEAT_ID = "__heartbeat__"
# the sweep can now be started between crons by a re-auth
# (credential_health_trigger). Two sweeps interleaving record_probe writes from
# different snapshots would leave rows briefly inconsistent, so a second
# instance must yield. /app/data is the writable bind mount shared by every
# job; the lock is advisory and released when the process exits.
LOCK_PATH = "/app/data/credential_health_probe.lock"
# The sweep runs every 6h; 30h is five missed runs. Wide enough that one blip or
# a slow night never pages anyone, tight enough that a genuinely stopped worker
# surfaces within a day and a bit.
DEFAULT_MAX_AGE_HOURS = 30


def _now():
    return datetime.now(timezone.utc)


def _safe_decrypt(value: str, *, what: str) -> str:
    """Decrypt, or return "" and log which row failed.

    ``lib.encryption.decrypt`` raises ``InvalidToken`` on a value it cannot
    decrypt — a row encrypted under a rotated key, a truncated write, hand-seeded
    test data. Found on dev 2026-08-27: a single such ``de_connection`` row
    aborted the entire sweep with a traceback, so one bad row would have taken
    down health monitoring for every good one.

    A sweep must survive the rows it is there to inspect (CLAUDE.md #45 — the
    dangerous failures are the ones the loop is designed to survive).
    """
    if not value:
        return ""
    try:
        return decrypt(value) or ""
    except Exception as exc:  # noqa: BLE001 — any decrypt failure is a skip, not a crash
        logging.warning(
            "probe_credential_health: cannot decrypt %s (%s) — skipping this credential",
            what,
            type(exc).__name__,
        )
        return ""


def _stamp_heartbeat(es, *, checked: int, unhealthy: int) -> None:
    doc = {
        "source_index": "__meta__",
        "source_esid": HEARTBEAT_ID,
        "provider": "__meta__",
        "status": "healthy",
        "label": "credential health sweep heartbeat",
        "reason": f"last sweep checked {checked} credential(s), {unhealthy} unhealthy",
        "last_probed_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
    }
    try:
        es.index(index=ch.INDEX, id=HEARTBEAT_ID, body=doc, refresh="wait_for")
    except Exception as exc:  # noqa: BLE001
        logging.error("probe_credential_health: heartbeat stamp failed: %s", exc)


def check_heartbeat(max_age_hours: int) -> int:
    """Exit 0 when the sweep ran recently, 1 when it did not.

    Reports the actual age and the last-seen timestamp — "stale" without saying
    *how* stale leaves an operator unable to tell "stopped an hour ago" from
    "never started", which are very different incidents.
    """
    doc = get_document_by_id(ch.INDEX, HEARTBEAT_ID)
    if not doc:
        print("CRITICAL: credential-health sweep has NO heartbeat row — " "the job has never completed a run on this cluster.")
        return 1
    raw = doc.get("last_probed_at") or ""
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"CRITICAL: heartbeat has unparseable last_probed_at={raw!r}")
        return 1

    age = _now() - last
    if age > timedelta(hours=max_age_hours):
        print(
            f"CRITICAL: credential-health sweep is STALE — last run "
            f"{age.total_seconds() / 3600:.1f}h ago at {last.isoformat()} "
            f"(threshold {max_age_hours}h). {doc.get('reason') or ''}"
        )
        return 1
    print(
        f"OK: credential-health sweep ran {age.total_seconds() / 3600:.1f}h ago "
        f"at {last.isoformat()}. {doc.get('reason') or ''}"
    )
    return 0


def _fb_app_user_credentials(es) -> list[dict]:
    """Legacy chain: de_asset_fb_apps -> de_asset_fb_app_users."""
    out: list[dict] = []
    try:
        apps = es.search(index="de_asset_fb_apps", body={"size": 500, "query": {"match_all": {}}})
    except Exception as exc:  # noqa: BLE001
        logging.error("probe_credential_health: cannot read de_asset_fb_apps: %s", exc)
        return out
    for app_hit in apps.get("hits", {}).get("hits", []):
        app = app_hit["_source"]
        app_id = app.get("fb_app_id") or ""
        app_secret = _safe_decrypt(app.get("fb_app_secret") or "", what=f"fb_app_secret on {app_hit['_id']}")
        if not app_id or not app_secret:
            continue
        try:
            users = es.search(
                index="de_asset_fb_app_users",
                body={"size": 200, "query": {"term": {"fb_app_esid": app_hit["_id"]}}},
            )
        except Exception as exc:  # noqa: BLE001
            logging.error("probe_credential_health: cannot read app_users: %s", exc)
            continue
        for u in users.get("hits", {}).get("hits", []):
            src = u["_source"]
            token = _safe_decrypt(src.get("fb_app_user_token") or "", what=f"fb_app_user_token on {u['_id']}")
            if not token:
                continue
            out.append(
                {
                    "source_index": "de_asset_fb_app_users",
                    "source_esid": u["_id"],
                    "provider": "facebook",
                    "label": f"{app.get('fb_app_title') or app_id} — {src.get('fb_app_user_title') or u['_id']}",
                    "workspace_esid": app.get("workspace_esid") or "",
                    "principal": "",
                    "token": token,
                    "app_id": app_id,
                    "app_secret": app_secret,
                    # the asset join keys on the app, not the user:
                    # every user token under an app is tried against the same teams.
                    "fb_app_esid": app_hit["_id"],
                    "fb_app_user_role": src.get("fb_app_user_role") or "",
                }
            )
    return out


def _connection_workspaces(es) -> dict[str, str]:
    """connection_esid -> workspace_esid, resolved via de_asset_teams.

    Verified against the live mappings 2026-08-27: ``de_connection`` has NO
    ``workspace_esid`` field, so it cannot be read off the connection row. Teams
    carry both ``connection_esid`` (the populated link, 13/35 ds_facebook teams)
    and ``workspace_esid`` (35/35), so the team table is the only place the join
    exists.

    Resolved once per sweep rather than per row, and stamped onto the health doc,
    because the banner reads it inside a Flask context processor on every page
    render and cannot afford this join per request.
    """
    out: dict[str, str] = {}
    try:
        res = es.search(
            index="de_asset_teams",
            body={
                "size": 2000,
                "query": {"exists": {"field": "connection_esid"}},
                "_source": ["connection_esid", "workspace_esid"],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("probe_credential_health: connection->workspace join failed: %s", exc)
        return out
    for h in res.get("hits", {}).get("hits", []):
        src = h["_source"]
        cid, ws = src.get("connection_esid"), src.get("workspace_esid")
        if cid and ws:
            out[cid] = ws
    return out


def _connection_credentials(es) -> list[dict]:
    """Wizard cohort: de_connection rows with provider=facebook."""
    out: list[dict] = []
    ws_by_connection = _connection_workspaces(es)
    try:
        from asset_management.web.services.wizard_oauth_service import WizardOAuthService

        meta_app = WizardOAuthService.get_meta_app()
        app_id, app_secret = meta_app.client_id, meta_app.client_secret
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "probe_credential_health: wizard meta-app unavailable (%s) — " "skipping de_connection rows this run",
            exc,
        )
        return out
    try:
        res = es.search(
            index="de_connection",
            body={"size": 500, "query": {"term": {"provider": "facebook"}}},
        )
    except Exception as exc:  # noqa: BLE001
        logging.error("probe_credential_health: cannot read de_connection: %s", exc)
        return out
    for h in res.get("hits", {}).get("hits", []):
        src = h["_source"]
        token = _safe_decrypt(src.get("oauth_token_enc") or "", what=f"oauth_token_enc on {h['_id']}")
        if not token:
            continue
        out.append(
            {
                "source_index": "de_connection",
                "source_esid": h["_id"],
                "provider": "facebook",
                "label": src.get("label") or h["_id"],
                # de_connection carries no workspace_esid (verified against the
                # live mapping) — it comes from the team that references it.
                "workspace_esid": ws_by_connection.get(h["_id"], ""),
                "principal": src.get("principal") or "",
                "token_kind": src.get("token_kind") or "",
                "token": token,
                "app_id": app_id,
                "app_secret": app_secret,
            }
        )
    return out


def _assets_for_credential(scope: AssetScope, cred: dict) -> list[dict]:
    """The assets this credential is expected to reach, by family."""
    try:
        if cred.get("source_index") == "de_asset_fb_app_users":
            return scope.for_fb_app_user(
                cred.get("fb_app_esid") or "",
                role=cred.get("fb_app_user_role") or "",
                user_esid=cred.get("source_esid") or "",
            )
        if cred.get("source_index") == "de_connection":
            return scope.for_connection(cred.get("source_esid") or "")
    except Exception as exc:  # noqa: BLE001 — the join is best-effort; token health still lands
        logging.warning(
            "probe_credential_health: asset join failed for %s/%s: %s",
            cred.get("source_index"),
            cred.get("source_esid"),
            type(exc).__name__,
        )
    return []


def _token_kind(cred: dict, tp) -> str:
    """``token_kind`` for the health row.

    Wizard rows carry it from ``de_connection``. Legacy
    ``de_asset_fb_app_users`` rows never did — and now that they carry per-asset
    probes they can derive ``permission_lost``, which the banner only
    explains correctly when it knows the token is a system user's: "re-assign the
    asset in Business Manager", not "replace the token". Meta tells us the kind
    on ``/debug_token`` (``type: SYSTEM_USER``), so stamp what Meta reported
    rather than assuming the family.
    """
    kind = str(cred.get("token_kind") or "")
    if kind:
        return kind
    if str(getattr(tp, "token_type", "") or "").upper() == "SYSTEM_USER":
        return "system_user"
    return ""


def run_sweep(*, dry_run: bool = False, renewal_threshold_days: int | None = None) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logging.error("probe_credential_health: no ES client")
        return 1

    threshold = renewal_threshold_days if renewal_threshold_days is not None else ch.RENEWAL_THRESHOLD_DAYS

    creds = _fb_app_user_credentials(es) + _connection_credentials(es)
    logging.info("probe_credential_health: %d credential(s) to probe", len(creds))

    # the per-credential asset join. Resolved once per sweep (teams,
    # page chains, IG docs), then answered per credential. Before this the sweep
    # probed tokens only and wrote ``assets: []`` on every row, so
    # per-asset IG importability could never fire.
    scope = AssetScope(es)

    by_status: dict[str, int] = {}
    unhealthy_rows: list[str] = []
    probed_assets = 0

    # Pass 1 — probe everything. Pass 2 (below) needs every credential's results
    # before any verdict: an asset this token cannot reach does not
    # count against it when another in-scope credential reaches it, because the
    # importers try their whole ladder and stop at the first token that works.
    probed: list[tuple[dict, object, list]] = []
    for cred in creds:
        tp = probe_token(cred["token"], cred["app_id"], cred["app_secret"])
        assets = _assets_for_credential(scope, cred)
        probed_assets += len(assets)
        aps = probe_assets(cred["token"], assets)
        probed.append((cred, tp, aps))

    covered = ch.apply_cross_credential_coverage([(cred["label"], aps) for cred, _tp, aps in probed])
    if covered:
        logging.info(
            "probe_credential_health: %d failed asset probe(s) are covered by another credential — not counted",
            covered,
        )

    # per-asset cause needs each asset's newest import date. Resolved
    # once per sweep, after the for_* pass has told the scope which assets exist.
    # A family whose aggregation failed is simply absent from the map (unknown),
    # so the classifier can never read "stopped" out of an ES error.
    recency = scope.import_recency()

    for cred, tp, aps in probed:
        status, reason = ch.derive_status(tp, aps, renewal_threshold_days=threshold)
        by_status[status] = by_status.get(status, 0) + 1

        line = f"{cred['label']!r} [{cred['source_index']}/{cred['source_esid']}] -> {status}"
        if reason:
            line += f" — {reason}"
        if status in ch.UNHEALTHY_STATUSES:
            unhealthy_rows.append(line)
            logging.warning("probe_credential_health: %s", line)
        else:
            logging.info("probe_credential_health: %s", line)

        if not dry_run:
            ch.record_probe(
                source_index=cred["source_index"],
                source_esid=cred["source_esid"],
                provider=cred["provider"],
                label=cred["label"],
                token=tp,
                assets=aps,
                workspace_esid=cred.get("workspace_esid") or "",
                principal=cred.get("principal") or "",
                token_kind=_token_kind(cred, tp),
                renewal_threshold_days=threshold,
                import_recency=recency,
            )

    unhealthy = sum(by_status.get(s, 0) for s in ch.UNHEALTHY_STATUSES)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "(none)"
    logging.info(
        "probe_credential_health: sweep complete — %s; %d asset probe(s) across %d credential(s)",
        summary,
        probed_assets,
        len(creds),
    )

    if not creds:
        # An empty sweep is indistinguishable from a healthy one unless we say so
        # (CLAUDE.md #22 / #24 — counts of intent are not measurements).
        logging.warning(
            "probe_credential_health: probed ZERO credentials — either none are "
            "configured or the source indices could not be read. This is NOT a "
            "clean bill of health."
        )

    # drop health rows whose source credential was deleted since the
    # last sweep; otherwise their last verdict (and the banner reading
    # it) outlives the credential forever. Confirmed-absence only; see
    # credential_health.prune_orphans.
    pruned = ch.prune_orphans(es, dry_run=dry_run)
    if pruned:
        logging.info(
            "probe_credential_health: %s %d orphaned health row(s): %s",
            "would prune" if dry_run else "pruned",
            len(pruned),
            ", ".join(pruned),
        )

    if not dry_run:
        _stamp_heartbeat(es, checked=len(creds), unhealthy=unhealthy)
    else:
        print("\n(dry-run — nothing persisted, heartbeat not stamped)")

    if unhealthy_rows:
        print("\nUNHEALTHY:")
        for row in unhealthy_rows:
            print(f"  {row}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="probe and report; persist nothing")
    ap.add_argument(
        "--check-heartbeat",
        action="store_true",
        help="do not sweep; exit non-zero if the last sweep is stale",
    )
    ap.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    ap.add_argument(
        "--renewal-threshold-days",
        type=int,
        default=None,
        help=f"override the renewal warning window (default {ch.RENEWAL_THRESHOLD_DAYS})",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.check_heartbeat:
        return check_heartbeat(args.max_age_hours)

    # one sweep at a time. Non-blocking: if the cron (or a re-auth
    # trigger) already holds the lock, this instance says so and exits clean.
    # The running sweep will record the same fresh state we would have.
    lock = _acquire_sweep_lock()
    if lock is None:
        logging.info("probe_credential_health: another sweep is already running — skipping this one")
        return 0
    try:
        return run_sweep(dry_run=args.dry_run, renewal_threshold_days=args.renewal_threshold_days)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:  # noqa: BLE001 — releasing a lock must never raise past main
            pass


def _acquire_sweep_lock():
    """Open ``LOCK_PATH`` and take a non-blocking exclusive flock.

    Returns the open file (keep it referenced — closing it releases the lock),
    or None when another process holds it. A lockfile that cannot be opened at
    all is treated as "not locked" with a warning: a missing /app/data on a dev
    box must not make the job refuse to run.
    """
    try:
        fh = open(LOCK_PATH, "a")  # noqa: SIM115 — held for the process lifetime
    except OSError as exc:
        logging.warning("probe_credential_health: cannot open lock %s (%s) — running unlocked", LOCK_PATH, exc)
        return _NoLock()
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.close()
        return None
    return fh


class _NoLock:
    """Stand-in when the lockfile is unavailable: behaves like a held lock."""

    def close(self) -> None:
        return None

    def fileno(self) -> int:  # pragma: no cover — flock(LOCK_UN) on it is caught by the caller
        raise OSError("no lock held")


if __name__ == "__main__":
    raise SystemExit(main())
