"""
Debug script to check Facebook Page token permissions and validity.
"""

import sys
import logging
import lib.logging
import requests
from import_scripts.facebook.lib.assets import get_fb_apps, get_fb_app_user_for_pages
from import_scripts.facebook.get_insights import get_fb_app_user_token
from lib.encryption import decrypt
from lib.fb_constants import FB_GRAPH_URL


def main(params):
    """
    Check token permissions and test basic API calls.
    """
    lib.logging.define("fb-debug-token")

    # Get FB apps
    fb_apps = get_fb_apps(filters=None)
    if not fb_apps:
        logging.error("No Facebook apps found")
        return

    fb_app = fb_apps[0]["_source"]
    fb_app_esid = fb_apps[0]["_id"]
    fb_app_id = fb_app.get("fb_app_id")

    logging.info(f"Using FB App: {fb_app.get('fb_app_title')} (ID: {fb_app_id})")

    # Get Page-scoped user
    fb_app_user_doc = get_fb_app_user_for_pages(fb_app_esid)
    if not fb_app_user_doc:
        logging.error("No Page-scoped FB app user found")
        return

    fb_app_user_esid = fb_app_user_doc["_id"]
    fb_app_user_title = fb_app_user_doc["_source"].get("fb_app_user_title")
    fb_app_user_token = get_fb_app_user_token(fb_app_user_esid)

    logging.info(f"Using FB App User: {fb_app_user_title}")
    logging.info(f"Token (first 20 chars): {fb_app_user_token[:20]}...")

    # Test 1: Debug token
    logging.info("\n=== Test 1: Debug Token ===")
    debug_url = f"{FB_GRAPH_URL}/debug_token"
    debug_params = {
        "input_token": fb_app_user_token,
        "access_token": f"{fb_app_id}|{decrypt(fb_app.get('fb_app_secret'))}",
    }

    try:
        response = requests.get(debug_url, params=debug_params)
        response.raise_for_status()
        data = response.json()

        token_data = data.get("data", {})
        logging.info(f"Token is valid: {token_data.get('is_valid')}")
        logging.info(f"Token type: {token_data.get('type')}")
        logging.info(f"Token expires: {token_data.get('expires_at', 'Never')}")
        logging.info(f"Scopes: {', '.join(token_data.get('scopes', []))}")
        logging.info(f"User ID: {token_data.get('user_id')}")

        # Check for required permissions
        scopes = token_data.get("scopes", [])
        required_scopes = ["pages_read_engagement", "read_insights"]
        missing_scopes = [s for s in required_scopes if s not in scopes]

        if missing_scopes:
            logging.warning(f"Missing required scopes: {', '.join(missing_scopes)}")
        else:
            logging.info("✓ All required scopes present")

    except Exception as e:
        logging.error(f"Failed to debug token: {e}")

    # Test 2: Get user's pages
    logging.info("\n=== Test 2: Get User's Pages ===")
    pages_url = f"{FB_GRAPH_URL}/me/accounts"
    pages_params = {"access_token": fb_app_user_token}

    try:
        response = requests.get(pages_url, params=pages_params)
        response.raise_for_status()
        data = response.json()

        pages = data.get("data", [])
        logging.info(f"Found {len(pages)} pages accessible to this token:")
        for page in pages[:5]:  # Show first 5
            logging.info(f"  - {page.get('name')} (ID: {page.get('id')})")
            logging.info(f"    Access token: {page.get('access_token', 'N/A')[:20]}...")

        if len(pages) > 5:
            logging.info(f"  ... and {len(pages) - 5} more")

    except Exception as e:
        logging.error(f"Failed to get pages: {e}")

    # Test 3: Try to access a specific page
    page_id = params.get("page_id")
    if page_id:
        logging.info(f"\n=== Test 3: Access Page {page_id} ===")

        # Try with user token
        page_url = f"{FB_GRAPH_URL}/{page_id}"
        page_params = {
            "fields": "id,name,access_token",
            "access_token": fb_app_user_token,
        }

        try:
            response = requests.get(page_url, params=page_params)
            response.raise_for_status()
            data = response.json()

            logging.info(f"✓ Successfully accessed page: {data.get('name')}")

            # Check if page access token is available
            page_access_token = data.get("access_token")
            if page_access_token:
                logging.info(f"✓ Page has its own access token: {page_access_token[:20]}...")
                logging.info("NOTE: You should use the page access token for insights, not the user token")
            else:
                logging.warning("Page access token not available - this might be the issue")

        except Exception as e:
            logging.error(f"Failed to access page: {e}")

    logging.info("\n=== Debug Complete ===")
    logging.info("If you see missing scopes or permission errors, you need to:")
    logging.info("1. Regenerate the token with required permissions")
    logging.info("2. Use page access tokens instead of user tokens for insights")
