# Migration Guide

This guide covers:

1. **[ ship runbook](#--ship-runbook)** — the recommended way to deploy these tickets to a fresh environment (one wrapper script + manual steps that need human judgment).
2. **[ follow-up: Marker cleanup + legacy text-mapping audit](#follow-up)** — detail on the marker + text-mapping pieces.
3. **[ follow-up: FB insight leak cleanup](#follow-up_2)** — detail on the FB-page-leak cleanup.
4. **[Unified to team-specific indices](#unified-to-team-specific-indices)** — historical one-time migration, kept for reference.

---

## ship runbook

When you're ready to deploy this batch to prod, the order is:

### 1. Standard deploy (auto-runs marker cleanup)

```bash
ssh ubuntu@<prod-host>
cd ~/<repo>
git pull origin main
bash deployment/02_deploy_stack.sh
```

`02_deploy_stack.sh` automatically runs `07_post_deploy_migrations.sh` after the health check passes — that's the marker cleanup for all 7 platforms. Idempotent.

### 2. Run the manual migrations

```bash
# Dry-run first — see what's broken / leaked before changing anything
bash deployment/08_post_deploy.sh

# If output looks reasonable, apply with snapshots:
bash deployment/08_post_deploy.sh --apply --repo daily_repo
```

The wrapper runs:
- `audit_legacy_text_mappings` (follow-up — see [section below](#follow-up))
- `cleanup_leaked_fb_insights` (see [section below](#follow-up_2))
- A reminder about the `45 7 * * *` `prewarm_city_geocache` cron that obsoletes

Each `--apply` step takes a snapshot first and refuses to proceed if the snapshot fails. So a misconfigured `daily_repo` produces a clean error, not data loss.

On staging or dev, pass `--repo migration_repo` instead.

### 3. Repopulate indices that step 2 deleted

If the audit flagged any broken indices, they were deleted. Re-run the relevant import to recreate them with the correct schema:

```bash
sudo docker exec imports_container python3 /app/python/import.py \
    facebook run_daily_imports --start_date <D> --end_date <D> --team_ids <TEAM>
```

Or just wait — the next daily cron run will recreate them.

### 4. Remove the standalone prewarm cron

```bash
sudo crontab -l            # confirm "45 7 * * * ... prewarm_city_geocache" exists
sudo crontab -e            # remove that line
```

 folds prewarm into `run_daily_imports`, so the standalone cron is redundant.

### 5. Verify

```bash
# Audit should now report a clean cluster
bash deployment/08_post_deploy.sh
```

And spot-check the affected dashboards (e.g. one team's Facebook Insights table should only show pages actually owned by that team).

### Restore from snapshot if anything goes wrong

The snapshot names are logged in step 2's output. To restore one:

```bash
sudo docker exec imports_container curl -X POST \
    "http://es01:9200/_snapshot/daily_repo/<snapshot-name>/_restore" \
    -H 'Content-Type: application/json' -d '{}'
```

Snapshots are named `pre-legacy-cleanup-<TS>` and `pre-fb-leak-cleanup-<TS>`.

---

## follow-up

### Automated: marker cleanup runs every deploy

`deployment/02_deploy_stack.sh` invokes `deployment/07_post_deploy_migrations.sh` after the health check passes. That script runs `cleanup_no_data_markers` for every platform that writes markers (FB ads, FB pages, IG, Google Ads, GA, YouTube, Chatwoot). It is idempotent and safe to re-run — each migration no-ops on a clean cluster.

You don't need to invoke `07_post_deploy_migrations.sh` directly. The standard prod deploy is:

```bash
ssh ubuntu@<prod-host>
cd ~/<repo>
git pull origin main
bash deployment/02_deploy_stack.sh
```

If for some reason you want to re-run only the migrations without redeploying the stack (e.g. you suspect a marker drift mid-cycle):

```bash
bash deployment/07_post_deploy_migrations.sh           # apply
bash deployment/07_post_deploy_migrations.sh --dry-run # report only, no writes
```

### Manual: legacy text-mapping audit and cleanup

Indices created before the fix may have ES-auto-detected mappings (`text +.keyword`) where the unified schema requires `keyword`. These indices break aggregation queries and the pre-filter. The fix is to delete the broken indices and let the next import recreate them via `_ensure_index_with_schema`.

This is **not** in the automated path because it deletes indices and re-imports, which is destructive enough to warrant operator confirmation.

#### Step 1: dry-run survey

```bash
sudo docker exec imports_container python3 /app/python/import.py \
    migration audit_legacy_text_mappings
```

Output lists every broken index and the offending fields. Empty output means the cluster is clean and no manual cleanup is needed.

#### Step 2: apply (snapshot + delete)

If the survey found broken indices, take a snapshot first and then delete them:

```bash
# On staging / dev (uses migration_repo):
sudo docker exec imports_container python3 /app/python/import.py \
    migration audit_legacy_text_mappings --apply --repo migration_repo

# On prod (uses daily_repo, the existing filesystem snapshot repo):
sudo docker exec imports_container python3 /app/python/import.py \
    migration audit_legacy_text_mappings --apply --repo daily_repo
```

The script:
1. Takes a snapshot named `pre-legacy-cleanup-<YYYYMMDD-HHMMSS>` to the named repo.
2. Refuses to proceed if the snapshot fails (you'll get an error and no deletes happen).
3. Deletes every broken index.
4. Logs a summary including the snapshot name (in case you need to restore).

#### Step 3: re-run imports to repopulate

The deleted indices are recreated by the next import that writes to them, with the correct schema via `_ensure_index_with_schema`. For a single team:

```bash
sudo docker exec imports_container python3 /app/python/import.py \
    facebook run_daily_imports --start_date <D> --end_date <D> --team_ids <TEAM>
```

Or just wait for the daily cron and the indices come back filled.

#### Step 4: verify

Re-run the audit. It should now report no broken indices:

```bash
sudo docker exec imports_container python3 /app/python/import.py \
    migration audit_legacy_text_mappings
```

#### Restore from snapshot if needed

If something goes wrong, the snapshot name was logged in step 2:

```bash
sudo docker exec imports_container curl -X POST \
    "http://es01:9200/_snapshot/<repo>/<snapshot-name>/_restore" \
    -H 'Content-Type: application/json' -d '{}'
```

### When to run the manual step

- **Once before promoting to prod** — the broken-index set is unknown until you run the survey on prod. Staging had 16 broken indices; prod's set will differ.
- **Whenever a future schema change adds a new keyword field** — the audit will surface indices that still have the old text-auto-mapping for it.

---

## follow-up

### Manual: FB insight leak cleanup

Insight docs can persist for FB pages that were once configured for a team and later removed from `team_facebook_page_id`. The page's data is no longer being imported (the asset chain doesn't reach it anymore), but the historical docs sit in the team's `*_fb_insights` / `*_fb_page_insights` index forever, polluting dashboards.

The cleanup script defines a leak as **a doc whose page-id field (`page_id` for ad insights, `fb_page_id` for page insights) is not in the index-owning team's current `team_facebook_page_id` list**. It also audits `de_asset_teams` for genuine cross-team page-id collisions.

#### Step 1: dry-run survey

```bash
sudo docker exec imports_container python3 /app/python/import.py \
    migration cleanup_leaked_fb_insights
```

Output reports per-index leak counts plus any cross-team page-id collisions in `de_asset_teams`. Empty output means the cluster is clean.

#### Step 2: apply (snapshot + delete)

```bash
# Staging / dev:
sudo docker exec imports_container python3 /app/python/import.py \
    migration cleanup_leaked_fb_insights --apply --repo migration_repo

# Prod:
sudo docker exec imports_container python3 /app/python/import.py \
    migration cleanup_leaked_fb_insights --apply --repo daily_repo
```

Snapshot name: `pre-fb-leak-cleanup-<YYYYMMDD-HHMMSS>` in the named repo. Refuses to delete if snapshot fails.

#### Indices skipped safely

- Empty indices (nothing to clean).
- Indices whose `team_title` doesn't resolve to a team in `de_asset_teams` (e.g. test-data leftovers).
- Teams with empty `team_facebook_page_id` lists (no truth to compare against — wouldn't be safe to assume everything is a leak).

#### When the audit reports cross-team collisions

If two teams share a page id in their `team_facebook_page_id` lists, that's a real asset-config error. The cleanup script will retain that page's docs in BOTH teams' indices — both teams' lists "claim" the page, so neither's docs look like leaks. The audit flags the collision so you know to fix the asset config in DEAM. After fixing, re-run the cleanup.

A future enhancement would add detect-and-warn at import time inside `get_page_insights.py`. As of 2026-05-04 the audit found zero collisions on dev, so this enhancement isn't currently load-bearing.

#### Restore from snapshot if needed

```bash
sudo docker exec imports_container curl -X POST \
    "http://es01:9200/_snapshot/<repo>/<snapshot-name>/_restore" \
    -H 'Content-Type: application/json' -d '{}'
```

### When to run

- **Once before promoting to prod** — the leak set is unknown until the survey runs on prod. Dev had 210 leak docs across 3 indices.
- **After any major asset-config change** that removes pages from teams. Now that the script exists and is idempotent, this becomes a self-healing maintenance task.

---

## Unified to team-specific indices

This guide covers migrating data from unified Elasticsearch indices to team-specific indices for role-based access control.

## Overview

**Before (Unified Indices):**
```
fb_insights           ← All teams mixed together
ig_insights           ← All teams mixed together
ga_metrics            ← All teams mixed together
echo_conversations    ← All teams mixed together
```

**After (Team-Specific Indices):**
```
kavanah_media_fb_insights
<team>_fb_insights
brisbane_fb_insights
kavanah_media_ga_metrics
...
```

## Migration Process

### Step 1: Run Migration Script

The migration script reads documents from unified indices, groups them by `team_title`, and creates team-specific indices.

**Migrate specific data source:**
```bash
# SSH to server
ssh -i ~/.ssh/id_ed25519 ubuntu@<server-ip>

# Navigate to directory
cd /home/ubuntu/DE-Reporting

# Migrate Facebook data
sudo docker exec -it imports_container python3 /app/python/import_scripts/migration/migrate_unified_to_team_indices.py facebook

# Migrate Instagram data
sudo docker exec -it imports_container python3 /app/python/import_scripts/migration/migrate_unified_to_team_indices.py instagram

# Migrate Google Analytics data
sudo docker exec -it imports_container python3 /app/python/import_scripts/migration/migrate_unified_to_team_indices.py google_analytics

# Migrate Echo data
sudo docker exec -it imports_container python3 /app/python/import_scripts/migration/migrate_unified_to_team_indices.py echo
```

**Migrate all data sources at once:**
```bash
sudo docker exec -it imports_container python3 /app/python/import_scripts/migration/migrate_unified_to_team_indices.py all
```

### Step 2: Verify Migration

The migration script automatically verifies document counts after migration. You can also manually verify:

**Check team-specific indices were created:**
```bash
# Facebook
sudo docker exec -it imports_container curl -s 'http://es01:9200/_cat/indices/*_fb_insights?v'

# Instagram
sudo docker exec -it imports_container curl -s 'http://es01:9200/_cat/indices/*_ig_insights?v'

# Google Analytics
sudo docker exec -it imports_container curl -s 'http://es01:9200/_cat/indices/*_ga_metrics?v'

# Echo
sudo docker exec -it imports_container curl -s 'http://es01:9200/_cat/indices/*_echo_conversations?v'
```

**Verify data integrity:**
```bash
# Query a specific team index
sudo docker exec -it imports_container curl -s 'http://es01:9200/kavanah_media_fb_insights/_search?size=1&pretty'

# Check that team_title matches index name
sudo docker exec -it imports_container curl -s 'http://es01:9200/kavanah_media_fb_insights/_search?pretty' \
  -H 'Content-Type: application/json' \
  -d '{"query": {"match_all": {}}, "size": 0, "aggs": {"teams": {"terms": {"field": "team_title.keyword"}}}}'
```

**Expected**: All documents in `kavanah_media_fb_insights` should have `team_title: "Kavanah Media"`

### Step 3: Test New Imports

Test that new imports write to team-specific indices:

```bash
# Run a fresh import (make sure to delete recent JSON files first if needed)
sudo docker exec -it imports_container python3 /app/python/import.py facebook run_daily_imports

# Verify new data went to team-specific indices
sudo docker exec -it imports_container curl -s 'http://es01:9200/_cat/indices/*_fb_insights?v'
```

### Step 4: Update Kibana Data Views

Create new data views that reference team-specific indices or wildcard patterns:

**Option A: Wildcard Data Views** (All teams combined)
- Navigate to Kibana → Stack Management → Data Views
- Create new data view with pattern: `*_fb_insights`
- This allows querying across all team indices

**Option B: Team-Specific Data Views**
- Create separate data views for each team
- Example: `kavanah_media_fb_insights`
- Better for strict access control

### Step 5: Clean Up Unified Indices

Once migration is verified and tested, delete the old unified indices:

**Interactive cleanup** (recommended):
```bash
# Cleanup specific data source (prompts for confirmation)
sudo docker exec -it imports_container python3 /app/python/import_scripts/migration/cleanup_unified_indices.py facebook

# Cleanup all data sources (prompts for each)
sudo docker exec -it imports_container python3 /app/python/import_scripts/migration/cleanup_unified_indices.py all
```

**Force cleanup** (no confirmation prompts):
```bash
# Use with caution!
sudo docker exec -it imports_container python3 /app/python/import_scripts/migration/cleanup_unified_indices.py all --force
```

**Manual deletion** (alternative):
```bash
# Delete individual indices
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/fb_insights'
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/ig_insights'
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/ga_metrics'
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/echo_conversations'
```

## Migration Script Details

### migrate_unified_to_team_indices.py

**Features:**
- Reads all documents from unified indices using scroll API
- Groups documents by `team_title` field
- Sanitizes team names for index naming (lowercase, underscores)
- Creates team-specific indices with proper schema
- Bulk indexes documents for performance
- Preserves document IDs for GA and Echo (deduplication)
- Verifies document counts after migration
- Handles large datasets efficiently

**Safety:**
- Non-destructive: Does not delete unified indices
- Idempotent: Can be run multiple times safely
- Creates new indices without affecting existing data

### cleanup_unified_indices.py

**Features:**
- Verifies team-specific indices exist before deleting
- Compares document counts (unified vs team)
- Interactive confirmation prompts
- Force mode for automation
- Per-source or bulk cleanup

**Safety Checks:**
- Confirms team indices exist before deletion
- Warns if document counts don't match
- Requires explicit confirmation (unless --force)

## Troubleshooting

### Issue: Migration script fails with "Failed to query active Echo teams"

**Cause**: Echo migration requires querying `de_asset_teams` index

**Solution**: Ensure asset indices exist and Elasticsearch is accessible

### Issue: Document count mismatch after migration

**Possible causes:**
- Documents with missing/null `team_title` go to 'unknown' index
- Duplicates if migration run multiple times

**Solution:**
```bash
# Check 'unknown' indices
sudo docker exec -it imports_container curl -s 'http://es01:9200/unknown_*/_count?pretty'

# If duplicates exist, delete team indices and re-run migration
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/*_fb_insights'
sudo docker exec -it imports_container python3 /app/python/import_scripts/migration/migrate_unified_to_team_indices.py facebook
```

### Issue: "Index already exists" error

**Cause**: Team-specific indices already exist from previous migration

**Solution**: Migration script handles this gracefully - it will append to existing indices. If you want a clean migration:

```bash
# Delete existing team indices
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/*_fb_insights'

# Exclude the unified index from deletion
# Re-run migration
```

## Rollback Procedure

If you need to rollback to unified indices:

1. **Keep unified indices**: Don't delete them until fully confident
2. **Re-run unified import**: Temporarily revert code to unified approach
3. **Delete team indices**: Remove team-specific indices if needed

```bash
# Emergency rollback: Delete team indices
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/*_fb_insights'
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/*_ig_insights'
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/*_ga_metrics'
sudo docker exec -it imports_container curl -X DELETE 'http://es01:9200/*_echo_conversations'

# Keep unified indices intact

# Revert code changes
cd /home/ubuntu/DE-Reporting
git revert <commit-hash>
```

## Post-Migration Checklist

- [ ] All data sources migrated successfully
- [ ] Document counts verified (unified == team totals)
- [ ] New imports tested and writing to team indices
- [ ] Kibana data views created/updated
- [ ] Visualizations tested with new data views
- [ ] Elasticsearch index permissions configured (if using role-based access)
- [ ] Unified indices deleted (after verification period)
- [ ] Documentation updated

## Performance Notes

**Migration Performance:**
- ~1000 documents per second (typical)
- 100K documents ≈ 2 minutes
- Bulk operations optimize throughput

**Recommendations:**
- Run during off-peak hours for large datasets
- Monitor Elasticsearch heap usage during migration
- Consider migrating data sources sequentially (not all at once)

## Support

For issues or questions:
1. Check Elasticsearch logs: `sudo docker logs es01 --tail 100`
2. Check import container logs: `sudo docker logs imports_container --tail 100`
3. Verify indices: `curl 'http://es01:9200/_cat/indices?v'`
