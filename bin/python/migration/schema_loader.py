"""
Schema loader utility for Elasticsearch index mappings.

This module provides functions to load index schemas from JSON files,
replacing inline schema definitions across the import scripts.
"""

import json
import os
import logging
from typing import Dict, Any

# Schema name mapping
SCHEMA_MAP = {
    "fb_insights": "unified_schema_fb_insights",
    "fb_insights_window": "unified_schema_fb_insights_window",
    "fb_page_insights": "unified_schema_fb_page_insights",
    "fb_page_posts": "unified_schema_fb_page_posts",
    "ga_metrics": "unified_schema_ga_metrics",
    # the GA4 dedup-window index ({team_esid}__ga_metrics_window) reuses
    # the ga_metrics field set but is its OWN source — it needs its own SCHEMA_MAP
    # entry so the per-source mapping template covers the *_ga_metrics_window
    # pattern (which *_ga_metrics does NOT match). Dedicated file (a copy of
    # unified_schema_ga_metrics) rather than aliasing, so a future ga_metrics-only
    # field change doesn't silently alter the window mapping.
    "ga_metrics_window": "unified_schema_ga_metrics_window",
    "echo_conversations": "unified_schema_echo_conversations",
    "echo_user_stats": "unified_schema_echo_user_stats",
    "ig_insights": "unified_schema_ig_insights",
    # windowed deduplicated IG reach (sister to fb_insights_window).
    "ig_insights_window": "unified_schema_ig_insights_window",
    "disciple_tools": "unified_schema_disciple_tools",
    # the YouTube importer (send_json_to_elastic.create_youtube_analytics_index)
    # builds an inline mapping identical to unified_schema_youtube_analytics.json, but the
    # source was never registered here — so it was missing from the data-stream guard
    # AND the per-source mapping-template guard. Registering it closes both gaps;
    # a fresh *_youtube_analytics index now gets the schema mapping at auto-create time.
    "youtube_analytics": "unified_schema_youtube_analytics",
    # google_ads + the two chatwoot sources carried inline mapping dicts in
    # their importers and had no schema file — so the mapping-template guard
    # skipped them (a net-new team could lose the create-index race and get dynamic
    # text mapping, the same failure mode as before). Schemas authored verbatim from the
    # importers' inline mappings; the importers now load from these files, so the
    # template and create_index_if_not_exists share one source of truth.
    "google_ads": "unified_schema_google_ads",
    "chatwoot_conversations": "unified_schema_chatwoot_conversations",
    "chatwoot_insights": "unified_schema_chatwoot_insights",
    # Bitly link-clicks importer ({team_esid}__bitly_clicks).
    "bitly_clicks": "unified_schema_bitly_clicks",
    # TikTok organic snapshots ({team_esid}__tiktok_organic).
    "tiktok_organic": "unified_schema_tiktok_organic",
    # monthly conversation rollups ({team_esid}__conversation_rollups).
    "conversation_rollups": "unified_schema_conversation_rollups",
    # per-country delivery breakdown of FB ad insights ({team_esid}__fb_insights_country).
    "fb_insights_country": "unified_schema_fb_insights_country",
}

# root keys Elasticsearch accepts in a ``mappings`` body (index create
# and composable-template ``template.mappings``). ``load_schema`` sends the file's
# root dict to ES VERBATIM, so every root key a schema file carries must be one of
# these — or the one annotation key below, which is stripped on load.
ES_ROOT_MAPPING_KEYS = frozenset(
    {
        "properties",
        "dynamic",
        "dynamic_templates",
        "dynamic_date_formats",
        "date_detection",
        "numeric_detection",
        "runtime",
        "enabled",
        "subobjects",
        "_meta",
        "_source",
        "_routing",
        "_field_names",
        "_data_stream_timestamp",
    }
)
# The sanctioned author-annotation key. Kept in the JSON for readers; never sent
# to ES (ES has no comment syntax and rejects it as an unsupported root parameter).
SCHEMA_COMMENT_KEY = "_comment"

