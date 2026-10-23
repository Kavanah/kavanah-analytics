import subprocess
from datetime import datetime, timedelta, timezone
import logging
import lib.logging


# trailing-refresh window for the per-day skip. Days within this many
# days of TODAY are ALWAYS re-fetched (FB legitimately mutates recent days:
# late-attributed conversions, delivery-status updates). Days older than that are
# treated as immutable and skipped when already imported. The tail is anchored on
# today, NOT on the requested range end — see _trailing_cutoff for why (anchoring
# on the range end collapsed rolling windows; staging live test, 2026-06-19).
# Defaulted to 7 to match FB's standard 7-day-click attribution window. Net: a
# rolling_7d schedule re-fetches its whole window (correct — all within the
# attribution window) and savings accrue only on windows LONGER than 7 days
# (rolling_30d skips its oldest ~23 days). May be widened to 28 (or made
# per-connector config) if a connector uses a 28-day window — doc.
TRAILING_REFRESH_DAYS = 7

# attribution-maturation tail. FB conversion actions (messaging
# contacts, leads, purchases) keep accruing for up to ~7 days after the
# impression day (7d_click), and a view-attributed conversion can land a further
# day late (1d_view) — so an ad-day's conversion counts are not final until
# ad_day + 8. The per-day ad pull writes a day's doc once (~24h after the ad-day)
# and, on its own, never rewrites it as attribution matures, freezing conversion
# fields at day-1 maturity (paused ad sets undercount vs Data Studio;).
#
# The trailing-refresh skip only PREVENTS re-fetching already-settled
# days WITHIN the requested range — it cannot ADD days. So once made a
# daily schedule request just [yesterday], the still-maturing prior days were no
# longer iterated at all. The fix: for the AD stage, always EXTEND the requested
# range to include the maturation tail (the last MATURATION_TAIL_DAYS days ending
# yesterday), regardless of how the schedule scoped the window. _should_skip_day
# still skips the SETTLED older days (idempotent), so re-pulling the tail every
# run is bounded — the only net new fetches are the days still inside the window.
# 8 = 7d_click + 1d_view (the safe upper bound). Page/IG/window-reach steps are
# NOT extended — they have no conversion-maturation issue and the window-reach
# pull's range is intentionally the user-requested range (dedup is range-specific).
MATURATION_TAIL_DAYS = 8

# rolling dedup-window upkeep. The scheduled daily run keeps a
# yesterday-anchored last-7, last-30 and last-90 dedup window fresh per team, so
# the dashboard's nearest-window snap serves the common presets a ≤1-day-old
# FB-deduplicated reach/frequency value. The common spans, not every preset: the
# snap tolerates off-by-a-day requests (it labels what it serves), and arbitrary
# custom ranges keep the Refresh button.
_ROLLING_WINDOW_SPANS = (7, 30, 90)
# the last-90 is the dashboards page DEFAULT range (now-90d/d);
# without its own window the snap served the last-30 under a 90-day
# view. Instagram keeps 7 + 30: its window API caps a span at 30 days.
_IG_ROLLING_WINDOW_SPANS = (7, 30)


def _run_ig_window_step(since, until, team_ids=None):
    """best-effort IG dedup-window pull over [since, until]
    (inclusive). Sister to _run_window_reach_step: a failure is logged and
    swallowed so it can never fail the run that spawned it. IG's API caps a
    window at 30 days; the child skips longer spans loudly, so callers don't
    pre-clamp. Single-day windows are skipped here for the same no-dedup-
    benefit reason as the FB step."""
    if since == until:
        logging.info("IG window: single-day range %s; skipping (no dedup benefit).", since)
        return "skipped"
    cmd = [
        "python3",
        "/app/python/import.py",
        "instagram",
        "get_instagram_insights",
        f"--window_since={since}",
        f"--window_until={until}",
    ]
    if team_ids:
        cmd.append(f"--team_ids={team_ids}")
    logging.info("IG window: pulling deduplicated reach %s..%s", since, until)
    try:
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            logging.warning("IG window pull exited %s for %s..%s (run continues).", result.returncode, since, until)
            return "failed"
        return "ok"
    except Exception as e:  # noqa: BLE001 — best-effort sibling step
        logging.warning("IG window pull failed for %s..%s: %s (run continues).", since, until, e)
        return "failed"


def _rolling_window_dates(span_days, today=None):
    """[since, until] for the yesterday-anchored rolling window of
    ``span_days`` days (until = yesterday UTC, matching the tail's anchor —
    today's partial day would bake an incomplete final day into the window)."""
    anchor = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    start = anchor - timedelta(days=span_days - 1)
    return [start.isoformat(), anchor.isoformat()]


def generate_date_range(start_date, end_date):
    """Generate a list of dates from start_date to end_date (inclusive)."""
    dates = []
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def _trailing_cutoff(dates, trailing_days=TRAILING_REFRESH_DAYS, today=None):
    """the earliest date that must STILL be re-fetched (the start of
    the mutable tail).

    Any date strictly BEFORE this cutoff is an immutable older day eligible for
    the existence-skip; any date >= cutoff is in the trailing-refresh window and
    is always fetched. Returns ``None`` when ``dates`` is empty so the caller
    skips nothing.

    The cutoff is anchored on **today** (the actual run date), NOT on the range
    end: "recent days still change" means recent relative to *now* (FB keeps
    attributing conversions to the last ~7 calendar days), independent of which
    window a schedule happened to request. Anchoring on the range end instead
    (the original design) collapsed for rolling windows — the last N days of an
    N-day window is the whole window, so a rolling_7d schedule could never skip
    anything (caught by the staging live test, 2026-06-19). Consequence: savings
    accrue only on windows LONGER than ``trailing_days`` (rolling_30d skips the
    oldest ~23 days; rolling_7d correctly skips nothing — its whole window is
    still mutable).

    ``today`` is injectable (a ``date``) for deterministic tests; it defaults to
    the current UTC date.
    """
    valid = [d for d in dates if d]
    if not valid:
        return None
    anchor = today or datetime.now(timezone.utc).date()
    cutoff = anchor - timedelta(days=max(0, trailing_days - 1))
    return cutoff.strftime("%Y-%m-%d")


