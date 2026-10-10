"""One-shot cleanup: clear dead ``de_asset_fb_apps.fb_app_token`` values.

``fb_app_token`` is vestigial. No importer authenticates with it — both the pages
importer (``get_page_insights.py``, via ``fb_app_map``) and the ads importer
(``get_insights.py``, via ``get_fb_app_user_token``) read
``de_asset_fb_app_users.fb_app_user_token`` instead. The only consumer was
``check_fb_token``, which repointed at the app_user rows.

While it was the consumer, the field produced a **daily false alarm**: a dead value
raised OAuth code 190, and ``_maybe_record_expired_fb_token`` recorded a
``connection_expired`` row naming an app whose real credentials were fine. Prod
carried this every scheduled Meta run since March 2026.

Prod audit 2026-08-27 — what the field actually held across the 8 app rows:

* six rows: a 32-char value, which is an app **secret**, not a token. Meta:
  "Invalid OAuth access token - Cannot parse access token".
* one agency app row: a USER token that expired 2026-03-20 (data access
  lapsed 2026-04-19). This is the one that generated the daily alarm.
* 'Kavanah Analytics Unified': an APP token (``{app_id}|{app_secret}``, 49 chars).
  Valid forever, ``scopes: []`` — it authenticates as the app and can read no
  insights data. Harmless, and NOT cleared by this tool (see below).

Conservative by design: this tool clears **only** values Meta provably rejects
(``is_valid: false``, or an error on ``/debug_token``). Anything Meta reports as
valid is left in place and reported, so a credential we have not understood is
never destroyed by a cleanup pass. On the 2026-08-27 prod data that means the 7
dead values are cleared and the Unified app token is retained — which matches
 "delete the 7 dead legacy app tokens" exactly.

Never prints a token value (CLAUDE.md burn patterns #26/#37) — only length, type,
and validity verdict.

Usage::

    # Dry-run (default) — report what would be cleared, write nothing.
    python3 /app/python/tools/clear_stale_fb_app_tokens.py

    # Apply the writes.
    python3 /app/python/tools/clear_stale_fb_app_tokens.py --apply
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")

import requests  # noqa: E402

from lib.encryption import decrypt  # noqa: E402
from lib.es_helpers import (  # noqa: E402
    get_all_documents,
    get_document_by_id,
    edit_document,
)

FIELD = "fb_app_token"
INDEX = "de_asset_fb_apps"


def _debug_token(token: str, app_id: str, app_secret: str) -> dict:
    """Return ``/debug_token``'s ``data`` payload, or a synthetic error dict.

    Fail-CLOSED on any transport/parse problem: an unreachable Graph API must
    never be read as "this token is dead", because that would let a network
    blip delete live credentials. Callers treat ``unknown=True`` as "leave it".
    """
    try:
        r = requests.get(
            "https://graph.facebook.com/debug_token",
            params={"input_token": token, "access_token": f"{app_id}|{app_secret}"},
            timeout=20,
        )
        body = r.json() or {}
    except Exception as e:  # noqa: BLE001 — network/parse
        return {"unknown": True, "reason": f"{type(e).__name__}"}
    if "error" in body:
        # Top-level error means our app token was rejected, which tells us
        # nothing definitive about the value under test.
        return {"unknown": True, "reason": str(body["error"].get("message"))}
    data = body.get("data")
    if not isinstance(data, dict):
        return {"unknown": True, "reason": "no data field"}
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry-run)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"clear_stale_fb_app_tokens — {mode}\n")

    docs = get_all_documents(INDEX) or []
    if not docs:
        print(f"No documents in {INDEX}. Nothing to do.")
        return 0

    cleared = kept_valid = kept_unknown = empty = 0

    for doc in docs:
        esid = doc["_id"]
        src = doc["_source"]
        title = src.get("fb_app_title") or "(untitled)"
        app_id = src.get("fb_app_id") or ""
        enc = src.get(FIELD) or ""

        if not enc:
            empty += 1
            continue

        token = decrypt(enc) or ""
        secret = decrypt(src.get("fb_app_secret") or "") or ""
        if not token:
            print(f"  {title!r} ({esid}): {FIELD} decrypted empty — treating as already clear")
            empty += 1
            continue

        if not app_id or not secret:
            kept_unknown += 1
            print(f"  {title!r} ({esid}): no app_id/app_secret to validate against — KEPT (len {len(token)})")
            continue

        data = _debug_token(token, app_id, secret)
        if data.get("unknown"):
            kept_unknown += 1
            print(f"  {title!r} ({esid}): could not validate ({data.get('reason')}) — KEPT (len {len(token)})")
            continue

        is_valid = data.get("is_valid")
        ttype = data.get("type")
        if is_valid:
            kept_valid += 1
            print(
                f"  {title!r} ({esid}): VALID type={ttype!r} scopes={data.get('scopes') or []} "
                f"— KEPT (len {len(token)}). Not used by any importer, but not provably dead."
            )
            continue

        err = (data.get("error") or {}).get("message") or "is_valid=false"
        print(f"  {title!r} ({esid}): DEAD type={ttype!r} — {err} (len {len(token)})")

        if not args.apply:
            cleared += 1
            continue

        current = get_document_by_id(INDEX, esid)
        if current is None:
            print(f"      !! could not re-read {esid}; skipping")
            continue
        current[FIELD] = ""
        if edit_document(INDEX, esid, current):
            # Assert the post-condition rather than trusting the return value
            # (CLAUDE.md: a self-check must read back the SPECIFIC change).
            verify = get_document_by_id(INDEX, esid) or {}
            if verify.get(FIELD):
                print(f"      !! write reported success but {FIELD} is still set on {esid}")
            else:
                cleared += 1
                print(f"      cleared {FIELD} on {esid} (verified empty on read-back)")
        else:
            print(f"      !! edit_document failed for {esid}")

    verb = "cleared" if args.apply else "would clear"
    print(
        f"\n{len(docs)} app row(s): {verb} {cleared}, kept-valid {kept_valid}, "
        f"kept-unvalidatable {kept_unknown}, already-empty {empty}"
    )
    if not args.apply:
        print("Dry-run — nothing was written. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
