.. _reach-metrics:

==============
Reach Metrics
==============

**Reach** in Kavanah Analytics refers to the number of unique people who saw your content
across paid and organic channels. This page explains how paid and organic reach differ,
which data sources contribute to each, and how to interpret reach metrics in your dashboards.

----

Paid vs. Organic Reach
-----------------------

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * -
     - Paid Reach
     - Organic Reach
   * - **Definition**
     - People who saw your content because you paid to show it (ads)
     - People who saw your content without paid promotion (posts, stories, search)
   * - **Sources**
     - Facebook Ads, Google Ads, Instagram Ads
     - Facebook Pages, Instagram (organic), YouTube, GA4 (organic sessions)
   * - **Key metrics**
     - Spend, impressions, reach, clicks, conversions, ROAS
     - Reach, impressions, engagement rate, page likes/follows, video views
   * - **Dashboard tab**
     - Facebook Ads, Google Ads
     - Facebook Pages, Instagram, YouTube, Google Analytics

Understanding the distinction helps you evaluate the efficiency of your paid spend
against your organic growth.

----

Facebook Paid Reach
--------------------

Facebook Ads reach data comes from the ``*fb_insights`` index. Key fields:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Field
     - Description
   * - ``reach``
     - Unique people who saw the ad at least once
   * - ``impressions``
     - Total times the ad was displayed (one person can have multiple impressions)
   * - ``spend``
     - Amount spent in the account currency
   * - ``clicks``
     - Total link clicks
   * - ``unique_clicks``
     - Unique people who clicked (deduplicated)
   * - ``ctr``
     - Click-through rate (``clicks / impressions``)
   * - ``cpm``
     - Cost per thousand impressions
   * - ``cpp``
     - Cost per reach (cost per unique person reached)
   * - ``frequency``
     - Average times each reached person saw the ad (``impressions / reach``)

Data is segmented by ``campaign_name``, ``adset_name``, and ``ad_name``.
All records include ``organization_title`` and ``team_title`` for role-based filtering.

----

Facebook Organic Reach
-----------------------

Facebook Pages organic reach comes from the ``*fb_insights`` index (page-level metrics).
Key organic metrics:

- ``page_post_engagements`` — Total engagements across all page posts
- ``page_fan_adds`` — New page likes/follows
- ``page_views_total`` — Total page views
- ``post_impressions_organic`` — Organic impressions on posts
- ``post_reach_organic`` — Unique organic reach on posts

----

Instagram Reach
----------------

Instagram reach data (from ``*ig_insights``) includes both organic and promoted content:

- ``reach`` — Unique accounts that saw your post or story
- ``impressions`` — Total views
- ``engagement`` — Likes + comments + saves + shares
- ``video_views`` — For video content
- ``story_exits`` — How many people left after seeing a story

Instagram data is linked to a Facebook App, so it uses the same authentication
as Facebook Ads.

----

Google Ads Reach
-----------------

Google Ads data (``*google_ads``) focuses on paid search and display performance.
Reach equivalents in Google Ads:

- ``impressions`` — Times an ad was shown
- ``clicks`` — Link clicks
- ``conversions`` — Tracked conversion actions
- ``cost_micros`` — Spend in micros (divide by 1,000,000 for currency value)
- ``search_impression_share`` — Percentage of eligible impressions your ads received

Note: Google Ads uses ``.keyword`` suffix for aggregations on text fields in
Elasticsearch. The Dashboard Builder handles this automatically.

----

Google Analytics Organic Reach
--------------------------------

GA4 data (``*ga_metrics``) measures organic (and paid) web traffic:

- ``sessions`` — Individual user sessions on your website
- ``users`` — Unique users
- ``newUsers`` — First-time visitors
- ``engagedSessions`` — Sessions with > 10 seconds engagement or a conversion
- ``engagement_rate`` — ``engagedSessions / sessions``

Traffic source segmentation: ``sessionSource`` (e.g. ``google``, ``facebook.com``,
``direct``) and ``sessionMedium`` (e.g. ``organic``, ``cpc``, ``referral``).

----

Cross-Platform Reach Analysis
-------------------------------

To compare reach across platforms in the Dashboard Builder:

1. Open **Dashboard Builder** at ``/dashboard-builder/``.
2. Add a **Trend Line** chart.
3. Add multiple sources — one per platform — each with a ``reach`` or ``impressions``
   field and a date field as the X axis.
4. Set a shared date range across all sources.

The Trend Line chart will overlay all platforms on a single date-histogram chart,
making it easy to compare reach trends side by side.

----

Interpreting Platform Availability
-------------------------------------

On the Dashboards page, the **platform availability indicators** (green/grey dots on
each tab) reflect whether data exists for the currently selected organisation and team.

A grey dot does not necessarily mean reach is zero — it means no documents for that
org/team combination were found in the platform's index. Common causes:

- The platform has not been imported for this organisation yet
- The organisation filter is set to an org that does not have this platform configured
- The data is more than the import window covers (e.g., data was only pulled for 7 days
  but the date range is 30 days)