def _maturation_tail_days(tail_days=MATURATION_TAIL_DAYS, today=None):
    """return the set of attribution-maturation tail days: the last
    ``tail_days`` calendar days ending YESTERDAY (anchored on ``today``).

    Single source of truth for the tail-day set, shared by
    :func:`_merge_maturation_tail` (which unions it INTO the requested dates) and
    the scheduled-pull force set (which force-overwrites exactly these days so the
    re-fetched, now-matured conversion counts replace the day-1 values — without a
    force flag the re-fetch is discarded by the day-skip and the
    per-ad write guard, so the tail re-pulls but never overwrites;).

    ``today`` is injectable (a ``date``) for deterministic tests; it defaults to
    the current UTC date. ``tail_days <= 0`` -> empty set (no tail).
    """
    if not tail_days or tail_days <= 0:
        return set()
    anchor = today or datetime.now(timezone.utc).date()
    yesterday = anchor - timedelta(days=1)
    return {(yesterday - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(tail_days)}


def _merge_maturation_tail(dates, tail_days=MATURATION_TAIL_DAYS, today=None):
    """return ``dates`` extended to include the attribution-maturation
    tail: the last ``tail_days`` calendar days ending YESTERDAY.

    The ad stage must re-pull every day whose conversion attribution is still
    settling so the late-attributed counts mature, no matter how short the
    requested range is (a daily schedule requests only ``[yesterday]``).
    This unions the tail INTO the requested dates rather than replacing them, so
    a longer backfill range is preserved and only genuinely-missing tail days are
    added. The returned list is sorted and de-duplicated.

    The tail is anchored on **today** (matching :func:`_trailing_cutoff`): "still
    maturing" means recent relative to now, independent of the requested window.
    Days the caller asked for that are OLDER than the tail are kept (a backfill of
    last month still runs); the settled ones among them are skipped downstream by
    :func:`_should_skip_day` exactly as before — this helper only guarantees the
    tail is PRESENT, not that every day is fetched.

    ``today`` is injectable (a ``date``) for deterministic tests; it defaults to
    the current UTC date. ``tail_days <= 0`` is treated as "no tail" and returns
    the input dates unchanged (sorted/de-duped).
    """
    merged = {d for d in dates if d}
    merged |= _maturation_tail_days(tail_days=tail_days, today=today)
    return sorted(merged)


def _skip_window_days():
    """the trailing-refresh skip-protection window (in days) for the
    ad stage.

    Must be wide enough that NO day in the attribution-maturation tail (added by
    :func:`_merge_maturation_tail`) is ever skipped as already-imported, or its
    still-settling conversion counts would freeze. The tail is anchored on
    YESTERDAY (oldest tail day = today - ``MATURATION_TAIL_DAYS``) while
    :func:`_trailing_cutoff` anchors on TODAY (cutoff = today - (window - 1)).
    For ``cutoff <= oldest_tail_day`` we need ``window >= MATURATION_TAIL_DAYS +
    1`` — the +1 reconciles the two anchors. ``max`` guards against
    ``TRAILING_REFRESH_DAYS`` ever being raised past that.
    """
    return max(TRAILING_REFRESH_DAYS, MATURATION_TAIL_DAYS + 1)


def _resolve_scope_team_esids(team_ids):
    """resolve a ``--team_ids`` filter to the set of team _ids it
    covers, for building the per-team ``*_fb_insights`` index names the skip
    pre-check queries.

    Returns an empty set on no filter (cron all-teams sweep) or any resolver
    error — and the caller treats an empty set as "can't scope -> never skip",
    so a resolution failure degrades to the current always-fetch behavior
    rather than wrongly skipping.
    """
    if not team_ids:
        return set()
    try:
        from import_scripts.facebook.lib.assets import resolve_team_filter_scope

        scope = resolve_team_filter_scope(team_ids)
    except Exception as exc:  # noqa: BLE001 — boundary; default to never-skip
        logging.warning("scope resolve raised: %s — disabling per-day skip", exc)
        return set()
    return set((scope or {}).get("team_esids") or set())


# the per-day date field on a *_fb_insights doc. The importer's daily
# pull stamps the day as ``insight_date_start`` (an ES ``date`` field; a term
# query against the YYYY-MM-DD string the per-day loop passes coerces and matches
# — verified on staging). There is NO ``date`` field on these docs, so an earlier
# {"date": ...} probe matched nothing and the skip never fired. ``kibana_date``
# carries the same value and is an equivalent fallback if this ever drifts.
_FB_INSIGHTS_DAY_FIELD = "insight_date_start"


def _day_already_imported(team_esids, date):
    """True iff EVERY team in ``team_esids`` already has at least one
    indexed ``*_fb_insights`` doc whose day (``insight_date_start``) is ``date``.

    Reuses the ``indexed_doc_present`` helper (an ``es.count`` term
    query that fails SAFE -> returns False -> re-fetch). We require ALL teams in
    scope to be populated before skipping: if even one team is missing the day,
    we re-fetch the whole day (the fetch is per-account, and a missing team
    means real data is absent). A no-spend day legitimately writes zero docs and
    so is NOT skippable by doc-count alone — re-fetching it is cheap (the probe
    returns "no ads ran" with no per-ad fan-out).

    Empty ``team_esids`` (cron sweep / unresolved scope) -> False (never skip).
    """
    if not team_esids:
        return False
    try:
        from lib.es_helpers import build_data_index_name, indexed_doc_present
    except Exception as exc:  # noqa: BLE001 — boundary; default to never-skip
        logging.warning("es_helpers import raised: %s — re-fetching %s", exc, date)
        return False

    for team_esid in team_esids:
        try:
            index = build_data_index_name(team_esid, "fb_insights")
        except Exception as exc:  # noqa: BLE001 — malformed esid: re-fetch
            logging.warning("index name for team %s raised: %s — re-fetching %s", team_esid, exc, date)
            return False
        if not indexed_doc_present(index, match_fields={_FB_INSIGHTS_DAY_FIELD: date}):
            return False
    return True


def _should_skip_day(date, *, team_esids, cutoff, force_refetch):
    """decide whether the per-day fetch for ``date`` can be skipped.

    Skip ONLY when all of:
      * ``force_refetch`` is False (the --force_refetch override re-pulls
        everything, e.g. for a mapping / field-catalog change),
      * ``cutoff`` is known AND ``date`` is strictly before it (immutable older
        day, outside the trailing-refresh tail),
      * every team in scope already has the day indexed
        (:func:`_day_already_imported`).

    Any other case returns False (fetch). This never touches the
    windowed reach pull — that is a separate post-loop step the caller leaves
    unconditional.
    """
    if force_refetch:
        return False
    if not cutoff or date >= cutoff:
        return False
    return _day_already_imported(team_esids, date)


def _resolve_connection_to_team_filter(connection_esid):
    """resolve a connection_esid to ``(team_id, principal)``.

    Used only on the on-demand kickoff path (step-6 finish). The
    07:05 cron does not pass ``--connection_esid`` and never enters
    this branch.

    Returns ``(team_id_str, principal_username)`` on success, or
    ``(None, None)`` on a missing connection / missing team
    membership. The caller logs a warning and aborts the progress
    writes — the import itself still runs (worst case it pulls every
    team's data, which is the earlier cron behaviour, not a
    regression).
    """
    try:
        from asset_management.web.services.connection_service import ConnectionService
    except Exception as exc:  # noqa: BLE001
        logging.warning("connection resolve: import failed: %s", exc)
        return (None, None)

    doc = ConnectionService.get_connection(connection_esid)
    if not doc:
        return (None, None)
    # pause imports when the user has disconnected this
    # connection from the Manage Connection page. Returning (None, None)
    # aborts the on-demand kickoff path without touching the cron's
    # all-teams sweep (no --connection_esid).
    if (doc.get("status") or "") == "revoked":
        logging.info(
            "facebook importer: skipping revoked connection_esid=%s",
            connection_esid,
        )
        return (None, None)
    principal = doc.get("principal") or None
    if not principal:
        return (None, None)

    # Resolve the principal's primary team. Imported lazily to avoid
    # pulling the whole analytics blueprint into the importer's import
    # graph; this helper lives in routes.py today but is pure ES read.
    try:
        from asset_management.web.blueprints.analytics.routes import _resolve_primary_team_esid
    except Exception as exc:  # noqa: BLE001
        logging.warning("connection resolve: _resolve_primary_team_esid import failed: %s", exc)
        return (None, principal)

    team_esid = _resolve_primary_team_esid(principal)

    # follow-up 1 — connection→team fallback. The principal of a
    # connection is NOT always a member of the team that imports through
    # it: operator/admin-owned connections (e.g. an operator owns the
    # Kavanah Team connection but has no team-membership row) reverse-
    # resolve to None via principal→primary-team. When that happens, fall
    # back to the team that CARRIES this connection_esid — the forward
    # direction the dashboard kickoff already uses (team→connection). This
    # is the durable scope source the importer should have used all along;
    # without it the import runs unfiltered across every team.
    if not team_esid:
        team_esid = _team_esid_by_connection(connection_esid)
        if team_esid:
            logging.info(
                "connection resolve: principal %r has no primary team; "
                "fell back to team %s carrying connection_esid=%s.",
                principal,
                team_esid,
                connection_esid,
            )

    return (team_esid, principal)


def _team_esid_by_connection(connection_esid):
    """Return the ``_id`` of the team whose ``connection_esid`` field
    matches ``connection_esid``, or ``None``.

    Forward lookup (team→connection), the inverse of the broken
    principal→primary-team walk. ``connection_esid`` on ``de_asset_teams``
    is an analyzed text field, so match BOTH the ``.keyword`` subfield
    (exact) and the raw field (fallback for mappings without .keyword) —
    the same dual-match the kickoff endpoint uses (app.py
    kickoff_meta_range_import). Fail-soft: any error returns None so the
    importer degrades to its prior (unfiltered) behaviour rather than
    aborting.
    """
    if not connection_esid:
        return None
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None
        resp = es.search(
            index="de_asset_teams",
            body={
                "size": 1,
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"connection_esid.keyword": connection_esid}},
                            {"term": {"connection_esid": connection_esid}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("_team_esid_by_connection(%r): %s", connection_esid, exc)
        return None
    hits = (resp.get("hits") or {}).get("hits") or []
    if not hits:
        return None
    return hits[0].get("_id")


def run_import_for_range(
    dates,
    log_usage,
    team_ids=None,
    progress_run_id=None,
    force_refetch=False,
    force_dates=None,
    days_base=0,
    backfill_bailout=False,
):
    """run get_insights ONCE for the whole date set.

    Replaces the per-date subprocess fan-out (one get_insights per ``--date``):
    the child asks Meta for ``[min..max]`` with ``time_increment=1`` — per-ad
    PER-DAY rows for the whole range in one call per ad account — and pays
    interpreter startup / token check / scope resolution once per RUN. ``dates``
    is the post-skip fetch set (possibly non-contiguous; the child
    drops rows for uncovered days at its per-row day gate).

    ``force_dates`` — the per-day force-overwrite subset (maturation
    tail), forwarded as ``--force_dates`` so the per-ad write guard
    re-writes exactly those days.
    ``force_refetch`` forces EVERY day and supersedes it.

    ``days_base`` — days the caller already accounted for via the skip pass; the
    child adds its per-account advance on top.

    On-demand runs (``progress_run_id`` set) execute via
    ``_run_subprocess_cancellable`` so the dashboard Cancel interrupts the ranged
    pull even inside a Meta throttle backoff; the cron path keeps a
    plain blocking run. Raises ``subprocess.CalledProcessError`` on a child
    failure or ``_ImportCancelled`` when cancelled.
    """
    dates = sorted(d for d in dates if d)
    logging.info("Running get_insights.py for %d day(s): %s..%s ...", len(dates), dates[0], dates[-1])
    get_insights_cmd = [
        "python3",
        "/app/python/import.py",
        "facebook",
        "get_insights",
        f"--dates={','.join(dates)}",
    ]
    if log_usage:
        get_insights_cmd.append("--log_usage")
    if team_ids:
        get_insights_cmd.append(f"--team_ids={team_ids}")
    # forward force_refetch so get_insights' per-ad write guard
    # re-WRITES instead of
    # skipping. Without this the day-skip is bypassed but the re-fetched data is
    # discarded at the file-write step (send sees 0 new files), so a forced
    # re-pull never overwrites (no re-map, no attribution maturation).
    if force_refetch:
        get_insights_cmd.append("--force_refetch=true")
    else:
        forced = sorted(set(force_dates or ()) & set(dates))
        if forced:
            get_insights_cmd.append(f"--force_dates={','.join(forced)}")
    # forward backfill bail-out so the child stops (keeping partial data)
    # on a Meta rate limit instead of sleeping. Set only by the Report Scheduler
    # backfill; harmless/absent everywhere else.
    if backfill_bailout:
        get_insights_cmd.append("--backfill_bailout=true")
    # let get_insights record its OWN user-actionable failures (strict-
    # mode zero teams, no traversable asset chain, revoked connection) against the
    # run's progress doc. Without this, those raise ValueError → the subprocess
    # exits non-zero → the dispatcher stamps only a generic CalledProcessError,
    # losing the real, correctable cause. No-op on the cron path.
    # the run_id also lets the child bump days_fetched per ad account
    # (the new bump point); progress_days_base anchors those bumps past
    # the days the skip pass already handled.
    if progress_run_id:
        get_insights_cmd.append(f"--progress_run_id={progress_run_id}")
        get_insights_cmd.append(f"--progress_days_base={int(days_base)}")
        # cancellable child: the ranged pull is now the long pole of
        # an on-demand run; Cancel must be able to interrupt it mid-backoff.
        _run_subprocess_cancellable(get_insights_cmd, progress_run_id=progress_run_id)
    else:
        subprocess.run(get_insights_cmd, check=True)


def _is_cancel_requested(progress_run_id):
    """fail-soft cancellation probe shared by the per-date loop.

    Returns True only when an on-demand run (non-None ``progress_run_id``)
    has been flipped to ``cancelling`` by the dashboard Cancel button.
    The cron path (``progress_run_id`` is None) returns False trivially.
    Any ES error is swallowed to False so a transient blip doesn't abort
    an import the user hasn't actually cancelled.
    """
    if not progress_run_id:
        return False
    try:
        from lib.import_progress_service import ImportProgressService

        return ImportProgressService.is_cancelling(progress_run_id)
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("is_cancelling raised: %s — proceeding", exc)
        return False


def _set_phase(progress_run_id, phase):
    """follow-up 3 — fail-soft phase recorder for the on-demand path.

    No-op on the cron path (None run_id). A phase-write failure must not
    abort the import.
    """
    if not progress_run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.set_phase(run_id=progress_run_id, phase=phase)
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("set_phase raised: %s", exc)


def _all_scoped_teams_source_off(team_ids, source):
    """do ALL teams in a concrete ``--team_ids`` scope have ``source``
    switched off?

    Returns True only when ``team_ids`` resolves to a non-empty, fully known
    esid set AND every one of those teams has the source disabled — the signal
    to skip a whole importer stage before its per-day fan-out. Returns False
    (never skip) for the cron/all-teams path (``team_ids`` is None) or on any
    resolution failure — fail-soft: a stage we can't prove is fully-off still
    runs (its per-team gate is the correctness backstop; this is only a
    fan-out optimization). Import the resolver + service lazily so this
    module's non-web callers don't pay for them."""
    if not team_ids:
        return False
    try:
        from import_scripts.facebook.lib.assets import resolve_team_filter_scope
        from asset_management.web.services.field_catalog_service import FieldCatalogService

        scope = resolve_team_filter_scope(team_ids)
        esids = sorted(scope.get("team_esids") or []) if scope else []
        if not esids:
            return False
        return all(not FieldCatalogService.source_enabled_for_team(esid, source) for esid in esids)
    except Exception as e:  # noqa: BLE001 — fail-soft: never let the optimization break a stage
        logging.warning("_all_scoped_teams_source_off(%r, %s) failed (%s); running the stage.", team_ids, source, e)
        return False


def _stamp_cancelled(progress_run_id):
    """write the terminal ``cancelled`` stamp for an on-demand run.

    No-op on the cron path (None run_id). Soft-fails: a progress-write
    error must not mask the fact that the import was cancelled.
    """
    if not progress_run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.complete_run(
            run_id=progress_run_id,
            status="cancelled",
        )
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("complete_run(cancelled) raised: %s", exc)


# how often to poll the cancel flag while a child subprocess runs.
# The child (get_insights_window) can sit in an uninterruptible Meta throttle
# backoff sleep (60s/120s); a blocking subprocess.run left the
# PARENT unable to act on Cancel for the whole backoff, so the dashboard "Cancel"
# hung for minutes. Poll every 2s and terminate the child the moment cancel is
# seen — responsive without busy-waiting.
_CANCEL_POLL_INTERVAL_SECONDS = 2


class _ImportCancelled(Exception):
    """Raised when a cancellable child subprocess was terminated by Cancel."""


def _run_subprocess_cancellable(cmd, *, progress_run_id):
    """Run ``cmd`` as a child, killing it early if the run is cancelled.

    Replaces a blocking ``subprocess.run(cmd, check=True)`` for the long-running
    window pull so the dashboard Cancel button is responsive even while the child
    is inside a Meta throttle backoff sleep. Behavior:

      * cron / no run_id → identical to ``subprocess.run(check=True)`` (we still
        Popen + wait, but never poll-cancel; a non-zero exit raises, unchanged).
      * on-demand run → poll ``is_cancelling`` every ~2s; on cancel, SIGTERM the
        child (SIGKILL if it doesn't die), then raise ``_ImportCancelled`` so the
        caller stops cleanly (KAN1241-03 decision: keep any partial docs already
        written; a later refresh overwrites them idempotently).

    Raises ``subprocess.CalledProcessError`` on a non-zero child exit (parity with
    ``check=True``) or ``_ImportCancelled`` when cancelled.
    """
    proc = subprocess.Popen(cmd)
    while True:
        try:
            rc = proc.wait(timeout=_CANCEL_POLL_INTERVAL_SECONDS)
        except subprocess.TimeoutExpired:
            if _is_cancel_requested(progress_run_id):
                logging.info("cancel: terminating window-pull child (pid=%s).", proc.pid)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logging.warning("cancel: child pid=%s ignored SIGTERM; killing.", proc.pid)
                    proc.kill()
                    proc.wait()
                raise _ImportCancelled()
            continue
        if rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
        return


def _lock_scope_for_run(team_ids):
    """the import-lock scope key for this run's team cohort.

    ``facebook::<connection_esid>`` when the cohort unambiguously maps to one
    account_wide wizard connection; "" (the connector-global ``facebook``
    key) for legacy/master-config teams, mixed cohorts, an unresolvable
    scope, or no team filter. The actual resolution lives in
    ``ImportLockService.meta_connection_scope_key`` so the scheduler's
    ``import_lock_scope`` stamp (Cancel's force_release) and the capacity
    panel's lock_group compute the byte-identical key. Fail-SAFE toward the
    global lock: any error here over-serializes, never under-serializes.
    """
    if not team_ids:
        return ""
    try:
        from lib.es_helpers import get_elasticsearch_client
        from lib.import_lock_service import ImportLockService

        return ImportLockService.meta_connection_scope_key(get_elasticsearch_client(), team_ids)
    except Exception as exc:  # noqa: BLE001 — boundary; default to the global lock
        logging.warning("lock-scope resolve raised: %s — using connector-global lock", exc)
        return ""


def _run_ad_stage(
    *, dates, log_usage, team_ids, progress_run_id, force_refetch=False, force_dates=None, backfill_bailout=False
):
    """Run the ad-insights stage: ranged fetch + send + archive + prewarm.

    Returns True iff the user cancelled mid-stage (the caller stamps the doc
    ``cancelled`` and returns). Returns False on a clean completion.

    Extracted from main() (selective-dispatch refactor) so the ad stage can be
    skipped wholesale for IG-only / page-only dashboards.

    the per-date subprocess fan-out is gone: the requested days are
    partitioned into skip vs fetch (the decision logic, unchanged), and
    the surviving days go to ONE ranged get_insights run
    (``run_import_for_range``) that asks Meta for per-day rows over the whole
    span. Cancel handling moved with it: one pre-fetch check, a cancellable
    child, and one post-fetch check replace the between-date polls.

    per-day existence skip. Older immutable days (outside the
    trailing-refresh window) whose data is already indexed for every team in
    scope are dropped from the fetch set, and the child's per-row day gate
    drops any rows Meta returns for them (the ranged call covers the full
    span). ``force_refetch`` bypasses the skip entirely (mapping /
    field-catalog re-pull). The skip needs a resolved team scope to build the
    per-team index names; the cron all-teams sweep (no team_ids) resolves to
    an empty scope and so never skips. The windowed reach step
    below stays unconditional.

    ``force_dates`` is the per-day subset that must FORCE-overwrite
    (bypass both the day-skip and, via ``--force_dates`` to
    get_insights, the per-ad write guard). The scheduled maturation-tail
    pull passes the tail days here so their re-fetched, now-matured conversion
    counts actually replace the day-1 values — the requested days (e.g. yesterday)
    fetch normally. ``force_refetch=True`` (manual backfill) forces EVERY day and
    supersedes this. ``None`` / empty -> no per-day force.
    """
    force_dates = force_dates or set()
    # resolve scope + cutoff ONCE before the loop (the scope query is
    # an ES round-trip we don't want to repeat per day). An empty scope or a
    # None cutoff disables the skip (every day fetches), preserving prior
    # behavior for the cron sweep and any unresolved scope.
    skip_scope = set() if force_refetch else _resolve_scope_team_esids(team_ids)
    # Read the module constant at call time (not via _trailing_cutoff's bound
    # default) so the tunable trailing-window is a single source of truth.
    # the skip-protection window must cover the whole maturation tail
    # so no still-settling tail day is skipped as already-imported (see
    # _skip_window_days).
    skip_window_days = _skip_window_days()
    cutoff = _trailing_cutoff(dates, trailing_days=skip_window_days) if skip_scope else None

    # partition the requested days: the skip DECISION is
    # unchanged, only the dispatch changed (surviving days go to ONE ranged
    # get_insights run instead of one subprocess per day).
    fetch_dates = []
    skipped_days = 0
    for date in dates:
        # a maturation-tail day forces an overwrite even on the
        # scheduled (non-force_refetch) path: it must NOT be skipped as
        # already-imported, and the child must re-write it past the
        # per-ad guard (--force_dates). The global force_refetch (manual
        # backfill) forces every day; force_dates forces just the tail subset.
        day_forced = force_refetch or (date in force_dates)

        if _should_skip_day(date, team_esids=skip_scope, cutoff=cutoff, force_refetch=day_forced):
            logging.info(
                "fb-importer: %s is already imported for all %d team(s) "
                "in scope and is older than the %d-day attribution-maturation tail "
                "(cutoff %s, skip-window %dd); skipping fetch.",
                date,
                len(skip_scope),
                MATURATION_TAIL_DAYS,
                cutoff,
                skip_window_days,
            )
            skipped_days += 1
        else:
            fetch_dates.append(date)

    # skipped days are handled instantly; account for them up front so
    # the pill starts at the right place before the ranged pull advances it.
    # Soft-fail: a progress write failure must not abort the import.
    if progress_run_id and skipped_days:
        try:
            from lib.import_progress_service import ImportProgressService

            ImportProgressService.update_progress(run_id=progress_run_id, days_fetched=skipped_days)
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("update_progress (skip) raised: %s", exc)

    # cancel check before the (single) fetch fires. Cron path (no
    # progress_run_id) is unaffected — is_cancelling is False.
    if _is_cancel_requested(progress_run_id):
        logging.info(
            "fb-importer: cancellation requested before the ranged ad "
            "fetch (%d skipped, %d to fetch); aborting ad stage.",
            skipped_days,
            len(fetch_dates),
        )
        return True

    if fetch_dates:
        try:
            run_import_for_range(
                fetch_dates,
                log_usage,
                team_ids=team_ids,
                progress_run_id=progress_run_id,
                force_refetch=force_refetch,
                force_dates=force_dates,
                days_base=skipped_days,
                backfill_bailout=backfill_bailout,
            )
        except _ImportCancelled:
            # the user cancelled mid-pull; the child was terminated.
            # Keep any partial files (an idempotent re-pull overwrites them) and
            # skip send/archive — the caller stamps the doc cancelled.
            logging.info("fb-importer: ranged ad fetch cancelled; breaking before post-loop steps.")
            return True

        # post-fetch cancellation check: a cancel that lands in the
        # child's final moments must still stop send/archive from firing.
        if _is_cancel_requested(progress_run_id):
            logging.info(
                "fb-importer: cancellation observed after the ranged ad " "fetch; breaking before post-loop steps."
            )
            return True

    # terminal day count for the stage (covers the integer-division
    # rounding of the child's per-account bumps).
    if progress_run_id:
        try:
            from lib.import_progress_service import ImportProgressService

            ImportProgressService.update_progress(run_id=progress_run_id, days_fetched=len(dates))
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("update_progress raised: %s", exc)

    # Facebook to Elastic
    logging.info("Running send_json_to_elastic.py...")
    # pass team_ids so the sender only indexes THIS run's teams. The
    # ad_insights/ dir is shared; without the scope a team-scoped run would
    # sweep up another team's un-archived leftover JSON (cross-workspace leak).
    send_cmd = ["python3", "/app/python/import.py", "facebook", "send_json_to_elastic"]
    if team_ids:
        send_cmd.append(f"--team_ids={team_ids}")
    subprocess.run(send_cmd, check=True)

    # Move JSON to Imported
    logging.info("Running archive_imported_json.py...")
    subprocess.run(
        ["python3", "/app/python/import.py", "facebook", "archive_imported_json"],
        check=True,
    )

    # Pre-warm the city geocode cache so the map dashboard doesn't fall back to
    # live Nominatim calls when a user first views any new campaigns.
    # Best-effort — failure here doesn't fail the import. scope to
    # team_ids when set.
    logging.info("Running prewarm_city_geocache.py...")
    prewarm_cmd = ["python3", "/app/python/import.py", "facebook", "prewarm_city_geocache"]
    if team_ids:
        prewarm_cmd.append(f"--team_ids={team_ids}")
    subprocess.run(prewarm_cmd, check=False)

    # deduplicated window-reach pull. The per-day loop above writes
    # per-day docs whose reach OVER-COUNTS when summed across a multi-day range
    # (reach is a unique-user count; FB only deduplicates it when asked for the
    # whole window in one call). After the per-day pass, fire ONE windowed pull
    # over [dates[0], dates[-1]] so the *_fb_insights_window index carries FB's
    # deduplicated reach/frequency for the range. Additive metrics
    # (spend/impressions/clicks) keep coming from the per-day docs — this is the
    # Option B split (see DECISIONS.md 2026-06-03).
    #
    # Skip a single-day range: the window == the day, so the per-day doc already
    # holds that day's (trivially deduplicated) reach and a window doc adds
    # nothing. Best-effort: a window-pull failure must not fail the per-day
    # import that already succeeded.
    _run_window_reach_step(dates, log_usage=log_usage, team_ids=team_ids, progress_run_id=progress_run_id)

    logging.info("Facebook Ad Insights completed successfully.")
    return False


def _run_window_reach_step(dates, *, log_usage, team_ids, progress_run_id=None, report_window_progress=False):
    """windowed dedup-reach pull over [dates[0], dates[-1]].

    Best-effort sibling step to the per-day ad stage. Writes the deduplicated
    window doc to ``*_fb_insights_window`` via get_insights_window +
    send_window_json_to_elastic. A single-day range is skipped (no dedup
    benefit). Any failure here is logged and swallowed so it cannot fail a
    per-day import that already completed.

    Returns a status string for callers that need it (dedup-only
    refresh stamps the progress doc on it): ``"ok"`` (window docs sent),
    ``"skipped"`` (single-day range), ``"failed"`` (subprocess error — still
    swallowed), ``"cancelled"`` (the user hit Cancel mid-pull; the
    child was terminated and the send step skipped). The per-day caller passes no
    ``progress_run_id`` (cron), so it never cancels — behavior unchanged.
    """
    sorted_dates = sorted(d for d in dates if d)
    if len(sorted_dates) < 2:
        logging.info(
            "window-reach: range is %d day(s); skipping windowed pull " "(no dedup benefit for a single day).",
            len(sorted_dates),
        )
        return "skipped"

    since, until = sorted_dates[0], sorted_dates[-1]
    logging.info("window-reach: pulling deduplicated window %s..%s", since, until)
    try:
        window_cmd = [
            "python3",
            "/app/python/import.py",
            "facebook",
            "get_insights_window",
            f"--since={since}",
            f"--until={until}",
        ]
        if team_ids:
            window_cmd.append(f"--team_ids={team_ids}")
        if log_usage:
            window_cmd.append("--log_usage")
        # dedup-only runs (the window pull is the ONLY stage) let the
        # child post per-account progress against the run's doc, so the pill
        # advances instead of sitting at 0% for the whole multi-account pull.
        # The FULL-import caller must NOT set this: its doc counts DAYS and is
        # near-done by the time this tail step runs — rebasing to accounts
        # would snap the pill backwards.
        if report_window_progress and progress_run_id:
            window_cmd.append(f"--progress_run_id={progress_run_id}")
        # the window pull is the long pole (per-account throttle
        # backoff). Run it cancellably so the dashboard Cancel button is
        # responsive even while the child sleeps out a Meta rate-limit backoff.
        _run_subprocess_cancellable(window_cmd, progress_run_id=progress_run_id)

        # a cancel between the pull and the send must NOT index a
        # partial window (decision: stop cleanly, keep files, skip send). Check
        # once more before the send commits docs to ES.
        if _is_cancel_requested(progress_run_id):
            logging.info("cancel: window pull done but cancel requested; skipping send.")
            return "cancelled"

        logging.info("window-reach: sending window docs to Elasticsearch...")
        # scope the window sender to this run's teams (shared dir).
        window_send_cmd = ["python3", "/app/python/import.py", "facebook", "send_window_json_to_elastic"]
        if team_ids:
            window_send_cmd.append(f"--team_ids={team_ids}")
        subprocess.run(window_send_cmd, check=True)
        logging.info("window-reach: completed for %s..%s", since, until)
        return "ok"
    except _ImportCancelled:
        # child terminated by Cancel. Keep any docs already written
        # (idempotent re-pull overwrites); report cancelled, don't swallow as fail.
        logging.info("window-reach: cancelled for %s..%s (partial docs kept).", since, until)
        return "cancelled"
    except Exception as exc:  # noqa: BLE001 — boundary: never fail the per-day import
        logging.warning(
            "window-reach pull for %s..%s failed: %s — per-day import is unaffected.",
            since,
            until,
            exc,
        )
        return "failed"


_VALID_STAGES = ("ad", "page", "ig")


def _parse_stages(params):
    """/B (KAN-XXX selective dispatch) — resolve which Meta stages to run.

    Returns a set drawn from {"ad", "page", "ig"}.

    Precedence:
      1. ``--stages=ad,page,ig`` (comma-list) — run exactly the listed stages.
         Unknown tokens are ignored; an empty/all-unknown list falls through to
         the default (run everything) rather than running nothing.
      2. ``--ads_only`` — back-compat alias for ``--stages=ad``.
      3. neither — run all three (the cron + wizard default; unchanged).

    The default-to-all behavior is the load-bearing safety net: a dashboard
    whose data views don't resolve to any Meta stage, or a caller that forgets
    the flag, imports everything rather than silently importing nothing.
    """
    raw = params.get("stages")
    if raw:
        wanted = {s.strip().lower() for s in str(raw).split(",") if s.strip()}
        selected = {s for s in wanted if s in _VALID_STAGES}
        if selected:
            return selected
        logging.warning(
            "KAN selective-dispatch: --stages=%r had no recognised stage " "(valid: %s); running all stages.",
            raw,
            ",".join(_VALID_STAGES),
        )
    if params.get("ads_only", False):
        return {"ad"}
    return set(_VALID_STAGES)


def main(params):
    lib.logging.define("fb-run-daily-imports")
    log_usage = params.get("log_usage", False)
    team_ids = params.get("team_ids")  # optional team filter
    # bypass the per-day existence skip and re-pull every day in the
    # range (mapping / field-catalog change,). The scheduler does NOT
    # pass this; only a manual / forced backfill does.
    force_refetch = params.get("force_refetch", False)

    # Selective stage dispatch (Meta family). ``stages`` is the set of
    # {ad,page,ig} this run will execute. ``--ads_only`` (legacy) maps to
    # {"ad"} inside _parse_stages; no ``--stages`` runs all three (cron/wizard).
    stages = _parse_stages(params)

    # dedup-only refresh. The dashboard "Refresh" button pulls ONLY
    # the windowed de-duplicated field (reach/frequency) for the selected range,
    # not the full per-day ad/page/ig pipeline. When set, this run skips all
    # per-day stages and fires ONLY ``_run_window_reach_step`` over the range.
    # Default (unset) preserves the existing wizard/cron/custom-range behavior
    # byte-for-byte. Accepts the string "true"/"1" (CLI) or a real bool (params).
    dedup_only = params.get("dedup_only") in (True, "true", "True", "1", 1)

    # extend the ad-stage range with the attribution-maturation tail so
    # still-settling FB conversion counts (messaging contacts, leads, purchases)
    # get re-pulled and overwritten. Set ONLY by the Report Scheduler's automated
    # recurring dispatch (the path that owns "keep recent data current"). NOT set
    # by the Import Data page backfill, the wizard, or a missing-dates backfill —
    # those pull the user's EXACT requested range. The signal is an explicit flag,
    # not the date shape: the scheduler and the Import Data page BOTH pass explicit
    # --start_date/--end_date, so only an opt-in flag can tell them apart. Accepts
    # the string "true"/"1" (CLI) or a real bool (params).
    maturation_tail = params.get("maturation_tail") in (True, "true", "True", "1", 1)

    # smart-backfill bail-out mode. Set ONLY by the Report Scheduler's
    # backfill (ReportScheduleService.backfill_now); the cron/scheduled/wizard paths
    # never pass it. When on, a Meta rate limit STOPS the ad-stage fetch gracefully
    # (get_insights keeps whatever it wrote and exits 0 so send/archive commit the
    # partial data) instead of the daily path's sleep-and-retry. Accepts the string
    # "true"/"1" (CLI) or a real bool (params).
    backfill_bailout = params.get("backfill_bailout") in (True, "true", "True", "1", 1)

    # startup reaper. Stamp any orphaned non-terminal progress
    # doc (importer died mid-run without a terminal stamp) terminal before
    # we do anything else. Runs on EVERY importer invocation — the 07:05
    # cron and every on-demand backfill — independent of whether this run
    # writes progress, so orphans never outlive ~24h. Fail-soft: a reaper
    # error must not block the import.
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.reap_stale_runs()
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("reap_stale_runs raised: %s — proceeding", exc)

    # on-demand kickoff. The step-6 finish handler passes
    # ``--connection_esid X``; we resolve it to a single-team filter
    # and write progress to ``de_import_progress`` so the building
    # screen + sync pill can render a live percentage. Backwards
    # compatible: without the flag, no resolve happens, no progress
    # is written, and the 07:05 cron path is untouched.
    connection_esid = params.get("connection_esid")
    progress_run_id = None
    progress_username = None

    # explicit progress run_id for the legacy page_filtered
    # dashboard custom-range path. That path (app.py kickoff_meta_range_import
    # -> _kickoff_on_demand_backfill with legacy_team_esid) spawns this
    # importer with --team_ids and NO --connection_esid, but the web layer
    # already minted the progress doc + run_id (keyed on a synthetic
    # legacy_team:<esid> connection). Without receiving that run_id the
    # importer never wires up the cancel-check, so the dashboard Cancel
    # button was a silent no-op for legacy teams (the doc froze at
    # 'cancelling'; confirmed on staging run_id c0616b0f). When passed,
    # this wins over the connection_esid-derived path below and we skip
    # the importer-side start_run (the doc already exists).
    explicit_run_id = params.get("progress_run_id")
    if explicit_run_id:
        progress_run_id = explicit_run_id

    if connection_esid:
        resolved_team, principal = _resolve_connection_to_team_filter(connection_esid)
        progress_username = principal
        if resolved_team:
            # If the caller passed both --team_ids and --connection_esid,
            # the connection-resolved team wins. The wizard only passes
            # --connection_esid; mixing the two is not a real call path,
            # but the precedence is deterministic if it ever happens.
            team_ids = resolved_team
        else:
            logging.warning(
                "-connection_esid=%r did not resolve to a team; "
                "running unfiltered import without progress writes.",
                connection_esid,
            )
            connection_esid = None  # disable progress writes — no team scope to report against

    # require a team scope. The cron full-cluster path is retired
    # (all daily imports run via the scheduler, which always passes
    # --team_ids); a scope-less invocation is a misconfiguration, not a sweep,
    # and falling through to a cluster-wide pull is a cross-workspace exposure.
    # Bail BEFORE the lock / any FB API call. Placed AFTER the connection_esid
    # resolution above so the on-demand path (which derives team_ids from the
    # connection) is honored. The resolves-to-zero half is the existing
    # strict-mode guard in get_insights.
    from lib.import_scope import require_scope

    require_scope({"team_ids": team_ids}, scope_param="team_ids", connector_id="meta_ads")

    # Determine date(s) to process
    specific_dates = params.get("dates")  # comma-separated list of specific dates
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    single_date = params.get("date")

    if specific_dates:
        dates = [d.strip() for d in specific_dates.split(",") if d.strip()]
        logging.info(f"Processing {len(dates)} specific dates")
    elif start_date and end_date:
        dates = generate_date_range(start_date, end_date)
        logging.info(f"Processing date range: {start_date} to {end_date} ({len(dates)} days)")
    elif single_date:
        dates = [single_date]
    else:
        # Default to previous day.
        # UTC-anchored: naive local time shifted "yesterday" by the
        # container's timezone on manual runs (the scheduler always passes
        # explicit UTC-derived ranges; this fallback only fires on bare CLI
        # invocations). Matches _trailing_cutoff / _maturation_tail_days.
        dates = [(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")]

    # start the progress doc BEFORE any subprocess fires.
    # The step-6 finish handler also writes the initial pending doc
    # synchronously so the building screen's first poll always finds
    # something; this start_run call is idempotent against that one
    # (60-minute window — see ImportProgressService docstring).
    #
    # skip when an explicit --progress_run_id was passed (legacy
    # page_filtered path): the web layer already created that doc, and we
    # have no progress_username to start a new one. We just adopt its run_id.
    if connection_esid and progress_username and not explicit_run_id:
        try:
            from lib.import_progress_service import ImportProgressService

            progress_run_id = ImportProgressService.start_run(
                username=progress_username,
                connection_esid=connection_esid,
                connector_id="meta_ads",
                days_total=len(dates),
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("start_run raised: %s", exc)

    # follow-up LOCK-001 — cluster-level FB importer lock.
    # Acquires before any FB API call (check_fb_token / get_insights
    # / etc.) so the 07:05 cron and an on-demand backfill can't race
    # on the FB app's token refresh. If the lock is held by another
    # FB importer, we log + exit cleanly — the in-flight import will
    # pull the same data anyway.
    #
    # Soft-fails: a missing or broken lock service is treated as
    # "couldn't get the lock; proceed." The race we're guarding
    # against is rare; degrading to no-lock is better than refusing
    # to import on a lock-service outage.
    # the lock is scoped per wizard CONNECTION. A cohort that
    # unambiguously maps to one account_wide connection acquires
    # ``facebook::<connection_esid>``: same-connection runs still serialize
    # (double-click / loser-resolve — same key, same doc)
    # while different connections hold different docs and run concurrently
    # (each has its own read-only de_connection token; no shared credential
    # to race on). Legacy/master-config teams, mixed cohorts, and any
    # unresolvable scope keep the connector-global "facebook" key (the shared
    # de_asset_fb_apps token refresh race is real there), and the
    # global↔scoped rule in ImportLockService makes the two classes mutually
    # exclusive. This supersedes the skip-the-lock branch: an
    # account_wide run is no longer lockless — it is immune only to OTHER
    # connections' locks, not to a held global lock or its own connection's.
    lock_id = None
    lock_scope = _lock_scope_for_run(team_ids)
    if lock_scope:
        logging.info(
            "lock: team_ids=%r resolves to wizard connection scope %r; " "acquiring the per-connection FB lock.",
            team_ids,
            lock_scope,
        )
    try:
        from lib.import_lock_service import ImportLockService

        # progress_run_id lets the global import queue show this run
        # as "queued (#N)" and honour a cancel while it waits for a slot.
        lock_id = ImportLockService.acquire("facebook", scope_id=lock_scope, progress_run_id=progress_run_id)
        if lock_id is None:
            logging.warning(
                "lock: another facebook importer holds a conflicting "
                "lock (scope=%r); exiting cleanly. The in-flight import will "
                "pull this run's data.",
                lock_scope,
            )
            # resolve our progress doc IFF it's a true orphan (the lock
            # winner is a DIFFERENT run that will never finalize it). The old
            # ``connection_esid and progress_run_id`` guard skipped the stamp on the
            # report-scheduler Run-now path (external run_id, no connection_esid),
            # orphaning the doc; resolve_orphaned_lock_loser handles it, and also
            # leaves a same-schedule winner's adopted doc alone (preserves).
            if progress_run_id:
                try:
                    from lib.import_progress_service import ImportProgressService

                    ImportProgressService.resolve_orphaned_lock_loser(progress_run_id)
                except Exception as exc:  # noqa: BLE001
                    logging.warning("lock: resolve_orphaned_lock_loser raised: %s", exc)
            return
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("lock: acquire raised: %s — proceeding", exc)

    start_time = datetime.now()
    logging.info(f"Import started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # Check FB token (pass team_ids so the token check only debugs
        # apps reachable from the filtered teams).
        logging.info("Running check_fb_token.py...")
        check_token_cmd = ["python3", "/app/python/import.py", "facebook", "check_fb_token"]
        if team_ids:
            check_token_cmd.append(f"--team_ids={team_ids}")
        # thread the run_id so check_fb_token can surface a dead
        # legacy-app token (code 190) to the Report Scheduler. The dead token
        # fails inside this subprocess, not downstream in get_insights, so this
        # is the only path that can surface it. No-op on the cron path (no run_id).
        if progress_run_id:
            check_token_cmd.append(f"--progress_run_id={progress_run_id}")
        subprocess.run(check_token_cmd, check=True)

        # dedup-only refresh. Skip the entire per-day ad/page/ig
        # pipeline and fire ONLY the windowed de-duplicated reach pull over the
        # range. This is the "Refresh" button path: cheap, pulls just the
        # non-additive field for the selected window. The per-day stages, their
        # send/archive/prewarm, and the page/IG subprocesses are all bypassed.
        if dedup_only:
            logging.info(
                "dedup-only refresh: skipping per-day ad/page/ig stages; "
                "running ONLY the windowed dedup-reach pull over %s..%s.",
                dates[0],
                dates[-1],
            )
            _set_phase(progress_run_id, "window_reach")
            window_status = _run_window_reach_step(
                dates,
                log_usage=log_usage,
                team_ids=team_ids,
                progress_run_id=progress_run_id,
                # the window pull is this run's only stage; let it
                # rebase the progress doc to per-account units and advance it.
                report_window_progress=True,
            )
            logging.info("dedup-only refresh: window-reach pull status=%s.", window_status)
            # the same Refresh also updates IG dedup windows for the
            # scoped teams (IG rides the Meta family; one button, both sources).
            # Best-effort: an IG failure never downgrades the FB refresh result.
            if not _all_scoped_teams_source_off(team_ids, "instagram"):
                _run_ig_window_step(dates[0], dates[-1], team_ids=team_ids)
            # A single-day range ("skipped") is a clean no-op success — the
            # per-day doc already holds that day's trivially-deduped reach. A
            # subprocess error ("failed") is a real failure the user must see,
            # so the Refresh pill shows an error rather than a false "done".
            # "cancelled" (the user hit Cancel mid-pull): stamp the
            # terminal cancelled status so the pill clears immediately instead of
            # hanging at "cancelling" (the whole point of the interruptible pull).
            if progress_run_id:
                try:
                    from lib.import_progress_service import ImportProgressService

                    if window_status == "cancelled":
                        ImportProgressService.complete_run(
                            run_id=progress_run_id,
                            status="cancelled",
                        )
                    elif window_status == "failed":
                        ImportProgressService.complete_run(
                            run_id=progress_run_id,
                            status="failed",
                            error="Deduplicated window-reach pull failed.",
                        )
                    else:
                        ImportProgressService.complete_run(
                            run_id=progress_run_id,
                            status="succeeded",
                        )
                except Exception as exc:  # noqa: BLE001 — boundary
                    logging.warning("dedup-only complete_run raised: %s", exc)
            return

        # Ad-insights stage (selective dispatch). Runs the per-date fetch loop
        # + send/archive/prewarm only when "ad" is in the selected stages. An
        # IG-only or page-only dashboard skips it entirely. Returns True if the
        # user cancelled mid-stage (caller stamps cancelled + returns).
        # if EVERY selected stage is switched off for the scoped
        # team(s), the whole run is a no-op. Surface it as a distinct phase so
        # the progress pill reads "nothing to import" instead of silently
        # marching the bar to 99% under a "Importing…" label.
        _stage_source = {"ad": "fb_ads", "page": "fb_pages", "ig": "instagram"}
        if stages and all(_all_scoped_teams_source_off(team_ids, _stage_source[s]) for s in stages if s in _stage_source):
            logging.info("every selected source is disabled for the scoped team(s) — no data will be imported.")
            _set_phase(progress_run_id, "all_sources_disabled")

        cancelled_by_user = False
        if "ad" in stages and _all_scoped_teams_source_off(team_ids, "fb_ads"):
            logging.info("fb_ads disabled for every scoped team — skipping the ad + window stage entirely.")
        elif "ad" in stages:
            # when the Report Scheduler's automated recurring dispatch
            # asks for it (--maturation_tail), extend the AD-stage range to cover
            # the attribution-maturation tail so still-settling conversion counts
            # get re-pulled and overwritten — even made a daily
            # schedule resolve to just [yesterday]. Settled older days are still
            # skipped by _should_skip_day, so this only adds fetches for days
            # genuinely still maturing. Page/IG/window-reach keep the original
            # ``dates``.
            #
            # Gated on the explicit flag, NOT the date shape: the scheduler and the
            # Import Data page BOTH pass explicit --start_date/--end_date, so only
            # an opt-in flag separates "automated recency pull" (extend) from "user
            # picked these exact days" (Import Data page / wizard / missing-dates
            # backfill — pull exactly what was asked, no tail bolted on).
            ad_dates = _merge_maturation_tail(dates) if maturation_tail else dates
            # on the scheduled tail pull, the tail days must
            # FORCE-overwrite, not just be re-fetched: re-fetching alone is
            # discarded by the day-skip and the per-ad write
            # guard, so conversions would never mature in ES. Force exactly the
            # tail subset (the requested days fetch normally). A manual backfill
            # (force_refetch) already forces every day, so no force_dates needed.
            tail_force_dates = _maturation_tail_days() if maturation_tail else set()
            if len(ad_dates) != len(dates):
                logging.info(
                    "ad stage: extended %d requested day(s) to %d to cover "
                    "the %d-day attribution-maturation tail (scheduled pull); "
                    "force-overwriting the tail days so matured conversions replace "
                    "day-1 values.",
                    len(dates),
                    len(ad_dates),
                    MATURATION_TAIL_DAYS,
                )
            cancelled_by_user = _run_ad_stage(
                dates=ad_dates,
                log_usage=log_usage,
                team_ids=team_ids,
                progress_run_id=progress_run_id,
                force_refetch=force_refetch,
                force_dates=tail_force_dates,
                backfill_bailout=backfill_bailout,
            )
        else:
            logging.info("Selective dispatch: 'ad' not in stages=%s; skipping ad insights.", sorted(stages))

        # rolling dedup-window upkeep. Gated on the same
        # scheduler-only maturation_tail flag as the tail pull: the dashboard's
        # nearest-window snap (dedup_routing.resolve_nearest_window) serves the
        # freshest pulled window, so keeping a yesterday-anchored last-7 and
        # last-30 window current means the common presets always snap to a
        # ≤1-day-old dedup value instead of whatever range someone last
        # refreshed. Manual paths (Import Data page / wizard) are untouched —
        # their _run_window_reach_step already covers the user's exact range.
        # Best-effort like the range window pull: a failure here never fails
        # the per-day import (status is logged inside the step). No
        # progress_run_id: this upkeep must not touch the run's progress doc
        # (the pill-rebase trap).
        if maturation_tail and "ad" in stages and not cancelled_by_user:
            for span_days in _ROLLING_WINDOW_SPANS:
                _run_window_reach_step(
                    _rolling_window_dates(span_days),
                    log_usage=log_usage,
                    team_ids=team_ids,
                )
        # same rolling upkeep for IG dedup windows (its own
        # 7 + 30 spans, which fit IG's 30-day API cap by construction). Gated
        # identically: scheduler path only, never after a user cancel; skipped
        # when no scoped team has Instagram on.
        if (
            maturation_tail
            and "ig" in stages
            and not cancelled_by_user
            and not _all_scoped_teams_source_off(team_ids, "instagram")
        ):
            for span_days in _IG_ROLLING_WINDOW_SPANS:
                rolling = _rolling_window_dates(span_days)
                _run_ig_window_step(rolling[0], rolling[-1], team_ids=team_ids)

        # item 2 — if the ad stage was cancelled mid-loop, skip the
        # remaining stages and jump straight to the terminal cancelled stamp.
        if cancelled_by_user:
            _stamp_cancelled(progress_run_id)
            # Release the LOCK-001 facebook lock and return — the finally
            # block at the bottom of main() also releases, but returning here
            # makes the cancel path explicit rather than relying on fall-through.
            if lock_id:
                try:
                    from lib.import_lock_service import ImportLockService

                    ImportLockService.release("facebook", lock_id, scope_id=lock_scope)
                except Exception:
                    pass
            return

        # Page-insights stage (selective dispatch). Runs only when "page" is in
        # the selected stages. Each between-stages cancel checkpoint below is
        # only meaningful for the on-demand path (progress_run_id set); the
        # cron path (no run_id) sails through.
        if "page" in stages and _all_scoped_teams_source_off(team_ids, "fb_pages"):
            logging.info("fb_pages disabled for every scoped team — skipping the page-insights stage entirely.")
        elif "page" in stages:
            # between-stages cancel checkpoint. The page stage is a
            # single blocking subprocess.run with no inner cancel-check, so a
            # cancel that landed before it starts is observed here and stamps
            # the doc terminal, keeping whatever earlier stages wrote (the
            # fe3cf9bc staging repro). A cancel MID-page-pull is still honored
            # only when that child returns (deferred — follow-up).
            if _is_cancel_requested(progress_run_id):
                logging.info(
                    "fb-importer: cancellation observed before page "
                    "insights; stamping cancelled (earlier-stage data retained)."
                )
                _stamp_cancelled(progress_run_id)
                if lock_id:
                    try:
                        from lib.import_lock_service import ImportLockService

                        ImportLockService.release("facebook", lock_id, scope_id=lock_scope)
                    except Exception:
                        pass
                return

            # follow-up 3 — record the phase so the pill shows
            # "Fetching page insights…" instead of a static 100%. No-op on cron.
            _set_phase(progress_run_id, "page_insights")

            # Facebook Page Insights (all team pages, not just active-ad pages)
            logging.info("Running run_page_daily_imports...")
            page_cmd = [
                "python3",
                "/app/python/import.py",
                "facebook",
                "run_page_daily_imports",
                f"--start_date={dates[0]}",
                f"--end_date={dates[-1]}",
            ]
            if team_ids:
                page_cmd.append(f"--team_ids={team_ids}")
            # share the run_id so the page importer tracks the same
            # progress doc (parity with the IG sibling below).
            if progress_run_id:
                page_cmd.append(f"--progress_run_id={progress_run_id}")
            # propagate backfill bail-out so a throttled page STOPS the
            # pull (keep partial) instead of sleeping; parity with the account cmd.
            if backfill_bailout:
                page_cmd.append("--backfill_bailout=true")
            subprocess.run(page_cmd, check=True)
        else:
            logging.info("Selective dispatch: 'page' not in stages=%s; skipping page insights.", sorted(stages))

        # Instagram stage (selective dispatch). Runs only when "ig" is selected.
        if "ig" in stages and _all_scoped_teams_source_off(team_ids, "instagram"):
            logging.info("instagram disabled for every scoped team — skipping the Instagram stage entirely.")
        elif "ig" in stages:
            # between-stages checkpoint: a cancel that landed during
            # the (potentially multi-minute) page pull, or before IG on an
            # IG-only run, is observed here before the IG child starts.
            if _is_cancel_requested(progress_run_id):
                logging.info(
                    "fb-importer: cancellation observed before Instagram; "
                    "stamping cancelled (earlier-stage data retained)."
                )
                _stamp_cancelled(progress_run_id)
                if lock_id:
                    try:
                        from lib.import_lock_service import ImportLockService

                        ImportLockService.release("facebook", lock_id, scope_id=lock_scope)
                    except Exception:
                        pass
                return

            # Instagram (uses same FB tokens — run after page insights to avoid
            # rate-limit overlap). item 1 — propagate team_ids so a
            # wizard-cohort run doesn't fan out to every team. Cron (no
            # team_ids) is unchanged.
            ig_cmd = [
                "python3",
                "/app/python/import.py",
                "instagram",
                "run_instagram_daily_imports",
                f"--start_date={dates[0]}",
                f"--end_date={dates[-1]}",
            ]
            if team_ids:
                ig_cmd.append(f"--team_ids={team_ids}")
            # follow-up 3 — entering the Instagram stage.
            _set_phase(progress_run_id, "instagram")
            # item 2 — share the progress_run_id so the IG sibling can
            # observe a mid-flight cancel and bail out cleanly.
            if progress_run_id:
                ig_cmd.append(f"--progress_run_id={progress_run_id}")
            # propagate backfill bail-out to the IG sibling too.
            if backfill_bailout:
                ig_cmd.append("--backfill_bailout=true")
            logging.info("Running run_instagram_daily_imports (team_ids=%r)...", team_ids)
            subprocess.run(ig_cmd, check=True)
            # multi-day runs also refresh the IG dedup window for
            # the exact requested range (mirrors the FB window step's contract:
            # dedup is range-specific, so the user's range is what gets pulled).
            if len(dates) >= 2:
                _run_ig_window_step(dates[0], dates[-1], team_ids=team_ids)
        else:
            logging.info("Selective dispatch: 'ig' not in stages=%s; skipping instagram.", sorted(stages))

        logging.info("Facebook/Instagram tasks completed for stages=%s.", sorted(stages))

        # terminal success stamp for the on-demand path. The
        # cron path (no run_id) is a no-op. Note: success means "the
        # importer pipeline completed without raising", not "100% of
        # the user's data is in ES" — the archive/prewarm steps are
        # best-effort and may have logged warnings without raising.
        # The pill maps succeeded → "done"
        # which is still the right UX even if a non-critical step
        # underperformed.
        if progress_run_id:
            try:
                from lib.import_progress_service import ImportProgressService

                ImportProgressService.complete_run(
                    run_id=progress_run_id,
                    status="succeeded",
                )
            except Exception as exc:  # noqa: BLE001 — boundary
                logging.warning("complete_run(succeeded) raised: %s", exc)
    except subprocess.CalledProcessError as e:
        logging.error(f"An error occurred: {e}")
        # terminal failure stamp. The check=True subprocesses
        # (get_insights, send_json_to_elastic, archive, page imports,
        # instagram) are the critical path; a CalledProcessError here
        # means the backfill didn't land. The pill maps failed → an
        # error pill instead of disappearing.
        if progress_run_id:
            try:
                from lib.import_progress_service import ImportProgressService

                ImportProgressService.complete_run(
                    run_id=progress_run_id,
                    status="failed",
                    error=str(e),
                )
            except Exception as exc:  # noqa: BLE001 — boundary
                logging.warning("complete_run(failed) raised: %s", exc)
        # signal the failure to the CALLER, not just to the progress
        # doc. This handler stamped the pill "failed" and then fell through, so
        # main() returned None and import.py exited 0: the Report Scheduler
        # recorded a false SUCCESS for an import whose critical path did not
        # land. The pill and the exit code disagreed, and only the pill was
        # right.
        #
        # Returning False (rather than re-raising) keeps the finally block's
        # lock release and the timing log intact on the way out, and matches the
        # bool convention the other connectors' main() already uses.
        # import.py:74+ is what converts this into a non-zero exit —
        # before that change a False return would have been discarded too.
        return False
    finally:
        # follow-up LOCK-001 — release the cluster lock so
        # the next FB importer (cron or on-demand) can proceed. The
        # release is idempotent and only clears the doc if our
        # lock_id still matches the current holder (per
        # ImportLockService.release).
        if lock_id:
            try:
                from lib.import_lock_service import ImportLockService

                ImportLockService.release("facebook", lock_id, scope_id=lock_scope)
            except Exception as exc:  # noqa: BLE001
                logging.warning("lock release raised: %s", exc)

        end_time = datetime.now()
        elapsed = end_time - start_time
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        logging.info(f"Import ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} | Total time: {minutes}m {seconds}s")
