.. _glossary:

========
Glossary
========

A canonical home for the words and acronyms used throughout the
Kavanah Analytics docs.

.. glossary::
   :sorted:

   Asset
      A configuration record stored in an ``de_asset_*`` Elasticsearch
      index — an Organisation, Team, Facebook App, Google Ads
      Customer, etc. Assets carry the credentials and metadata the
      import pipeline uses to talk to a third-party platform.

   Asset Explorer
      The Management Console page (``/assets/``) where you browse,
      add, edit, and delete every asset record. Gated on the
      ``mc_asset_explorer`` permission.

   Bypassed (2FA state)
      A short-lived superuser-granted exception that lets a user log
      in once without 2FA. Consumed on the next successful login.
      Used to recover users who've lost their TOTP device or YubiKey.

   Connector
      A configured pipeline between Kavanah and a third-party platform
      (Facebook, Google Ads, Echo, Chatwoot, etc.). Each connector is
      built from one or more Assets.

   Data Connectors (page)
      The Management Console page (``mc_data_connectors``) that walks
      you through setting up a Connector with a guided wizard.
      Currently covers Facebook, Echo, and Chatwoot; the manual
      Asset-Explorer chain still works for everything else.

   Data Explorer
      The natural-language query assistant in the Management Console
      (``mc_data_explorer``). Powered by Anthropic Claude (or AWS
      Bedrock) with a model selector and per-day usage tracking.

   Data View
      A label for an Elasticsearch index pattern (``*fb_insights``,
      ``*google_ads``, etc.) that maps to a platform's data. The Data
      Explorer and Dashboard Builder both call them "Data Views".
      Defined in ``deployment/create-data-views.sh``.

   DEAM
      *Digital Engagement Asset Management* — the historical name for
      the asset-management web interface. Often used interchangeably
      with "the asset manager" or "the Management Console."

   Digital Impact Report
      A pre-formatted full-page dashboard summarising one
      organisation's impact across paid and organic channels. Three
      variants: Facebook DIR, Google DIR, and Combined DIR.

   ENFORCE_MFA
      Deployment-wide config flag (in ``master-config.yaml`` or the env
      variable of the same name) that forces every user to enrol in
      2FA. Defaults to ``true``.

   ESID
      *Elasticsearch Document ID* — the unique ``_id`` of a document
      in an Elasticsearch index. Many asset records reference each
      other by ESID (e.g. a Team's ``organization_esid`` field).

   Impact Report
      Short for :term:`Digital Impact Report`.

   ``mc_*`` permission
      A permission flag attached to a role that grants access to one
      Management Console page (``mc_user_management``,
      ``mc_dashboard_builder``, ``mc_data_explorer``, etc.). Replaced
      the old ``org_admin`` role gate.

   MFA
      Multi-Factor Authentication. Used interchangeably with 2FA in
      the Kavanah Analytics docs. Two methods are supported: TOTP and
      YubiKey.

   Org Admin
      A user who manages other users within their own organisation.
      Historically a built-in role (``org_admin``); this
      is implemented by granting a custom role the
      ``mc_user_management`` permission plus the relevant
      ``org_<esid>`` role.

   Organisation
      A top-level client / tenant in Kavanah Analytics. Stored in
      ``de_asset_organization``. Every imported data record is tagged
      with an ``organization_title`` for filtering.

   Pending (2FA state)
      An account where an admin has enabled 2FA but the user hasn't
      completed enrolment yet. The user is forced through enrolment
      on next sign-in.

   Public Impact Report
      A Digital Impact Report published to a public URL via the
      Publish flow. Anyone with the URL can view the report
      without a Kavanah login; data is server-side scoped to the
      report's assigned org or team.

   Reach
      The metric counting unique people who saw your content. For
      Facebook this is the platform-reported reach figure; for organic
      and Echo data, Kavanah computes a comparable estimate.

   Restore from Backup (page)
      The Management Console page (``mc_restore_backup``) that lists
      local asset backups and S3 Elasticsearch snapshots, and lets you
      restore individual config files. Successor to the legacy
      ``/api/backup/*`` tarball API.

   superuser
      The built-in Elasticsearch security role that grants
      unrestricted access to everything in Kavanah. ``superuser`` and
      ``asset_admin`` carry every ``mc_*`` permission by default.

   Team
      A subdivision within an :term:`Organisation`. Stored in
      ``de_asset_teams``. Imported data records that belong to a team
      carry both ``organization_title`` and ``team_title``.

   TOTP
      *Time-based One-Time Password* — the authenticator-app form of
      2FA. Compatible with Google Authenticator, Authy, 1Password,
      etc. Issuer name is "Kavanah Analytics".

   YubiKey
      A USB hardware key from Yubico that produces a one-time password
      when tapped. The hardware-key form of 2FA in Kavanah Analytics.
      Validation requires outbound internet access to Yubico's API.
