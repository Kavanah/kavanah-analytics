#!/usr/bin/env python3
"""
Instagram Authentication Diagnostic Tool

Systematically checks each component of the Instagram import authentication flow
to identify where the error 100 is originating.

Usage:
    python3 /app/python/import.py instagram diagnose_instagram_auth
    python3 /app/python/import.py instagram diagnose_instagram_auth --profile-id=123456789
    python3 /app/python/import.py instagram diagnose_instagram_auth --verbose
"""

import sys
import logging
import lib.logging
from datetime import datetime
from facebook_business.api import FacebookAdsApi
from facebook_business.exceptions import FacebookRequestError
from facebook_business.adobjects.iguser import IGUser
from import_scripts.facebook.lib.assets import (
    get_fb_apps,
    get_fb_app_users,
    get_ig_accounts,
    get_fb_teams,
    get_fb_app_user_for_instagram,
)
from import_scripts.facebook.get_insights import get_fb_app_user_token
from lib.encryption import decrypt


# Color codes for terminal output
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"


def print_status(message, status="info"):
    """Print colored status messages."""
    if status == "success":
        print(f"{Colors.GREEN}✓ {message}{Colors.RESET}")
    elif status == "error":
        print(f"{Colors.RED}✗ {message}{Colors.RESET}")
    elif status == "warning":
        print(f"{Colors.YELLOW}⚠ {message}{Colors.RESET}")
    else:
        print(f"{Colors.BLUE}ℹ {message}{Colors.RESET}")


def check_asset_configuration():
    """Step 1: Verify Instagram accounts are configured in asset manager."""
    lib.logging.define("ig-diagnose")
    print("\n" + "=" * 80)
    print("STEP 1: Checking Instagram Asset Configuration")
    print("=" * 80)

    # Get Instagram accounts
    ig_accounts = get_ig_accounts(filters=None)

    if not ig_accounts:
        print_status("No Instagram accounts found in de_asset_ig_accounts index", "error")
        return False

    print_status(f"Found {len(ig_accounts)} Instagram account(s)", "success")

    # Validate each account has required fields
    valid_accounts = 0
    invalid_accounts = []

    for ig_doc in ig_accounts:
        ig_data = ig_doc["_source"]
        esid = ig_doc["_id"]
        username = ig_data.get("ig_account_username", "unknown")
        ig_id = ig_data.get("ig_account_id")

        if ig_id:
            print(f"  ✓ {username} (ESID: {esid}, IG ID: {ig_id})")
            valid_accounts += 1
        else:
            print(f"  ✗ {username} (ESID: {esid}) - MISSING ig_account_id")
            invalid_accounts.append(esid)

    if invalid_accounts:
        print_status(f"{len(invalid_accounts)} account(s) missing ig_account_id field", "warning")
        return False

    print_status(f"All {valid_accounts} Instagram accounts properly configured", "success")
    return True


def check_team_configuration():
    """Step 2: Verify teams have Instagram profiles assigned."""
    lib.logging.define("ig-diagnose")
    print("\n" + "=" * 80)
    print("STEP 2: Checking Team Instagram Configuration")
    print("=" * 80)

    teams = get_fb_teams(filters=None)
    teams_with_ig = [t for t in teams if t["_source"].get("team_instagram_profile_esids")]

    if not teams_with_ig:
        print_status("No teams configured with Instagram profiles", "error")
        return False

    print_status(f"Found {len(teams_with_ig)} team(s) with Instagram profiles", "success")

    # Get all Instagram accounts for ESID validation
    ig_accounts = get_ig_accounts(filters=None)
    valid_esids = {doc["_id"] for doc in ig_accounts}

    # Check each team's configuration
    for team_doc in teams_with_ig[:5]:  # Show first 5
        team = team_doc["_source"]
        team_title = team.get("team_title", "Unknown")
        profile_esids = team.get("team_instagram_profile_esids", [])

        if not isinstance(profile_esids, list):
            profile_esids = [profile_esids]

        print(f"\n  Team: {team_title}")
        print(f"    Configured ESIDs: {profile_esids}")

        # Validate ESIDs exist
        for esid in profile_esids:
            if esid in valid_esids:
                print(f"      ✓ ESID {esid} exists in de_asset_ig_accounts")
            else:
                print(f"      ✗ ESID {esid} NOT FOUND in de_asset_ig_accounts")

    return True


