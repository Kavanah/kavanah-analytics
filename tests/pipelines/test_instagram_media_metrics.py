"""IG media insights requested Meta-removed metrics.

Meta removed the "engagement" and "carousel_album_*" media metrics
(replaced by "total_interactions") and renamed the reel "plays" metric to
"views". One invalid metric fails the whole get_insights call with Graph
error #100, which silently blanked media insights for EVERY media item.
These tests pin the metric lists to Meta's valid set and the roll-up
back-compat in massage_instagram_data.
"""

from unittest import mock

import import_scripts.instagram.get_instagram_insights as ig_insights


# The valid set straight from the Graph #100 error observed on prod
# 2026-07-13 (v25.0). "impressions" is deliberately absent: it appears in
# Meta's validation list but is rejected per-media for v22+ media
# (second prod 400, fbtrace ABIQV6Ip8r) — "views" is its successor. If
# Meta prunes this set again, update BOTH this test and
# _MEDIA_INSIGHT_METRICS from the fresh error/reference docs.
_META_VALID_MEDIA_METRICS = {
    "reach",
    "replies",
    "saved",
    "likes",
    "comments",
    "shares",
    "total_interactions",
    "follows",
    "profile_visits",
    "profile_activity",
    "navigation",
    "ig_reels_video_view_total_time",
    "ig_reels_avg_watch_time",
    "views",
    "reels_skip_rate",
    "reposts",
    "facebook_views",
    "crossposted_views",
    "total_views",
    "total_likes",
    "total_comments",
    "link_clicks",
}


class TestMediaInsightMetricLists:

    def test_should_request_only_meta_valid_metrics_for_every_media_type(self):
        for media_type, metrics in ig_insights._MEDIA_INSIGHT_METRICS.items():
            invalid = set(metrics) - _META_VALID_MEDIA_METRICS
            assert not invalid, f"{media_type} requests Meta-removed metrics: {sorted(invalid)}"

    def test_should_not_request_removed_engagement_or_plays_metrics(self):
        for media_type, metrics in ig_insights._MEDIA_INSIGHT_METRICS.items():
            assert "engagement" not in metrics, media_type
            assert "carousel_album_engagement" not in metrics, media_type
            assert "plays" not in metrics, media_type
            assert "impressions" not in metrics, media_type

    def test_should_request_views_successor_everywhere(self):
        for media_type, metrics in ig_insights._MEDIA_INSIGHT_METRICS.items():
            assert "views" in metrics, media_type

    def test_should_request_total_interactions_replacement_everywhere(self):
        for media_type, metrics in ig_insights._MEDIA_INSIGHT_METRICS.items():
            assert "total_interactions" in metrics, media_type

    def test_should_fall_back_to_reel_metrics_for_unknown_media_type(self):
        with mock.patch.object(ig_insights, "IGMedia") as fake_media_cls:
            fake_media_cls.return_value.get_insights.return_value = []
            ig_insights.fetch_instagram_media_insights("123", "SOMETHING_NEW")

        params = fake_media_cls.return_value.get_insights.call_args.kwargs["params"]
        assert params["metric"] == ig_insights._MEDIA_INSIGHT_METRICS["REEL"]


class TestRollupBackCompat:
    """massage_instagram_data must keep the historical total fields alive."""

    @staticmethod
    def _massage(media_insights, media_type="IMAGE"):
        data = {
            "media": [
                {
                    "id": "m1",
                    "media_type": media_type,
                    "insights": media_insights,
                }
            ]
        }
        with mock.patch.object(ig_insights, "_resolve_team_country", return_value="US"):
            return ig_insights.massage_instagram_data(data, {"team_title": "T", "_id": "abc"}, "2026-07-13")

    def test_should_populate_total_engagement_from_total_interactions(self):
        doc = self._massage({"total_interactions": 7})
        assert doc["ig_media_total_engagement"] == 7

    def test_should_prefer_legacy_engagement_key_when_present(self):
        # Docs re-massaged from pre-rename JSON still carry "engagement".
        doc = self._massage({"engagement": 3, "total_interactions": 7})
        assert doc["ig_media_total_engagement"] == 3

    def test_should_populate_total_plays_from_views_for_reels(self):
        doc = self._massage({"views": 11}, media_type="REEL")
        assert doc["ig_media_total_plays"] == 11

    def test_should_populate_total_impressions_from_views_for_non_reels(self):
        doc = self._massage({"views": 9}, media_type="IMAGE")
        assert doc["ig_media_total_impressions"] == 9
        assert doc["ig_media_total_plays"] == 0

    def test_should_not_double_count_reel_views_into_impressions(self):
        doc = self._massage({"views": 11}, media_type="REEL")
        assert doc["ig_media_total_impressions"] == 0
        assert doc["ig_media_total_plays"] == 11

    def test_should_prefer_legacy_impressions_key_when_present(self):
        doc = self._massage({"impressions": 4, "views": 9}, media_type="IMAGE")
        assert doc["ig_media_total_impressions"] == 4
