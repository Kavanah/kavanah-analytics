"""
Elasticsearch schema migrations for Digital Engagement Dashboard.

This module provides:
- JSON schema definitions for all index types (fb_insights, ga_metrics, echo_conversations)
- Schema loading utilities
- Migration utilities for schema changes

Usage:
    from migration.schema_loader import load_schema
    schema = load_schema('unified_schema_fb_insights')
"""

__version__ = "1.0.0"
