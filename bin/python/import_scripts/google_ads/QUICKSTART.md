# Google Ads API Import - Quick Start Guide

## Overview

This module imports YouTube advertising performance data from the Google Ads API. It fetches metrics for VIDEO campaigns and indexes them to Elasticsearch for visualization in Kibana.

## Prerequisites

### 1. Google Ads API Access

You need a Google Ads account with API access enabled. Follow these steps:

1. **Apply for Google Ads API Access**
   - Visit: https://developers.google.com/google-ads/api/docs/get-started/oauth-cloud-project
   - Apply for a developer token (can take 1-2 days for approval)

2. **Create OAuth 2.0 Credentials**
   - Go to: https://console.cloud.google.com/apis/credentials
   - Create a new OAuth 2.0 Client ID (Application type: Web application)
   - Note your Client ID and Client Secret

3. **Generate Refresh Token**
   - Use OAuth 2.0 Playground: https://developers.google.com/oauthplayground/
   - Select "Google Ads API" scope: `https://www.googleapis.com/auth/adwords`
   - Authorize and exchange authorization code for refresh token

### Required Credentials

- **Developer Token**: From Google Ads → Tools & Settings → API Center
- **Client ID**: OAuth 2.0 Client ID from Google Cloud Console
- **Client Secret**: OAuth 2.0 Client Secret
- **Refresh Token**: From OAuth 2.0 Playground
- **Login Customer ID** (optional): Only if accessing through a Manager (MCC) account

## Installation

### 1. Install Google Ads API Library

Run the installation script:

```bash
./bin/scripts/install_google_ads_api.sh
```

This will:
- Install the `google-ads` Python library
- Prompt you for API credentials
- Create `/app/reporting_configs/google-ads.yaml` config file

### 2. Configure Credentials

If you skipped configuration during installation, manually edit:

```bash
/app/reporting_configs/google-ads.yaml
```

Example configuration:

```yaml
# Google Ads API Configuration
developer_token: YOUR_DEVELOPER_TOKEN
client_id: YOUR_CLIENT_ID.apps.googleusercontent.com
client_secret: YOUR_CLIENT_SECRET
refresh_token: YOUR_REFRESH_TOKEN

# Optional: Only for MCC accounts
#login_customer_id: 1234567890

use_proto_plus: True
```

**Important**: File permissions should be `600` (owner read/write only)

## Asset Setup

### 1. Add Google Ads Customers

Use the asset management system to add Google Ads customers:

```bash
python3 /app/python/asset-management.py
```

Navigate to: **Google Ads Assets → Add Customer**

Required fields:
- **Customer ID**: 10-digit Google Ads customer ID (format: `123-456-7890`)
- **Customer Title**: Display name

### 2. Link Customer to Team

In the Teams management section:

1. Edit the team that should use this Google Ads customer
2. Set **Google Ads Integration Status** to `Active`
3. Select the **Google Ads Customer** from dropdown
4. Save

## Running Imports

### Test Connection (Optional)

```bash
python3 /app/python/import.py google_ads test_connection
```

### Run Daily Import

Import the last 30 days of data:

```bash
python3 /app/python/import.py google_ads run_daily_imports
```

### Custom Date Range

```bash
python3 /app/python/import.py google_ads run_daily_imports \
  --start_date=2025-01-01 \
  --end_date=2025-01-29
```

### Import Specific Customer Only

```bash
python3 /app/python/import.py google_ads run_daily_imports \
  --customer_ids=123-456-7890
```

### Individual Steps

You can also run steps individually:

```bash
# Step 1: Fetch data from Google Ads API
python3 /app/python/import.py google_ads get_video_campaign_data \
  --start_date=2025-01-01 \
  --end_date=2025-01-29

# Step 2: Index to Elasticsearch
python3 /app/python/import.py google_ads send_json_to_elastic

# Step 3: Archive processed files
python3 /app/python/import.py google_ads archive_imported_json
```

