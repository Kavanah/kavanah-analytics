# Elasticsearch Schema Migrations

This directory contains JSON schema definitions for all Elasticsearch data indices in the Digital Engagement Dashboard. These schemas serve as the source of truth for index mappings and facilitate schema management across the system.

## Purpose

The migration system provides:
- **Centralized schema definitions**: All index mappings defined in JSON files rather than scattered across Python code
- **Schema versioning**: Track changes to field definitions over time
- **Documentation**: Clear reference for what fields exist in each index
- **Consistency**: Ensure all indices follow the same field conventions

## Directory Structure

```
migration/                                  # Migration utilities
├── README.md                              # This file
├── __init__.py                            # Module initialization
├── schema_loader.py                       # Schema loading utilities
├── verify_schemas.py                      # Schema validation script
└── FUTURE_WORK.md                         # Future enhancements

../schemas/                                 # JSON schema definitions (sibling dir)
├── __init__.py
├── unified_schema_fb_insights.json        # Facebook Ads insights (89 fields)
├── unified_schema_ga_metrics.json         # Google Analytics metrics (24 fields)
├── unified_schema_echo_conversations.json # Echo conversations (32 fields)
└── unified_schema_ig_insights.json        # Instagram insights (27 fields)
```

## Available Schemas

### unified_schema_fb_insights.json

**Purpose**: Facebook Ads insights data including ad performance, spend, engagement, and page metrics

**Field Categories**:
- **Ad fields** (12): ad_account_id, ad_campaign_id, ad_created, ad_id, ad_name, ad_preview, ad_status, ad_campaign_name
- **Adset fields** (6): adset_id, adset_optimization_goal, adset_promoted_object, adset_billing_event, adset_bid_strategy, adset_name, adset_targeting
- **Creative fields** (5): creative_id, creative_title, creative_body, creative_image_url, creative_video_id
- **Insight metrics** (33): insight_spend, insight_reach, insight_impressions, insight_clicks, insight_cpm, insight_cpc, insight_ctr, plus various action and cost-per-action metrics
- **Page fields** (13): page_id, page_name, page_fan_count, page_followers_count, page_engagement, page_verification_status, etc.
- **Team fields** (6): team_site_url, team_title, team_country, team_city, team_esid, **organization_title**
- **Result fields** (5): result_type, result_count, result_label, cost_per_result, cost_result_conversations, cost_result_link_clicks
- **System fields** (2): kibana_date, @timestamp

**Total**: 60 fields

**Dynamic Fields**: `adset_promoted_object`, `insight_targeting`, `adset_targeting` allow flexible object structures

### unified_schema_ga_metrics.json

**Purpose**: Google Analytics 4 metrics including page views, user engagement, and sessions

**Field Categories**:
- **Property fields** (2): property_id, property_name
- **Dimensions** (8): pageTitle, pagePath, sessionSource, sessionMedium, country, city, platform, date
- **Metrics** (8): screenPageViews, scrolledUsers, activeUsers, userEngagementDuration, eventCount, sessions, totalUsers, engagedSessions
- **Team fields** (3): team_esid, team_title, **organization_title**
- **System fields** (2): kibana_date, @timestamp

**Total**: 23 fields

### unified_schema_echo_conversations.json

**Purpose**: Echo messaging platform conversation data including contacts, status, and outcomes

**Field Categories**:
- **Core fields** (7): id, client_id, type, topic, name, user, duration
- **Timestamp fields** (4): created_at, closed_at, expiration, kibana_date
- **Chat fields** (2): chat_id, referrer
- **Status fields** (4): outcome, status, flag, tags
- **Contact fields** (4): client_contact, link, notes, pdf, country
- **Team fields** (7): team_country, team_country_iso3_code, team_city, team_site_url, team_title, team_esid, **organization_title**
- **Source field** (1): echo_source
- **System field** (1): @timestamp

**Total**: 31 fields

## Schema Format

All schema files follow the **mappings-only format** compatible with the existing `create_index_if_not_exists()` function in `lib/es_helpers.py`:

```json
{
  "properties": {
    "field_name": {"type": "keyword"},
    "date_field": {"type": "date", "format": "yyyy-MM-dd"},
    "numeric_field": {"type": "integer"},
    "text_field": {"type": "text"},
    "dynamic_object": {"type": "object", "dynamic": "true"}
  }
}
```

This format is wrapped in `{"mappings": mapping}` when creating indices.

## Field Types Reference

### Common Field Types

- **keyword**: Exact-value matching (IDs, titles, status values, URLs)
- **text**: Full-text search (descriptions, bodies, topics)
- **integer**: Whole numbers (counts, IDs)
- **long**: Large integers (client_id, chat_id)
- **float**: Decimal numbers (costs, rates, durations)
- **boolean**: True/false values (page_is_published, page_is_permanently_closed)
- **date**: Timestamps with format specification
- **object**: Nested structures (can be dynamic)

### Date Formats

- **Facebook**: `yyyy-MM-dd'T'HH:mm:ssZ` (ISO 8601)
- **Google Analytics**: `yyyyMMdd` (compact format)
- **Echo**: `MMMM dd, yyyy HH:mm:ss 'UTC'` (friendly format)
- **Kibana display**: `yyyy-MM-dd` (all indices)
- **System timestamp**: `@timestamp` (standard Elasticsearch date type)

## Organization Hierarchy

The `organization_title` field references the organization hierarchy:

```
Organization (de_asset_organization)
  └─ Region (de_asset_regions)
      └─ Country (de_asset_countries)
          └─ City (de_asset_cities)
              └─ Team (de_asset_teams)
```

