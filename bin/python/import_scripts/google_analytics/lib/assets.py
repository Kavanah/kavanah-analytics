# Import the es_helpers module
# asset reads go through the paginated fetch — the single-shot
# get_all_documents silently truncates at its size cap (1000) once an asset
# index outgrows it, dropping assets from the import in arbitrary ES order.
from lib.es_helpers import get_all_documents_paginated
import logging


# Function to build Elasticsearch query
def build_es_query(filters):
    if not filters:
        # If no filters, return match_all query
        return {"match_all": {}}

    must_clauses = []
    for key, value in filters.items():
        if isinstance(value, list):
            # Use 'terms' for multiple values
            if key == "_id":
                must_clauses.append({"ids": {"values": value}})
            else:
                must_clauses.append({"terms": {key: value}})
        else:
            # Use 'term' for single value
            if key == "_id":
                must_clauses.append({"ids": {"values": [value]}})
            else:
                must_clauses.append({"term": {key: value}})

    return {"bool": {"must": must_clauses}}


# summarize a build_es_query body into the meaningful identifiers
# (ids + field=value filters) for a readable log line, instead of dumping the raw
# bool/must DSL. Best-effort: an unrecognised shape falls back to a compact repr.
def _summarize_query(query):
    try:
        must = (query or {}).get("bool", {}).get("must")
        if not must:
            return "match_all" if (query or {}).get("match_all") is not None else str(query)
        parts = []
        for clause in must:
            if "ids" in clause:
                vals = clause["ids"].get("values") or []
                parts.append("id=" + (",".join(map(str, vals)) if vals else "<none>"))
                continue
            body = clause.get("term") or clause.get("terms")
            if body:
                field, value = next(iter(body.items()))
                parts.append(f"{field}={value}")
        return ", ".join(parts) if parts else str(query)
    except Exception:  # noqa: BLE001 — logging helper must never raise
        return str(query)


# Empty-result helper. an expected negative lookup logs at INFO with a
# short human summary, not a WARNING dumping the raw query DSL.
def handle_no_results(index, query):
    logging.info("No match in %s for %s", index, _summarize_query(query))
    return []


# Function to get Google Analytics accounts
def get_ga_accounts(filters=None):
    index = "de_asset_ga_accounts"

    # Build the Elasticsearch query
    query = build_es_query(filters)

    try:
        # Call es_helpers to get all documents from the index
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving Google Analytics accounts: {e}")
        return handle_no_results(index, query)


# Function to get Google Analytics properties
def get_ga_properties(filters=None):
    index = "de_asset_ga_properties"

    # Build the Elasticsearch query
    query = build_es_query(filters)

    try:
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving Google Analytics properties: {e}")
        return handle_no_results(index, query)


# Function to get teams
def get_ga_teams(filters=None):
    index = "de_asset_teams"

    query = build_es_query(filters)

    try:
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving teams: {e}")
        return handle_no_results(index, query)


# Function to get team metadata by GA property ID
def _ga_property_esid_for_id(property_id):
    """resolve a bare GA4 ``ga_property_id`` to its
    de_asset_ga_properties asset ESID (the value the wizard stores in a team's
    ``team_ga4_property_esids``). Returns the esid string, or None.

    de_asset_ga_properties.ga_property_id is mapped as a pure ``keyword`` (no
    ``.keyword`` subfield), so a bare term matches it directly. the
    original order queried ``ga_property_id.keyword`` first — 0 hits every run +
    a scary "No results found" WARNING — and only resolved via the bare-field
    fallback. Query the bare field first; keep the ``.keyword`` fallback for any
    GA-properties index still mapped text+keyword (the trap,
    which is per-index — don't assume it applies here).
    """
    pid = str(property_id or "").split("/")[-1].strip()
    if not pid:
        return None
    docs = get_ga_properties(filters={"ga_property_id": pid})
    if not docs:
        # Fallback for any GA-properties index mapped text+keyword.
        docs = get_ga_properties(filters={"ga_property_id.keyword": pid})
    return docs[0]["_id"] if docs else None


def get_team_by_ga_property_id(property_id):
    """
    Get team metadata by Google Analytics property ID.

    GA architecture uses a team-centric model. there are TWO linkage
    conventions and both must be honored or wizard-connected properties never
    import:
      * ``team_ga4_properties``      — list of BARE ga_property_id values
        (legacy / operator-seeded teams).
      * ``team_ga4_property_esids``  — list of de_asset_ga_properties ASSET
        ESIDS (what the Data Connectors / onboarding wizard writes —
        connector_wizard_service.configure_ga4_property).
    A property matches a team if its bare id is in the former OR its asset esid
    is in the latter (union — nothing regresses, wizard teams start working).

    when MORE THAN ONE team claims the property, log a WARNING naming
    the competing teams (the model is one property -> one team) and return the
    first match. Behavior is otherwise unchanged.

    Args:
        property_id: GA4 property ID (string)

    Returns:
        dict: Team metadata with team_esid, team_title
        False: If property not found in any team
    """
    # Only get teams with ds_google_analytics enabled
    all_teams = get_ga_teams(filters={"ds_google_analytics": "true"})

    if not all_teams:
        logging.warning("No teams found in de_asset_teams")
        return False

    bare_id = str(property_id or "").split("/")[-1].strip()
    # Resolve the property's asset esid ONCE (only needed for the esid-list path).
    property_esid = _ga_property_esid_for_id(bare_id)

    # Collect EVERY team that claims this property. The product model is one GA4
    # property -> one team, so a single claimant is the norm and "first-wins" is
    # correct. (per a teammate 2026-06-18): when >1 team claims it, that's
    # either a deliberate TEST team reusing assets or a real double-assignment —
    # either way, log a WARNING naming the competing teams so a real one surfaces
    # in the import log instead of silently attributing data to an arbitrary
    # team. We still return the first match (behavior unchanged).
    matches = []
    for team_doc in all_teams:
        team_source = team_doc["_source"]
        bare_ids = team_source.get("team_ga4_properties") or []
        esids = team_source.get("team_ga4_property_esids") or []

        matched = bare_id in {str(p) for p in bare_ids} or (property_esid is not None and property_esid in esids)
        if matched:
            matches.append(
                {
                    "team_esid": team_doc["_id"],
                    "team_title": team_source.get("team_title"),
                    "organization_title": team_source.get("organization_title", "Unknown"),
                    "team_country": team_source.get("team_country"),
                }
            )

    if not matches:
        # Property not found in any team
        logging.warning(f"Property {property_id} not in any team (team_ga4_properties or team_ga4_property_esids)")
        return False

    if len(matches) > 1:
        competing = ", ".join(f"{m['team_title']} ({m['team_esid']})" for m in matches)
        logging.warning(
            f"GA4 property {property_id} is claimed by {len(matches)} teams: {competing}. "
            f"The model is one property -> one team; attributing data to the first "
            f"({matches[0]['team_title']}). If this is not a deliberate test team, fix the double-assignment."
        )

    return matches[0]