def check_fb_app_configuration(ig_accounts=None):
    """Step 3: Verify Facebook App configuration."""
    lib.logging.define("ig-diagnose")
    print("\n" + "=" * 80)
    print("STEP 3: Checking Facebook App Configuration")
    print("=" * 80)

    fb_apps = get_fb_apps(filters=None)

    if not fb_apps:
        print_status("No Facebook apps found", "error")
        return None

    print_status(f"Found {len(fb_apps)} Facebook app(s)", "success")

    # Show all apps with their linked portfolios
    for app_doc in fb_apps:
        app = app_doc["_source"]
        portfolio = app.get("fb_account_title", "NOT LINKED")
        print(f"  - {app.get('fb_app_title', 'Unknown')} (ESID: {app_doc['_id']}) → Portfolio: {portfolio}")

    # Determine which app to use:
    # If IG accounts are provided, pick the app matching the first account's fb_account_esid or fb_account_title.
    # Fall back to the first app with credentials.
    selected_doc = None

    if ig_accounts:
        first_ig = ig_accounts[0]["_source"]
        ig_portfolio_esid = first_ig.get("fb_account_esid")
        ig_portfolio_title = first_ig.get("fb_account_title")

        for app_doc in fb_apps:
            app = app_doc["_source"]
            if ig_portfolio_esid and app.get("fb_account_esid") == ig_portfolio_esid:
                selected_doc = app_doc
                print_status(
                    f"\nSelected app by portfolio ESID match: {app.get('fb_app_title')}",
                    "success",
                )
                break
            if ig_portfolio_title and app.get("fb_account_title") == ig_portfolio_title:
                selected_doc = app_doc
                print_status(
                    f"\nSelected app by portfolio title match: {app.get('fb_app_title')}",
                    "success",
                )
                break

        if not selected_doc:
            print_status(
                f"\nNo app matched portfolio '{ig_portfolio_title}' — falling back to first app",
                "warning",
            )

    if not selected_doc:
        selected_doc = fb_apps[0]

    fb_app = selected_doc["_source"]
    fb_app_esid = selected_doc["_id"]
    app_id = fb_app.get("fb_app_id")
    app_title = fb_app.get("fb_app_title", "Unknown")
    app_secret = fb_app.get("fb_app_secret")

    print(f"\n  Using App: {app_title}")
    print(f"    App ID: {app_id}")
    print(f"    App ESID: {fb_app_esid}")
    print(f"    Has Secret: {'Yes' if app_secret else 'No'}")

    if not app_id or not app_secret:
        print_status("Facebook app missing credentials", "error")
        return None

    return {
        "esid": fb_app_esid,
        "id": app_id,
        "secret": decrypt(app_secret),
        "title": app_title,
    }


def check_fb_app_user_configuration(fb_app_esid):
    """Step 4: Verify FB App User exists with role="Pages"."""
    lib.logging.define("ig-diagnose")
    print("\n" + "=" * 80)
    print("STEP 4: Checking FB App User Configuration")
    print("=" * 80)

    # Get all users for this app
    app_users = get_fb_app_users(filters={"fb_app_esid": fb_app_esid})

    if not app_users:
        print_status(f"No FB app users found for app ESID {fb_app_esid}", "error")
        return None

    print_status(f"Found {len(app_users)} FB app user(s) for this app", "info")

    # Show all users with their roles
    for user_doc in app_users:
        user = user_doc["_source"]
        user_title = user.get("fb_app_user_title", "Unknown")
        user_role = user.get("fb_app_user_role", "NOT SET")
        has_token = bool(user.get("fb_app_user_token"))

        print(f"\n  User: {user_title}")
        print(f"    Role: {user_role}")
        print(f"    Has Token: {'Yes' if has_token else 'No'}")
        print(f"    ESID: {user_doc['_id']}")

    # Get the user that would be used for Instagram (role="Pages")
    fb_app_user_doc = get_fb_app_user_for_instagram(fb_app_esid)

    if not fb_app_user_doc:
        print_status("No FB app user with role='Pages' found", "error")
        print_status("Instagram requires a user with role='Pages' for authentication", "warning")
        return None

    user_esid = fb_app_user_doc["_id"]
    user_data = fb_app_user_doc["_source"]
    user_title = user_data.get("fb_app_user_title", "Unknown")

    print_status(f"Selected user: {user_title} (role='Pages')", "success")

    # Get and validate token
    token = get_fb_app_user_token(user_esid)

    if not token:
        print_status("User has no token or token decryption failed", "error")
        return None

    print(f"    Token length: {len(token)} characters")
    print(f"    Token starts with: {token[:20]}...")

    return {"esid": user_esid, "title": user_title, "token": token}


