"""Task 1 — verify DT Site-to-Site Link auth + capture field shape.

Standalone, no-dependency-on-the-importer probe. Run it BEFORE building the
pull importer to confirm, against a live Disciple.Tools site:

  1. Site-to-Site Link auth works (the hourly-rotating transfer token is accepted).
  2. The `dt-posts/v2` namespace is reachable.
  3. `GET /wp-json/dt-posts/v2/contacts?limit=1` returns `{posts, total}`.
  4. The contact (and optionally group) field shape, dumped for the mapping spec.

Site-to-Site Link is configured in wp-admin -> Site Links on the DT site:
  - Site 1 / Site 2 = the two domains (DT + our remote), in whatever order the
    admin entered them. It issues a `token`.

The Authorization header is NOT the raw token. It is an hourly-rotating bearer.
Matching the DT theme verbatim (dt-core/admin/site-link-post-type.php):

    site_key       = md5(token + site1 + site2)          # domains: scheme+slashes stripped
    transfer_token = md5(site_key + gmdate('Y-m-dH'))    # UTC, e.g. 2026-06-0518
    Authorization: Bearer <transfer_token>

DT accepts the previous, current, and next UTC hour (±1h tolerance). The two
domains are concatenated in the order they were saved as site1/site2, which an
external caller can't know — so we try both orderings.

Usage:
    python3 verify_site_link.py \
        --dt-domain dt.example.org \
        --our-domain dashboard.example.org \
        --token <site-link-token> \
        [--dump-groups] [--out /tmp/dt_shape.json]

Nothing here writes to Elasticsearch. It only reads from DT and prints/saves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


def _norm_domain(domain: str) -> str:
    """Strip scheme and all slashes, matching DT's save-time normalization."""
    d = domain.replace("https://", "").replace("http://", "").replace("//", "")
    return d.replace("/", "").strip()


def candidate_transfer_tokens(token: str, dt_domain: str, our_domain: str) -> list[tuple[str, str]]:
    """All bearer tokens DT could accept right now.

    Mirrors dt-core/admin/site-link-post-type.php:
      site_key       = md5(token + site1 + site2)
      transfer_token = md5(site_key + gmdate('Y-m-dH'))  with prev/current/next hour.
    site1/site2 order is unknown to an external caller, so we emit both orderings.
    Returns (label, transfer_token) pairs.
    """
    a, b = _norm_domain(dt_domain), _norm_domain(our_domain)
    now = datetime.now(timezone.utc)
    hours = {
        "current": now,
        "prev": now - timedelta(hours=1),
        "next": now + timedelta(hours=1),
    }
    out: list[tuple[str, str]] = []
    for order_label, (s1, s2) in {"dt+our": (a, b), "our+dt": (b, a)}.items():
        # nosemgrep: python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5
        # MD5 is the Disciple.Tools Site-to-Site Link wire protocol (DT's PHP
        # side computes the same hash and compares), NOT a security control.
        # Changing the algorithm breaks the handshake. See client.py:_bearer.
        site_key = hashlib.md5((token + s1 + s2).encode("utf-8")).hexdigest()  # nosemgrep
        for hour_label, dt_ in hours.items():
            stamp = dt_.strftime("%Y-%m-%d%H")  # PHP date('Y-m-dH')
            tt = hashlib.md5((site_key + stamp).encode("utf-8")).hexdigest()  # nosemgrep
            out.append((f"{order_label}/{hour_label}", tt))
    return out


def dt_get(url: str, transfer_token: str) -> tuple[int, dict | None, str]:
    """GET a DT REST URL with the site-link bearer. Returns (status, json, raw)."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {transfer_token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except urllib.error.URLError as e:
        return 0, None, f"URLError: {e.reason}"

    try:
        return status, json.loads(raw), raw
    except json.JSONDecodeError:
        return status, None, raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify DT Site-to-Site Link auth + field shape")
    parser.add_argument("--dt-domain", required=True, help="DT site domain, no scheme (e.g. dt.example.org)")
    parser.add_argument("--our-domain", required=True, help="Our domain registered in the Site Link")
    parser.add_argument("--token", required=True, help="Site-to-Site Link token from wp-admin")
    parser.add_argument("--dump-groups", action="store_true", help="Also probe the groups post-type")
    parser.add_argument("--out", default="", help="Optional path to write the captured field shape JSON")
    args = parser.parse_args()

    base = f"https://{args.dt_domain}/wp-json"
    print(f"[i] UTC hour {datetime.now(timezone.utc):%Y-%m-%d %H}:00 — trying both site1/site2 orderings\n")

    # Step 1 — find a bearer DT accepts (unknown domain order + ±1h tolerance).
    probe_url = f"{base}/dt-posts/v2/contacts?limit=1"
    transfer_token = ""
    for label, candidate in candidate_transfer_tokens(args.token, args.dt_domain, args.our_domain):
        status, _, _ = dt_get(probe_url, candidate)
        print(f"[>] auth probe [{label}] -> status={status}")
        if status == 200:
            transfer_token = candidate
            print(f"[=] site-link auth OK via ordering/hour: {label}\n")
            break
    if not transfer_token:
        print("\n[!] VERIFY FAILED — no candidate token accepted (still 401).")
        print("    Re-check the token, and confirm both domains entered in the Site Link.")
        return 1

    captured: dict = {}
    ok = True

    # Step 2/3 — namespace reachability + list shape, one call.
    for post_type in ["contacts", "groups"] if args.dump_groups else ["contacts"]:
        url = f"{base}/dt-posts/v2/{post_type}?limit=1"
        print(f"[>] GET {url}")
        status, body, raw = dt_get(url, transfer_token)

        if status == 200 and isinstance(body, dict) and "posts" in body:
            total = body.get("total")
            posts = body.get("posts") or []
            print(f"    OK  status=200  total={total}  returned={len(posts)}")
            sample = posts[0] if posts else {}
            captured[post_type] = {
                "total": total,
                "top_level_keys": sorted(sample.keys()),
                "sample": sample,
            }
            if sample:
                print(f"    top-level fields: {', '.join(sorted(sample.keys()))}")
        else:
            ok = False
            snippet = raw[:400].replace("\n", " ")
            print(f"    FAIL status={status}  body={snippet}")
            if status == 401:
                print("    -> 401: site-link auth rejected. Check token, both domain strings,")
                print("            and that the Site Link is enabled for a non-DT system.")

    if args.out and captured:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(captured, f, indent=2, ensure_ascii=False)
        print(f"\n[i] Field shape written to {args.out}")

    print("\n[=] VERIFY OK" if ok else "\n[!] VERIFY FAILED — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
