"""
update_v15.py — RETIRED (no-op). Originally: uncheck ds_* checkboxes where no
asset is selected.

 (2026-06-19): this migration is permanently disabled. It is left in
place only so the version sequence and the applied-updates state file stay
intact; it must never run its original logic again.

Why it was retired:

* It decided "no asset selected" by inspecting a hard-coded set of LEGACY team
  fields (``team_facebook_page_id``, ``team_youtube_channel_handle``,
  ``team_ga4_properties``, ``team_google_ads_integration_status``,
  ``ig_account_id``). The ESID migration moved the linked-asset data to
  ``*_esid`` fields and emptied those legacy fields. So on current data this
  migration would read EVERY wired connector as "empty" and set ``ds_*=false``,
  bulk-unchecking live connectors — the exact failure mode fixed in the
  team form (the read-only ``ds_*`` fields were being deleted on save).

* ``update_v*`` scripts auto-run via ``asset-management.py``'s
  ``check_for_updates`` and are recorded in
  ``/app/reporting_configs/asset_management_version_updates.json``. v15 has
  already run on prod/staging and is recorded as applied, so it will not re-run
  in normal operation. But if that state file is ever lost or reset (ES/data
  migration state is runtime-only and invisible to git — see CLAUDE.md /),
  a flag-UNCHECKING migration re-running against current data is a data-loss
  hazard. Neutering it removes that hazard entirely.

The flag state is now maintained correctly by the app and
the one-time data repair. Nothing should ever bulk-uncheck ``ds_*``
flags again; if a connector is genuinely unlinked, the per-team edit / disconnect
path handles it.
"""

import logging
import os
from datetime import datetime

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v15] %(message)s")


def run_update():
    """Retired no-op. Returns "success" so the version stays recorded as applied
    (and never re-attempts the original destructive logic), without touching any
    team document. See module docstring /.
    """
    msg = "update_v15 is RETIRED: no-op, no team documents modified."
    logging.info(msg)
    print(msg)
    return "success"


if __name__ == "__main__":
    run_update()