# asset-tier schema registry. Unlike the data tier (per-team
# ``{team_esid}__<suffix>`` patterns keyed by suffix), asset indices are fixed
# singleton names, so this maps the EXACT index name to its schema file.
# Every schema carries a ``dynamic_templates`` strings_as_keyword rule so any
# field the schema doesn't enumerate (legacy stragglers, future wizard/importer
# stamps) maps to bare ``keyword`` instead of drifting to ``text + .keyword``
# the failure class.
ASSET_SCHEMA_MAP = {
    "de_asset_teams": "asset_schema_teams",
    "de_asset_workspace": "asset_schema_workspace",
    "de_asset_organization": "asset_schema_organization",
    "de_asset_fb_accounts": "asset_schema_fb_accounts",
    "de_asset_fb_apps": "asset_schema_fb_apps",
    "de_asset_fb_ad_accounts": "asset_schema_fb_ad_accounts",
    "de_asset_fb_app_users": "asset_schema_fb_app_users",
    "de_asset_fb_pages": "asset_schema_fb_pages",
    "de_asset_echo_tenants": "asset_schema_echo_tenants",
    "de_asset_ga_accounts": "asset_schema_ga_accounts",
    "de_asset_ga_properties": "asset_schema_ga_properties",
    "de_asset_ga4_accounts": "asset_schema_ga4_accounts",
    "de_asset_google_ads_customers": "asset_schema_google_ads_customers",
    "de_asset_ig_accounts": "asset_schema_ig_accounts",
    "de_asset_youtube_accounts": "asset_schema_youtube_accounts",
    "de_asset_youtube_channels": "asset_schema_youtube_channels",
    "de_asset_chatwoot_accounts": "asset_schema_chatwoot_accounts",
    # read-only projection of TikTok de_connection rows.
    "de_asset_tiktok_accounts": "asset_schema_tiktok_accounts",
    # Not in ASSET_CONFIGS, but sort targets of the title probe —
    # they must be explicit keyword for the same reasons as the teams index.
    "de_asset_countries": "asset_schema_countries",
    "de_asset_cities": "asset_schema_cities",
}


def load_schema(schema_name: str) -> Dict[str, Any]:
    """
    Load a schema JSON file from the schemas directory.

    Args:
        schema_name: Name of schema file without .json extension
                    (e.g., 'unified_schema_fb_insights')

    Returns:
        Dictionary containing the schema mapping (properties object), ready to be
        sent to ES as a ``mappings`` body — the ``_comment`` annotation, if the
        file carries one, is stripped

    Raises:
        FileNotFoundError: If schema file doesn't exist
        json.JSONDecodeError: If schema file is invalid JSON
        KeyError: If schema is missing 'properties' key
        ValueError: If the schema carries a root key ES would reject

    Example:
        >>> schema = load_schema('unified_schema_fb_insights')
        >>> print(len(schema['properties']))
        89
    """
    # Get path to schemas directory
    # Schema directory is now at bin/python/schemas (sibling to migration/)
    schemas_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "schemas")
    schema_path = os.path.join(schemas_dir, f"{schema_name}.json")

    # Check if file exists
    if not os.path.exists(schema_path):
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    # Load JSON file
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(f"Invalid JSON in schema file {schema_name}.json: {e.msg}", e.doc, e.pos)

    # this dict goes to ES verbatim as a ``mappings`` body: through
    # ``indices.create`` (ensure_data_index) and through the per-source
    # composable template. ES rejects any root key it does not know ("Root
    # mapping definition has unsupported parameters" on create, "invalid
    # composite mappings" on template install), so an author annotation at the
    # root made BOTH paths fail for ig_insights_window — and every such index
    # was then born by the importer's bulk write under dynamic mapping (the
    # drift; 22 of 47 drifted prod indices). Strip the one
    # sanctioned annotation key; refuse anything else unknown loudly here, at
    # the source, rather than letting ES refuse it quietly at app start.
    schema.pop(SCHEMA_COMMENT_KEY, None)
    unknown = sorted(k for k in schema if k not in ES_ROOT_MAPPING_KEYS)
    if unknown:
        raise ValueError(
            f"Schema {schema_name} has root keys Elasticsearch rejects in a mappings body: {unknown}. "
            f"Allowed: {sorted(ES_ROOT_MAPPING_KEYS)}; the '{SCHEMA_COMMENT_KEY}' annotation is stripped on load."
        )

    # Validate schema structure
    if "properties" not in schema:
        raise KeyError(f"Schema {schema_name} is missing 'properties' key")

    logging.info(f"Loaded schema {schema_name} with {len(schema['properties'])} fields")

    return schema


