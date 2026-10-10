"""
Tests for scripts.seed_kavanah_demo_dt (seeder, now the daily demo DT
pipeline).

The push→pull cutover orphaned the old daily demo DT path
(generate_demo_data → disciple-tools-staging → promote). The daily cron now
runs this seeder with --reseed instead, so two properties become load-bearing:

1. The curated totals stay exact even with the field enrichments
   (contact_place / quick_button_* / baptism_generation).
2. The doc dates are anchored at "today", so a daily --reseed keeps the data
   inside the report's rolling 90-day window.

Pure doc-builder tests — no Elasticsearch.
"""

from datetime import date, timedelta

import pytest

import scripts.seed_kavanah_demo_dt as seeder


class TestCuratedTotalsWithEnrichedFields:

    def test_exact_totals_preserved_and_enrichment_fields_present(self):
        """should keep the 5 curated totals exact while every contact carries
        the map/quick-button fields the retired daily generator provided."""
        docs = seeder.build_docs()

        assert seeder._planned_counts(docs) == seeder._TARGETS

        contacts = [d for d in docs if d["post_type"] == "contacts"]
        assert all(d["meta"].get("contact_place") in seeder._CITIES for d in contacts)
        assert all(d["meta"].get("quick_button_contact_established") for d in contacts)
        assert all(d["meta"].get("quick_button_meeting_complete") for d in contacts)

    def test_baptism_generation_only_on_baptized_contacts(self):
        """ES `exists` matches empty strings, so non-baptized contacts must
        OMIT baptism_generation entirely (same trap as church_start_date)."""
        docs = seeder.build_docs()
        contacts = [d for d in docs if d["post_type"] == "contacts"]

        baptized = [d for d in contacts if d["meta"]["milestones"] == "milestone_baptized"]
        not_baptized = [d for d in contacts if d["meta"]["milestones"] != "milestone_baptized"]

        assert all("baptism_generation" in d["meta"] for d in baptized)
        assert all("baptism_generation" not in d["meta"] for d in not_baptized)


class TestSlidingWindowAnchoredAtToday:

    def test_doc_dates_span_trailing_window_ending_today(self):
        """should date docs across the trailing WINDOW_DAYS ending today, so a
        daily --reseed keeps the curated totals inside the report's rolling
        date range instead of decaying out of it."""
        docs = seeder.build_docs()
        created_dates = sorted(d["date_created"][:10] for d in docs)

        today = date.today()
        window_start = today - timedelta(days=seeder.WINDOW_DAYS - 1)
        assert created_dates[0] >= window_start.isoformat()
        # _spread_date's integer truncation puts the newest doc at today-1;
        # the point is the window is anchored at NOW, not at seed time.
        assert created_dates[-1] >= (today - timedelta(days=2)).isoformat()
        assert created_dates[-1] <= today.isoformat()
