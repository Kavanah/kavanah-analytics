"""Field policy for the anonymized demo clone.

This is an ALLOWLIST. A string field leaves the transform unchanged only when
this module says so; anything unclassified is dropped and reported. The
failure mode we are designing against is the one that does not raise: a new
field appears in an importer, nobody adds a rule, and a real person's name or a
partner ministry's campaign title walks into the demo tenant unnoticed. Dropping
the unknown field blanks a chart, which someone sees; leaking it does not.

Every entry below was written against a live field census of the source team's
twelve indices (2026-09-07), not against the schema files alone — the docs carry
fields the schemas do not map (dynamic mapping), and those are exactly the ones
a schema-driven policy would miss.

Actions, in the order :func:`classify` tries them:

``team``   identity of the team — overwritten from the demo team's asset doc
``drop``   free text, contact details, targeting blobs — removed outright
``id``     platform identifiers — deterministic same-shape pseudonym
``label``  human names/titles/paths — ``"Demo <kind> <n>"`` with a stable ``n``
``url``    links — pointed at ``demo.example.com``
``keep``   categorical values (statuses, currencies, countries, report types)
``date``   date/time fields, detected by key shape
``number`` numeric strings (the FB importer writes ``"insight_clicks": "14"``)

Nested paths use dots (``ig_media.ig_media_caption``); list elements share the
parent's path.
"""

from __future__ import annotations

import re
from typing import Literal

Action = Literal["team", "drop", "id", "label", "url", "keep", "date", "number", "unclassified"]

# Values that mean "no value" or "aggregate" in every source. They are never
# pseudonymized or relabeled — ``adset_id: "all"`` is a grain sentinel the
# window read path matches on, and ``"unknown"`` / ``"(not set)"`` are what
# the platforms themselves emit for missing dimensions.
SENTINELS: frozenset[str] = frozenset(
    {"", "/", "all", "unknown", "Unknown", "N/A", "(not set)", "(none)", "(direct)", "(other)"}
)

# --- team identity -----------------------------------------------------------

TEAM_FIELDS: frozenset[str] = frozenset(
    {
        "team_esid",
        "team_title",
        "organization_title",
        "team_site_url",
        "team_url",
        "team_country",
        "team_city",
        "team_country_iso3_code",
    }
)

# --- dropped outright ----------------------------------------------------------
# Free text written by or about people, contact details, and platform blobs
# that embed third-party identifiers (WhatsApp numbers inside
# adset_promoted_object; page/post ids as JSON keys inside the snapshot).

DROP_FIELDS: frozenset[str] = frozenset(
    {
        "adset_promoted_object",
        "adset_targeting",
        "page_video_view_time_snapshot",
        "creative_body",
        "creative_title",
        "creative_image_url",
        "ad_link_href",
        "ig_user_profile_picture_url",
        "ig_user_biography",
        "topic",  # Echo: the seeker's opening message
        "notes",
        "disposition_notes",
        "pdf",
        "link",
        "client_contact",
        "sender_email",
        "sender_phone",
        "assignee_email",
        # per-conversation attributes of the SEEKER. Religion,
        # country and language are quasi-identifiers when they sit on one row
        # with an outcome and a pseudonymous client id; already stopped
        # the Echo importer writing them, and the clone refuses them too so an
        # older source doc can never carry them across. Geography survives only
        # as the country-grain ISO2 code the Echo map aggregates on
        # (``contact_country_iso2``, kept below). The two disposition duplicates
        # go with them: ``outcome`` is the single categorical the Outcomes
        # dashboard counts, and a platform enum id is an enumerable identifier.
        "client_religion",
        "client_country",
        "client_language",
        "disposition_outcome_id",
        "disposition_outcome_title",
    }
)

# --- identifiers → same-shape pseudonyms ---------------------------------------
# field → (namespace, keep_prefix). Fields sharing a namespace map the same
# original value to the same pseudonym, so a page id stays joinable across
# fb_insights, fb_page_insights, fb_page_posts and Echo's fb_page_id.
# keep_prefix preserves a platform prefix the dashboards or links depend on.

