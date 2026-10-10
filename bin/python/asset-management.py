import importlib.util
import os
import sys

# migration state moved from a per-container JSON file
# (/app/reporting_configs/asset_management_version_updates.json) to the
# de_meta_migrations ES marker index (lib/migration_state). The file only ever
# existed where someone ran this interactive CLI — on prod it never existed at
# all, which is exactly how three migrations went silently unapplied (v16/v22/
# v27, found 2026-08-26; same class as the incident). The marker lives
# in the same cluster the migrations mutate, so the environment is implicit
# and the state cannot be read against the wrong env.
#
# Non-interactive modes (runbook-friendly exit codes):
#   asset-management.py --migrations-status        # 0 = none pending, 1 = pending, 2 = state unknown
#   asset-management.py --apply-pending            # run pending in order, record markers
#   asset-management.py --mark-applied-through N [--note "..."]   # backfill markers, run nothing

# Hardcoded current version of the application
current_version = 1


def main():
    # Run the update precheck to ensure all necessary updates are applied
    check_for_updates()

    # Proceed to the normal menu logic
    from asset_management.menus import MenuManager

    menu_manager = MenuManager()
    menu_manager.run()


def _es_or_exit():
    """An ES client, or a hard exit — migration state must never be guessed.

    Fail-CLOSED: with ES unreachable the applied-set is UNKNOWN; falling back
    to 'nothing applied' would re-run every migration, and 'everything
    applied' would silently skip pending ones. Exit 2 distinguishes this from
    the pending(1)/clean(0) outcomes for runbook use."""
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Elasticsearch unavailable — migration state UNKNOWN. Refusing to guess.")
        sys.exit(2)
    return es


def migrations_status() -> int:
    """Print applied-vs-pending for this cluster. Exit codes: 0 clean, 1 pending, 2 unknown."""
    from lib import migration_state

    es = _es_or_exit()
    st = migration_state.migration_status(es)
    if st.get("unknown"):
        print("ERROR: could not read the de_meta_migrations marker index — state UNKNOWN.")
        return 2
    applied = st["applied"]
    print(f"Migrations: {len(st['available'])} shipped, {len(applied)} recorded applied, {len(st['pending'])} pending.")
    for v in st["available"]:
        marker = applied.get(v)
        if marker:
            print(
                f"  update_v{v:<3} applied  {str(marker.get('applied_at', ''))[:19]}  method={marker.get('method', '?')}  by={marker.get('applied_by', '?')}"
            )
        else:
            print(f"  update_v{v:<3} PENDING")
    return 1 if st["pending"] else 0


def apply_pending() -> int:
    """Run every pending migration in version order, recording a marker per
    success. A skip/abort/failure stops recording for that version but
    continues to the next (matching the old menu behavior)."""
    from lib import migration_state

    es = _es_or_exit()
    st = migration_state.migration_status(es)
    if st.get("unknown"):
        print("ERROR: migration state UNKNOWN — fix ES access before applying anything.")
        return 2
    if not st["pending"]:
        print("No pending migrations.")
        return 0
    paths = dict(get_update_scripts())
    failures = 0
    for version in st["pending"]:
        path = paths.get(version)
        if not path:
            print(f"update_v{version}: listed as pending but no script file found — skipping.")
            failures += 1
            continue
        if run_update(version, path):
            ok = migration_state.mark_applied(es, version, method="runner", applied_by=os.environ.get("USER", ""))
            if not ok:
                print(f"update_v{version}: RAN but the marker write FAILED — record it by hand (--mark-applied-through).")
                failures += 1
        else:
            failures += 1
    return 1 if failures else 0


def mark_applied_through(max_version: int, note: str = "") -> int:
    """Backfill markers for update_v1..v<max_version> WITHOUT running anything —
    for environments whose migrations were verified applied out-of-band (the
    2026-08-26 prod audit trail lives on)."""
    from lib import migration_state

    es = _es_or_exit()
    st = migration_state.migration_status(es)
    if st.get("unknown"):
        print("ERROR: migration state UNKNOWN — fix ES access before backfilling markers.")
        return 2
    marked = 0
    for version in st["available"]:
        if version <= max_version and version not in st["applied"]:
            if migration_state.mark_applied(es, version, method="backfill", applied_by=os.environ.get("USER", ""), note=note):
                marked += 1
            else:
                print(f"update_v{version}: marker write failed.")
                return 1
    print(f"Backfilled {marked} marker(s) through v{max_version}.")
    return 0


def check_for_updates():
    """Menu-path precheck: apply any unapplied updates (now ES-marker-backed).

    Interactive history: this ran on every CLI launch, reading the JSON state
    file. Same behavior, shared store — the markers this writes are the ones
    --migrations-status and the Status Reports panel read."""
    rc = apply_pending()
    if rc == 2:
        # apply_pending already printed why; the menu is unusable without ES anyway.
        sys.exit(2)


def get_update_scripts():
    """Finds all available update scripts in the updates directory."""
    from lib import migration_state

    update_scripts = []
    update_dir = migration_state.updates_dir()

    for filename in os.listdir(update_dir):
        if filename.startswith("update_v") and filename.endswith(".py"):
            version_str = filename.split("_v")[-1].replace(".py", "")
            try:
                version_num = int(version_str)
                update_scripts.append((version_num, os.path.join(update_dir, filename)))
            except ValueError:
                continue

    return sorted(update_scripts)  # Sort by version number


def run_update(update_version, update_path):
    """Dynamically imports and runs the update script."""
    print(f"Running update version {update_version}...")

    # Dynamically import the update script
    spec = importlib.util.spec_from_file_location(f"update_v{update_version}", update_path)
    update_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update_module)

    # Check if the update is interactive or quiet
    if hasattr(update_module, "run_update"):
        result = update_module.run_update()  # Run the update function and get the result
        if result == "skip" or result == "abort":
            print(f"Update v{update_version} was skipped or aborted by the user.")
            return False  # Do not record the update
        return True
    else:
        print(f"Update v{update_version} does not have a run_update function.")
        return False  # In case the update script is missing the required function


def _parse_cli(argv):
    """Return (mode, arg, note) for the non-interactive modes, or
    (None, None, "") for the interactive menu."""
    if "--migrations-status" in argv:
        return ("status", None, "")
    if "--apply-pending" in argv:
        return ("apply", None, "")
    if "--mark-applied-through" in argv:
        idx = argv.index("--mark-applied-through")
        try:
            max_version = int(argv[idx + 1])
        except (IndexError, ValueError):
            return ("error", None, "")
        note = ""
        if "--note" in argv:
            nidx = argv.index("--note")
            note = argv[nidx + 1] if nidx + 1 < len(argv) else ""
        return ("mark", max_version, note)
    return (None, None, "")


if __name__ == "__main__":
    mode, arg, note = _parse_cli(sys.argv[1:])
    if mode == "status":
        sys.exit(migrations_status())
    elif mode == "apply":
        sys.exit(apply_pending())
    elif mode == "mark":
        sys.exit(mark_applied_through(arg, note))
    elif mode == "error":
        print('Usage: --mark-applied-through <N> [--note "..."]')
        sys.exit(2)
    else:
        main()