def check_token_validity(app_id, app_secret, user_token):
    """Step 5: Verify token is valid and has required permissions."""
    lib.logging.define("ig-diagnose")
    print("\n" + "=" * 80)
    print("STEP 5: Checking Token Validity and Permissions")
    print("=" * 80)

    import requests

    # Check token info
    debug_url = f"https://graph.facebook.com/debug_token"
    params = {"input_token": user_token, "access_token": f"{app_id}|{app_secret}"}

    try:
        response = requests.get(debug_url, params=params)
        data = response.json()

        if "error" in data:
            print_status(f"Token validation error: {data['error']['message']}", "error")
            return False

        token_data = data.get("data", {})

        # Check if token is valid
        is_valid = token_data.get("is_valid", False)
        expires_at = token_data.get("expires_at", 0)
        scopes = token_data.get("scopes", [])

        print(f"  Token Valid: {is_valid}")

        if expires_at > 0:
            from datetime import datetime

            expiry_date = datetime.fromtimestamp(expires_at)
            days_until_expiry = (expiry_date - datetime.now()).days
            print(f"  Expires: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')} ({days_until_expiry} days)")
        else:
            print(f"  Expires: Never (long-lived token)")

        print(f"\n  Token Scopes ({len(scopes)}):")
        for scope in scopes:
            print(f"    - {scope}")

        # Check for required Instagram permissions
        required_perms = [
            "instagram_basic",
            "instagram_manage_insights",
            "pages_read_engagement",
            "pages_show_list",
        ]

        missing_perms = [p for p in required_perms if p not in scopes]

        if missing_perms:
            print_status(f"\nMissing required permissions: {missing_perms}", "error")
            print_status("Token needs Instagram and Pages permissions", "warning")
            return False

        print_status("Token has all required Instagram permissions", "success")
        return True

    except Exception as e:
        print_status(f"Error checking token: {e}", "error")
        return False


def test_api_initialization(app_id, app_secret, user_token):
    """Step 6: Test FacebookAdsApi.init()."""
    lib.logging.define("ig-diagnose")
    print("\n" + "=" * 80)
    print("STEP 6: Testing Facebook API Initialization")
    print("=" * 80)

    try:
        FacebookAdsApi.init(app_id, app_secret, user_token)
        print_status("FacebookAdsApi.init() successful", "success")
        return True
    except Exception as e:
        print_status(f"FacebookAdsApi.init() failed: {e}", "error")
        return False


def test_instagram_profile_access(instagram_profile_id, profile_info=None):
    """Step 7: Test actual Instagram profile access."""
    lib.logging.define("ig-diagnose")
    print("\n" + "=" * 80)
    print(f"STEP 7: Testing Instagram Profile Access")
    if profile_info:
        print(f"Profile: {profile_info.get('username', 'unknown')} (ID: {instagram_profile_id})")
    print("=" * 80)

    try:
        # Try to fetch basic profile info
        user = IGUser(instagram_profile_id).api_get(fields=["id", "username", "name"])
        user_data = user.export_all_data()

        print_status(f"Successfully accessed Instagram profile", "success")
        print(f"  ID: {user_data.get('id')}")
        print(f"  Username: {user_data.get('username')}")
        print(f"  Name: {user_data.get('name')}")

        return True

    except FacebookRequestError as e:
        error_code = e.api_error_code()
        error_message = e.api_error_message()

        print_status(f"Instagram profile access FAILED", "error")
        print(f"  Error Code: {error_code}")
        print(f"  Error Message: {error_message}")

        # Provide specific guidance based on error code
        if error_code == 100:
            print("\n  DIAGNOSIS: Error 100 - Unsupported Get Request or Invalid ID")
            print("  Possible causes:")
            print("    1. Instagram account is not a Business or Creator account")
            print("    2. Instagram account is not connected to a Facebook Page")
            print("    3. Instagram account ID is incorrect")
            print("    4. Token doesn't have access to this Instagram account")
            print("    5. The Facebook Page connected to Instagram isn't accessible by this token")
        elif error_code == 10:
            print("\n  DIAGNOSIS: Error 10 - Permission Denied")
            print("  Possible causes:")
            print("    1. Token missing required permissions")
            print("    2. Instagram account not linked to a Page owned by the token user")

        return False