def get_schema_for_index_type(index_type: str) -> Dict[str, Any]:
    """
    Get the appropriate schema for an index type.

    This is the main function used by import scripts to load schemas.

    Args:
        index_type: One of 'fb_insights', 'ga_metrics', 'echo_conversations'

    Returns:
        Schema mapping dictionary ready for create_index_if_not_exists()

    Raises:
        ValueError: If index_type is not recognized
        FileNotFoundError: If schema file doesn't exist
        json.JSONDecodeError: If schema file is invalid JSON

    Example:
        >>> from lib.es_helpers import create_index_if_not_exists
        >>> schema = get_schema_for_index_type('fb_insights')
        >>> create_index_if_not_exists(es, 'brisbane_fb_insights', schema)
    """
    # Validate index type
    if index_type not in SCHEMA_MAP:
        valid_types = ", ".join(SCHEMA_MAP.keys())
        raise ValueError(f"Unknown index type '{index_type}'. " f"Valid types are: {valid_types}")

    # Get schema name from mapping
    schema_name = SCHEMA_MAP[index_type]

    # Load and return schema
    return load_schema(schema_name)


def get_schema_for_asset_index(index_name: str) -> Dict[str, Any]:
    """
    Get the schema for an asset-tier index by its exact index name.

    Args:
        index_name: Exact asset index name (e.g. 'de_asset_fb_apps')

    Returns:
        Schema dictionary with 'properties' and (for asset schemas)
        'dynamic_templates', ready to use as an index 'mappings' body.

    Raises:
        ValueError: If index_name is not registered in ASSET_SCHEMA_MAP
        FileNotFoundError: If the schema file doesn't exist

    Example:
        >>> schema = get_schema_for_asset_index('de_asset_fb_apps')
        >>> schema['properties']['fb_account_esid']
        {'type': 'keyword'}
    """
    if index_name not in ASSET_SCHEMA_MAP:
        valid = ", ".join(sorted(ASSET_SCHEMA_MAP.keys()))
        raise ValueError(f"Unknown asset index '{index_name}'. Registered indices are: {valid}")

    return load_schema(ASSET_SCHEMA_MAP[index_name])


def validate_schema(schema: Dict[str, Any]) -> bool:
    """
    Validate that a schema has the required structure.

    Checks:
    - Has 'properties' key
    - Properties is a dict
    - Each property has 'type' key
    - organization_title field exists (for Phase 2)

    Args:
        schema: Schema dictionary to validate

    Returns:
        True if schema is valid

    Raises:
        ValueError: If schema validation fails with details about the issue

    Example:
        >>> schema = load_schema('unified_schema_fb_insights')
        >>> validate_schema(schema)
        True
    """
    # Check has properties key
    if "properties" not in schema:
        raise ValueError("Schema is missing 'properties' key")

    properties = schema["properties"]

    # Check properties is a dict
    if not isinstance(properties, dict):
        raise ValueError("Schema 'properties' must be a dictionary")

    # Check each property has a type
    for field_name, field_config in properties.items():
        if not isinstance(field_config, dict):
            raise ValueError(f"Field '{field_name}' configuration must be a dictionary")

        if "type" not in field_config:
            raise ValueError(f"Field '{field_name}' is missing required 'type' key")

    # Check organization_title field exists (Phase 2 requirement)
    if "organization_title" not in properties:
        raise ValueError("Schema is missing 'organization_title' field " "(required for Phase 2 migration)")

    # Check organization_title is keyword type
    org_field = properties["organization_title"]
    if org_field.get("type") != "keyword":
        raise ValueError(f"Field 'organization_title' must be type 'keyword', " f"got '{org_field.get('type')}'")

    logging.info(f"Schema validation passed: {len(properties)} fields, " f"organization_title present")

    return True


def list_available_schemas() -> Dict[str, str]:
    """
    List all available schema mappings.

    Returns:
        Dictionary mapping index types to schema file names

    Example:
        >>> schemas = list_available_schemas()
        >>> print(schemas)
        {'fb_insights': 'unified_schema_fb_insights', ...}
    """
    return SCHEMA_MAP.copy()


# Module-level cache to avoid reloading schemas
_SCHEMA_CACHE = {}


def get_schema_cached(index_type: str) -> Dict[str, Any]:
    """
    Get schema with caching to improve performance.

    Same as get_schema_for_index_type() but caches loaded schemas
    in memory to avoid repeated file I/O.

    Args:
        index_type: One of 'fb_insights', 'ga_metrics', 'echo_conversations'

    Returns:
        Schema mapping dictionary

    Example:
        >>> schema1 = get_schema_cached('fb_insights')  # Loads from file
        >>> schema2 = get_schema_cached('fb_insights')  # Returns cached version
        >>> schema1 is schema2
        True
    """
    if index_type not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[index_type] = get_schema_for_index_type(index_type)

    return _SCHEMA_CACHE[index_type]


def clear_schema_cache():
    """
    Clear the schema cache.

    Useful for testing or when schemas are updated at runtime.
    """
    global _SCHEMA_CACHE
    _SCHEMA_CACHE = {}
    logging.info("Schema cache cleared")
