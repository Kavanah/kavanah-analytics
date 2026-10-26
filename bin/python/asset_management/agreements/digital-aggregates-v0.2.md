# Digital Engagement Aggregates — Data Sharing Agreement

**Agreement id:** `digital-aggregates` · **Version:** 0.2 · **Status: DRAFT — placeholder for counsel.** This text is the working draft Global.Church uses to build and test the publish flow. It is not a legal agreement until counsel has reviewed it and a signed version replaces it. Every release published under it carries the SHA-256 of these exact words, so a later version changes the hash and prior releases stay anchored to the text they accepted.

---

## 1. Parties

**The Contributor** — the organization whose workspace owner accepts this agreement inside its analytics system (the "partner system"), on the organization's behalf.

**The Steward** — Global.Church, Inc., a 501(c)(3), which receives the release, stores it, and renders it on the shared map and through its API.

Acceptance is recorded by the partner system with the accepting role (workspace owner), the time, and the hash of this text. No person is named in the release.

## 2. What the Contributor shares

A **release product**: a file of aggregate counts at **country × month** grain, one row per team label × country × month, for the teams and countries the Contributor chose, over at most the last 24 months.

| Count | Definition | Source |
|---|---|---|
| Exposed | People exposed: ad impressions and video views | Google Ads impressions, YouTube views, Facebook impressions from ad sets targeting a single country |
| Interactions | Clicks and sessions | Google Ads clicks, GA4 sessions, Facebook clicks from single-country ad sets |
| Touchpoints | Conversations started, by the seeker's country | Echo conversations |
| Active | Conversations with a spiritual-conversation or multiplier outcome | Echo conversations |

Each row carries an opaque per-team identifier assigned by the Contributor's system. No team title leaves with the row, and nothing in the file identifies a person or a team by name. The Steward holds the identifier as confidential provenance and never renders it.

The release also carries a manifest: schema version, the grain in words, the suppression rule (none, §4), the sources used, the countries included, the organization's directory identifier, whether the organization wants to be named, this agreement's id, version, hash and acceptance time, and when and by what software the file was generated.

## 3. What never leaves the Contributor's system

Conversation rows. Contacts, groups and seeker identifiers. Responder names. Campaigns, ad sets, creatives, budgets and spend. Platform account identifiers. Anything below country grain. The Steward records no address for these and never requests them under this agreement.

## 4. Grain and suppression

The grain is country × month. **No small-cell suppression and no restricted-country list apply at this grain**: a country-level count of digital conversations locates no one. The Contributor's own choice of which teams and countries to include is the control.

A country the Contributor leaves out is simply absent from the release. The Steward renders it as **"no contribution reported"**, exactly as it renders a country no one reaches. It is never rendered as "withheld".

## 5. Attribution

Every count on the map and in the API is attributed to its Contributor, under this agreement's version and hash, with the date it was loaded.

The Contributor chooses whether to be **named**. The default is **"a contributing ministry"**. While few ministries contribute, an unnamed contribution is thin cover: a reader who knows which ministries run digital outreach in a country may guess the source, and the Contributor should decide to include a country with that in mind.

## 6. What the Steward does with the release

- Validates it against the published schema and refuses anything that is not country × month.
- Stores the file with its hash and loads it into the shared knowledge graph under the Contributor's attribution.
- Renders the counts on the shared map and answers questions about them through its API and assistant, at country grain, with the attribution in §5.
- Combines counts across Contributors where they share a definition, so the map shows the fuller picture, and lets a reader choose which Contributors' counts to include. Every count stays attributed to its Contributor (§5) and carries its definition (§2). Counts with different definitions are shown side by side, never added together.
- Does not sell the release, and does not share it with any party on terms other than these.

## 7. Supersession and withdrawal

A later release from the same Contributor replaces its earlier cells. The Contributor may withdraw at any time by publishing an empty release or by writing to the Steward; the Steward removes the Contributor's cells from the map and the graph within five business days and keeps only the record that a release existed and was withdrawn (its id, hash and dates).

## 8. Test data

A release marked as test data (a demo or test workspace) is labelled as such everywhere it appears and is never presented as a ministry's figures.

## 9. Term

This agreement applies to each release that carries its hash, from acceptance until withdrawal under §7. A new version of this text is a new agreement; it does not alter releases accepted under an earlier one.

## 10. Contact

Global.Church, Inc. — paul.martel@global.church · security reports: security@global.church
