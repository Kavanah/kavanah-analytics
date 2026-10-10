# Contributing to the Kavanah Analytics docs

This file is the working manual for editing the Sphinx documentation
under `docs/documentation/`. The intended reader is anyone — Kavanah
team or external contributor — who needs to add a page, fix wording,
or refresh content after a code change.

## Quick build loop

```bash
cd docs/documentation
python3 -m pip install -r requirements.txt
python3 -m sphinx -b html -E -q . _build/html
open _build/html/index.html   # macOS — adjust for your OS
```

`-E` discards Sphinx's cached environment so every source file is
re-read; this is the right default while iterating because Sphinx's
incremental builds occasionally miss cross-page changes (toctree
shuffles, glossary additions). `-q` keeps the output to warnings + the
final summary.

For a strict build (warnings → errors), add `-W`:

```bash
python3 -m sphinx -b html -E -W . _build/html
```

For a link audit:

```bash
python3 -m sphinx -b linkcheck . _build/linkcheck
```

## Where to put a new page

Decide first whether the page is a **guide** (step-by-step
walkthrough, second person, "click X then Y") or a **reference**
(facts, tables, terminology):

| Subject | Guide goes in | Reference goes in |
|---|---|---|
| Dashboards page (viewer) | `guide-dashboards/` | `dashboards/` |
| Management Console pages | `guide-management-console/` | `management-console/` |
| Data Explorer | `guide-data-explorer/` | (no separate ref folder yet) |
| Onboarding / first-run | `getting-started/` | — |
| REST API | — | `api-reference/` |
| Release notes | — | `release-notes/` |
| Self-hosted admin | — | `self-hosted/` |

After adding the file, wire it into the corresponding `index.rst`
(both the `toctree` directive and the "In this section" list).

The top-level `index.rst` is the landing page — only update it for
brand-new top-level sections, not individual pages.

## Style conventions

**Voice and audience.** End-user guides are second-person ("click
**Save & Refresh**"). Reference docs are third-person ("the Save &
Refresh button…"). The split is in the previous section's table.

**Headings.** Use the standard hierarchy:

```rst
==========
Page title  (= overline + underline, level 1)
==========

Section
=======

Subsection
----------

Sub-subsection
~~~~~~~~~~~~~~
```

The autosectionlabel extension generates `:ref:` targets for every
heading. **Section names within a single file must be unique** or
Sphinx will warn about duplicate labels — disambiguate ("Step 2 —
Confirm tenant" vs. "Step 2 — Confirm account").

**Quoting UI elements.** Bold the exact text the user clicks:
**Save & Refresh**, **+ Create User**. Quote tab names and field
labels the same way.

**Code blocks.** Use ` `` ` for inline code (file names, env vars,
small command fragments) and `.. code-block:: <lang>` for multi-line
shell snippets. Common languages: `bash`, `python`, `json`, `text`.

**Cross-references.** Prefer `:doc:` over `:ref:` for whole-page
links — the path is more obvious and survives heading renames.
Always use absolute doc paths from the `documentation/` root, e.g.
`:doc:`/management-console/two-factor-auth``, not relative paths.

**Tables.** `list-table` is preferred over reStructuredText grid
tables — easier to edit and review.

**Lists.** Use `-` for bullet lists. Don't mix `-` and `*` in one
file.

**Notes and warnings.** Reach for these in moderation:

```rst
.. note::

   Brief callout — extra context, a tip, or a non-critical
   limitation.

.. warning::

   Surface a destructive action or a setup gotcha that genuinely
   bites people.

.. tip::

   Pointer to related content or a "did you know" shortcut.
```

Don't admonition-spam — most paragraphs should be plain prose.

## When the underlying code changes

Whenever a Flask route, button label, perm gate, or data shape
changes, the corresponding doc needs the same update in the same PR.
The audit you do before writing the doc:

1. **Code is the source of truth.** Read the actual template /
   route, don't trust earlier doc copy.
2. **Look for permission gates.** Search for `_require_admin`,
   `_require_superuser`, `_require_mc`, `_require_data_explorer` to
   confirm what role / `mc_*` perm controls the route.
3. **Quote UI text exactly** as it appears in the template — button
   labels, status badges, dialog titles. Future-you (or someone
   filing a bug) will compare the doc against the running app.
4. **Note when a flow changed.** If KAN-XYZ changed the behaviour,
   reference the ticket inline (e.g. `(admins no longer
   pre-select the MFA method)`). Keeps the why visible without
   needing a separate changelog page.

## Permission-aware navigation

The sidebar hides whole sections from users who don't have the
required role. The mapping lives in
`_static/custom.js` (`SECTION_PREFIXES`) and the Flask backend's
`/api/documentation-permissions` endpoint.

If you add a top-level toctree caption to the main `index.rst` for a
new section that should be role-gated:

1. Add the slug → URL prefix entry to `SECTION_PREFIXES` in
   `_static/custom.js`.
2. Add the matching key to the response shape in the Flask endpoint
   that serves `documentation-permissions` (look for
   `permissions["sections"]`).
3. Restart the imports container so the Flask change takes effect.

Captions for *public* sections (e.g. Reference, Getting Started)
don't need any wiring — `identifySection()` returns `null` for
unknown URL prefixes and leaves the caption visible to everyone.

## Deploying

Docs are static HTML served by nginx from `/var/www/deam-docs/`,
**not** by Flask. Pushing to `dev` alone doesn't update the live
site — the Sphinx build has to run on the host.

The deploy is a single command on the dev server:

```bash
ssh ubuntu@<dev-server-ip>
cd ~/DE-Reporting && git pull origin dev && bash deployment/step7-build-docs.sh
```

`step7-build-docs.sh` is idempotent and takes ~30 seconds.

## Style checklist before opening a PR

- [ ] Page builds with no warnings (`sphinx-build -W` is clean).
- [ ] Linkcheck is clean (`sphinx-build -b linkcheck`).
- [ ] All UI text quoted exactly as in the template.
- [ ] Permission gating mentioned where relevant (matches the
      `_require_*` call in the route handler).
- [ ] New page wired into the parent `index.rst` toctree and "In
      this section" list.
- [ ] Cross-references use absolute `:doc:` paths.
- [ ] No mixed `-` / `*` bullets, no duplicate sub-headings within a
      file.

## Where to ask

Open a under the **KAN** project tagged `area:docs`, or
mention `@docs` in the dev channel.
