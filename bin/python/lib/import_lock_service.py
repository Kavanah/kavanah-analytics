"""Cluster-level importer lock.

Serialises importers that share a provider's auth surface so the
07:05 cron and an on-demand backfill can't race on the provider's
token refresh.

Concrete failure mode this addresses:

  1. The 07:05 daily FB cron starts, calls ``check_fb_token``, which
     does an FB OAuth refresh and writes the new access token to ES.
  2. At 07:06 a user finishes onboarding; the wizard's on-demand
     kickoff Popens its own ``run_daily_imports``.
  3. Both importers now hold copies of the access token in memory.
     If either calls ``check_fb_token`` again before the other
     finishes, the second refresh invalidates the first's token —
     subsequent API calls 401, and the import aborts mid-day.

The cheapest fix is a process-level mutex. We don't need anything
fancy (Redis, file lock); an ES doc at ``de_import_locks/{connector}``
is enough at our scale. Acquire on importer entry, release on exit
(try/finally). A second acquire returns ``None`` if the lock is
held and not yet expired.

TTL design
----------

Lock TTL = 30 minutes — wider than any real FB backfill in
practice (a single ad account's 90-day pull is well under 10 min),
narrow enough that a crashed-holding-the-lock importer doesn't
block the next day's cron.

Granularity = per-connector, not global. A future Google Ads
importer running concurrently with FB is fine — they don't share a
token surface. The kickoff helper consults
``_CONNECTOR_IMPORTER_REGISTRY[*]["lock_connector"]`` so multiple
connectors can share a lock when they share a provider (e.g.
``fb_ads`` and ``fb_pages`` both lock on ``facebook``).

Dead-holder self-healing
-----------------------------------

A holder killed mid-run (container restart, OOM, SIGKILL) never runs
its ``finally`` release, so its lock sat held for the full 30-min TTL
and its progress doc sat ``cancelling``/``pending`` forever — blocking
every import of the scope and requiring manual ES surgery (staging,
2026-07-07). Every acquirer and the lock live in the same container
(``imports_container``), so liveness is provable: ``acquire`` stamps
the holder's identity (pid + /proc starttime + pgid + hostname +
progress run_id) onto the lock doc, and a later acquire/probe that
finds a held-but-unexpired lock whose holder is PROVABLY dead treats
it as expired — breaking the lock immediately and stamping the dead
run's orphaned progress doc terminal. Death requires positive proof
(hostname mismatch = replaced container, or exact pid gone AND no
surviving process-group member — FB's page/instagram sub-importers
stay in the holder's group, so a dead parent with a live
child keeps the lock). Anything unknowable (no /proc on Windows dev,
earlier doc without identity, probe error) falls back to the TTL
— false-alive is safe, false-dead would re-open the token-refresh
race the lock exists to prevent.

Global vs scoped exclusion
-------------------------------------

A connector's locks come in two classes: the connector-GLOBAL key
(``scope_id`` empty — the bare-connector doc) and per-SCOPE keys
(``<connector>::<scope_id>``). A global acquire means "I
may touch every scope of this connector" (the unscoped bulk sweep,
or a run whose scope can't be resolved), so the two classes must
mutually exclude: a scoped acquire loses to a held global lock, and
a global acquire loses to ANY held scoped lock of that connector.
Two DIFFERENT scopes still run concurrently — that's the point of
scoping. Before the classes ignored each other (acquire
only checked its own doc id), so a bulk sweep could race a scoped
run on the scope they share.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

LOCK_INDEX = "de_import_locks"

# Lock TTL. See module docstring for the choice.
_LOCK_TTL = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _es():
    """Lazy ES lookup so tests can patch at lib.es_helpers."""
    from lib.es_helpers import get_elasticsearch_client

    return get_elasticsearch_client()


def _lock_doc_id(connector: str, scope_id: str = "") -> str:
    """the ES doc id for a lock.

    ``connector`` alone (no scope_id) → connector-global lock, the historical
    behavior: at most one lock doc per connector, every caller serializes against
    every other (the legacy-FB shared-token path and the bulk cron rely on this).

    ``connector + scope_id`` → a per-SCOPE lock keyed ``<connector>::<scope_id>``,
    so two imports of the same connector but DIFFERENT scopes (different Google Ads
    customers / GA4 properties / Echo tenants) hold DIFFERENT docs and run
    concurrently, while two imports of the SAME scope still serialize (preserving
    the same-schedule double-click guard, and the lock-loser orphan
    resolve — same scope → same key → same doc).

    The separator ``::`` can't collide with a connector name (connectors are bare
    identifiers like ``google_ads``) so a scoped key is unambiguous.
    """
    s = (scope_id or "").strip()
    return f"{connector}::{s}" if s else connector


class ImportLockService:
    """Advisory import lock backed by ``de_import_locks``.

    per-connector (serialize a connector's shared-credential imports).
    an optional ``scope_id`` narrows the lock to one connection / customer
    / property / tenant, so independent scopes of the same connector run in
    parallel. Omitting ``scope_id`` keeps the original connector-global behavior.
    """

    @staticmethod
    def scope_key_for_ids(ids) -> str:
        """build a STABLE per-scope lock key from a set of resource ids
        (Google Ads customer ids, GA4 property ids, Echo tenant esids).

        Accepts a comma-separated string (the importers' ``--customer_ids`` /
        ``--property_ids`` flag shape) or an iterable of ids. Returns a
        normalized key: ids split, stripped, de-duped, sorted, comma-joined — so
        two dispatches covering the SAME set of ids produce the SAME key
        (serialize) regardless of order or duplicates, while different sets
        produce different keys (run in parallel).

        Empty / falsy input → "" → the caller acquires the connector-GLOBAL lock
        (e.g. a cron full-pull with no id filter must serialize against everything).
        """
        if not ids:
            return ""
        if isinstance(ids, str):
            parts = ids.split(",")
        else:
            parts = list(ids)
        norm = sorted({str(p).strip() for p in parts if str(p).strip()})
        return ",".join(norm)

    @staticmethod
    def echo_tenant_scope_key(es, team_esids) -> str:
        """resolve a set of Echo team esids to the per-TENANT lock
        scope key.

        Echo's lock guards GraphQL rate-limit exhaustion, which is per-TENANT
        (each tenant has its own API endpoint / quota), not per-team. So two
        teams under DIFFERENT tenants are independent and should run
        concurrently, while two imports touching the SAME tenant must still
        serialize. The lock is therefore keyed on the set of tenants the teams
        belong to, NOT on the teams themselves.

        Each ``de_asset_teams`` doc carries its tenant directly in
        ``echo_tenant_esid`` (the same field ``get_active_teams`` joins on), so
        this is a single ids-lookup — no tenant-table hop. The distinct tenant
        esids are fed through :meth:`scope_key_for_ids` so the importer (at its
        lock site) and the scheduler (stamping ``run_state.import_lock_scope``
        for Cancel's force_release) compute a BYTE-IDENTICAL key from the same
        function — the one place a drift would let Cancel miss the lock doc.

        Empty / unresolvable ``team_esids`` → "" → the caller falls back to the
        connector-GLOBAL echo lock (the cron full-pull path, which has no team
        filter and must serialize against everything — conservative).

        Soft-fails to "" (connector-global) on any ES error: a degraded lookup
        must over-serialize, never under-serialize (under-serialize = the
        rate-limit exhaustion the lock exists to prevent)."""
        if es is None or not team_esids:
            return ""
        if isinstance(team_esids, str):
            ids = [t.strip() for t in team_esids.split(",") if t.strip()]
        else:
            ids = [str(t).strip() for t in team_esids if str(t).strip()]
        if not ids:
            return ""
        try:
            resp = es.search(
                index="de_asset_teams",
                body={
                    "query": {"ids": {"values": ids}},
                    "_source": ["echo_tenant_esid"],
                    "size": len(ids),
                },
            )
            tenants = {(h.get("_source") or {}).get("echo_tenant_esid") for h in resp.get("hits", {}).get("hits", [])}
            tenants.discard(None)
            tenants.discard("")
        except Exception as exc:  # noqa: BLE001 — boundary; degrade to global lock
            logging.warning("echo_tenant_scope_key: tenant resolve failed: %s — using connector-global lock", exc)
            return ""
        return ImportLockService.scope_key_for_ids(tenants)

    @staticmethod
    def meta_connection_scope_key(es, team_esids) -> str:
        """resolve a Meta run's team esids to its per-CONNECTION
        lock scope key, or "" for the connector-global ``facebook`` lock.

        Wizard-based Meta connections each hold their own token in their own
        ``de_connection`` doc (multi-account connectors +), so two
        teams on DIFFERENT connections share no token surface and may import
        concurrently. The lock is therefore keyed on the run's resolved
        connection — but ONLY when the whole cohort unambiguously maps to one
        wizard connection:

          * every team must resolve, be ``fb_import_mode == "account_wide"``
            (a legacy/page_filtered team shares the master-config token and
            needs the global lock), and carry a ``connection_esid``;
          * the cohort must resolve to exactly ONE distinct connection.
            Unlike :meth:`scope_key_for_ids` (google_ads/echo precedent), a
            multi-connection cohort does NOT get a compound set key: a
            ``{connA,connB}`` key would not exclude a concurrent ``connA``
            run (different key, same connection), breaking the same-
            connection serialization AC (double-click /
            loser-resolve). Multi-connection batches over-serialize on the
            global lock instead — conservative;
          * the connection's provider must be facebook (the
            generic team pointer can be overwritten by another connector's
            wizard; a hijacked pointer falls back to global).

        Any resolution failure soft-fails to "" (connector-global): a
        degraded lookup must over-serialize, never under-serialize. The
        importer's lock site, the scheduler's ``import_lock_scope`` stamp
        (Cancel's force_release), and the capacity panel's lock_group all
        call THIS function so every surface computes a byte-identical key.
        """
        if es is None or not team_esids:
            return ""
        if isinstance(team_esids, str):
            ids = [t.strip() for t in team_esids.split(",") if t.strip()]
        else:
            ids = [str(t).strip() for t in team_esids if str(t).strip()]
        if not ids:
            return ""
        try:
            resp = es.search(
                index="de_asset_teams",
                body={
                    "query": {"ids": {"values": ids}},
                    "_source": ["fb_import_mode", "connection_esid"],
                    "size": len(ids),
                },
            )
            hits = resp.get("hits", {}).get("hits", [])
            if len(hits) != len(set(ids)):
                # Some team didn't resolve — can't prove the cohort is all
                # wizard-connection; take the global lock.
                return ""
            connections = set()
            for h in hits:
                src = h.get("_source") or {}
                if (src.get("fb_import_mode") or "") != "account_wide":
                    return ""
                conn = (src.get("connection_esid") or "").strip()
                if not conn:
                    return ""
                connections.add(conn)
            if len(connections) != 1:
                return ""
            conn_esid = next(iter(connections))
            conn_doc = es.get(index="de_connection", id=conn_esid)
            provider = ((conn_doc.get("_source") or {}).get("provider") or "").strip().lower()
            if provider and provider != "facebook":
                logging.warning(
                    "meta_connection_scope_key: team pointer %s has provider=%r "
                    "(not facebook hijack) — using connector-global lock",
                    conn_esid,
                    provider,
                )
                return ""
        except Exception as exc:  # noqa: BLE001 — boundary; degrade to global lock
            logging.warning(
                "meta_connection_scope_key: resolve failed for %r: %s — using connector-global lock",
                team_esids,
                exc,
            )
            return ""
        return ImportLockService.scope_key_for_ids([conn_esid])

    @staticmethod
    def acquire(connector: str, scope_id: str = "") -> str | None:
        """Try to claim the lock for ``connector`` (optionally narrowed to
        ``scope_id``).

        Returns a fresh ``lock_id`` (random UUID) when the caller now
        holds the lock, or ``None`` when an unexpired lock is already
        held for the same (connector, scope_id) key — or for a
        mutually-exclusive one: a scoped acquire loses to a held connector-
        GLOBAL lock, and a global acquire loses to any held scoped lock of
        the connector (see the module docstring).

        Implementation note: the ES doc id is ``_lock_doc_id(connector,
        scope_id)`` so there's at most one lock doc per (connector, scope).
        A previous run's expired doc is silently overwritten — that's the
        "crashed holder doesn't block forever" branch.

        Soft-fails to ``None`` on ES outage, which the importer
        treats as "couldn't get the lock; proceed without it." The
        race we're guarding against is rare; an ES outage that drops
        the lock is rarer still and the import is still better than
        no import.
        """
        if not connector:
            return None
        es = _es()
        if es is None:
            return None

        doc_id = _lock_doc_id(connector, scope_id)
        now = _now()
        existing = _get(es, doc_id)
        if existing is not None:
            expires_at = _parse_iso(existing.get("expires_at"))
            if expires_at is not None and expires_at > now:
                if not _holder_is_dead(existing):
                    # Held, unexpired, holder alive (or unprovable). Caller loses.
                    return None
                # held and unexpired, but the recorded holder is
                # provably dead (killed mid-run without releasing). Treat as
                # expired: stamp its orphaned progress doc terminal and fall
                # through to overwrite, instead of blocking the scope for the
                # rest of the 30-min TTL.
                logging.warning(
                    "lock: breaking lock %s — holder pid=%r host=%r is dead (expires %s)",
                    doc_id,
                    existing.get("holder_pid"),
                    existing.get("holder_host"),
                    existing.get("expires_at"),
                )
                _stamp_orphaned_run(existing)
            else:
                # Held but expired (crashed prior holder). Fall through
                # to overwrite — but log so an operator notices a stuck
                # importer trend.
                logging.info(
                    "lock: overwriting expired lock on %s held by %r " "(expired at %s)",
                    doc_id,
                    existing.get("holder"),
                    existing.get("expires_at"),
                )

        # global↔scoped mutual exclusion. A held lock in the
        # OTHER class also blocks this acquire (see module docstring).
        # heal=True: a dead cross-class blocker is deleted (its
        # doc id is NOT ours to overwrite) and its orphan doc stamped.
        blocker = _cross_class_hold(es, connector, scope_id, now, heal=True)
        if blocker is not None:
            logging.info(
                "lock: acquire of %s blocked by held %s (scope_id=%r, expires %s)",
                doc_id,
                _lock_doc_id(blocker.get("connector") or connector, blocker.get("scope_id") or ""),
                blocker.get("scope_id"),
                blocker.get("expires_at"),
            )
            return None

        lock_id = str(uuid.uuid4())
        doc = {
            "connector": connector,
            "scope_id": (scope_id or "").strip(),
            "lock_id": lock_id,
            "holder": "import_lock_service",
            "acquired_at": now.isoformat(),
            "expires_at": (now + _LOCK_TTL).isoformat(),
            # holder identity for the dead-holder liveness check
            # (see module docstring). Read from _source only, never queried
            # by term, so the dynamic mapping of these fields is irrelevant.
            **_holder_identity(),
        }
        try:
            es.index(
                index=LOCK_INDEX,
                id=doc_id,
                document=doc,
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("lock acquire failed for %s: %s", doc_id, exc)
            return None
        return lock_id

    @staticmethod
    def release(connector: str, lock_id: str, scope_id: str = "") -> None:
        """Release ``(connector, scope_id)``'s lock if (and only if) ``lock_id``
        matches the current holder.

        The lock_id check prevents a stale holder from accidentally
        releasing a lock that's since been re-acquired by another
        importer (e.g. holder A's lock expired, holder B acquired,
        holder A wakes up and calls release). Without the check
        A's release would clear B's lock and re-open the race.

        Soft-fails silently — the lock will expire on its own.
        """
        if not connector or not lock_id:
            return
        es = _es()
        if es is None:
            return
        doc_id = _lock_doc_id(connector, scope_id)
        current = _get(es, doc_id)
        if current is None:
            return
        if current.get("lock_id") != lock_id:
            logging.info(
                "lock release: not the current holder of %s " "(asked lock_id=%r, current=%r); leaving lock in place.",
                doc_id,
                lock_id,
                current.get("lock_id"),
            )
            return
        try:
            es.delete(index=LOCK_INDEX, id=doc_id, refresh="wait_for")
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("lock release failed for %s: %s", doc_id, exc)

    @staticmethod
    def force_release(connector: str, scope_id: str = "") -> bool:
        """release ``(connector, scope_id)``'s lock UNCONDITIONALLY (no
        lock_id match), for the cancel path where the holder has been killed.

        A SIGKILL'd importer never runs its own clean release, so its lock would
        sit held until the TTL/reaper expires it — blocking the next import of that
        scope. The cancel just confirmed the process group is dead, so the lock
        is definitionally orphaned and safe to clear. Returns True if a doc was
        deleted (or already absent), False on ES error. Soft-fails — a stuck lock is
        recoverable (it expires), so this must never raise into the cancel path."""
        if not connector:
            return False
        es = _es()
        if es is None:
            return False
        doc_id = _lock_doc_id(connector, scope_id)
        try:
            es.delete(index=LOCK_INDEX, id=doc_id, refresh="wait_for")
            return True
        except Exception as exc:  # noqa: BLE001 — boundary; 404 (already gone) is fine
            msg = str(exc).lower()
            if "not_found" in msg or "404" in msg:
                return True
            logging.warning("force_release failed for %s: %s", doc_id, exc)
            return False

    @staticmethod
    def is_held(connector: str, scope_id: str = "") -> bool:
        """Cheap check used by the on-demand kickoff path.

        ``True`` only when a doc exists for ``(connector, scope_id)`` AND its
        ``expires_at`` is still in the future. An expired doc returns ``False`` —
        the caller can proceed (and ``acquire`` will overwrite the
        stale doc).

        Note: with a per-scope lock, ``is_held(connector)`` (no
        scope_id) only checks the connector-GLOBAL doc, not the per-scope docs —
        so it is NOT a reliable "is any import of this connector running" probe
        anymore. The Report Scheduler status-poll handles this by preferring the
        per-run progress doc and using ``is_held`` only as a brief best-effort
        startup fallback (routes.py api_import_status).

        Soft-fails to ``False`` on ES outage so a transient ES
        problem doesn't block legitimate kickoffs.
        """
        if not connector:
            return False
        es = _es()
        if es is None:
            return False
        doc = _get(es, _lock_doc_id(connector, scope_id))
        if doc is None:
            return False
        expires_at = _parse_iso(doc.get("expires_at"))
        if expires_at is None:
            return False
        if expires_at <= _now():
            return False
        # a held doc whose holder is provably dead reads as not
        # held, so kickoff gates answer the same question acquire will
        # (acquire is what actually breaks/overwrites the doc; this probe
        # stays pure — no writes).
        return not _holder_is_dead(doc)

    @staticmethod
    def is_blocked(connector: str, scope_id: str = "") -> bool:
        """would ``acquire(connector, scope_id)`` lose right now?

        ``is_held`` checks only the exact (connector, scope_id) doc; this
        probe ALSO applies the global↔scoped exclusion rule, so kickoff
        gates can ask the same question the importer's acquire will answer:
        held same-key doc, OR (scoped) a held connector-global lock, OR
        (global) any held scoped lock of the connector.

        Soft-fails to ``False`` on ES outage — same posture as ``is_held``:
        a transient ES problem must not block legitimate kickoffs (the
        importer's own acquire is the real gate).
        """
        if not connector:
            return False
        es = _es()
        if es is None:
            return False
        now = _now()
        doc = _get(es, _lock_doc_id(connector, scope_id))
        if doc is not None:
            expires_at = _parse_iso(doc.get("expires_at"))
            if expires_at is not None and expires_at > now and not _holder_is_dead(doc):
                return True
        return _cross_class_hold(es, connector, scope_id, now) is not None


def _cross_class_hold(es: Any, connector: str, scope_id: str, now: datetime, heal: bool = False) -> dict | None:
    """the unexpired lock doc in the OTHER class that excludes an
    acquire of ``(connector, scope_id)``, or ``None``.

    Scoped acquire (non-empty ``scope_id``) → the connector-GLOBAL doc, if
    held. Global acquire (empty ``scope_id``) → any held per-scope doc of
    the connector, found by scanning ``de_import_locks`` (the index holds at
    most a handful of docs — one per in-flight (connector, scope) — so a
    match_all scan filtered in Python sidesteps the text-vs-keyword mapping
    trap on a dynamically-mapped index entirely).

    a held doc whose holder is provably dead never blocks. With
    ``heal=True`` (acquire's path) the dead doc is also deleted and its
    orphaned progress doc stamped terminal; probes (``is_blocked``) leave
    ``heal`` off so they stay pure reads.

    Soft-fails to ``None`` (no blocker) on any ES error, matching acquire's
    posture: the race this guards is rare, and an import is better than no
    import on a degraded lock service.
    """
    if (scope_id or "").strip():
        global_doc = _get(es, connector)
        if global_doc is not None:
            expires_at = _parse_iso(global_doc.get("expires_at"))
            if expires_at is not None and expires_at > now:
                if not _holder_is_dead(global_doc):
                    return global_doc
                if heal:
                    _break_dead_lock(es, connector, global_doc)
        return None
    try:
        resp = es.search(
            index=LOCK_INDEX,
            body={"query": {"match_all": {}}, "size": 200},
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as exc:  # noqa: BLE001 — boundary; degrade to "no blocker"
        logging.warning("lock: scoped-holds scan failed for %s: %s — proceeding", connector, exc)
        return None
    for hit in (resp.get("hits") or {}).get("hits") or []:
        src = hit.get("_source") or {}
        if src.get("connector") != connector:
            continue
        if not (src.get("scope_id") or "").strip():
            continue
        expires_at = _parse_iso(src.get("expires_at"))
        if expires_at is not None and expires_at > now:
            if not _holder_is_dead(src):
                return src
            if heal:
                _break_dead_lock(es, hit.get("_id") or _lock_doc_id(connector, src.get("scope_id") or ""), src)
    return None


def _get(es: Any, connector: str) -> dict | None:
    """Look up the current lock doc for ``connector``, or ``None``.

    The ES client (elasticsearch 9.x) returns an
    ``elastic_transport.ObjectApiResponse``, NOT a plain ``dict`` — it
    has a mapping interface (``.get``) but fails ``isinstance(resp, dict)``.
    The original ``isinstance(resp, dict)`` guard therefore made this
    helper always return ``None``, silently turning the whole lock
    service into a no-op (is_held always False, release never deleted,
    acquire never blocked). Only ``_source`` needs to be a real dict;
    read it off the response via the mapping interface regardless of the
    response's concrete type.
    """
    try:
        resp = es.get(index=LOCK_INDEX, id=connector)
    except Exception:
        return None
    src = resp.get("_source") if hasattr(resp, "get") else None
    return src if isinstance(src, dict) else None


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# holder liveness (see "Dead-holder self-healing" in the module
# docstring). Everything below is only ever a *positive proof of death*
# detector: any unknowable state returns "alive" so the TTL remains the
# fallback contract.


def _my_hostname() -> str:
    """This process's hostname (== container id inside Docker), or ""."""
    try:
        return socket.gethostname() or ""
    except OSError:
        return ""


def _proc_available() -> bool:
    """Whether /proc exists — liveness is only provable on Linux."""
    return os.path.isdir("/proc")


def _proc_starttime(pid: Any) -> str | None:
    """The starttime ticks of ``pid`` from ``/proc/<pid>/stat``, or ``None``
    when the pid doesn't exist / /proc is unreadable / non-Linux.

    (pid, starttime) is a kernel-unique process identity — stronger than a
    cmdline compare — so a recycled pid can never impersonate the holder.
    The comm field (field 2) may contain spaces/parens, so fields are taken
    after the LAST ')': index 19 there is overall field 22, starttime.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            raw = fh.read().decode("ascii", "replace")
        return raw.rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _pgid_alive(pgid: Any) -> bool:
    """Whether ANY live process belongs to process group ``pgid``.

    The FB importer's page/instagram sub-importers are separate processes in
    the holder's group and keep using
    the shared token after a parent-only death — so the lock must survive
    them. Scans /proc (a container holds a few dozen processes at most);
    an unscannable /proc reports True (assume survivors — conservative).
    """
    try:
        pg = str(int(pgid))
    except (TypeError, ValueError):
        return False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                raw = fh.read().decode("ascii", "replace")
            if raw.rsplit(")", 1)[1].split()[2] == pg:  # field 5, pgrp
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def _holder_identity() -> dict[str, Any]:
    """The identity fields ``acquire`` stamps onto a fresh lock doc."""
    from lib.import_run_context import get_run_id

    return {
        "holder_pid": os.getpid(),
        "holder_pid_start": _proc_starttime(os.getpid()),
        "holder_pgid": os.getpgrp() if hasattr(os, "getpgrp") else None,
        "holder_host": _my_hostname(),
        "holder_run_id": get_run_id(),
    }


def _holder_is_dead(src: dict) -> bool:
    """True only when the lock doc's recorded holder is PROVABLY dead.

    Proof, in order:
      * doc carries no identity (earlier) → alive (TTL is the contract);
      * hostname mismatch → dead — every acquirer lives in the single
        ``imports_container``, so a different hostname means the writing
        container was replaced and its whole PID namespace is gone;
      * no /proc (Windows dev) → alive (unprovable);
      * exact holder pid alive with matching starttime (or no recorded
        starttime to refute it) → alive;
      * pid gone/recycled but a process-group member survives → alive
        (sub-importer children still hold the token surface);
      * otherwise → dead.
    """
    pid = src.get("holder_pid")
    host = (src.get("holder_host") or "").strip()
    if not pid or not host:
        return False
    me = _my_hostname()
    if me and host != me:
        return True
    if not _proc_available():
        return False
    start_now = _proc_starttime(pid)
    if start_now is not None:
        start_doc = src.get("holder_pid_start")
        if start_doc is None or str(start_doc) == str(start_now):
            return False
    pgid = src.get("holder_pgid")
    if pgid is not None and _pgid_alive(pgid):
        return False
    return True


def _stamp_orphaned_run(src: dict) -> None:
    """Stamp a dead holder's progress doc terminal (the ES surgery the
    2026-07-07 incident needed by hand): ``cancelling`` → ``cancelled``
    (honor the user's unobserved cancel), ``pending``/``running`` →
    ``failed``. No-op when the holder had no run_id (cron path) or the doc
    is already terminal (``complete_run``'s guard). Fail-soft — orphan
    bookkeeping must never abort the acquire that triggered it.
    """
    run_id = (src.get("holder_run_id") or "").strip()
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        doc = ImportProgressService.get_run(run_id)
        if not doc or doc.get("status") in ("succeeded", "failed", "cancelled"):
            return
        if doc.get("status") == "cancelling":
            ImportProgressService.complete_run(run_id=run_id, status="cancelled")
        else:
            ImportProgressService.complete_run(
                run_id=run_id,
                status="failed",
                error="orphaned: importer died holding the import lock",
            )
    except Exception as exc:  # noqa: BLE001 — boundary; never abort the acquire
        logging.warning("_stamp_orphaned_run run_id=%r: %s", run_id, exc)


def _break_dead_lock(es: Any, doc_id: str, src: dict) -> None:
    """Delete a dead holder's cross-class lock doc and stamp its orphan
    (, acquire's ``heal`` path — the doc id is not the acquirer's
    own key, so it must be deleted rather than overwritten). Fail-soft.
    """
    logging.warning(
        "lock: breaking cross-class lock %s — holder pid=%r host=%r is dead (expires %s)",
        doc_id,
        src.get("holder_pid"),
        src.get("holder_host"),
        src.get("expires_at"),
    )
    _stamp_orphaned_run(src)
    try:
        es.delete(index=LOCK_INDEX, id=doc_id, refresh="wait_for")
    except Exception as exc:  # noqa: BLE001 — boundary; the TTL still expires it
        logging.warning("lock: delete of dead lock %s failed: %s", doc_id, exc)
