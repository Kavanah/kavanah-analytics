# Google Ads API - Service Account Authentication Setup

> **⚠️ DEPRECATED.** Google Ads service-account
> authentication is **retired**. The importer is now **OAuth-only**: each team
> authorizes its own Google account through the Data Connectors wizard, and a
> customer with no active OAuth connection is skipped (and flagged in the Report
> Scheduler) rather than read via a shared service account. There is no
> `google-ads-sa.json` / `google-ads-sa.json.enc` on disk any more. The
> `developer_token` in `google-ads.yaml` is still required (every API call needs
> it regardless of auth method). The steps below are kept only for historical
> reference — **do not install a service-account key.**

**Last Updated**: January 29, 2026 (deprecated 2026-06-19)

This guide covers setting up Google Ads API access using **service account authentication** for the Digital Engagement Dashboard.

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Step 1: Create Service Account](#step-1-create-service-account)
4. [Step 2: Enable Google Ads API](#step-2-enable-google-ads-api)
5. [Step 3: Get Developer Token](#step-3-get-developer-token)
6. [Step 4: Grant Google Ads Access](#step-4-grant-google-ads-access)
7. [Step 5: Install Service Account Key](#step-5-install-service-account-key)
8. [Step 6: Configure Credentials](#step-6-configure-credentials)
9. [Step 7: Add Google Ads Customers](#step-7-add-google-ads-customers)
10. [Step 8: Test Import](#step-8-test-import)
11. [Troubleshooting](#troubleshooting)

---

## Overview

### Authentication Methods

The Google Ads import script supports two authentication methods:

| Method | Files Required | Use Case |
|--------|----------------|----------|
| **Service Account** (New) | `google-ads-sa.json` + `google-ads.yaml` (developer token only) | Production automation, server-to-server |
| **OAuth 2.0** (Legacy) | `google-ads.yaml` (full OAuth credentials) | Development, user-based access |

**This guide covers service account authentication**, which is recommended for production deployments.

### Why Service Accounts?

✅ **No user interaction** required after setup
✅ **Programmatic access** without OAuth flow
✅ **Better for automation** - no refresh token expiration concerns
✅ **Clear service identity** - separate from personal accounts
✅ **Consistent with GA4** - matches existing pattern

---

## Prerequisites

- **Google Cloud Project** with billing enabled
- **Access to Google Ads account** (Standard or Admin level)
- **SSH access** to Digital Engagement server
- **Google Ads Customer ID** (format: `123-456-7890`)

---

## Step 1: Create Service Account

### 1.1 Open Google Cloud Console

Go to [Google Cloud Console](https://console.cloud.google.com/)

### 1.2: Create Service Account

1. Navigate to **IAM & Admin → Service Accounts**
2. Click **+ CREATE SERVICE ACCOUNT**
3. Fill in details:
   - **Service account name**: `google-ads-api-sa`
   - **Service account ID**: (auto-generated)
   - **Description**: "Service account for Google Ads API access"
4. Click **CREATE AND CONTINUE**
5. **Skip** granting roles (not needed for Google Ads API)
6. Click **DONE**

### 1.3: Create JSON Key

1. Click on the newly created service account
2. Go to the **KEYS** tab
3. Click **ADD KEY → Create new key**
4. Select **JSON** format
5. Click **CREATE**
6. **Save the downloaded JSON file securely** (e.g., `google-ads-sa.json`)

⚠️ **IMPORTANT**: This JSON file contains sensitive credentials. Store it securely and never commit it to version control.

---

## Step 2: Enable Google Ads API

### 2.1: Navigate to API Library

1. In Google Cloud Console, go to **APIs & Services → Library**
2. Search for "**Google Ads API**"
3. Click on "**Google Ads API**"
4. Click **ENABLE**

---

## Step 3: Get Developer Token

**Note**: The Google Ads API still requires a developer token even with service account authentication.

### 3.1: Access Google Ads API Center

1. Sign into [Google Ads](https://ads.google.com)
2. Click the **Tools & Settings** icon (wrench) in the top right
3. Under "Setup", click **API Center**

### 3.2: Obtain Developer Token

- If you see a developer token: **Copy it**
- If you don't have one: **Apply for access** (approval takes 1-2 business days)
- New accounts receive a "test" token (limited functionality)

**Test Token Limitations**:
- Works only with test accounts
- Good for development/testing
- **Not suitable for production**

### 3.3: Apply for Production Access (if needed)

1. In API Center, click **Apply for API access**
2. Fill out the application form
3. Wait for approval (typically 24-48 hours)
4. Once approved, your token status changes to "Approved"

---

## Step 4: Grant Google Ads Access

The service account needs explicit access to your Google Ads account.

### 4.1: Get Service Account Email

From the JSON key file you downloaded, find the `client_email` field:

```json
{
  "client_email": "google-ads-api-sa@your-project.iam.gserviceaccount.com",
  ...
}
```

### 4.2: Add Service Account to Google Ads

1. Sign into [Google Ads](https://ads.google.com)
2. Click **Tools & Settings** → **Access and security** → **Users**
3. Click the **Plus (+)** button
4. Enter the service account email: `google-ads-api-sa@your-project.iam.gserviceaccount.com`
5. Select access level:
   - **Standard**: Recommended (view and edit access)
   - **Read only**: If you only need to fetch data (not modify campaigns)
6. Click **SEND INVITATION**

**Note**: The service account email will receive an invitation, but it doesn't need to accept it. Access is granted immediately.

### 4.3: Verify Access

1. Go to **Tools & Settings** → **Access and security** → **Users**
2. Confirm the service account email appears in the user list

---

## Step 5: Install Service Account Key

### 5.1: Copy JSON Key to Server

From your local machine:

```bash
# Copy JSON key to server (replace with your key filename)
scp -i ~/.ssh/id_ed25519 google-ads-sa.json ubuntu@<server-ip>:/tmp/
```

### 5.2: Move Key to Container

SSH into the server and move the key:

```bash
# SSH into server
ssh -i ~/.ssh/id_ed25519 ubuntu@<server-ip>

# Move key to container config directory
sudo docker exec imports_container bash -c 'mv /tmp/google-ads-sa.json /app/reporting_configs/ && chmod 600 /app/reporting_configs/google-ads-sa.json'

# Verify file exists and has correct permissions
sudo docker exec imports_container ls -la /app/reporting_configs/google-ads-sa.json
```

Expected output:
```
-rw------- 1 root root 2339 Jan 29 12:00 /app/reporting_configs/google-ads-sa.json
```

---

## Step 6: Configure Credentials

### 6.1: Create or Update YAML Config

Create a minimal YAML config with just the developer token:

```bash
sudo docker exec imports_container bash -c 'cat > /app/reporting_configs/google-ads.yaml <<EOF
# Google Ads API Configuration - Service Account Mode
# Service account credentials loaded from google-ads-sa.json

# Developer token (required)
developer_token: YOUR_DEVELOPER_TOKEN_HERE

# Optional: Only needed for Manager (MCC) accounts
#login_customer_id: 1234567890

# API settings
use_proto_plus: True
EOF
chmod 600 /app/reporting_configs/google-ads.yaml'
```

### 6.2: Update Developer Token

Edit the config file to add your actual developer token:

```bash
sudo docker exec -it imports_container nano /app/reporting_configs/google-ads.yaml
```

Replace `YOUR_DEVELOPER_TOKEN_HERE` with your actual developer token from Step 3.

**Config File Structure**:

```yaml
# Minimal config for service account authentication
developer_token: abcd1234EFGH5678  # Your actual token
use_proto_plus: True

# Optional: Only add if using Manager (MCC) account
# login_customer_id: 1234567890
```

---

## Step 7: Add Google Ads Customers

### 7.1: Get Customer ID

1. Sign into [Google Ads](https://ads.google.com)
2. Look at the top right corner
3. Copy the **Customer ID** (format: `123-456-7890`)

### 7.2: Add Customer via Asset Management

```bash
# SSH into server
ssh -i ~/.ssh/id_ed25519 ubuntu@<server-ip>

# Launch asset management CLI
sudo docker exec -it imports_container python3 /app/python/asset-management.py
```

**Steps in Asset Management**:
1. Navigate to **Google Ads Assets** → **Add Customer**
2. Enter:
   - **Customer ID**: `123-456-7890` (from Google Ads account)
   - **Customer Title**: Display name (e.g., "Acme Corp - Google Ads")
3. Save the customer

### 7.3: Link Customer to Team

1. Navigate to **Teams** → **Edit Team**
2. Select the team you want to link
3. Set **Google Ads Integration Status** to **Active**
4. Select **Google Ads Customer** from the dropdown
5. Save the team

---

## Step 8: Test Import

### 8.1: Test Authentication

Test if credentials are working:

```bash
sudo docker exec -it imports_container python3 -c "
import sys
sys.path.insert(0, '/app/python')
from import_scripts.google_ads.get_video_campaign_data import load_google_ads_credentials
from google.ads.googleads.client import GoogleAdsClient

# Load credentials
creds = load_google_ads_credentials()
print('✓ Credentials loaded')

# Initialize client
client = GoogleAdsClient.load_from_dict(creds)
print('✓ Google Ads API client initialized')
print('✓ Service account authentication successful')
"
```

Expected output:
```
Using service account authentication for Google Ads API
✓ Google Ads service account credentials loaded successfully
✓ Credentials loaded
✓ Google Ads API client initialized
✓ Service account authentication successful
```

### 8.2: Test Data Fetch

Fetch video campaign data for the last 7 days:

```bash
sudo docker exec -it imports_container python3 /app/python/import.py google_ads get_video_campaign_data --start_date=2025-01-22 --end_date=2025-01-29
```

### 8.3: Check Output Files

```bash
# Check if JSON files were created
sudo docker exec imports_container find /app/data/google_ads/video_campaigns -name "*.json" -ls

# Check a sample file
sudo docker exec imports_container find /app/data/google_ads/video_campaigns -name "*.json" -type f | head -1 | xargs -I {} sudo docker exec imports_container cat {}
```

### 8.4: Run Full Import Pipeline

```bash
# Run full import (fetch + index + archive)
sudo docker exec imports_container python3 /app/python/import.py google_ads run_daily_imports
```

### 8.5: Verify Data in Elasticsearch

```bash
sudo docker exec imports_container python3 -c "
import sys
sys.path.insert(0, '/app/python')
from lib.es_helpers import ESHelper

es = ESHelper()
result = es.search(index='*_google_ads', body={'query': {'match_all': {}}})
total = result['hits']['total']['value']
print(f'✓ Total Google Ads records in Elasticsearch: {total}')

if total > 0:
    sample = result['hits']['hits'][0]['_source']
    print(f'\nSample record:')
    print(f'  Campaign: {sample.get(\"campaign_name\")}')
    print(f'  Date: {sample.get(\"date\")}')
    print(f'  Impressions: {sample.get(\"impressions\")}')
    print(f'  Video Views: {sample.get(\"video_views\")}')
    print(f'  Cost: ${sample.get(\"cost\")}')
"
```

---

## Troubleshooting

### Error: "Service account key not found"

**Symptom**: `FileNotFoundError: Service account key not found: /app/reporting_configs/google-ads-sa.json`

**Solution**:
```bash
# Check if file exists
sudo docker exec imports_container ls -la /app/reporting_configs/google-ads-sa.json

# If missing, re-copy from Step 5
```

---

### Error: "Developer token required"

**Symptom**: `ValueError: Developer token required for Google Ads API`

**Solution**:
```bash
# Edit YAML config and add developer token
sudo docker exec -it imports_container nano /app/reporting_configs/google-ads.yaml

# Add this line:
developer_token: YOUR_ACTUAL_TOKEN
```

---

### Error: "AuthorizationError.USER_PERMISSION_DENIED"

**Symptom**: Google Ads API returns permission denied error

**Possible Causes**:
1. Service account not added to Google Ads account
2. Insufficient access level (needs Standard or Admin)
3. Manager (MCC) account requires `login_customer_id`

**Solution**:
```bash
# 1. Verify service account email is in Google Ads users list
# Go to: ads.google.com → Tools & Settings → Users

# 2. Check access level (should be Standard or Admin)

# 3. If using MCC account, add login_customer_id to YAML:
sudo docker exec -it imports_container nano /app/reporting_configs/google-ads.yaml

# Add this line (replace with your MCC customer ID without dashes):
login_customer_id: 1234567890
```

---

### Error: "No Google Ads customers found"

**Symptom**: Warning message: "No Google Ads customers found. Add customers via asset management."

**Solution**:
```bash
# Add customers via asset management (Step 7)
sudo docker exec -it imports_container python3 /app/python/asset-management.py

# Navigate to: Google Ads Assets → Add Customer
```

---

### Error: "Skipping customer - not assigned to any team"

**Symptom**: Warning: "Skipping customer [name] - not assigned to any team or Google Ads integration not active"

**Solution**:
```bash
# Link customer to team (Step 7.3)
sudo docker exec -it imports_container python3 /app/python/asset-management.py

# Navigate to: Teams → Edit Team
# Set: Google Ads Integration Status = Active
# Select: Google Ads Customer from dropdown
```

---

### No Data Returned

**Symptom**: Import runs successfully but returns 0 campaign records

**Possible Causes**:
1. No VIDEO campaigns in the date range
2. Customer ID is incorrect
3. Campaigns are paused/removed

**Solution**:
```bash
# 1. Verify VIDEO campaigns exist in Google Ads
# Go to: ads.google.com → Campaigns → Filter by "Video"

# 2. Check customer ID format
# Should be: 123-456-7890 (with dashes)

# 3. Try wider date range
sudo docker exec imports_container python3 /app/python/import.py google_ads get_video_campaign_data --start_date=2024-01-01 --end_date=2025-01-29
```

---

## Authentication Method Detection

The import script automatically detects which authentication method to use:

**Detection Logic**:
1. If `/app/reporting_configs/google-ads-sa.json` exists → **Service Account**
2. Otherwise → **OAuth 2.0** (legacy method)

**To Switch Methods**:

```bash
# Switch to OAuth (remove service account file)
sudo docker exec imports_container rm /app/reporting_configs/google-ads-sa.json

# Switch to Service Account (add service account file)
# Follow Steps 5-6
```

---

## Security Best Practices

### 1. File Permissions

Ensure credentials have restricted permissions:

```bash
# Check permissions (should be 600)
sudo docker exec imports_container ls -la /app/reporting_configs/google-ads-sa.json

# Fix if needed
sudo docker exec imports_container chmod 600 /app/reporting_configs/google-ads-sa.json
```

### 2. Key Rotation

Rotate service account keys every 6-12 months:

```bash
# 1. Create new key in Google Cloud Console
# 2. Copy new key to server (follow Step 5)
# 3. Test import with new key
# 4. Delete old key from Google Cloud Console
```

### 3. Encryption (Optional)

Encrypt the service account key at rest using the project's encryption helper:

```bash
# Encrypt key
sudo docker exec imports_container python3 -c "
import sys
sys.path.insert(0, '/app/python')
from lib.encryption import encrypt_json_file
encrypt_json_file(
    '/app/reporting_configs/google-ads-sa.json',
    '/app/reporting_configs/google-ads-sa.json.enc'
)
print('✓ Key encrypted')
"

# Remove plaintext
sudo docker exec imports_container rm /app/reporting_configs/google-ads-sa.json

# Note: Requires modification to import script to decrypt before use
```

### 4. Audit Access

Monitor API usage in Google Cloud Console:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Navigate to **APIs & Services → Dashboard**
3. Click **Google Ads API**
4. Review usage metrics and quotas

---

## Next Steps

1. **Schedule Daily Imports**: Set up cron job for automated imports
2. **Kibana Dashboards**: Create visualizations for YouTube Ads performance
3. **Alerts**: Configure alerts for failed imports
4. **YouTube Analytics API**: Set up organic YouTube data imports (see separate guide)

---

## Additional Resources

- [Google Ads API Documentation](https://developers.google.com/google-ads/api/docs/start)
- [Video Campaigns Guide](https://developers.google.com/google-ads/api/docs/video/overview)
- [Service Account Authentication](https://cloud.google.com/iam/docs/service-accounts)
- [Google Ads API Forum](https://groups.google.com/g/adwords-api)

---

**Questions or Issues?**

Check the [main project README](../../../../README.md) or consult the [Troubleshooting](#troubleshooting) section above.

---

*Last Updated: January 29, 2026*
*Part of Digital Engagement Dashboard Project*