## Data Flow

1. **Fetch**: `get_video_campaign_data.py`
   - Queries Google Ads API for VIDEO campaigns
   - Fetches performance metrics (impressions, clicks, cost, video views, etc.)
   - Matches campaigns to teams via customer ESID
   - Saves as JSON: `/app/data/google_ads/video_campaigns/{customer_esid}/{team_esid}/`

2. **Index**: `send_json_to_elastic.py`
   - Reads JSON files from `video_campaigns/` directory
   - Creates team-specific indices: `{team_title}_google_ads`
   - Indexes documents with deduplication
   - Logs summary of indexed documents

3. **Archive**: `archive_imported_json.py`
   - Moves processed files to `video_campaigns_imported/`
   - Prevents reprocessing on next run

## Elasticsearch Indices

Data is indexed to team-specific indices:

**Pattern**: `{team_title}_google_ads`

Example: `example_team_google_ads`

### Available Fields

**Campaign Info**:
- campaign_id, campaign_name, campaign_status
- campaign_channel_type (always "VIDEO")

**Ad Group Info**:
- ad_group_id, ad_group_name, ad_group_status

**Ad Info**:
- ad_id, ad_name, video_id

**Metrics**:
- impressions, clicks, cost, conversions
- video_views, video_quartile_*_rate
- average_cpv, average_cpm, ctr
- view_through_conversions

**Metadata**:
- team_title, team_esid, organization_title
- google_ads_customer_id, google_ads_customer_title
- date, @timestamp, kibana_date

## Kibana Visualization

After import, create visualizations in Kibana:

1. **Create Index Pattern**:
   - Go to: Stack Management → Index Patterns
   - Pattern: `*_google_ads`
   - Time field: `kibana_date`

2. **Example Visualizations**:
   - Video view trends over time
   - Cost per view by campaign
   - Click-through rate comparisons
   - Video completion rates (quartile analysis)
   - Campaign performance by team/organization

## Troubleshooting

### "Config file not found" Error

Run the installation script:
```bash
./bin/scripts/install_google_ads_api.sh
```

### "No Google Ads customers found" Warning

Add customers via asset management system. Ensure customers are linked to teams with active Google Ads integration.

### API Authentication Errors

- Verify developer token is approved (not test token)
- Check OAuth credentials are correct
- Ensure refresh token hasn't expired
- If using MCC, verify `login_customer_id` is set

### "Rate limit exceeded" Errors

Google Ads API has rate limits. The script will automatically retry with exponential backoff, but you may need to reduce the date range.

### No Data Returned

- Verify the customer ID has VIDEO campaigns
- Check date range includes campaign activity
- Ensure campaigns had impressions/spend during period
- Review Google Ads account for campaign status

## API Documentation

- **Google Ads API**: https://developers.google.com/google-ads/api/docs/start
- **Video Campaigns**: https://developers.google.com/google-ads/api/docs/video/overview
- **OAuth Setup**: https://developers.google.com/google-ads/api/docs/get-started/oauth-cloud-project
- **GAQL Reference**: https://developers.google.com/google-ads/api/docs/query/overview

## Support

For issues with:
- **API credentials**: See Google Ads API documentation
- **Import scripts**: Check logs in `/app/data/logs/`
- **Asset management**: Use `asset-management.py` CLI

## What Data Is Imported?

This module imports **YouTube advertising campaigns only** (VIDEO channel type). It does NOT import:
- Organic YouTube channel analytics (use YouTube Analytics API for that)
- Search/Display/Shopping campaigns (different channel types)
- Non-video ad formats

The channel information (Channel Name, Channel Handle) mentioned in your original question is NOT required for this import. The Google Ads API provides advertising performance data based on the **Google Ads Customer ID** (the advertiser account), not the YouTube channel being promoted.

To get organic YouTube metrics (subscribers, views, watch time), you would need a separate integration with the YouTube Analytics API using the channel ID.
