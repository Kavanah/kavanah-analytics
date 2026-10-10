from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    TeamSlugCollisionError,
    add_document,
    check_team_slug_unique,
    edit_document,
    get_all_documents,
    get_document_by_id,
    get_elasticsearch_client,
)

INDEX_NAME = "de_asset_teams"


def _extract_org_esids(responses):
    """Return the org ESID list from a team-form response dict.

    The form stores ``organization_esid`` as either a string (single-org)
    or list (multi-org). Normalize to a list so the slug check sees a
    uniform shape.
    """
    raw = responses.get("organization_esid") or responses.get("organization_esids") or []
    if isinstance(raw, str):
        return [raw] if raw else []
    return [x for x in raw if x]


def _sync_team_url_scalar(responses):
    """keep the legacy scalar ``team_url`` in sync with the first
    entry of the multi-value ``team_urls`` list (parity with the web form's
    ``primary_scalar_field`` hook). The ~6 importers that stamp
    ``team_site_url = team.get("team_url")`` (FB / IG / YouTube) read the
    scalar, so it must always hold one representative URL. Mutates ``responses``
    in place; no-op when ``team_urls`` is missing/empty."""
    urls = responses.get("team_urls")
    if isinstance(urls, list) and urls:
        responses["team_url"] = urls[0]


def _seed_team_urls_from_scalar(responses):
    """seed ``team_urls`` from the legacy scalar ``team_url`` for a
    team predating the update_v20 migration, so the CLI edit form carries the
    existing single URL over instead of re-prompting from empty. Only seeds
    when the list is missing/empty and the scalar is present."""
    existing = responses.get("team_urls")
    if isinstance(existing, list) and existing:
        return
    scalar = (responses.get("team_url") or "").strip()
    if scalar:
        responses["team_urls"] = [scalar]


