"""
One-time migration: rewrite ES security roles space:team-analytics -> space:default.

Runs against the Elasticsearch on the SAME host (uses https://localhost:9200).
Idempotent: a re-run after success is a no-op (prints "Already migrated").

WHEN TO RUN
-----------
Before or immediately after deploying the commit "feat(kibana): consolidate to
default space (Kavanah Analytics)" to a new environment. The code in that
commit assumes every org_*/team_* security role grants access to space:default.
Without this migration, all non-admin Kibana access breaks on that environment.

Already done on:
  - dev-reporting (2026-04-27)

Still needed on:
  - production (kavanahanalytics.com)
  - any other long-lived environment that had team-analytics roles

USAGE
-----
SSH to the target server, then:

    PASS=$(sudo docker inspect imports_container \
        --format '{{range .Config.Env}}{{println .}}{{end}}' \
        | grep ES01_PASSWORD= | cut -d= -f2-)

    # Dry run first — shows count and a sample of changes:
    ESPASS="$PASS" python3 deployment/migrate-roles-to-default-space.py --dry-run

    # If the count looks right, execute:
    ESPASS="$PASS" python3 deployment/migrate-roles-to-default-space.py

PRESERVES per-role privilege sets — does NOT normalize. A role with
['space_read'] keeps space_read; a role with ['feature_dashboard.read',
'feature_discover.read'] keeps both. Only the resources list is rewritten.

VERIFICATION
------------
At the end the script re-fetches all roles and confirms zero references
to team-analytics remain. Exit 0 = clean.

ROLLBACK
--------
Edit the SOURCE constant below: swap the two strings, re-run. The script
is symmetric.
"""
import json, os, urllib.request, urllib.error, urllib.parse, base64, ssl, sys

SOURCE = "space:team-analytics"
TARGET = "space:default"

PASS = os.environ.get("ESPASS")
if not PASS:
    print("error: set ESPASS environment variable to the elastic user password", file=sys.stderr)
    sys.exit(2)

auth = "Basic " + base64.b64encode(f"elastic:{PASS}".encode()).decode()
ES = "https://localhost:9200"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def call(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    headers = {"Authorization": auth, "Content-Type": "application/json"}
    req = urllib.request.Request(ES + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


dry_run = "--dry-run" in sys.argv
status, roles = call("GET", "/_security/role")
if status != 200:
    print(f"error: GET /_security/role returned {status}: {roles}", file=sys.stderr)
    sys.exit(1)

to_update = []
for name, role in roles.items():
    apps = role.get("applications", [])
    new_apps = []
    changed = False
    for a in apps:
        a2 = json.loads(json.dumps(a))
        new_resources = []
        for r in a2.get("resources", []):
            if r == SOURCE:
                new_resources.append(TARGET)
                changed = True
            else:
                new_resources.append(r)
        # de-dup (in case the role already had both)
        seen = []
        for r in new_resources:
            if r not in seen:
                seen.append(r)
        a2["resources"] = seen
        new_apps.append(a2)
    if changed:
        to_update.append((name, role, new_apps))

print(f"Roles needing update: {len(to_update)}")
if not to_update:
    print("Already migrated. No-op.")
    sys.exit(0)

if dry_run:
    print("DRY RUN — would update:")
    for name, _, new_apps in to_update[:5]:
        sample_resources = new_apps[0].get("resources") if new_apps else []
        print(f"  {name} -> resources={sample_resources}")
    if len(to_update) > 5:
        print(f"  ... and {len(to_update)-5} more")
    sys.exit(0)

failed = []
for name, role, new_apps in to_update:
    body = {
        "cluster": role.get("cluster", []),
        "indices": role.get("indices", []),
        "applications": new_apps,
        "run_as": role.get("run_as", []),
        "metadata": role.get("metadata", {}),
        "transient_metadata": role.get("transient_metadata", {}),
    }
    for k in ("global", "remote_indices", "remote_cluster"):
        if k in role:
            body[k] = role[k]
    enc = urllib.parse.quote(name, safe="")
    code, resp = call("PUT", f"/_security/role/{enc}", body)
    if code in (200, 201):
        print(f"  [OK]   {name}")
    else:
        print(f"  [FAIL] {name}: {code} {resp}")
        failed.append(name)

print()
print(f"Updated: {len(to_update) - len(failed)}/{len(to_update)}")
if failed:
    print(f"Failed: {failed}", file=sys.stderr)
    sys.exit(1)

# Verify no role still references the source
status, roles2 = call("GET", "/_security/role")
remaining = [n for n, r in roles2.items() if SOURCE in json.dumps(r)]
if remaining:
    print(f"warn: {len(remaining)} roles still reference {SOURCE}: {remaining[:10]}", file=sys.stderr)
    sys.exit(1)
print(f"verified: no roles reference {SOURCE}.")