ID_FIELDS: dict[str, tuple[str, int]] = {
    "ad_account_id": ("fb_ad_account", 0),
    "ad_campaign_id": ("fb_campaign", 0),
    "ad_id": ("ad", 0),
    "adset_id": ("fb_adset", 0),
    "creative_id": ("fb_creative", 0),
    "creative_video_id": ("fb_video", 0),
    "video_asset_id": ("fb_video", 0),
    "page_id": ("fb_page", 0),
    "fb_page_id": ("fb_page", 0),
    "post_id": ("fb_post", 0),
    "ig_user_id": ("ig_user", 0),
    "ig_media.ig_media_id": ("ig_media", 0),
    "campaign_id": ("gads_campaign", 0),
    "ad_group_id": ("gads_ad_group", 0),
    "google_ads_customer_id": ("gads_customer", 0),
    "google_ads_customer_esid": ("gads_customer_esid", 0),
    "google_ads_metric_customer_id": ("gads_customer", 0),
    "property_id": ("ga_property", 0),
    "youtube_channel_id": ("yt_channel", 2),  # keep the "UC" channel prefix
    "youtube_channel_esid": ("yt_channel_esid", 0),
    "video_id": ("yt_video", 0),
    "link_id": ("bitly_link", len("bit.ly/")),
    "id": ("echo_conversation", 0),
    "client_id": ("echo_client", 0),
    "chat_id": ("echo_chat", 0),
    "claimed_by_user_id": ("echo_agent", 0),
    "conversation_id": ("chatwoot_conversation", 0),
    "conversation_uuid": ("chatwoot_conversation_uuid", 0),
    "chatwoot_account_id": ("chatwoot_account", 0),
    "chatwoot_account_esid": ("chatwoot_account_esid", 0),
    "sender_id": ("chatwoot_sender", 0),
    "assignee_id": ("chatwoot_assignee", 0),
}

# --- names and titles → generic labels ----------------------------------------
# field → template; ``{n}`` is a stable four-digit bucket of the original value,
# so the same campaign keeps the same label across days and indices.

LABEL_FIELDS: dict[str, str] = {
    "ad_campaign_name": "Demo Campaign {n}",
    "ad_name": "Demo Ad {n}",
    "adset_name": "Demo Ad Set {n}",
    "ad_link_name": "Demo Link {n}",
    "page_name": "Kavanah Demo Page {n}",
    "fb_page_name": "Kavanah Demo Page {n}",
    "page_username": "kavanahdemo{n}",
    "fb_page_username": "kavanahdemo{n}",
    "ig_user_name": "Kavanah Demo Instagram {n}",
    "ig_user_username": "kavanahdemo{n}",
    "video_asset_name": "Demo Video {n}",
    "ig_media.ig_media_caption": "Demo media post {n}",
    "post_message": "Demo post {n}",
    "campaign_name": "Demo Campaign {n}",
    "ad_group_name": "Demo Ad Group {n}",
    "google_ads_customer_title": "Kavanah Demo Ads Account",
    "campaign_labels": "demo-label-{n}",
    "app_id": "com.kavanahdemo.app{n}",  # numeric app ids take the id path instead (see classify)
    "property_name": "Demo Property {n}",
    "pageTitle": "Demo Page {n}",
    "pagePath": "/demo-page-{n}",
    "unifiedScreenClass": "Demo Screen {n}",
    "youtube_channel_title": "Kavanah Demo Channel {n}",
    "youtube_channel_handle": "@kavanahdemo{n}",
    "video_title": "Demo Video {n}",
    "link_title": "Demo Link {n}",
    "tags": "demo-tag-{n}",
    "tag_names": "demo-tag-{n}",
    "referrer": "Kavanah Demo Page {n}",
    "claimed_by_user_name": "Demo Agent {n}",
    "client_name": "Demo Client {n}",
    "utm_campaign": "demo-utm-{n}",
    "utm_term": "demo-utm-{n}",
    "assignee_name": "Demo Agent {n}",
    "sender_name": "Demo Client {n}",
    "sender_instagram_username": "kavanahdemo{n}",
    "chatwoot_account_title": "Kavanah Demo Inbox",
    "inbox_names": "Demo Inbox {n}",
    "labels": "demo-label-{n}",
}

# --- links → demo.example.com ---------------------------------------------------

URL_FIELDS: dict[str, str] = {
    "ad_preview": "https://demo.example.com/ad/{n}",
    "page_link": "https://demo.example.com/page/{n}",
    "post_permalink_url": "https://demo.example.com/post/{n}",
    "ig_media.ig_media_permalink": "https://demo.example.com/media/{n}",
    "ig_media.ig_media_url": "https://demo.example.com/media/{n}/file",
    "ig_user_website": "https://demo.example.com",
    "long_url": "https://demo.example.com/l/{n}",
    "linkUrl": "https://demo.example.com/link/{n}",
    "echo_source": "https://demo.echoglobal.example",
    "utm_url": "https://demo.example.com/utm/{n}",
    "dt_conversation_url": "https://demo.example.com/dt/{n}",
}

# --- categorical strings that pass through --------------------------------------
# Statuses, enums, currencies, geography of the AUDIENCE (not of a person),
# GA dimensions the charts group by. Nothing here names a person, an asset, or
# the source team.