def add():
    title = "Adding New Team"
    instructions = [
        "Please provide the following details for the new Team.",
        "Fields marked with * are required.",
        "Note: Facebook App User selection is available during team edit after initial creation.",
    ]
    fields = [
        {"name": "team_title", "prompt": "Enter Team Title*:", "type": "text"},
        # (a teammate): a team can have more than one URL. ``list_input``
        # prompts for URLs one per line until "done" (>=1 required), mirroring
        # the web form's Add button. ``_sync_team_url_scalar`` keeps the legacy
        # scalar ``team_url`` = team_urls[0] for the importers.
        {"name": "team_urls", "prompt": "Team URLs*", "type": "list_input"},
        {
            # organization is required so the CLI cannot create
            # orphan teams (no org → invisible in the scope-filtered list
            # view, and check_team_slug_unique short-circuits its
            # uniqueness branch, letting duplicate-titled teams pile up).
            # The web Asset Explorer has enforced required-org since
            # ; the CLI add form had no org field at all. A single
            # org via store_associated_id writes organization_esid (string)
            # which _extract_org_esids normalizes to a list for the slug
            # check, and RoleFilterService treats string-or-list uniformly.
            "name": "organization_title",
            "prompt": "Select Organization*:",
            "type": "choice",
            "es_index": "de_asset_organization",
            "title_field": "organization_title",
            "module": "organizations",
            "allow_add": False,
            "store_associated_id": True,
        },
        {"name": "organization_esid", "type": "hidden"},
        {
            "name": "team_region",
            "prompt": "Select Region*:",
            "type": "choice",
            "es_index": "de_asset_regions",
            "title_field": "region_title",
            "module": "regions",
        },
        {
            "name": "team_country",
            "prompt": "Select Country*:",
            "type": "choice",
            "es_index": "de_asset_countries",
            "title_field": "country_title",
            "module": "countries",
        },
        {
            "name": "team_city",
            "prompt": "Select City*:",
            "type": "choice",
            "es_index": "de_asset_cities",
            "title_field": "city_title",
            "module": "cities",
        },
        # Echo Integration
        {
            "name": "echo_tenant_title",
            "prompt": "Select Echo Tenant*:",
            "type": "choice",
            "es_index": "de_asset_echo_tenants",
            "title_field": "echo_tenant_title",
            "module": "echo_tenants",
            "store_associated_id": True,
        },
        {"name": "echo_tenant_esid", "type": "hidden"},
        # Google Analytics Integration
        {
            "name": "team_ga_integration_status",
            "prompt": "Google Analytics Integration Status*:",
            "type": "choice",
            "choices": ["Active", "Disabled"],
            "allow_add": False,
            "condition_affects": True,
        },
        {
            "name": "ga_property_title",
            "prompt": "Select Google Analytics Property*:",
            "type": "choice",
            "condition": lambda responses: responses.get("team_ga_integration_status") == "Active",
            "es_index": "de_asset_ga_properties",
            "title_field": "ga_property_title",
            "module": "ga_properties",
            "store_associated_id": True,
        },
        {
            "name": "ga_property_esid",
            "type": "hidden",
            "condition": lambda responses: responses.get("team_ga_integration_status") == "Active",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        if responses.get("team_ga_integration_status") == "Disabled":
            responses.pop("ga_property_title", None)
            responses.pop("ga_property_esid", None)

        # derive the legacy scalar team_url from team_urls[0].
        _sync_team_url_scalar(responses)

        # reject empty team_title. The web Asset Explorer's
        # Add Team form has enforced this (2026-05-19);
        # the CLI was the remaining path that could land a stub team
        # like ``'New Team'`` (placeholder typed-and-saved) in
        # de_asset_teams. Match the web form's "<label> is required"
        # phrasing so operator-side errors don't surface in a third shape.
        if not (responses.get("team_title") or "").strip():
            print("Title is required.")
            return

        # reject a create with no organization. The choice field
        # returns None if the operator picks "Go Back" (or no orgs exist),
        # leaving organization_esid unset. Without this guard the team
        # lands orphaned: invisible in the scope-filtered list view and
        # exempt from the slug-uniqueness check.
        if not _extract_org_esids(responses):
            print("Organization is required.")
            return

        # reject a create that would produce an alias-slug
        # collision in any org the team would belong to.
        es = get_elasticsearch_client()
        if es is not None:
            try:
                check_team_slug_unique(
                    es,
                    team_title=responses.get("team_title", ""),
                    org_esids=_extract_org_esids(responses),
                )
            except TeamSlugCollisionError as exc:
                print(
                    "A team in this organization already uses a similar name. "
                    f"Sanitized slugs collide: {exc.slug!r}. Pick a different name."
                )
                return

        # Index the data into Elasticsearch
        if add_document(INDEX_NAME, responses):
            print("Team added successfully!")
        else:
            print("Failed to add Team.")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Teams found.")
        return

    # List titles and allow user to select one for details
    print("Teams:")
    teams = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("team_title", "No Title")
        team_id = hit["_id"]
        teams.append((team_id, title))
        print(f"{idx}. {title}")

    while True:
        try:
            selected_idx = int(input("\nEnter the number of the Team to view: ")) - 1
            if 0 <= selected_idx < len(teams):
                selected_id, selected_title = teams[selected_idx]
                break
            else:
                print(f"Invalid selection. Please enter a number between 1 and {len(teams)}.")
        except ValueError:
            print("Invalid input. Please enter a valid number.")

    view_details(selected_id)


def view_details(team_id):
    team = get_document_by_id(INDEX_NAME, team_id)
    if not team:
        return

    print("\nTeam Details:")
    print("----------------------")
    for key, value in team.items():
        # Only show Facebook fields if integration is active
        if key in ["team_facebook_page_id", "team_facebook_page_esids"] and team.get("team_fb_integration_status") != "Active":
            continue
        # Only show Google Analytics fields if integration is active
        if key in ["ga_property_title", "ga_property_esid"] and team.get("team_ga_integration_status") != "Active":
            continue
        print(f"{key}: {value}")
    print()

    print("1) Edit\n2) Delete\n3) Back")
    choice = input("Enter your choice: ").strip()

    if choice == "1":
        edit(team_id)
    elif choice == "2":
        delete(team_id)
    elif choice == "3":
        return
    else:
        print("Invalid choice. Returning to main menu.")


def edit(team_id):
    team = get_document_by_id(INDEX_NAME, team_id)
    if not team:
        return

    title = "Editing Team"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]

    fields = [
        {"name": "team_title", "prompt": "Team Title:", "type": "text"},
        # (a teammate): multi-URL team. ``list_input`` carries over the
        # existing URLs (seeded from the scalar for pre-migration teams via
        # ``_seed_team_urls_from_scalar`` below); editing re-prompts the list.
        {"name": "team_urls", "prompt": "Team URLs:", "type": "list_input"},
        {
            # org is required on edit too, so an edit can't strip
            # a team's last org and orphan it. The existing org carries
            # over via form.responses = team.copy(); re-selecting writes a
            # fresh organization_esid through store_associated_id.
            "name": "organization_title",
            "prompt": "Organization:",
            "type": "choice",
            "es_index": "de_asset_organization",
            "title_field": "organization_title",
            "module": "organizations",
            "allow_add": False,
            "store_associated_id": True,
        },
        {"name": "organization_esid", "type": "hidden"},
        {
            "name": "team_region",
            "prompt": "Region:",
            "type": "choice",
            "es_index": "de_asset_regions",
            "title_field": "region_title",
            "module": "regions",
        },
        {
            "name": "team_country",
            "prompt": "Country:",
            "type": "choice",
            "es_index": "de_asset_countries",
            "title_field": "country_title",
            "module": "countries",
        },
        {
            "name": "team_city",
            "prompt": "City:",
            "type": "choice",
            "es_index": "de_asset_cities",
            "title_field": "city_title",
            "module": "cities",
        },
        # Echo fields for editing
        {
            "name": "echo_tenant_title",
            "prompt": "Echo Tenant:",
            "type": "choice",
            "es_index": "de_asset_echo_tenants",
            "title_field": "echo_tenant_title",
            "module": "echo_tenants",
            "store_associated_id": True,
        },
        {"name": "echo_tenant_esid", "type": "hidden"},
        # Google Analytics fields for editing
        {
            "name": "team_ga_integration_status",
            "prompt": "Google Analytics Integration Status:",
            "type": "choice",
            "choices": ["Active", "Disabled"],
            "allow_add": False,
            "condition_affects": True,
        },
        {
            "name": "ga_property_title",
            "prompt": "Google Analytics Property:",
            "type": "choice",
            "condition": lambda responses: responses.get("team_ga_integration_status") == "Active",
            "es_index": "de_asset_ga_properties",
            "title_field": "ga_property_title",
            "module": "ga_properties",
            "store_associated_id": True,
        },
        {
            "name": "ga_property_esid",
            "type": "hidden",
            "condition": lambda responses: responses.get("team_ga_integration_status") == "Active",
        },
    ]

    form = FormHandler(title, instructions, fields)
    form.responses = team.copy()
    # pre-migration teams carry only the scalar team_url; seed the
    # team_urls list from it so the edit form shows the existing URL and the
    # collect-inputs skip-if-present logic carries it over unchanged.
    _seed_team_urls_from_scalar(form.responses)

    action, responses = form.edit_mode()

    if action == "submit":
        if responses.get("team_ga_integration_status") == "Disabled":
            responses.pop("ga_property_title", None)
            responses.pop("ga_property_esid", None)

        # keep the legacy scalar team_url = team_urls[0].
        _sync_team_url_scalar(responses)

        updated_document = {**team, **responses}

        # reject an edit that clears team_title (parity with
        # add()'s empty-title gate; the web edit path enforces this
        # via FormService.validate_required_fields).
        if not (updated_document.get("team_title") or "").strip():
            print("Title is required.")
            return

        # reject an edit that leaves the team with no org. Parity
        # with add()'s required-org gate; prevents stripping a team's last
        # org and orphaning it.
        if not _extract_org_esids(updated_document):
            print("Organization is required.")
            return

        # reject an edit that would produce an alias-slug
        # collision. Exclude this team itself so an edit that doesn't
        # change the title is a no-op (or a same-slug rename).
        es = get_elasticsearch_client()
        if es is not None:
            try:
                check_team_slug_unique(
                    es,
                    team_title=updated_document.get("team_title", ""),
                    org_esids=_extract_org_esids(updated_document),
                    exclude_team_esid=team_id,
                )
            except TeamSlugCollisionError as exc:
                print(
                    "A team in this organization already uses a similar name. "
                    f"Sanitized slugs collide: {exc.slug!r}. Pick a different name."
                )
                return

        if edit_document(INDEX_NAME, team_id, updated_document):
            print("Team updated successfully!")
        else:
            print("Failed to update team.")
    else:
        print("Operation canceled.")


def delete(team_id):
    confirmation = input(f"Are you sure you want to delete the Team? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, team_id):
            print("Team deleted successfully.")
        else:
            print("Failed to delete Team.")
    else:
        print("Delete operation canceled.")