**Populating organization_title**:
1. Each document has a `team_title` or `team_esid` field
2. Look up the team in `de_asset_teams` index
3. Extract the team's `organization_title` field
4. Store it in the document's `organization_title` field

Example:
```python
from lib.es_helpers import get_document

team_doc = get_document('de_asset_teams', team_esid)
data['organization_title'] = team_doc.get('organization_title', 'Unknown')
```

## Usage

### Loading Schemas (Future)

When the schema_loader utility is implemented, you can use:

```python
from migration.schema_loader import load_schema

# Load a schema
mapping = load_schema('unified_schema_fb_insights')

# Use with existing index creation function
from lib.es_helpers import create_index_if_not_exists, get_elasticsearch_client

es = get_elasticsearch_client()
create_index_if_not_exists(es, 'team_name_fb_insights', mapping)
```

### Current Integration Points

The schemas extracted from these files:
- **Facebook**: `bin/python/import_scripts/facebook/send_json_to_elastic.py` (lines 10-101)
- **Google Analytics**: `bin/python/lib/es_helpers.py` (lines 202-228)
- **Echo**: `bin/python/import_scripts/echo/send_json_to_elastic.py` (lines 24-56)

## Field Naming Conventions

1. **Prefixes**:
   - `ad_*`: Facebook Ad fields
   - `adset_*`: Facebook Ad Set fields
   - `creative_*`: Facebook Creative fields
   - `insight_*`: Facebook Insights metrics
   - `page_*`: Facebook Page fields
   - `team_*`: Team metadata fields
   - `fb_*`: Facebook asset references
   - `ga_*`: Google Analytics fields
   - `echo_*`: Echo system fields

2. **Suffixes**:
   - `_id`: Unique identifier (always keyword type)
   - `_esid`: Elasticsearch document ID reference
   - `_count`: Count metrics (integer type)
   - `_title`: Display name (keyword type)
   - `_url`: Web addresses (keyword type)
   - `_date`: Date fields (date type with format)

3. **Special Fields**:
   - `kibana_date`: Formatted date for Kibana visualization (always yyyy-MM-dd format)
   - `@timestamp`: System timestamp (standard ES date type)
   - `result_*`: Computed optimization goal metrics
   - `cost_*`: Financial cost calculations

## Updating Schemas

When adding or modifying fields:

1. **Update the JSON schema file**:
   ```json
   {
     "properties": {
       "existing_field": {"type": "keyword"},
       "new_field": {"type": "keyword"}
     }
   }
   ```

2. **Update the corresponding import script** (when integrated):
   - Ensure the script populates the new field
   - Update field extraction logic if needed

3. **Update Elasticsearch indices**:
   - New fields are added automatically when first indexed
   - For existing indices, use Elasticsearch's `PUT mapping` API
   - Or create migration script to reindex with new schema

4. **Document the change**:
   - Update this README with field count
   - Note the purpose of the new field
   - Update any related documentation

## Schema Validation

To validate JSON syntax:

```bash
# Using Python (from bin/python directory)
python -m json.tool schemas/unified_schema_fb_insights.json

# Using jq (if installed)
jq . schemas/unified_schema_fb_insights.json
```

To verify field counts:

```bash
# Count fields in a schema (Unix/Linux/macOS, from bin/python directory)
jq '.properties | length' schemas/unified_schema_fb_insights.json

# Count fields (Windows PowerShell, from bin/python directory)
(Get-Content schemas/unified_schema_fb_insights.json | ConvertFrom-Json).properties.PSObject.Properties.Count
```

## Migration Strategy (Future)

When migrating existing data to include new fields like `organization_title`:

1. **Create migration script** in `migration/v1_add_organization_fields/`
2. **Use Elasticsearch Reindex API** to create new indices with updated schema
3. **Populate new fields** using inline Painless scripts
4. **Create aliases** to point to new indices
5. **Verify data integrity** before deleting old indices
6. **Update import scripts** to populate new fields going forward

## Troubleshooting

### Schema Loading Errors

**Problem**: JSON syntax error when loading schema
**Solution**: Validate JSON syntax using `python -m json.tool`

### Missing Fields in Index

**Problem**: Field exists in schema but not in Elasticsearch
**Solution**: Fields are only created when first document with that field is indexed. Check import scripts populate the field.

### Type Conflicts

**Problem**: Elasticsearch rejects document due to type mismatch
**Solution**: Check field type in schema matches data type being indexed. Common issues:
- Sending string to integer field
- Sending integer to date field
- Date format doesn't match specified format

### Dynamic Field Issues

**Problem**: Too many fields in dynamic object (`insight_targeting`, `adset_promoted_object`)
**Solution**: These fields intentionally allow dynamic expansion. Monitor field count and consider field limit increases if needed.

## References

- **Elasticsearch Mapping Documentation**: https://www.elastic.co/guide/en/elasticsearch/reference/current/mapping.html
- **Field Data Types**: https://www.elastic.co/guide/en/elasticsearch/reference/current/mapping-types.html
- **Date Format Syntax**: https://www.elastic.co/guide/en/elasticsearch/reference/current/mapping-date-format.html
- **Asset Management Config**: `bin/python/asset_management/web/asset_configs.py`
- **ES Helpers**: `bin/python/lib/es_helpers.py`

## Version History

- **v1.0.0** (2026-01-28): Initial schema extraction from inline Python definitions
  - Created unified schemas for fb_insights, ga_metrics, echo_conversations
  - Added organization_title field to all three schemas
  - Established mappings-only JSON format