def check_page_account_linkage():
    """Step 7.5: Verify Instagram → Page → Account chain for all Instagram accounts."""
    lib.logging.define("ig-diagnose")
    print("\n" + "=" * 80)
    print("STEP 7.5: Checking Instagram → Page → Account Linkage")
    print("=" * 80)

    # Get all Instagram accounts
    ig_accounts = get_ig_accounts(filters=None)
    if not ig_accounts:
        print_status("No Instagram accounts found", "warning")
        return True

    print(f"\nFound {len(ig_accounts)} Instagram account(s)\n")

    complete_chains = 0
    missing_page_esids = 0
    page_not_found = 0
    missing_account_esids = 0

    for ig_doc in ig_accounts:
        ig_account = ig_doc["_source"]
        ig_account_id = ig_account.get("ig_account_id", "unknown")
        ig_username = ig_account.get("ig_account_username", "unknown")
        fb_page_esid = ig_account.get("fb_page_esid")

        print(f"Instagram @{ig_username} (ID: {ig_account_id})")

        if not fb_page_esid:
            print_status("  No fb_page_esid configured", "error")
            print("    → Action: Configure fb_page_esid in asset explorer")
            missing_page_esids += 1
            continue

        print(f"  → FB Page ESID: {fb_page_esid}")

        # Look up the FB Page
        from import_scripts.facebook.lib.assets import get_fb_pages

        fb_page_docs = get_fb_pages(filters={"_id": fb_page_esid})
        if not fb_page_docs:
            print_status(f"  → Page ESID not found in de_asset_fb_pages", "error")
            print("    → Action: Fix fb_page_esid reference in asset explorer")
            page_not_found += 1
            continue

        fb_page = fb_page_docs[0]["_source"]
        fb_page_name = fb_page.get("fb_page_name", "unknown")
        fb_account_esid = fb_page.get("fb_account_esid")

        print(f"  → FB Page: {fb_page_name}")

        if not fb_account_esid:
            print_status(f"  → FB Account ESID: NOT CONFIGURED", "warning")
            print("    → Action: Set fb_account_esid for this page in asset explorer")
            print(
                f"    → Command: python3 /app/python/asset-management.py update fb_page {fb_page_esid} --fb_account_esid <ACCOUNT_ESID>"
            )
            missing_account_esids += 1
        else:
            print_status(f"  → FB Account ESID: {fb_account_esid}", "success")
            print("    ✓ Complete chain verified")
            complete_chains += 1

        print()

    # Summary
    print("=" * 80)
    print("LINKAGE SUMMARY")
    print("=" * 80)
    print(f"Complete chains:              {complete_chains}")
    print(f"Missing fb_page_esid:         {missing_page_esids}")
    print(f"Invalid fb_page_esid:         {page_not_found}")
    print(f"Missing fb_account_esid:      {missing_account_esids}")
    print("=" * 80)

    if missing_page_esids > 0 or page_not_found > 0:
        print_status("\n⚠ Some Instagram accounts have invalid page linkages", "error")
        print("Fix these issues in asset explorer before proceeding.")
        return False

    if missing_account_esids > 0:
        print_status(
            f"\n⚠ {missing_account_esids} Facebook Page(s) need fb_account_esid configured",
            "warning",
        )
        print("\nThis will cause Error 100 when the import script cannot find the correct token.")
        print("\nTo fix:")
        print("  1. Run the populate_instagram_page_account_links diagnostic (private: internal/tools/debug/)")
        print("  2. Or manually set fb_account_esid using asset-management.py or asset explorer")
        return False

    print_status("\n✓ All Instagram accounts have complete Page → Account chain!", "success")
    return True


