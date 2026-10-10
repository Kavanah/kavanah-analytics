"""
Shared team-metadata helpers used by multiple import scripts.
Defined here to avoid circular imports between get_insights.py and get_page_insights.py.
"""


def _resolve_team_country(team):
    """Return the team's ISO2 country code as a single string.

    Handles all storage variants found in de_asset_teams documents:
      - team_country as a plain string  → returned directly
      - team_country as a list          → first element returned
      - team_country_text as a comma-separated string → first code returned
      - team_country_text as a list     → first element returned
      - missing / empty                 → None
    Always returns a single string or None; never a list.
    """
    country = team.get("team_country")
    if isinstance(country, list):
        country = country[0] if country else None
    country = (country or "").strip()
    if country:
        return country
    country_text = team.get("team_country_text")
    if isinstance(country_text, list):
        country_text = country_text[0] if country_text else None
    country_text = (country_text or "").strip()
    if not country_text:
        return None
    codes = [c.strip() for c in country_text.split(",") if c.strip()]
    return codes[0] if codes else None
