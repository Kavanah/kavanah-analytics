import argparse
import importlib
import os
import sys
import logging
import lib.logging

# Setup Logging
lib.logging.setup_logging()


def main():
    parser = argparse.ArgumentParser(description="Import script runner")

    # Known required arguments
    parser.add_argument("source", help="Source name (e.g., facebook)")
    parser.add_argument("action", help="Action script to run")

    # Parse known arguments and capture the remaining ones
    args, unknown_args = parser.parse_known_args()

    # Convert unknown args to a dictionary
    # Supports both formats:
    #   --key=value (e.g., '--date=2024-10-01')
    #   --key value (e.g., '--start_date 2024-10-01')
    params = {}
    i = 0
    while i < len(unknown_args):
        arg = unknown_args[i]
        if arg.startswith("--"):
            key = arg.lstrip("--")

            # Check if it's --key=value format
            if "=" in key:
                key_value = key.split("=", 1)
                params[key_value[0]] = key_value[1]
                i += 1
            # Check if next arg exists and doesn't start with --
            elif i + 1 < len(unknown_args) and not unknown_args[i + 1].startswith("--"):
                params[key] = unknown_args[i + 1]
                i += 2
            else:
                # Standalone flag like --verbose
                params[key] = True
                i += 1
        else:
            i += 1

    # Dynamically load the appropriate action module
    module_name = f"import_scripts.{args.source}.{args.action}"
    try:
        action_module = importlib.import_module(module_name)
    except ImportError as e:
        print(f"Error importing module {module_name}: {e}")
        # return a non-zero exit so callers (lib.import_runner, the
        # manual Import Data Page) record this as a FAILED import. A bare
        # ``return`` here exited 0, so a bad source/action was reported as a
        # successful no-op.
        return 1

    # Run the action script's main. a ValueError raised by an
    # importer (e.g. lib.import_scope.require_scope when a connector is invoked
    # without its team scope) used to be caught, printed, and swallowed: main()
    # returned normally → exit code 0 → lib.import_runner recorded the failed
    # import as success and the UI showed a silent green no-op. Print the message
    # AND propagate a non-zero exit so the wrapper records success=False and the
    # already-classified user error (record_user_error) surfaces.
    try:
        result = action_module.main(params)
    except ValueError as ve:
        print(f"Error: {ve}")
        return 1

    # honor a FAILURE signalled by return value, not just by raising.
    # This return value used to be discarded, so five connectors whose main()
    # reports failure with a bool (echo, facebook, google_ads, google_analytics,
    # youtube) exited 0 and lib.import_runner recorded success=True. Reproduced
    # on prod 2026-08-04: `youtube run_daily_imports --team_ids does-not-exist`
    # printed nothing and exited 0, even though get_youtube_analytics raised
    # scope_resolution_error correctly — run_daily_imports caught the child's
    # CalledProcessError, returned False, and that False was dropped here.
    #
    # The two dispatch paths disagreed, which is why this stayed hidden: running
    # a connector as `python3 -m import_scripts.<x>.run_daily_imports` hits its
    # own `if __name__` block and does sys.exit(0 if success else 1) — correct.
    # Only the import.py path (the one the Report Scheduler uses) lost it.
    #
    # The check is deliberately NARROW, because main() return conventions across
    # import_scripts are genuinely inconsistent and mutually inverted:
    #   * bools        — True = success  (migrate_team_fb_fields, run_daily_imports)
    #   * exit codes   — 0 = success     (verify_site_link: `return 0 if ok else 1`)
    #   * strings      — "success"       (several migration scripts)
    #   * dicts/None   — a report, or nothing at all
    # `if result:` would read `return 0` as failure; `if not result:` would read
    # `return {}` as failure. So ONLY an explicit False or a non-zero int counts
    # as failure; None / True / 0 / strings / dicts all remain success. Note
    # `isinstance(True, int)` is True in Python, so the bool check must come
    # first — otherwise `return True` would be compared against 0 as an int.
    if result is False:
        print("Error: import reported failure (main() returned False)")
        return 1
    if isinstance(result, int) and not isinstance(result, bool) and result != 0:
        print(f"Error: import reported failure (main() returned {result})")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
