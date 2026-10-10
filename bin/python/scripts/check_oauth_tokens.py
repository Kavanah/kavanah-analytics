#!/usr/bin/env python3
"""
Check OAuth Token Status for FB App Users

This script checks the status of all Facebook OAuth tokens and provides
refresh URLs for expired or missing tokens.

Set APP_PUBLIC_HOSTNAME (deployment/.env -> docker-compose.yml) to
print full https:// refresh URLs; when it is blank the script prints the
path relative to the app host instead.
"""

import sys
import os
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.es_helpers import get_all_documents


def check_token_status():
    """Check the status of all FB App User OAuth tokens."""

    print("=" * 70)
    print("OAuth Token Status Report")
    print("=" * 70)
    print()

    # Get all FB App Users
    users = get_all_documents("de_asset_fb_app_users")
    apps = get_all_documents("de_asset_fb_apps")

    # Create app lookup
    app_lookup = {app["_id"]: app["_source"] for app in apps}

    # Get current time
    now = datetime.now()

    # Group users by app
    users_by_app = {}
    for user in users:
        app_esid = user["_source"].get("fb_app_esid")
        if app_esid not in users_by_app:
            users_by_app[app_esid] = []
        users_by_app[app_esid].append(user)

    # Track issues
    expired_count = 0
    expiring_soon_count = 0
    missing_exp_count = 0

    # Print status for each app
    for app_esid, app_users in users_by_app.items():
        app_data = app_lookup.get(app_esid, {})
        app_title = app_data.get("fb_app_title", "Unknown App")
        app_id = app_data.get("fb_app_id", "Unknown")

        print(f"App: {app_title} (ID: {app_id})")
        print("-" * 70)

        for user in app_users:
            user_data = user["_source"]
            user_title = user_data.get("fb_app_user_title", "Unknown")
            user_role = user_data.get("fb_app_user_role", "Unknown")

            print(f"\n  {user_title}")
            print(f"    Role: {user_role}")
            print(f"    Has Token: {bool(user_data.get('fb_app_user_token'))}")

            # Check expiration
            exp_timestamp = user_data.get("fb_app_user_token_exp")

            status_icon = "?"
            status_msg = "Unknown"
            needs_refresh = False

            if exp_timestamp:
                try:
                    exp_timestamp_int = int(exp_timestamp)
                    exp_date = datetime.fromtimestamp(exp_timestamp_int)
                    days_until_exp = (exp_date - now).days

                    if days_until_exp < 0:
                        status_icon = "❌"
                        status_msg = f"EXPIRED {abs(days_until_exp)} days ago"
                        needs_refresh = True
                        expired_count += 1
                    elif days_until_exp < 7:
                        status_icon = "⚠️ "
                        status_msg = f"Expires in {days_until_exp} days"
                        needs_refresh = True
                        expiring_soon_count += 1
                    else:
                        status_icon = "✅"
                        status_msg = f"Expires in {days_until_exp} days"

                    print(f"    {status_icon} {status_msg} ({exp_date.strftime('%Y-%m-%d %H:%M')})")

                except (ValueError, TypeError):
                    status_icon = "⚠️ "
                    status_msg = "Invalid expiration timestamp"
                    needs_refresh = True
                    missing_exp_count += 1
                    print(f"    {status_icon} {status_msg}")
            else:
                status_icon = "⚠️ "
                status_msg = "No expiration date (likely expired)"
                needs_refresh = True
                missing_exp_count += 1
                print(f"    {status_icon} {status_msg}")

            # Show Facebook User ID if available
            fb_user_id = user_data.get("fb_app_user_fbid")
            if fb_user_id:
                print(f"    FB User ID: {fb_user_id}")

            # Show refresh URL if needed
            auth_url = user_data.get("fb_app_user_auth_url")
            if needs_refresh and auth_url and app_esid:
                # the public hostname is deployment config, not source.
                # APP_PUBLIC_HOSTNAME is the only var compose passes into imports_container
                # for this (SERVER_NAME is nginx-only and never reaches the app container).
                hostname = os.environ.get("APP_PUBLIC_HOSTNAME", "").strip()
                if hostname:
                    print(f"    🔗 Refresh: https://{hostname}/{app_esid}/{auth_url}")
                else:
                    print(f"    🔗 Refresh path: /{app_esid}/{auth_url}  (set APP_PUBLIC_HOSTNAME for a full URL)")

        print()

    # Summary
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"Total FB App Users: {len(users)}")
    print(f"Expired tokens: {expired_count}")
    print(f"Expiring soon (< 7 days): {expiring_soon_count}")
    print(f"Missing expiration date: {missing_exp_count}")
    print()

    if expired_count + expiring_soon_count + missing_exp_count > 0:
        print("⚠️  Action Required: Refresh tokens using the URLs shown above")
        print()
        print("Priority for Instagram imports:")
        print("  1. Refresh all 'Pages' role users first")
        print("  2. Then refresh 'Ads' role users if needed for Facebook Ads imports")
        print()
        return 1
    else:
        print("✅ All tokens are valid and not expiring soon")
        print()
        return 0


if __name__ == "__main__":
    exit_code = check_token_status()
    sys.exit(exit_code)
