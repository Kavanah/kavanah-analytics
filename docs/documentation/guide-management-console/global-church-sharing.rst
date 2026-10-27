.. _guide-global-church-sharing:

=======================================
Sharing with the Global.Church Map
=======================================

`Global.Church <https://global.church>`_ keeps a shared map of where
digital outreach is reaching people, country by country. An organization
on Kavanah Analytics can choose to contribute its own reach to that map.
This page explains exactly what is shared, what is never shared, how to
turn sharing on and off, and how a release is published.

.. important::

   **Sharing is off for every organization until its workspace owner
   turns it on.** Nothing about your data reaches Global.Church while it
   is off. Turning it on is a decision the workspace owner makes on the
   organization's behalf, and it can be reversed at any time.

----

What leaves, and what never leaves
==================================

A release is one file per organization. It contains, for each
**team × country × month** over the last 24 months, four whole numbers:

.. list-table::
   :header-rows: 1
   :widths: 20 45 35

   * - Count
     - Definition
     - Source
   * - **Exposed**
     - People exposed to your content: ad impressions and video views
     - Google Ads impressions, YouTube views, Facebook impressions by the
       country the ads delivered in
   * - **Interactions**
     - Clicks and sessions
     - Google Ads clicks, GA4 sessions, Facebook clicks by the country the
       ads delivered in
   * - **Touchpoints**
     - Conversations started, by the seeker's country
     - Echo conversations
   * - **Active**
     - Conversations with a spiritual-conversation or multiplier outcome
     - Echo conversations

Beside the counts travels a short manifest: which sources the counts
came from, which countries you chose to include, whether your
organization wants to be named on the map, and which version of the
data-sharing agreement was accepted (with the SHA-256 hash of its
text).

Each row identifies the team only by an **opaque identifier** — a
one-way hash of the team's internal id keyed with a secret that belongs
to your organization. No team name leaves. Global.Church cannot turn
the identifier back into a name; it only lets a later release replace
the earlier one for the same team.

**What never leaves Kavanah:**

- Every conversation, contact, group and seeker identifier.
- Responder names.
- Campaigns, ad sets, creatives, budgets and spend.
- Platform account identifiers.
- Regions, cities, and anything below country grain.

The file format cannot even represent anything finer than a country and
a month, and Kavanah validates every release against that format before
it is sent. A release that would carry anything else is refused, not
sent.

A country you leave out is simply absent from the release. Global.Church
shows it as "no contribution reported", exactly as it shows a country no
one reaches — never as "withheld".

Facebook ads are placed by where they actually delivered, using the
per-country breakdown Meta reports. For days imported before that
breakdown was collected, an ad set that targeted exactly one country is
placed in it, and an ad set that targeted several countries is reported
once as an unplaced total in the manifest rather than guessed into any
country.

----

Turning sharing on
==================

There are two places to do it. Both show the same explanation and the
same agreement text, and both record the same thing.

**At signup.** The workspace step of the signup wizard offers a box:
*Share country-level reach with the Global.Church shared map, and accept
its data-sharing agreement on behalf of this organization.* It is
unticked by default. Below it you can expand and read the full agreement
before deciding. If you tick it, sharing is turned on for your
organization as soon as it is created.

**Later, from the organization's sharing page.** Open the organization
in the Asset Explorer and follow the *Manage sharing* link in the
*Publish to Global.Church* section. The sharing page shows:

- whether sharing is on or off, and who last changed it;
- what leaves and what stays;
- the agreement, its version, and the SHA-256 of its text computed at
  that moment;
- an acceptance box and a **Turn sharing on** button.

Only the **workspace owner** can turn sharing on or off. Other admins
and Kavanah staff can read the page but not change it.

When sharing is turned on, Kavanah records on the organization: that
sharing is on, who turned it on and when, and which agreement (id,
version and hash) was accepted. The same acceptance travels in every
release the organization later publishes.

----

Publishing a release
====================

Once sharing is on, the organization gains a **Publish to Global.Church**
page. Publishing is a deliberate action by the workspace owner; nothing
is sent automatically.

1. **Choose the teams** to include. Only teams that belong to the
   organization are offered.
2. **Choose the countries.** Kavanah lists the countries present in the
   selected teams' data over the last 24 months. Untick any you do not
   want to share. The preview updates to show the four counts per country
   that would leave.
3. **Choose whether to be named.** The default is *"a contributing
   ministry"*. Bear in mind that while few ministries contribute, an
   unnamed contribution is thin cover: someone who knows which ministries
   run digital outreach in a country could guess. Decide which countries
   to include with that in mind.
4. **Accept the agreement** by ticking the box. The page shows the
   agreement text and its hash.
5. **Publish.** Kavanah builds the release, validates it, and sends it
   to the Global.Church gateway. Only after the gateway accepts the file
   does Kavanah record the acceptance and the receipt (release id,
   manifest hash and time) on the organization. If the gateway refuses,
   nothing is recorded and the page shows the gateway's reason.

A later release from the same organization replaces its earlier cells
on the map. A demo or test workspace can mark its release as *test
data*; Global.Church labels such releases everywhere they appear and
never presents them as a ministry's figures.

----

Turning sharing off, and withdrawing
====================================

On the sharing page, the workspace owner can **Turn sharing off**. From
that moment no further release can be published: the publish page
disappears and any scheduled publish is refused.

Turning sharing off does **not** recall releases already published. To
withdraw those, write to Global.Church at the contact given in the
agreement; under section 7 of the agreement they remove the
organization's cells from the map within five business days and keep
only the record that a release existed and was withdrawn.

Turning sharing back on later keeps the same opaque team identifiers,
so a new release still replaces the earlier cells rather than sitting
beside them.

----

For administrators
==================

The feature is controlled at two levels:

**Platform switch.** The whole feature sits behind the
``GLOBAL_CHURCH_PUBLISH`` feature flag, which is **off** on a fresh
installation. While it is off, the export refuses to run, the signup box
is not offered, and the sharing and publish pages do not exist. Turn it
on from inside the imports container::

    python3 -m tools.feature_flags show GLOBAL_CHURCH_PUBLISH
    python3 -m tools.feature_flags set GLOBAL_CHURCH_PUBLISH on

**Per-organization settings.** Three fields on the organization record
are set by a superuser, not by the workspace owner: the gateway URL (an
``https`` origin on an allow-listed host), the partner API key issued by
Global.Church (stored encrypted and used only to authenticate the
upload), and the organization's Global.Church directory URI. The publish
page tells the owner which of these are still missing.

The sharing service adds the fields it needs to the organization index
automatically the first time sharing is turned on; no migration step is
required when upgrading.

.. note::

   On Kavanah's hosted service the platform switch stays off until the
   first live publish has been tested and reviewed with Global.Church.
   Until then this page describes what the feature does, not what is
   available in your workspace today.
