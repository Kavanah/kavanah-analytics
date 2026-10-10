"""the legacy GA asset type (``ga_accounts``) and the GA4 OAuth type
(``ga4_accounts``) both rendered under the ``google_analytics`` category with the
identical title "Google Analytics Accounts", producing two indistinguishable rows
in Asset Explorer. The fix relabels the legacy type so the two are distinct.
"""

from asset_management.web.asset_configs import ASSET_CONFIGS


class TestKan996GaAccountTitles:
    """Both GA account asset types live under the same category; their titles
    must be distinct so Asset Explorer doesn't show two identical rows."""

    def test_legacy_and_ga4_account_titles_are_distinct(self) -> None:
        # Arrange — both types exist and share the google_analytics category.
        legacy = ASSET_CONFIGS["ga_accounts"]
        ga4 = ASSET_CONFIGS["ga4_accounts"]
        assert legacy["category"] == "google_analytics"
        assert ga4["category"] == "google_analytics"

        # Act / Assert — titles must differ (the bug was an identical string).
        assert legacy["title"] != ga4["title"], (
            "Legacy ga_accounts and ga4_accounts share a title — Asset Explorer "
            "renders two indistinguishable 'Google Analytics Accounts' rows."
        )

    def test_legacy_title_is_marked_legacy(self) -> None:
        # The GA4 type keeps the clean name; the legacy type is the one marked.
        assert ASSET_CONFIGS["ga4_accounts"]["title"] == "Google Analytics Accounts"
        assert "Legacy" in ASSET_CONFIGS["ga_accounts"]["title"]
