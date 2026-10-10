"""Audit FB app rows in ``de_asset_fb_apps`` against the Meta Graph API.

For each row we hit ``GET /{app-id}`` with an app access token
(``{app_id}|{app_secret}``) and report what Meta returns. Use this when
you have a pile of FB App IDs registered in Kavanah Media and want to
know which ones still talk to Meta, what type they are, and where to
go in the App Dashboard to check their App Review state.

**What this tool can tell you (Graph API):**
- The app exists and our stored secret is valid (200 from Graph).
- App ``name``, ``category``, ``app_type``, and dashboard link.
- Whether the app has currently-installed permissions (a weak signal
  of "configured").

**What this tool cannot tell you (Meta dashboard only):**
- App Review status per permission (Approved / In Review / Not approved).
- The Live-Mode toggle. Meta dropped the ``mode`` / ``live`` fields from
  the Graph API in v4+; there is no public endpoint that returns it.

So the output for every row includes a ``dashboard_url`` you can open to
confirm review status manually.

Usage (inside imports_container — same pattern as the other tools in
this directory)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.fb_app_status_audit

    # JSON output instead of a table (pipe-friendly):
    docker exec -w /app/python imports_container python3 \\
        -m tools.fb_app_status_audit --json

    # Audit a single app by ID (skip the ES sweep):
    docker exec -w /app/python imports_container python3 \\
        -m tools.fb_app_status_audit --app-id 1234567890
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests

# Three-candidate resolver — matches account_type_backfill.py and
# connector_notify_list.py so this works from a repo-local pytest run,
# a `python -m` invocation from bin/python, and inside imports_container.
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

from lib.encryption import decrypt  # noqa: E402
from lib.es_helpers import get_all_documents  # noqa: E402
from lib.fb_constants import FB_GRAPH_URL  # noqa: E402

_INDEX = "de_asset_fb_apps"
_GRAPH_TIMEOUT = 10  # seconds — these are tiny GET calls, no need to wait long.


def _decrypt_or_none(value: str | None) -> str | None:
    """Decrypt or return None if value missing / undecryptable.

    Some rows have a raw token stored (older imports) and some have an
    encrypted one. Try decrypt first, fall through to the raw value if
    decrypt fails — Fernet raises on non-Fernet input.
    """
    if not value:
        return None
    try:
        return decrypt(value)
    except Exception:  # noqa: BLE001 — Fernet raises a typed error we don't import
        # If it doesn't look like base64 ciphertext, assume it's raw.
        return value


def _probe_app(app_id: str, app_secret: str) -> dict[str, Any]:
    """Hit Graph for one app and return a normalized result dict.

    Keys returned (always present, may be ``None``):
      ok, http_status, name, category, app_type, link, error,
      permissions_count, dashboard_url
    """
    out: dict[str, Any] = {
        "ok": False,
        "http_status": None,
        "name": None,
        "category": None,
        "app_type": None,
        "link": None,
        "error": None,
        "permissions_count": None,
        "dashboard_url": f"https://developers.facebook.com/apps/{app_id}/",
    }
    access_token = f"{app_id}|{app_secret}"

    # Step 1 — app metadata. This confirms the (app_id, secret) pair works.
    try:
        resp = requests.get(
            f"{FB_GRAPH_URL}/{app_id}",
            params={
                "access_token": access_token,
                "fields": "id,name,category,link,app_type",
            },
            timeout=_GRAPH_TIMEOUT,
        )
    except requests.RequestException as e:
        # the exception text repeats the request URL, whose
        # access_token is "<app_id>|<app_secret>"; report the class only.
        out["error"] = f"network error: {type(e).__name__}"
        return out

    out["http_status"] = resp.status_code
    try:
        body = resp.json()
    except ValueError:
        out["error"] = f"non-JSON response: {resp.text[:200]}"
        return out

    if resp.status_code != 200:
        meta_err = body.get("error", {}) if isinstance(body, dict) else {}
        out["error"] = meta_err.get("message") or f"http {resp.status_code}: {body}"
        return out

    out["ok"] = True
    out["name"] = body.get("name")
    out["category"] = body.get("category")
    out["app_type"] = body.get("app_type")
    out["link"] = body.get("link")

    # Step 2 — currently-installed permissions. This is a weak "is the
    # app configured at all" signal. It does NOT carry review status.
    try:
        perm_resp = requests.get(
            f"{FB_GRAPH_URL}/{app_id}/permissions",
            params={"access_token": access_token},
            timeout=_GRAPH_TIMEOUT,
        )
        if perm_resp.status_code == 200:
            perm_body = perm_resp.json()
            data = perm_body.get("data", []) if isinstance(perm_body, dict) else []
            out["permissions_count"] = len(data)
    except requests.RequestException:
        # Don't fail the whole row over the optional second probe.
        pass

    return out


def _load_rows_from_es() -> list[dict[str, Any]]:
    """Pull every de_asset_fb_apps row. Returns raw _source dicts plus _id."""
    raw = get_all_documents(_INDEX) or []
    rows: list[dict[str, Any]] = []
    for hit in raw:
        src = hit.get("_source", {}) if isinstance(hit, dict) else {}
        src["_id"] = hit.get("_id") if isinstance(hit, dict) else None
        rows.append(src)
    return rows


def _audit_row(row: dict[str, Any]) -> dict[str, Any]:
    """Audit one ES row. Returns a flat dict ready for table or JSON."""
    app_id = (row.get("fb_app_id") or "").strip()
    title = row.get("fb_app_title") or "(no title)"
    secret_enc = row.get("fb_app_secret")
    secret = _decrypt_or_none(secret_enc)

    base = {
        "esid": row.get("_id"),
        "title": title,
        "app_id": app_id,
    }
    if not app_id:
        return {**base, "ok": False, "error": "fb_app_id missing", "dashboard_url": None}
    if not secret:
        return {
            **base,
            "ok": False,
            "error": "fb_app_secret missing or undecryptable",
            "dashboard_url": f"https://developers.facebook.com/apps/{app_id}/",
        }

    probe = _probe_app(app_id, secret)
    return {**base, **probe}


def _print_table(results: list[dict[str, Any]]) -> None:
    """Render the audit as an aligned ASCII table.

    Columns: status, title, app_id, name, app_type, perms, error/dashboard.
    Failed probes show the error; successful ones show the dashboard URL
    so the operator can click through to verify review status.
    """
    rows = []
    for r in results:
        status = "OK" if r.get("ok") else "FAIL"
        detail = r.get("error") or r.get("dashboard_url") or ""
        rows.append(
            (
                status,
                (r.get("title") or "")[:30],
                r.get("app_id") or "",
                (r.get("name") or "")[:30],
                r.get("app_type") or "",
                str(r.get("permissions_count") if r.get("permissions_count") is not None else ""),
                detail,
            )
        )

    headers = ("Status", "Title", "App ID", "Graph Name", "Type", "Perms", "Detail / Dashboard")
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i]) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*row))

    print()
    print(
        "Note: 'OK' means our stored secret authenticates to Graph and the app is reachable.\n"
        "      It does NOT confirm App Review status or Live-Mode — Meta doesn't expose those\n"
        "      over the Graph API. Open each dashboard URL above to verify those manually."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fb_app_status_audit",
        description="Audit de_asset_fb_apps rows against the Meta Graph API.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    parser.add_argument(
        "--app-id",
        help="Audit a single FB app by id, looked up in ES (skips full sweep).",
    )
    args = parser.parse_args(argv)

    rows = _load_rows_from_es()
    if args.app_id:
        rows = [r for r in rows if (r.get("fb_app_id") or "").strip() == args.app_id.strip()]
        if not rows:
            print(f"no de_asset_fb_apps row found for fb_app_id={args.app_id!r}", file=sys.stderr)
            return 2

    results = [_audit_row(r) for r in rows]

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        _print_table(results)

    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