def main(params):
    """Main diagnostic orchestration."""
    lib.logging.define("ig-diagnose")

    print("\n")
    print("=" * 80)
    print(" INSTAGRAM AUTHENTICATION DIAGNOSTIC TOOL")
    print("=" * 80)
    print(f" Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    verbose = params.get("verbose", False)
    test_profile_id = params.get("profile-id") or params.get("profile_id")

    # Run diagnostic steps
    results = {}

    # Step 1: Check asset configuration
    results["assets"] = check_asset_configuration()
    if not results["assets"]:
        print_status("\nDiagnostic stopped: Instagram asset configuration issues found", "error")
        return

    # Step 2: Check team configuration
    results["teams"] = check_team_configuration()

    # Step 3: Check FB App — if a specific profile-id was given, filter to that account
    # so the right FB app is selected rather than defaulting to the first account's app
    all_ig_accounts = get_ig_accounts(filters=None)
    if test_profile_id:
        ig_accounts_for_app_check = [
            a for a in all_ig_accounts if str(a["_source"].get("ig_account_id", "")) == str(test_profile_id)
        ]
        if not ig_accounts_for_app_check:
            print_status(
                f"Warning: profile-id {test_profile_id} not found in de_asset_ig_accounts — using first account for app selection",
                "warning",
            )
            ig_accounts_for_app_check = all_ig_accounts
    else:
        ig_accounts_for_app_check = all_ig_accounts
    fb_app = check_fb_app_configuration(ig_accounts=ig_accounts_for_app_check)
    if not fb_app:
        print_status("\nDiagnostic stopped: Facebook app configuration issues found", "error")
        return

    # Step 4: Check FB App User
    fb_app_user = check_fb_app_user_configuration(fb_app["esid"])
    if not fb_app_user:
        print_status("\nDiagnostic stopped: FB App User configuration issues found", "error")
        print("\nRECOMMENDATION:")
        print("  1. Create a FB App User with role='Pages' in the asset manager")
        print("  2. Ensure the user has a valid long-lived token")
        print("  3. Token must have Instagram and Pages permissions")
        return

    # Step 5: Check token validity
    results["token"] = check_token_validity(fb_app["id"], fb_app["secret"], fb_app_user["token"])
    if not results["token"]:
        print_status("\nDiagnostic stopped: Token validation failed", "error")
        print("\nRECOMMENDATION:")
        print("  1. Regenerate the FB App User token through Facebook's Graph API Explorer")
        print("  2. Ensure these permissions are granted:")
        print("     - instagram_basic")
        print("     - instagram_manage_insights")
        print("     - pages_read_engagement")
        print("     - pages_show_list")
        print("  3. Update the token in the asset manager")
        return

    # Step 6: Test API initialization
    results["api_init"] = test_api_initialization(fb_app["id"], fb_app["secret"], fb_app_user["token"])
    if not results["api_init"]:
        print_status("\nDiagnostic stopped: API initialization failed", "error")
        return

    # Step 7: Test Instagram profile access
    if test_profile_id:
        # Test specific profile
        ig_accounts = get_ig_accounts(filters=None)
        profile_info = next(
            (acc["_source"] for acc in ig_accounts if acc["_source"].get("ig_account_id") == test_profile_id),
            None,
        )

        results["profile_access"] = test_instagram_profile_access(test_profile_id, profile_info)
    else:
        # Test first profile found
        ig_accounts = get_ig_accounts(filters=None)
        if ig_accounts:
            first_account = ig_accounts[0]["_source"]
            profile_id = first_account.get("ig_account_id")

            if profile_id:
                results["profile_access"] = test_instagram_profile_access(profile_id, first_account)
            else:
                print_status("First Instagram account has no ig_account_id", "error")
        else:
            print_status("No Instagram accounts to test", "warning")

    # Step 7.5: Check Page → Account linkage for all Instagram accounts
    results["linkage"] = check_page_account_linkage()

    # Final summary
    print("\n")
    print("=" * 80)
    print(" DIAGNOSTIC SUMMARY")
    print("=" * 80)

    all_passed = all(
        [
            results.get("assets", False),
            results.get("teams", False),
            results.get("token", False),
            results.get("api_init", False),
            results.get("profile_access", False),
            results.get("linkage", False),
        ]
    )

    if all_passed:
        print_status("\nAll checks PASSED - Instagram import should work", "success")
    else:
        print_status("\nSome checks FAILED - see recommendations above", "error")

    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    main({})