KEEP_FIELDS: frozenset[str] = frozenset(
    {
        # Facebook ads / windows
        "ad_status",
        "effective_status",
        "adset_bid_strategy",
        "adset_billing_event",
        "adset_optimization_goal",
        "ad_call_to_action_type",
        "ad_call_to_action_app_destination",  # Meta enum (MESSENGER, ...) — seen in older docs, staging dry run 2026-09-07
        "insight_quality_ranking",  # Meta ranking enums (UNKNOWN, AVERAGE, BELOW_AVERAGE_10, ...)
        "insight_conversion_rate_ranking",
        "insight_engagement_rate_ranking",
        "insight_account_currency",
        "insight_attribution_setting",
        "insight_buying_type",
        "insight_objective",
        "insight_optimization_goal",
        "result_label",
        "result_type",
        "publisher_platform",
        "grain",
        "age",
        "gender",
        "page_verification_status",
        "location_cities.name",
        "location_countries.name",
        "location_countries.iso2",
        # Facebook page posts / Instagram
        "post_type",
        "ig_media.ig_media_type",
        # Google Ads
        "campaign_channel_type",
        "campaign_status",
        "ad_group_status",  # Google Ads enum (ENABLED, PAUSED, REMOVED) — older docs, staging dry run 2026-09-07
        "app_store",
        "record_type",
        "geo_country_code",
        "geo_country_name",
        # Google Analytics
        "report_type",
        "eventName",
        "firstUserMedium",
        "firstUserSource",
        "sessionMedium",
        "sessionSource",
        "deviceCategory",
        "newVsReturning",
        "platform",
        "country",
        "city",
        "audienceName",
        # Echo
        "status",
        "locale",
        "objectClass",
        "referrer_channel",
        "referrer_channel_icon",
        "contact_country_iso2",  # country-grain code the Echo map aggregates on
        "outcome",  # the one categorical the Outcomes dashboard counts (aggregate use only)
        "filterLabel",
        "filter_label",
        "utm_source",
        "utm_medium",
        # Chatwoot (not cloned today; listed so the policy is complete if it ever is)
        "channel_type",
        "priority",
        "assignee_type",
        "sla_policy_id",
    }
)

# --- date/time detection by key shape --------------------------------------------

_DATE_KEYS: frozenset[str] = frozenset({"@timestamp", "kibana_date", "date", "expiration", "insight_window_label"})
_DATE_SUFFIXES: tuple[str, ...] = ("_at", "_time", "_created", "_timestamp", "_start", "_stop", "_end", "_since")
_NUMERIC_STRING = re.compile(r"^-?\d+(\.\d+)?$")


def is_date_key(path: str) -> bool:
    """True when the leaf key names a date/time field (``created_at``, ``insight_date_start``, ...)."""
    leaf = path.rsplit(".", 1)[-1]
    return leaf in _DATE_KEYS or leaf.endswith(_DATE_SUFFIXES) or leaf.startswith("insight_date")


def is_numeric_string(value: str) -> bool:
    """True for ``"14"``, ``"0.786"``, ``"-3"`` — metric values the FB importer writes as strings."""
    return bool(_NUMERIC_STRING.match(value))


def classify(path: str, value: object) -> Action:
    """Return the action for a string-valued field at ``path``.

    Only strings need classifying: numbers, booleans and ``None`` carry no
    identity and pass through unchanged; dicts and lists are walked and their
    leaves classified under a dotted path. Callers should not pass those here.
    """
    leaf = path.rsplit(".", 1)[-1]
    if leaf in TEAM_FIELDS:
        return "team"
    if path in DROP_FIELDS or leaf in DROP_FIELDS:
        return "drop"
    if path == "app_id" and isinstance(value, str) and value.isdigit():
        return "id"  # Apple app-store ids are numeric; bundle ids take the label path
    if path in ID_FIELDS or leaf in ID_FIELDS:
        return "id"
    if path in LABEL_FIELDS or leaf in LABEL_FIELDS:
        return "label"
    if path in URL_FIELDS or leaf in URL_FIELDS:
        return "url"
    if path in KEEP_FIELDS or leaf in KEEP_FIELDS:
        return "keep"
    if is_date_key(path):
        return "date"
    if isinstance(value, str) and is_numeric_string(value):
        return "number"
    return "unclassified"


def id_rule(path: str) -> tuple[str, int]:
    """Namespace and preserved-prefix length for an ``id`` field."""
    if path in ID_FIELDS:
        return ID_FIELDS[path]
    leaf = path.rsplit(".", 1)[-1]
    if leaf in ID_FIELDS:
        return ID_FIELDS[leaf]
    if path == "app_id":
        return ("app_store_id", 0)
    raise KeyError(path)


def label_template(path: str) -> str:
    """Label template for a ``label`` field."""
    if path in LABEL_FIELDS:
        return LABEL_FIELDS[path]
    return LABEL_FIELDS[path.rsplit(".", 1)[-1]]


def url_template(path: str) -> str:
    """URL template for a ``url`` field."""
    if path in URL_FIELDS:
        return URL_FIELDS[path]
    return URL_FIELDS[path.rsplit(".", 1)[-1]]
