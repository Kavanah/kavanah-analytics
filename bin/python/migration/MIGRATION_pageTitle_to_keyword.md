# Migration: Change pageTitle from text to keyword in GA Metrics Indexes

**Date**: 2026-02-05
**Issue**: pageTitle field in `*ga_metrics` indexes is type `text` but should be `keyword`
**Impact**: Affects aggregations, sorting, and Kibana visualizations that use pageTitle

## Changes Made

### 1. Schema Update
**File**: `bin/python/schemas/unified_schema_ga_metrics.json`

Changed:
```json
"pageTitle": {"type": "text"}
```

To:
```json
"pageTitle": {"type": "keyword"}
```

This ensures new GA metrics indexes will have the correct field type.

### 2. Migration Script
**File**: `bin/python/migration/migrate_ga_pageTitle_to_keyword.py`

Created a migration script to update existing indexes.

## Running the Migration

### Dry Run (Recommended First)

Test the migration without making changes:

```bash
# SSH into server
ssh -i ~/.ssh/id_ed25519 ubuntu@<server-ip>

# Navigate to app directory
cd /opt/digital-engagement-dashboard

# Pull latest changes
git pull origin main

# Run dry-run inside container
sudo docker exec -it imports_container python3 /app/python/migration/migrate_ga_pageTitle_to_keyword.py --dry-run
```

### Execute Migration

Once dry-run looks good, run the actual migration:

```bash
sudo docker exec -it imports_container python3 /app/python/migration/migrate_ga_pageTitle_to_keyword.py
```

### What the Script Does

The script automatically handles the Elasticsearch template issue by creating a higher-priority template that allows regular index creation (instead of forcing data streams).

For each `*ga_metrics` index:

1. **Check current mapping** - Verifies pageTitle is currently type `text`
2. **Create temp index** - Creates `{index}_migrating_{timestamp}` with updated schema
3. **Reindex data** - Copies all documents to temp index
4. **Verify** - Confirms document counts match
5. **Swap** - Creates final index, reindexes from temp, deletes old index and temp
6. **Alias** - Creates alias so `{index}` queries use `{index}_final`

### Expected Output

```
==========================================================
Processing index: brisbane_ga_metrics
==========================================================
Current pageTitle type: text
Document count: 15234
Created temporary index: brisbane_ga_metrics_migrating_20260205_143022
Reindexing brisbane_ga_metrics -> brisbane_ga_metrics_migrating_20260205_143022...
Reindexed 15234 documents
Verification passed: 15234 documents in both indexes
Creating final index: brisbane_ga_metrics_final
Reindexing brisbane_ga_metrics_migrating_20260205_143022 -> brisbane_ga_metrics_final
Verification passed: 15234 documents in both indexes
Deleting old index: brisbane_ga_metrics
Deleting temp index: brisbane_ga_metrics_migrating_20260205_143022
Creating alias brisbane_ga_metrics -> brisbane_ga_metrics_final
Migration complete: queries to 'brisbane_ga_metrics' will use 'brisbane_ga_metrics_final'
✓ Successfully migrated brisbane_ga_metrics

==========================================================
Migration Summary
==========================================================
Total indexes: 5
Successfully migrated: 5
Already correct (skipped): 0
Failed: 0
```

## Rollback Plan

If issues occur, the migration can be stopped at any point:

- **During reindex**: Temp index can be deleted, original index remains unchanged
- **After completion**: Contact administrator to restore from Elasticsearch snapshots

## Verification

After migration, verify pageTitle is now keyword type:

```bash
# Check mapping for an index
sudo docker exec es01 curl -s "localhost:9200/brisbane_ga_metrics/_mapping/field/pageTitle" | python3 -m json.tool
```

Expected output:
```json
{
  "brisbane_ga_metrics": {
    "mappings": {
      "pageTitle": {
        "full_name": "pageTitle",
        "mapping": {
          "pageTitle": {
            "type": "keyword"
          }
        }
      }
    }
  }
}
```

## Kibana Impact

After migration:
- **Aggregations** on pageTitle will work more efficiently
- **Sorting** by pageTitle will be faster
- **Filters** for exact page title matches will work as expected
- **Existing visualizations** using pageTitle should continue to work

You may need to refresh field lists in Kibana:
1. Go to Stack Management → Index Patterns
2. Select the affected index pattern (e.g., `*ga_metrics`)
3. Click "Refresh field list"

## Estimated Time

- **Small indexes** (<10k docs): ~30 seconds per index
- **Medium indexes** (10k-100k docs): ~2-5 minutes per index
- **Large indexes** (>100k docs): ~10+ minutes per index

The script processes indexes sequentially to minimize load on Elasticsearch.

## Monitoring

Watch the migration progress:

```bash
# Follow script output
sudo docker exec -it imports_container python3 /app/python/migration/migrate_ga_pageTitle_to_keyword.py

# In another terminal, monitor Elasticsearch
sudo docker exec es01 curl -s "localhost:9200/_tasks?detailed=true&actions=*reindex" | python3 -m json.tool
```

## Support

If issues occur during migration:
1. Note the index name and error message
2. Check Elasticsearch logs: `sudo docker logs es01 --tail 100`
3. Contact system administrator
