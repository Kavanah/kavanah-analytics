"""
Diagnostic script: find orphaned Facebook assets in the ESID chain.

Walks the full chain (fb_accounts → fb_apps → fb_ad_accounts) and reports:
  - fb_accounts with no fb_apps
  - fb_apps with no fb_ad_accounts
  - fb_ad_accounts with no fb_app_user (can't call FB API)
  - fb_apps whose fb_account_esid points to a missing fb_account

Also cross-references against teams via page_id_to_team where possible.

Usage (inside container):
    python3 /app/python/import.py facebook diagnose_orphaned_assets
"""

import logging
import lib.logging
from import_scripts.facebook.lib.assets import (
    get_fb_accounts,
    get_fb_apps,
    get_fb_ad_accounts,
    get_fb_app_users,
    get_fb_teams,
)


def main(params=None):
    lib.logging.define("fb-diagnose-orphans")

    # ── Load all assets ──
    all_accounts = get_fb_accounts()
    all_apps = get_fb_apps()
    all_ad_accounts = get_fb_ad_accounts()
    all_app_users = get_fb_app_users()
    all_teams = get_fb_teams()

    # Build lookup maps
    account_by_id = {doc["_id"]: doc["_source"] for doc in all_accounts}
    app_by_id = {doc["_id"]: doc["_source"] for doc in all_apps}
    ad_account_by_id = {doc["_id"]: doc["_source"] for doc in all_ad_accounts}
    app_user_by_id = {doc["_id"]: doc["_source"] for doc in all_app_users}

    # Reverse maps: parent_esid → list of children
    apps_by_account_esid = {}
    for doc in all_apps:
        parent = doc["_source"].get("fb_account_esid", "")
        apps_by_account_esid.setdefault(parent, []).append(doc)

    ad_accounts_by_app_esid = {}
    for doc in all_ad_accounts:
        parent = doc["_source"].get("fb_app_esid", "")
        ad_accounts_by_app_esid.setdefault(parent, []).append(doc)

    # Build page_id → team mapping
    page_id_to_team = {}
    for team_doc in all_teams:
        team = team_doc["_source"]
        team["_id"] = team_doc["_id"]
        page_ids = team.get("team_facebook_page_id", [])
        if isinstance(page_ids, list):
            for pid in page_ids:
                page_id_to_team[str(pid)] = team
        elif page_ids:
            page_id_to_team[str(page_ids)] = team

    # ── Helpers ──
    def team_label(team):
        if not team:
            return "(no team)"
        return f"{team.get('team_title', 'Unknown')} [esid={team.get('_id', '?')}]"

    sep = "=" * 70

    # ── Report ──
    print(f"\n{sep}")
    print("FACEBOOK ASSET CHAIN DIAGNOSTIC")
    print(f"{sep}")
    print(f"  fb_accounts:     {len(all_accounts)}")
    print(f"  fb_apps:         {len(all_apps)}")
    print(f"  fb_ad_accounts:  {len(all_ad_accounts)}")
    print(f"  fb_app_users:    {len(all_app_users)}")
    print(f"  teams:           {len(all_teams)}")
    print(f"  page→team maps:  {len(page_id_to_team)}")
    print()

    # ── 1. fb_accounts with no child fb_apps ──
    orphan_accounts = []
    for doc in all_accounts:
        esid = doc["_id"]
        src = doc["_source"]
        children = apps_by_account_esid.get(esid, [])
        if not children:
            orphan_accounts.append((esid, src))

    print(f"\n{sep}")
    print(f"1. FB ACCOUNTS WITH NO APPS ({len(orphan_accounts)} found)")
    print(sep)
    if orphan_accounts:
        for esid, src in orphan_accounts:
            title = src.get("fb_account_title", "Untitled")
            acct_id = src.get("fb_account_id", "?")
            print(f"  • {title}")
            print(f"    esid:       {esid}")
            print(f"    account_id: {acct_id}")
            print()
    else:
        print("  None — all fb_accounts have at least one fb_app.\n")

    # ── 2. fb_apps with no child fb_ad_accounts ──
    orphan_apps = []
    for doc in all_apps:
        esid = doc["_id"]
        src = doc["_source"]
        children = ad_accounts_by_app_esid.get(esid, [])
        if not children:
            orphan_apps.append((esid, src))

    print(f"\n{sep}")
    print(f"2. FB APPS WITH NO AD ACCOUNTS ({len(orphan_apps)} found)")
    print(sep)
    if orphan_apps:
        for esid, src in orphan_apps:
            title = src.get("fb_app_title", "Untitled")
            app_id = src.get("fb_app_id", "?")
            parent_esid = src.get("fb_account_esid", "")
            parent = account_by_id.get(parent_esid)
            parent_title = parent.get("fb_account_title", "?") if parent else "(MISSING)"
            print(f"  • {title}")
            print(f"    esid:           {esid}")
            print(f"    app_id:         {app_id}")
            print(f"    fb_account:     {parent_title} [esid={parent_esid}]")
            print()
    else:
        print("  None — all fb_apps have at least one fb_ad_account.\n")

    # ── 3. fb_apps whose fb_account_esid is missing ──
    dangling_apps = []
    for doc in all_apps:
        esid = doc["_id"]
        src = doc["_source"]
        parent_esid = src.get("fb_account_esid", "")
        if parent_esid and parent_esid not in account_by_id:
            dangling_apps.append((esid, src, parent_esid))

    print(f"\n{sep}")
    print(f"3. FB APPS POINTING TO MISSING FB ACCOUNTS ({len(dangling_apps)} found)")
    print(sep)
    if dangling_apps:
        for esid, src, parent_esid in dangling_apps:
            title = src.get("fb_app_title", "Untitled")
            print(f"  • {title}")
            print(f"    esid:              {esid}")
            print(f"    fb_account_esid:   {parent_esid} ← DOES NOT EXIST")
            print()
    else:
        print("  None — all fb_apps reference valid fb_accounts.\n")

    # ── 4. fb_ad_accounts whose fb_app_esid is missing ──
    dangling_ad_accounts = []
    for doc in all_ad_accounts:
        esid = doc["_id"]
        src = doc["_source"]
        parent_esid = src.get("fb_app_esid", "")
        if parent_esid and parent_esid not in app_by_id:
            dangling_ad_accounts.append((esid, src, parent_esid))

    print(f"\n{sep}")
    print(f"4. FB AD ACCOUNTS POINTING TO MISSING FB APPS ({len(dangling_ad_accounts)} found)")
    print(sep)
    if dangling_ad_accounts:
        for esid, src, parent_esid in dangling_ad_accounts:
            title = src.get("fb_ad_account_title", "Untitled")
            ad_id = src.get("fb_ad_account_id", "?")
            print(f"  • {title}")
            print(f"    esid:          {esid}")
            print(f"    ad_account_id: {ad_id}")
            print(f"    fb_app_esid:   {parent_esid} ← DOES NOT EXIST")
            print()
    else:
        print("  None — all fb_ad_accounts reference valid fb_apps.\n")

    # ── 5. fb_ad_accounts with no valid app user token ──
    no_token = []
    for doc in all_ad_accounts:
        esid = doc["_id"]
        src = doc["_source"]
        user_esid = src.get("fb_app_user_esid", "")
        if not user_esid or user_esid not in app_user_by_id:
            no_token.append((esid, src, user_esid))

    print(f"\n{sep}")
    print(f"5. FB AD ACCOUNTS WITH NO VALID USER TOKEN ({len(no_token)} found)")
    print(sep)
    if no_token:
        for esid, src, user_esid in no_token:
            title = src.get("fb_ad_account_title", "Untitled")
            ad_id = src.get("fb_ad_account_id", "?")
            app_esid = src.get("fb_app_esid", "")
            app = app_by_id.get(app_esid)
            app_title = app.get("fb_app_title", "?") if app else "(MISSING APP)"
            reason = "empty" if not user_esid else f"{user_esid} ← DOES NOT EXIST"
            print(f"  • {title}")
            print(f"    esid:              {esid}")
            print(f"    ad_account_id:     {ad_id}")
            print(f"    fb_app:            {app_title}")
            print(f"    fb_app_user_esid:  {reason}")
            print()
    else:
        print("  None — all fb_ad_accounts have a valid user token.\n")

    # ── 6. Full chain summary with team associations ──
    print(f"\n{sep}")
    print("6. FULL CHAIN SUMMARY (account → app → ad account → teams)")
    print(sep)
    for acct_doc in all_accounts:
        acct_esid = acct_doc["_id"]
        acct = acct_doc["_source"]
        acct_title = acct.get("fb_account_title", "Untitled")
        child_apps = apps_by_account_esid.get(acct_esid, [])

        print(f"\n  📁 {acct_title} [esid={acct_esid}]")
        if not child_apps:
            print(f"     └── (no apps)")
            continue

        for app_doc in child_apps:
            app_esid = app_doc["_id"]
            app = app_doc["_source"]
            app_title = app.get("fb_app_title", "Untitled")
            child_ad_accounts = ad_accounts_by_app_esid.get(app_esid, [])

            print(f"     ├── 📱 {app_title} [esid={app_esid}]")
            if not child_ad_accounts:
                print(f"     │   └── (no ad accounts)")
                continue

            for ad_doc in child_ad_accounts:
                ad_esid = ad_doc["_id"]
                ad = ad_doc["_source"]
                ad_title = ad.get("fb_ad_account_title", "Untitled")
                ad_id = ad.get("fb_ad_account_id", "?")
                user_esid = ad.get("fb_app_user_esid", "")
                has_token = "✓" if user_esid and user_esid in app_user_by_id else "✗"

                print(f"     │   ├── 💰 {ad_title} (act_{ad_id}) token={has_token}")

    # ── 7. Team → page ID → which ad accounts serve those pages ──
    print(f"\n\n{sep}")
    print("7. TEAMS AND THEIR PAGE IDS")
    print(sep)
    for team_doc in sorted(all_teams, key=lambda d: d["_source"].get("team_title", "")):
        team = team_doc["_source"]
        title = team.get("team_title", "Untitled")
        page_ids = team.get("team_facebook_page_id", [])
        if isinstance(page_ids, str):
            page_ids = [page_ids] if page_ids else []

        print(f"\n  🏢 {title} [esid={team_doc['_id']}]")
        if page_ids:
            for pid in page_ids:
                print(f"     page_id: {pid}")
        else:
            print(f"     (no page IDs configured)")

    print(f"\n{sep}")
    print("DONE")
    print(sep)
