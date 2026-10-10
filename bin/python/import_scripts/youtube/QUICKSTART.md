# YouTube Analytics Import - Quick Start Guide

## Overview

This guide walks you through setting up YouTube Analytics data imports using **Service Account authentication**. This approach enables automated, server-to-server data collection for organic YouTube channel metrics.

### What You'll Get

**Organic YouTube Channel Metrics**:
- Views and estimated minutes watched
- Subscribers gained/lost
- Engagement: likes, comments, shares
- Playlist metrics: videos added/removed
- Average view duration

**Data Organization**:
- Daily metrics per channel
- Team-based Elasticsearch indices
- Automatic deduplication by channel + date

---

## Prerequisites

### 1. Google Cloud Project

You need a Google Cloud project with billing enabled:
- Go to [Google Cloud Console](https://console.cloud.google.com)
- Create new project or use existing one
- Enable billing (free tier available)

### 2. YouTube Channel Access

You must have **Owner** or **Manager** access to YouTube channels you want to import. You'll need to grant service account access in YouTube Studio.

### 3. Required Python Libraries

Already included in container if using Docker deployment:
```bash
google-api-python-client
google-auth
google-auth-oauthlib
google-auth-httplib2
```

---

## Step 1: Enable YouTube APIs

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Select your project
3. Navigate to **APIs & Services > Library**
4. Search and enable these APIs:
   - **YouTube Analytics API**
   - **YouTube Data API v3**

---

## Step 2: Create Service Account

### 2.1 Create Service Account

1. Go to **IAM & Admin > Service Accounts**
2. Click **+ CREATE SERVICE ACCOUNT**
3. Fill in details:
   - **Service account name**: `youtube-analytics-sa`
   - **Service account ID**: `youtube-analytics-sa` (auto-generated)
   - **Description**: "Service account for YouTube Analytics data imports"
4. Click **CREATE AND CONTINUE**
5. Skip role assignment (click **CONTINUE**)
   - *Note: YouTube channel access is granted separately in YouTube Studio*
6. Click **DONE**

### 2.2 Create and Download Key

1. Click on the service account you just created
2. Go to **Keys** tab
3. Click **ADD KEY > Create new key**
4. Select **JSON** format
5. Click **CREATE**
6. Save the downloaded JSON file securely (e.g., `youtube-analytics-sa.json`)

**⚠️ IMPORTANT**: This key file grants access to your YouTube channels. Store it securely and never commit to version control.

### 2.3 Note the Service Account Email

You'll need the service account email for granting YouTube channel access:
```
youtube-analytics-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

Copy this email - you'll use it in Step 3.

---

## Step 3: Grant YouTube Channel Access

**You must do this for EACH YouTube channel you want to import.**

### 3.1 Access YouTube Studio

1. Go to [YouTube Studio](https://studio.youtube.com)
2. Select the channel you want to grant access to
3. Click **Settings** (gear icon in left sidebar)

### 3.2 Grant Service Account Access

1. Click **Permissions** in the left menu
2. Click **INVITE** button (top right)
3. Enter the service account email:
   ```
   youtube-analytics-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com
   ```
4. Select access level:
   - **Viewer** - Sufficient for analytics data (recommended)
   - **Editor** - If you need to modify channel settings
5. Click **INVITE**

### 3.3 Verify Access

The service account email will receive an invitation email (which you can ignore). Access is granted immediately.

**Repeat Steps 3.1-3.2 for all 13 channels you want to import.**

---

## Step 4: Install Service Account Key on Server

### 4.1 Upload Key to Server

```bash
# Copy key file to server
scp -i ~/.ssh/id_ed25519 youtube-analytics-sa.json ubuntu@<server-ip>:/tmp/
```

### 4.2 Move to Container and Set Permissions

```bash
# SSH into server
ssh -i ~/.ssh/id_ed25519 ubuntu@<server-ip>

# Move key to reporting_configs directory
sudo docker exec imports_container bash -c 'mv /tmp/youtube-analytics-sa.json /app/reporting_configs/'

# Set secure permissions (read/write for owner only)
sudo docker exec imports_container chmod 600 /app/reporting_configs/youtube-analytics-sa.json

# Verify file exists
sudo docker exec imports_container ls -la /app/reporting_configs/youtube-analytics-sa.json
```

Expected output:
```
-rw------- 1 root root 2367 Jan 29 12:00 /app/reporting_configs/youtube-analytics-sa.json
```

---

## Step 5: Add YouTube Channels to Asset Management

### 5.1 Get Channel IDs

For each YouTube channel, you need the **Channel ID** (format: `UCxxxxxxxxxxxxxx`).

**Where to find Channel ID**:

**Method 1: YouTube Studio**
1. Go to [YouTube Studio](https://studio.youtube.com)
2. Select channel
3. Click **Settings > Channel**
4. Copy the **Channel ID** (starts with `UC`, 24 characters)

**Method 2: Channel URL**
- If URL is `youtube.com/@username`, the handle is `@username`
- Channel ID can be found via: `youtube.com/channel/UCxxxxxxxxxxxxxx`

### 5.2 Add Channels via Asset Management

```bash
# SSH into server
ssh -i ~/.ssh/id_ed25519 ubuntu@<server-ip>

# Run asset management CLI
sudo docker exec -it imports_container python3 /app/python/asset-management.py
```

### 5.3 Navigate Menu

1. Main menu: Select **YouTube Channels**
2. YouTube Channels menu: Select **Add YouTube Channel**
3. Fill in details:
   - **Channel Title**: Display name (e.g., "TeamName Official")
   - **Channel ID**: `UCxxxxxxxxxxxxxx` (24 characters, starts with UC)
   - **Channel Handle**: `@username` (optional, for reference)
4. Confirm to save
5. Repeat for all 13 channels

**Example**:
```
Enter Channel Title*: Kavanah Media Official
Enter Channel ID (UCxxxxxxxxxxxxxx)*: UC1234567890123456789012
Enter Channel Handle (@username): @kavanah
```

---

## Step 6: Link Channels to Teams

Channels must be linked to teams for proper data organization and Elasticsearch indexing.

### 6.1 Link via Asset Management

```bash
# In asset-management.py menu:
1. Select **Teams**
2. Select **View All Teams**
3. Select the team you want to link channel to
4. Select **Edit Team**
5. Scroll to **YouTube Integration**
6. Select **Link YouTube Channel**
7. Choose channel from list
8. Save changes
```

### 6.2 Verify Team Configuration

Check that team has:
- `team_youtube_integration_status`: "Active"
- `youtube_channel_esid`: Channel Elasticsearch document ID

---

## Step 7: Test Import

### 7.1 Test Data Fetch

Run a test fetch for last 7 days:

```bash
sudo docker exec -it imports_container python3 /app/python/import.py youtube get_youtube_analytics --start_date=2026-01-22 --end_date=2026-01-29
```

**Expected output**:
```
============================================================
Processing: Kavanah Media Official
Channel ID: UC1234567890123456789012
============================================================
Team: Kavanah Media
✓ Fetched 7 daily records for channel UC1234567890123456789012 (2026-01-22 to 2026-01-29)
```

### 7.2 Verify JSON Files

Check that JSON files were created:

```bash
sudo docker exec imports_container find /app/data/youtube_analytics/channel_metrics -name "*.json" -ls
```

Expected output:
```
/app/data/youtube_analytics/channel_metrics/{channel_esid}/{team_esid}/2026-01-22_{channel_esid}_metrics.json
/app/data/youtube_analytics/channel_metrics/{channel_esid}/{team_esid}/2026-01-23_{channel_esid}_metrics.json
...
```

### 7.3 Inspect JSON Data

```bash
sudo docker exec imports_container cat /app/data/youtube_analytics/channel_metrics/*/*/2026-01-29_*_metrics.json | head -50
```

Expected structure:
```json
{
  "@timestamp": "2026-01-29T12:34:56.789123+00:00",
  "average_view_duration": 245.5,
  "comments": 12,
  "date": "2026-01-29",
  "estimated_minutes_watched": 1234,
  "likes": 89,
  "organization_title": "Kavanah Media",
  "shares": 5,
  "subscribers_gained": 15,
  "subscribers_lost": 3,
  "team_city": "Toronto",
  "team_country": "Canada",
  "team_esid": "abc123",
  "team_title": "Kavanah Media",
  "team_url": "kavanah.com",
  "videos_added_to_playlists": 2,
  "videos_removed_from_playlists": 0,
  "views": 5432,
  "youtube_channel_esid": "xyz789",
  "youtube_channel_handle": "@kavanah",
  "youtube_channel_id": "UC1234567890123456789012",
  "youtube_channel_title": "Kavanah Media Official"
}
```

---

## Step 8: Index to Elasticsearch

### 8.1 Run Indexing Script

```bash
sudo docker exec imports_container python3 /app/python/import.py youtube send_json_to_elastic
```

**Expected output**:
```
✓ Index kavanah_media_youtube_analytics ensured to exist
Progress: 100 indexed, 0 errors
============================================================
YouTube Analytics Indexing Complete
============================================================
Total JSON files processed: 91
Successfully indexed: 91
Errors: 0
Indices created/updated: 1
Index names: kavanah_media_youtube_analytics
============================================================
```

### 8.2 Verify Elasticsearch Data

```bash
sudo docker exec imports_container python3 -c "
from lib.es_helpers import get_elasticsearch_client

client = get_elasticsearch_client()
result = client.count(index='*_youtube_analytics')
print(f'Total YouTube Analytics records: {result[\"count\"]}')
"
```

---

## Step 9: Run Full Import Workflow

### 9.1 Run Daily Import Orchestrator

This runs all three steps: fetch → index → archive

```bash
sudo docker exec -it imports_container python3 /app/python/import.py youtube run_daily_imports --start_date=2026-01-01 --end_date=2026-01-29
```

**Expected output**:
```
============================================================
YouTube Analytics Daily Import Workflow
============================================================
Date range: 2026-01-01 to 2026-01-29
============================================================

============================================================
Step: Fetch YouTube Analytics Data
Command: python3 /app/python/import.py youtube get_youtube_analytics --start_date 2026-01-01 --end_date 2026-01-29
============================================================
✓ Fetch YouTube Analytics Data completed successfully

============================================================
Step: Index to Elasticsearch
Command: python3 /app/python/import.py youtube send_json_to_elastic
============================================================
✓ Index to Elasticsearch completed successfully

============================================================
Step: Archive Processed Files
Command: python3 /app/python/import.py youtube archive_imported_json
============================================================
✓ Archive Processed Files completed successfully

============================================================
✓ YouTube Analytics Daily Import Workflow COMPLETED
============================================================
```

### 9.2 Verify Archived Files

Check that files were moved to archive:

```bash
sudo docker exec imports_container find /app/data/youtube_analytics/channel_metrics_imported -name "*.json" -ls | head -10
```

---

## Step 10: Schedule Daily Imports

### 10.1 Add to Crontab

```bash
# Edit crontab on server
crontab -e

# Add daily import at 6:00 AM
0 6 * * * docker exec imports_container python3 /app/python/import.py youtube run_daily_imports >> /var/log/youtube_analytics_import.log 2>&1
```

### 10.2 Verify Cron Job

```bash
crontab -l | grep youtube
```

---

## Step 11: Visualize in Kibana

### 11.1 Access Kibana

1. Go to: `https://<your-host>/kibana`
2. Login with credentials

### 11.2 Create Index Pattern

1. **Management > Stack Management > Index Patterns**
2. Click **Create index pattern**
3. Index pattern: `*_youtube_analytics`
4. Click **Next step**
5. Time field: `kibana_date`
6. Click **Create index pattern**

### 11.3 Explore Data

1. **Analytics > Discover**
2. Select index pattern: `*_youtube_analytics`
3. Set time range (last 30 days)
4. View fields:
   - `youtube_channel_title.keyword`
   - `team_title.keyword`
   - `views`, `estimated_minutes_watched`
   - `subscribers_gained`, `subscribers_lost`
   - `likes`, `comments`, `shares`

### 11.4 Create Visualizations

**Example: Channel Views Over Time**
1. **Visualize Library > Create visualization**
2. Type: **Line chart**
3. X-axis: `date` (Date histogram)
4. Y-axis: Sum of `views`
5. Split series: `youtube_channel_title.keyword`
6. Save: "YouTube Channel Views - Last 30 Days"

**Example: Subscriber Growth**
1. Type: **Area chart**
2. X-axis: `date`
3. Y-axis: Sum of `subscribers_gained` (green) + Sum of `subscribers_lost` (red)
4. Split series: `youtube_channel_title.keyword`

---

## Troubleshooting

### Error: "Service account key not found"

**Symptoms**:
```
FileNotFoundError: Service account key not found: /app/reporting_configs/youtube-analytics-sa.json
```

**Solutions**:
1. Verify file exists:
   ```bash
   sudo docker exec imports_container ls -la /app/reporting_configs/youtube-analytics-sa.json
   ```
2. Check file permissions (should be 600):
   ```bash
   sudo docker exec imports_container stat -c '%a' /app/reporting_configs/youtube-analytics-sa.json
   ```
3. Re-upload key if missing (see Step 4)

---

### Error: "insufficientPermissions" or "forbidden"

**Symptoms**:
```
Permission denied for channel UCxxxxxxxxxxxxxx:
  The caller does not have permission
  → Service account needs 'Viewer' access in YouTube Studio
  → Go to: studio.youtube.com → Settings → Permissions
```

**Solutions**:
1. **Verify service account email** is correct:
   ```bash
   sudo docker exec imports_container python3 -c "
   import json
   with open('/app/reporting_configs/youtube-analytics-sa.json') as f:
       data = json.load(f)
       print(data['client_email'])
   "
   ```
2. **Grant access in YouTube Studio** (repeat Step 3):
   - Go to YouTube Studio for the channel
   - Settings > Permissions
   - Invite service account email
   - Grant "Viewer" access
3. **Wait 5-10 minutes** for permissions to propagate
4. **Test with single channel**:
   ```bash
   sudo docker exec -it imports_container python3 /app/python/import.py youtube get_youtube_analytics --channel_ids=UC1234567890123456789012 --start_date=2026-01-28 --end_date=2026-01-29
   ```

---

### Error: "No YouTube channels found"

**Symptoms**:
```
Found 0 YouTube channels in Elasticsearch
No YouTube channels found. Add channels via asset management.
```

**Solutions**:
1. **Add channels via asset management** (Step 5)
2. **Verify channels exist in Elasticsearch**:
   ```bash
   sudo docker exec imports_container python3 -c "
   from lib.es_helpers import search_index
   result = search_index('de_asset_youtube_channels', {'match_all': {}}, size=100)
   print(f'Channels: {result[\"hits\"][\"total\"][\"value\"]}')
   for hit in result['hits']['hits']:
       print(f'  - {hit[\"_source\"][\"youtube_channel_title\"]} ({hit[\"_source\"][\"youtube_channel_id\"]})')
   "
   ```

---

### Error: "Skipping channel - not assigned to any team"

**Symptoms**:
```
Skipping channel Kavanah Media Official - not assigned to any team or YouTube integration not active
```

**Solutions**:
1. **Link channel to team** (Step 6)
2. **Verify team configuration**:
   ```bash
   sudo docker exec imports_container python3 -c "
   from lib.es_helpers import search_index
   result = search_index('de_asset_teams', {
       'bool': {
           'must': [
               {'term': {'team_youtube_integration_status.keyword': 'Active'}}
           ]
       }
   }, size=100)
   print(f'Teams with YouTube integration: {result[\"hits\"][\"total\"][\"value\"]}')
   for hit in result['hits']['hits']:
       team = hit['_source']
       print(f'  - {team[\"team_title\"]}: {team.get(\"youtube_channel_esid\", \"NOT LINKED\")}')
   "
   ```
3. **Set YouTube integration status to "Active"** in asset management

---

### Error: "quotaExceeded"

**Symptoms**:
```
YouTube Analytics API error: Quota exceeded for quota metric 'Queries' and limit 'Queries per day'
```

**Solutions**:
1. **Check quota usage**: [Google Cloud Console > APIs & Services > Dashboard](https://console.cloud.google.com/apis/dashboard)
2. **Default quota**: 10,000 queries/day (sufficient for 13 channels × 30 days = 390 queries)
3. **Request quota increase** if needed:
   - Go to **APIs & Services > YouTube Analytics API > Quotas**
   - Click **EDIT QUOTAS**
   - Request increase with justification
4. **Reduce frequency**: Run imports once per day instead of multiple times

---

### Error: Invalid Channel ID Format

**Symptoms**:
```
⚠️  Warning: Channel ID should start with 'UC' and be 24 characters long.
```

**Solutions**:
1. **Find correct Channel ID**:
   - YouTube Studio > Settings > Channel
   - Copy the ID (format: `UCxxxxxxxxxxxxxx`, exactly 24 characters)
2. **Don't use**:
   - Channel handle (`@username`)
   - Channel custom URL (`youtube.com/c/customname`)
   - Video IDs (different format)

---

### No Data Returned for Channel

**Symptoms**:
```
✓ Fetched 0 daily records for channel UCxxxxxxxxxxxxxx
No data returned for channel Kavanah Media Official
```

**Possible causes**:
1. **Channel has no activity** in date range
2. **Date range is in the future** (YouTube Analytics only has historical data)
3. **Service account doesn't have access** (no error thrown, just no data)

**Solutions**:
1. **Check channel activity**:
   - Go to YouTube Studio > Analytics
   - Verify channel has views/activity in date range
2. **Use date range with known activity**:
   ```bash
   --start_date=2026-01-01 --end_date=2026-01-29
   ```
3. **Verify service account access** in YouTube Studio

---

## Architecture Overview

### Data Flow

```
YouTube Channels (13 channels)
        ↓
Service Account Authentication
(youtube-analytics-sa.json)
        ↓
get_youtube_analytics.py
- Queries YouTube Analytics API
- Daily metrics per channel
- Saves as JSON files
        ↓
/app/data/youtube_analytics/channel_metrics/
{channel_esid}/{team_esid}/YYYY-MM-DD_{channel_esid}_metrics.json
        ↓
send_json_to_elastic.py
- Creates team-specific indices
- Pattern: {team_title}_youtube_analytics
- MD5 deduplication
        ↓
Elasticsearch Indices
- kavanah_media_youtube_analytics
- [other_team]_youtube_analytics
        ↓
archive_imported_json.py
- Moves to channel_metrics_imported/
- Prevents reprocessing
        ↓
Kibana Dashboards
- Channel performance
- Subscriber growth
- Engagement metrics
```

### File Structure

```
/app/data/youtube_analytics/
├── channel_metrics/                # Pending import
│   ├── {channel_esid}/
│   │   └── {team_esid}/
│   │       ├── 2026-01-22_{channel_esid}_metrics.json
│   │       ├── 2026-01-23_{channel_esid}_metrics.json
│   │       └── ...
└── channel_metrics_imported/        # Archived after import
    └── [same structure]
```

### Elasticsearch Indices

**Asset Indices**:
- `de_asset_youtube_channels` - Channel configurations
- `de_asset_teams` - Team configurations with channel links

**Data Indices** (per team):
- `{team_title}_youtube_analytics` - Daily channel metrics

---

## Security Best Practices

### 1. Service Account Key Protection

**DO**:
- ✅ Store key with 600 permissions (owner read/write only)
- ✅ Keep key in `/app/reporting_configs/` (not in code repo)
- ✅ Use encrypted storage if available (see Phase 3 in plan)
- ✅ Rotate keys every 6-12 months

**DON'T**:
- ❌ Commit service account key to version control
- ❌ Share key file via email or Slack
- ❌ Store key in world-readable locations
- ❌ Use same service account for multiple purposes

### 2. Principle of Least Privilege

**Service Account Permissions**:
- Grant only "Viewer" access in YouTube Studio
- Don't grant project-level IAM roles
- Use separate service accounts for different APIs

**Channel Access**:
- Only grant access to channels you need to import
- Regularly audit service account access
- Remove access when no longer needed

### 3. Key Rotation

**Schedule**:
- Create new service account key every 6-12 months
- Update key on server
- Delete old key from Google Cloud Console
- Monitor for unauthorized access attempts

**Rotation Steps**:
1. Create new key in Google Cloud Console
2. Upload new key to server
3. Test imports with new key
4. Delete old key from Google Cloud Console
5. Update documentation with rotation date

---

## Optional: Encryption Enhancement

See `PLAN-youtube-import-authentication-strategy.md` Phase 3 for adding encryption support.

**Benefits**:
- Service account keys encrypted at rest
- Uses existing `lib/encryption.py` (Fernet encryption)
- Reduces risk if server is compromised

**Setup Time**: 1-2 hours

---

## API Metrics Reference

### YouTube Analytics API Metrics

| Metric | Field Name | Type | Description |
|--------|-----------|------|-------------|
| Views | `views` | long | Total video views |
| Watch Time | `estimated_minutes_watched` | long | Total minutes watched |
| Avg Duration | `average_view_duration` | float | Average watch time per view (seconds) |
| Subscribers Gained | `subscribers_gained` | integer | New subscribers |
| Subscribers Lost | `subscribers_lost` | integer | Unsubscribes |
| Likes | `likes` | integer | Total likes |
| Comments | `comments` | integer | Total comments |
| Shares | `shares` | integer | Total shares |
| Playlist Adds | `videos_added_to_playlists` | integer | Videos added to playlists |
| Playlist Removes | `videos_removed_from_playlists` | integer | Videos removed from playlists |

### Dimensions

- **day**: Daily breakdown (YYYY-MM-DD format)

### Date Range

- **Default**: Last 30 days
- **Maximum**: No hard limit, but large ranges may hit quota limits
- **Recommended**: 30-90 day imports, run daily for incremental updates

---

## Support Resources

### Google Documentation

- [YouTube Analytics API](https://developers.google.com/youtube/analytics)
- [Service Account Authentication](https://cloud.google.com/iam/docs/service-accounts)
- [YouTube Data API](https://developers.google.com/youtube/v3)

### Project Documentation

- Main README: `../../README.md`
- Deployment Notes: `docs/dev-server-deployment-notes.md`
- Authentication Strategy: `docs/PLAN-youtube-import-authentication-strategy.md`

### Troubleshooting

1. Check container logs:
   ```bash
   sudo docker logs imports_container --tail 100
   ```

2. Check import logs:
   ```bash
   sudo docker exec imports_container find /app/logs -name "*youtube*" -exec tail {} \;
   ```

3. Verify Elasticsearch connectivity:
   ```bash
   sudo docker exec imports_container python3 -c "from lib.es_helpers import get_elasticsearch_client; client = get_elasticsearch_client(); print(client.info())"
   ```

---

## Next Steps

After successful setup:

1. **Create Kibana Dashboards**:
   - Channel performance comparison
   - Subscriber growth trends
   - Engagement rates (likes/views, comments/views)
   - Watch time analysis

2. **Set Up Alerts** (optional):
   - Subscriber loss threshold
   - Views drop detection
   - Engagement rate changes

3. **Optimize Import Schedule**:
   - Daily imports at 6:00 AM (recommended)
   - Weekly full re-sync (optional)
   - Monthly data validation

4. **Add More Channels**:
   - Repeat Steps 3, 5, and 6 for new channels
   - No code changes needed

---

**Last Updated**: 2026-01-29
**Version**: 1.0.0
**Author**: Digital Engagement Dashboard Team
