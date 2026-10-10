"""
Form Service - handles form processing, encryption, and ESID storage.
"""

import sys
import os
import re
import string
import unicodedata

# Add parent directories to path for imports
sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)

from lib.encryption import encrypt, decrypt
from .asset_service import AssetService


# reference-data source indices are global lookup tables, not
# tenant-owned assets. They legitimately carry no ``workspace_esid``, so the
# workspace-narrowing in ``get_select_options`` must NOT fail-closed on them
# (that would blank the Country / Region dropdowns on the team form for every
# workspace-scoped user). Every other source index is tenant-owned and DOES
# fail-closed. Keep this list in sync with the reference-data ``source`` values
# in ``asset_configs.py`` (currently the team form's countries + regions).
_REFERENCE_DATA_SOURCES = frozenset(
    {
        "de_asset_countries",
        "de_asset_regions",
    }
)

# The workspace index is the one tenant-owned source whose docs do NOT carry a
# ``workspace_esid`` field — a workspace's own identity IS its ``_id``. So the
# workspace-narrowing below must key on ``hit["_id"]`` for this source, not on
# ``src.get("workspace_esid")`` (which is always None here, dropping every
# option and blanking the Workspace dropdown for any privileged user — the
# teams/orgs forms and the superuser-only data-source workspace selector).
# ``RoleFilterService.get_allowed_workspaces`` already returns workspace ESIDs
# (doc ``_id``s), so the allow-list comparison is correct once we read the
# right key. Literal string kept in sync with
# ``permissions_index_service.WORKSPACE_INDEX`` (same convention as the
# reference-data sources above).
_WORKSPACE_SOURCE_INDEX = "de_asset_workspace"


# default maximum length per user-typed field type, enforced
# server-side. MUST mirror the HTML ``maxlength`` fallbacks in form.html so the
# client and server agree. A field's own ``max_length`` overrides the default.
# Picker types (select / multi_select / choice / hidden / logo / section_divider)
# are not free-typed and carry no default.
_FIELD_MAXLEN_DEFAULTS = {
    "text": 255,
    "url": 2048,
    "password": 512,
    "textarea": 2000,
    "list": 255,  # applied per item
}


class FormService:
    """Service class for form processing operations."""

    @staticmethod
    def is_reference_data_source(index_name: str) -> bool:
        """True for a global lookup index (countries / regions) that no
        tenant owns, so a posted reference into it needs no scope check."""
        return index_name in _REFERENCE_DATA_SOURCES

    @staticmethod
    def validate_required_fields(form_data, config, mode="add"):
        """
        Validate that all required fields are present and non-empty.

        Args:
            form_data: Form data from request.form
            config: Asset configuration from ASSET_CONFIGS
            mode: 'add' or 'edit'

        Returns:
            Tuple of (is_valid, error_message)
        """
        errors = []

        for field in config["fields"]:
            # Skip virtual/UI-only field types
            if field.get("type") in ("section_divider", "data_source_checkbox"):
                continue

            # Skip edit-only fields in add mode
            if field.get("edit_only") and mode == "add":
                continue

            # Skip conditional fields if condition not met
            if field.get("condition"):
                condition_field = field["condition"]["field"]
                condition_value = field["condition"]["value"]
                actual_value = form_data.get(condition_field, "")
                if actual_value != condition_value:
                    continue

            # Check if field is required
            if field.get("required"):
                # Skip encrypted fields in edit mode (they can be left blank to keep existing value)
                if field.get("encrypted") and mode == "edit":
                    continue

                # follow-up: multi_select store_esid fields don't
                # submit the bare ``name`` key — the widget posts
                # ``{name}_esids`` + ``{name}_titles`` hidden inputs.
                # Reading form_data[name] returns empty even when the
                # picker has selections, so the bare-name check was a
                # silent always-empty for these fields. Read the esids
                # input instead, mirroring process_form_data's branch
                # at form_service.py:152.
                if field.get("type") == "multi_select" and field.get("store_esid"):
                    field_value = form_data.get(f"{field['name']}_esids", "").strip()
                else:
                    field_value = form_data.get(field["name"], "").strip()
                if not field_value:
                    errors.append(f"{field['label']} is required")

            # length + format validation for user-typed fields.
            # multi_select/select/etc. are picked from sources, not typed, so
            # they are excluded (the per-type default map only covers typed
            # types). For ``list`` fields the submitted value is a newline-
            # joined blob, so each entry is validated independently — checking
            # the blob would mis-measure length and pattern-match across items.
            ftype = field.get("type", "text")
            if ftype in _FIELD_MAXLEN_DEFAULTS:
                raw = form_data.get(field["name"], "")
                if ftype == "list":
                    values = [v.strip() for v in raw.split("\n") if v.strip()]
                else:
                    values = [raw.strip()] if raw.strip() else []

                max_len = field.get("max_length", _FIELD_MAXLEN_DEFAULTS[ftype])
                pattern = field.get("pattern")
                for value in values:
                    if len(value) > max_len:
                        errors.append(f"{field['label']} must be {max_len} characters or fewer")
                        break  # one length error per field is enough
                if pattern:
                    for value in values:
                        if not re.fullmatch(pattern, value):
                            errors.append(f"{field['label']} format is invalid")
                            break

        if errors:
            return False, "; ".join(errors)
        return True, None

    @staticmethod
    def generate_random_string(length):
        """Generate a random alphanumeric string of the given length.

        delegates to the CSPRNG generator in ``lib.oauth_state`` —
        these values seed the ``*_auth_url`` / ``*_auth_state`` OAuth fields via
        ``auto_generate``, so ``random.choices`` was never appropriate.
        """
        from lib.oauth_state import random_alnum

        return random_alnum(length)

    @staticmethod
    def generate_unique_value(index_name, field_name, length):
        """Generate a unique random string for a field."""
        while True:
            value = FormService.generate_random_string(length)
            if AssetService.check_unique_value(index_name, field_name, value):
                return value

    @staticmethod
    def process_form_data(form_data, config, existing_data=None, mode="add"):
        """
        Process form submission data.

        Args:
            form_data: Form data from request.form
            config: Asset configuration from ASSET_CONFIGS
            existing_data: Existing document data (for edit mode)
            mode: 'add' or 'edit'

        Returns:
            Processed document data ready for Elasticsearch
        """
        processed = {}

        for field in config["fields"]:
            name = field["name"]

            # Skip virtual/UI-only field types (no form input, nothing to process)
            # logo fields are handled separately via request.files, not request.form
            if field.get("type") in ("section_divider", "logo"):
                continue

            value = form_data.get(name, "").strip() if form_data.get(name) else ""

            # Skip edit-only fields in add mode
            if field.get("edit_only") and mode == "add":
                continue

            # Skip conditional fields if condition not met
            if field.get("condition"):
                condition_field = field["condition"]["field"]
                condition_value = field["condition"]["value"]
                actual_value = form_data.get(condition_field, "")
                if actual_value != condition_value:
                    continue

            # Handle encrypted fields
            if field.get("encrypted"):
                original_encrypted_value = existing_data.get(name) if existing_data else None

                if value:
                    # Check if value changed by comparing with original encrypted value
                    # In edit mode, form submits the encrypted value if user didn't change it
                    if mode == "edit" and original_encrypted_value and value == original_encrypted_value:
                        # Value unchanged - keep existing encrypted value
                        processed[name] = original_encrypted_value
                    else:
                        # New value or changed value - encrypt it
                        processed[name] = encrypt(value)
                elif existing_data and name in existing_data:
                    # Blank but has existing - keep existing encrypted value
                    processed[name] = existing_data[name]
                continue

            # Handle multi_select fields with ESID storage
            if field.get("type") == "multi_select" and field.get("store_esid") and field.get("source"):
                esid_field = field.get("esid_field", name.replace("_title", "_esid"))
                value_field_name = field.get("value_field")  # Optional: specific field to store (e.g., 'country_iso3_code')

                # Get the arrays from hidden fields
                esids_value = form_data.get(f"{name}_esids", "").strip()
                titles_value = form_data.get(f"{name}_titles", "").strip()
                values_value = form_data.get(f"{name}_values", "").strip() if value_field_name else None

                if esids_value and titles_value:
                    # Split by delimiter
                    esids = [e.strip() for e in esids_value.split("|||") if e.strip()]
                    titles = [t.strip() for t in titles_value.split("|||") if t.strip()]

                    # Store ESID array
                    processed[esid_field] = esids

                    # If value_field is specified, store those values instead of titles
                    if value_field_name and values_value:
                        values = [v.strip() for v in values_value.split("|||") if v.strip()]
                        processed[name] = values
                    else:
                        # Otherwise store titles as before
                        processed[name] = titles
                elif not field.get("required"):
                    # Empty value on optional field
                    if mode == "edit":
                        # read-only connector fields (team form)
                        # are no longer submitted, so they arrive empty
                        # here on every edit. preserve_on_empty keeps the
                        # stored value instead of deleting it — otherwise
                        # editing a team would wipe its connectors.
                        if field.get("preserve_on_empty"):
                            pass
                        else:
                            # In edit mode, mark for deletion
                            processed[f"__delete__{name}"] = True
                            processed[f"__delete__{esid_field}"] = True
                    # In add mode, simply don't add these fields
                continue

            # Handle select fields with ESID storage
            if field.get("store_esid") and field.get("source"):
                esid_field = field.get("esid_field", name.replace("_title", "_esid"))
                if value:
                    # The select value is the document ID (esid)
                    processed[esid_field] = value
                    # Look up the title for display
                    doc = AssetService.get_by_id(field["source"], value)
                    if doc:
                        processed[name] = doc.get(field["source_field"], "")
                elif not field.get("required"):
                    # Empty value on optional field - handle for both add and edit mode
                    if mode == "edit":
                        # read-only connector fields (team form)
                        # aren't submitted; preserve their stored value
                        # rather than deleting it on every team edit.
                        if field.get("preserve_on_empty"):
                            pass
                        else:
                            # In edit mode, mark for deletion
                            processed[f"__delete__{name}"] = True
                            processed[f"__delete__{esid_field}"] = True
                    # In add mode, simply don't add these fields (leave them out of the document)
                continue

            # Handle list fields (multiline text -> list)
            if field.get("type") == "list":
                # a ``list`` field may carry ``primary_scalar_field``
                # — a legacy single-value column kept in sync with the first
                # list entry so existing readers (e.g. importers reading the
                # scalar ``team_url``) stay unbroken when the field gains an
                # Add button. The list is the source of truth; the scalar is
                # derived (first entry, or deleted when the list is empty).
                primary_scalar = field.get("primary_scalar_field")
                if value:
                    # Split by newlines and filter empty lines
                    items = [item.strip() for item in value.split("\n") if item.strip()]
                    processed[name] = items
                    if primary_scalar:
                        processed[primary_scalar] = items[0]
                elif not field.get("required"):
                    # Empty value on optional list field
                    if mode == "edit":
                        # In edit mode, mark for deletion
                        processed[f"__delete__{name}"] = True
                        if primary_scalar:
                            processed[f"__delete__{primary_scalar}"] = True
                    # In add mode, simply don't add this field
                continue

            # checkbox_group — a fixed set of static options rendered
            # as checkboxes, all submitting under the same field name. Collect
            # the checked values (via getlist when the form is a MultiDict) into
            # a keyword list, filtered to the field's declared option values so
            # a crafted POST can't smuggle arbitrary values in. Empty selection
            # clears the field (delete in edit mode) so "untick everything"
            # reverts the team to the default-list fallback.
            if field.get("type") == "checkbox_group":
                allowed = {opt["value"] for opt in field.get("options", [])}
                if hasattr(form_data, "getlist"):
                    submitted = form_data.getlist(name)
                else:
                    raw = form_data.get(name)
                    submitted = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
                selected = [v for v in submitted if v in allowed]
                if selected:
                    processed[name] = selected
                elif not field.get("required"):
                    if mode == "edit" and not field.get("preserve_on_empty"):
                        processed[f"__delete__{name}"] = True
                    # add mode: leave the field out of the document
                continue

            # Regular fields
            if value:
                processed[name] = value
            elif not field.get("required"):
                # Empty value on optional field - handle for both add and edit mode
                if mode == "edit":
                    if field.get("preserve_on_empty"):
                        # Don't delete — keep the existing value from the stored document
                        pass
                    else:
                        # In edit mode, mark for deletion
                        processed[f"__delete__{name}"] = True
                # In add mode, simply don't add this field (leave it out of the document)

        # Handle auto-generated fields
        if "auto_generate" in config and mode == "add":
            for field_name, gen_config in config["auto_generate"].items():
                length = gen_config.get("length", 15)
                if gen_config.get("unique"):
                    processed[field_name] = FormService.generate_unique_value(config["index"], field_name, length)
                else:
                    processed[field_name] = FormService.generate_random_string(length)

        return processed

    @staticmethod
    def prepare_for_display(item, config):
        """
        Prepare item data for display, masking encrypted fields and filtering ESID fields.

        Args:
            item: Document data from Elasticsearch
            config: Asset configuration

        Returns:
            Display-ready item data
        """
        display_item = dict(item) if item else {}

        # Filter out ESID fields (internal references that shouldn't be displayed)
        display_item = {k: v for k, v in display_item.items() if not k.endswith("_esid") and not k.endswith("_esids")}

        for field in config["fields"]:
            name = field["name"]
            if field.get("encrypted") and name in display_item:
                # Mask encrypted fields
                display_item[name] = "********"

        return display_item

    @staticmethod
    def prepare_for_edit(item, config):
        """
        Prepare item data for edit form.
        Encrypted fields are left blank (user enters new value or keeps existing).
        NOTE: Do NOT filter ESID fields here - they're needed for multi-select fields!

        Args:
            item: Document data from Elasticsearch
            config: Asset configuration

        Returns:
            Edit-form-ready item data
        """
        edit_item = dict(item) if item else {}

        for field in config["fields"]:
            name = field["name"]

            # Keep encrypted value visible in edit form for transparency
            # (user can see encrypted value, change detection happens in process_form_data)

            # Prepare multi_select fields for editing
            if field.get("type") == "multi_select" and field.get("store_esid"):
                esid_field = field.get("esid_field", name.replace("_title", "_esid"))
                value_field_name = field.get("value_field")  # Optional: specific field stored (e.g., 'country_iso3_code')

                # Get arrays from stored data (ESID fields are NOT filtered out here!)
                esids = edit_item.get(esid_field, [])
                stored_values = edit_item.get(name, [])  # Could be titles or value_field values

                # Ensure they're lists
                if not isinstance(esids, list):
                    esids = [esids] if esids else []
                if not isinstance(stored_values, list):
                    stored_values = [stored_values] if stored_values else []

                # If we have ESIDs, we always need to look up titles from source documents
                # This ensures we display the current value from the source, not stale data
                if esids and field.get("source"):
                    new_titles = []
                    valid_esids = []
                    valid_stored_values = []
                    # when a stale ESID doesn't resolve, try a
                    # by-value recovery against the source index so we can
                    # still render the friendly title for any team whose
                    # ESIDs and primary value array have drifted apart.
                    fallback_docs = None

                    for i, esid in enumerate(esids):
                        try:
                            doc = AssetService.get_by_id(field["source"], esid)
                            if doc:
                                # Successfully found the document
                                valid_esids.append(esid)
                                # Get the current title from source_field
                                current_title = doc.get(field["source_field"], "N/A")
                                new_titles.append(current_title)
                                # Keep the stored value if it exists
                                if i < len(stored_values):
                                    valid_stored_values.append(stored_values[i])
                                else:
                                    # If no stored value, use the current title
                                    valid_stored_values.append(current_title)
                            else:
                                # ESID didn't resolve. Try recovering by
                                # matching the stored primary value (e.g.
                                # team_facebook_page_id entry) against the
                                # source_field of any source doc — fixes the
                                # team_facebook_page_esids divergence case
                                # without needing a backfill.
                                recovered_doc = None
                                recovered_esid = ""
                                if i < len(stored_values) and field.get("source_field"):
                                    stored_val = str(stored_values[i])
                                    if fallback_docs is None:
                                        fallback_docs = AssetService.get_all(field["source"])
                                    for hit in fallback_docs:
                                        src = hit["_source"]
                                        if str(src.get(field["source_field"], "")) == stored_val:
                                            recovered_doc = src
                                            recovered_esid = hit["_id"]
                                            break
                                if recovered_doc:
                                    valid_esids.append(recovered_esid)
                                    current_title = recovered_doc.get(field["source_field"], "N/A")
                                    new_titles.append(current_title)
                                    valid_stored_values.append(current_title)
                                else:
                                    # True orphan — keep ESID slot but show
                                    # the raw stored value so the admin can
                                    # see (and fix) the unresolved entry.
                                    valid_esids.append(esid)
                                    fallback_title = stored_values[i] if i < len(stored_values) else f"[Missing: {esid}]"
                                    new_titles.append(fallback_title)
                                    valid_stored_values.append(fallback_title)
                        except Exception as e:
                            # Error looking up - keep ESID but use stored value as fallback
                            valid_esids.append(esid)
                            fallback_title = stored_values[i] if i < len(stored_values) else f"[Error: {esid}]"
                            new_titles.append(fallback_title)
                            valid_stored_values.append(fallback_title)

                    # cover the asymmetric-drift case where the
                    # primary value array (team_facebook_page_id) has more
                    # entries than the ESID array — append by-value matches
                    # for any trailing stored_values not already represented
                    # so dropped pages still surface in the chips.
                    if field.get("source_field") and len(stored_values) > len(esids) and not value_field_name:
                        if fallback_docs is None:
                            fallback_docs = AssetService.get_all(field["source"])
                        for stored_val in stored_values[len(esids) :]:
                            stored_str = str(stored_val)
                            matched = False
                            for hit in fallback_docs:
                                src = hit["_source"]
                                if str(src.get(field["source_field"], "")) == stored_str:
                                    valid_esids.append(hit["_id"])
                                    current_title = src.get(field["source_field"], "N/A")
                                    new_titles.append(current_title)
                                    valid_stored_values.append(current_title)
                                    matched = True
                                    break
                            if not matched:
                                valid_esids.append("")
                                new_titles.append(stored_str)
                                valid_stored_values.append(stored_str)

                    # Update with the looked-up values
                    esids = valid_esids
                    stored_values = valid_stored_values
                    # Override stored_values with the fresh titles we just looked up
                    # This ensures we always show current data
                    if new_titles:
                        stored_values = new_titles

                # If esids are missing but we have stored_values, try to look them up
                # This handles legacy data or cases where esids weren't properly stored
                elif not esids and stored_values and field.get("source"):
                    new_esids = []
                    new_stored_values = []
                    # Get all docs from source index
                    all_docs = AssetService.get_all(field["source"])
                    for stored_val in stored_values:
                        # Try to find a matching document
                        found = False
                        for hit in all_docs:
                            doc = hit["_source"]
                            # Try matching against source_field first
                            if doc.get(field["source_field"]) == stored_val:
                                new_esids.append(hit["_id"])
                                # Store the current value from source_field (in case it was legacy data)
                                new_stored_values.append(doc.get(field["source_field"]))
                                found = True
                                break
                            # If source_field didn't match, try matching against common title fields
                            # This handles legacy data where friendly names were stored
                            else:
                                for title_field in [
                                    "country_title",
                                    "country_iso3_code",
                                    "ga_property_display_name",
                                    "ga_property_title",
                                    "ga_property_id",
                                    "fb_page_name",
                                    "fb_page_slug",
                                    "fb_page_id",
                                    "city_title",
                                    "region_title",
                                    "organization_title",
                                ]:
                                    if doc.get(title_field) == stored_val:
                                        new_esids.append(hit["_id"])
                                        # Store the value from source_field (convert legacy to new format)
                                        new_stored_values.append(doc.get(field["source_field"]))
                                        found = True
                                        break
                                if found:
                                    break

                        # FIX: Always preserve the item, even without an ESID match
                        # This ensures the item is displayed and can be re-selected
                        if not found:
                            new_esids.append("")  # Empty ESID placeholder
                            new_stored_values.append(stored_val)  # Keep original value

                    esids = new_esids
                    stored_values = new_stored_values

                # At this point, stored_values contains either:
                # - Fresh titles looked up from source documents (if we had ESIDs), OR
                # - Converted values from legacy data lookup
                # Now we need to determine what to display vs what to store
                titles = stored_values  # This is what gets displayed
                values = []  # This is for value_field (if applicable)

                # If value_field is specified, we may need to handle it differently
                # But since we already looked up titles above, we just use stored_values as-is
                # (stored_values was already set correctly in the lookup logic)

                # Store as delimited strings for JavaScript to parse
                # Make sure we're working with strings
                esids_list = [str(e) for e in esids] if esids else []
                titles_list = [str(t) for t in titles] if titles else []
                values_list = [str(v) for v in values] if values else []

                esids_str = "|||".join(esids_list)
                titles_str = "|||".join(titles_list)
                values_str = "|||".join(values_list)

                edit_item[f"{name}_esids"] = esids_str
                edit_item[f"{name}_titles"] = titles_str
                if value_field_name:
                    edit_item[f"{name}_values"] = values_str

                # If a display_format block is configured, also surface a
                # parallel _displays / _tooltips pair so the edit form can
                # render slug-with-tooltip chips for already-selected items
                # (the dropdown options carry the same data via data-attrs).
                display_format = field.get("display_format")
                if display_format and field.get("source"):
                    displays = []
                    tooltips = []
                    for esid in esids_list:
                        if esid:
                            try:
                                doc = AssetService.get_by_id(field["source"], esid)
                            except Exception:
                                doc = None
                        else:
                            doc = None
                        if doc:
                            d, t = FormService._format_display_value(doc, display_format)
                        else:
                            d, t = "", ""
                        displays.append(d)
                        tooltips.append(t)
                    edit_item[f"{name}_displays"] = "|||".join(displays)
                    edit_item[f"{name}_tooltips"] = "|||".join(tooltips)

            # seed a ``primary_scalar_field`` list from its legacy
            # scalar for any doc predating the scalar→list migration, so the
            # edit form shows the existing single value even before the manual
            # update_vN seed has run on this environment. Only seeds when the
            # list is missing/empty and the scalar is present; the migration
            # (and subsequent saves) take over once the list exists.
            primary_scalar = field.get("primary_scalar_field")
            if field.get("type") == "list" and primary_scalar:
                existing_list = edit_item.get(name)
                has_list = isinstance(existing_list, list) and existing_list
                scalar_val = edit_item.get(primary_scalar)
                if not has_list and isinstance(scalar_val, str) and scalar_val.strip():
                    edit_item[name] = [scalar_val.strip()]

            # Convert list fields to newline-separated string for textarea
            if field.get("type") == "list" and name in edit_item:
                if isinstance(edit_item[name], list):
                    edit_item[name] = "\n".join(edit_item[name])

            # select fields with string options ("true"/"false")
            # may have a stored value that was coerced to a real boolean
            # by the create/edit path. Stringify here so the template's
            # ``{% if item.get(name) == option %}`` comparison matches
            # ("true"/"false") instead of (True/False).
            if field.get("type") == "select" and name in edit_item:
                stored = edit_item[name]
                if isinstance(stored, bool):
                    edit_item[name] = "true" if stored else "false"

        return edit_item

    @staticmethod
    def enrich_workspace_titles(items, config):
        """resolve each item's ``workspace_esid`` to its
        ``workspace_title`` for the list view's Workspace column.

        The backfill stamps the load-bearing
        ``workspace_esid`` but never the human-readable ``workspace_title``,
        so the column rendered blank for every backfilled doc. Resolve it at
        read time (one ``de_asset_workspace`` load, cached across all items)
        rather than denormalising the title into each asset doc — the title
        stays correct even if a workspace is later renamed, and new writers
        that set only the esid don't reintroduce blanks.

        Only runs for configs that actually expose a ``workspace_title``
        select field (the data-source asset types via
        ``_SUPERUSER_WORKSPACE_FIELD``). No-ops otherwise.
        """
        has_ws_field = any(
            f.get("name") == "workspace_title" and f.get("esid_field") == "workspace_esid" for f in config.get("fields", [])
        )
        if not has_ws_field or not items:
            return items

        ws_docs = AssetService.get_all("de_asset_workspace") or []
        title_by_esid = {hit["_id"]: (hit["_source"] or {}).get("workspace_title", "") for hit in ws_docs}

        for item in items:
            # Don't clobber a title that's already present (e.g. a doc that
            # stored both). Only fill when blank and an esid resolves.
            if not item.get("workspace_title"):
                esid = item.get("workspace_esid")
                if esid and esid in title_by_esid:
                    item["workspace_title"] = title_by_esid[esid]
        return items

    @staticmethod
    def enrich_for_list_display(items, config):
        """For each multi_select field with a ``display_format`` block, attach
        a ``<field>__display_items`` list to each item — one entry per linked
        ESID with ``{display, tooltip}`` keys. Used by list.html to render
        slug-with-tooltip cells without burning an N+1 lookup per row.

        Source docs for each referenced index are loaded once and reused
        across every item in the list.
        """
        fields_with_format = [
            f for f in config["fields"] if f.get("type") == "multi_select" and f.get("display_format") and f.get("source")
        ]
        # checkbox_group fields store raw option VALUES (e.g.
        # "now-30d/d", "cal:this_quarter"). The list view would otherwise show
        # those raw tokens; map each stored value to its human label via the
        # field's own static ``options`` so the cell reads "Last 30 days, This
        # Quarter". Uses the same ``__display_items`` channel as multi_select so
        # list.html renders it through one code path.
        checkbox_group_fields = [f for f in config["fields"] if f.get("type") == "checkbox_group" and f.get("options")]

        if not (fields_with_format or checkbox_group_fields) or not items:
            return items

        for item in items:
            for field in checkbox_group_fields:
                label_by_value = {opt["value"]: opt.get("label", opt["value"]) for opt in field["options"]}
                stored = item.get(field["name"], []) or []
                if not isinstance(stored, list):
                    stored = [stored]
                display_items = [
                    {"display": label_by_value.get(v, str(v)), "tooltip": ""} for v in stored if v not in (None, "")
                ]
                item[f"{field['name']}__display_items"] = display_items

        if not fields_with_format:
            return items

        source_lookup_cache = {}

        for item in items:
            for field in fields_with_format:
                source = field["source"]
                source_field = field.get("source_field")
                if source not in source_lookup_cache:
                    docs = AssetService.get_all(source)
                    by_esid = {hit["_id"]: hit["_source"] for hit in docs}
                    by_value = {}
                    if source_field:
                        for hit in docs:
                            sf_val = hit["_source"].get(source_field)
                            if sf_val is not None and sf_val != "":
                                by_value[str(sf_val)] = hit["_source"]
                    source_lookup_cache[source] = (by_esid, by_value)
                by_esid, by_value = source_lookup_cache[source]

                esid_field = field.get("esid_field", field["name"].replace("_title", "_esid"))
                esids = item.get(esid_field, [])
                if not isinstance(esids, list):
                    esids = [esids] if esids else []
                esids = [e for e in esids if e]

                # fall back to the primary value field (e.g.
                # team_facebook_page_id) when the ESID array is empty or
                # missing. team_facebook_page_id is the importer's source of
                # truth; team_facebook_page_esids is a companion that can
                # legitimately diverge for teams configured before the ESID
                # sync was added. Rendering must show the same answer in both
                # views regardless of which field happens to be populated.
                display_items = []
                if esids:
                    iter_items = [("esid", e) for e in esids]
                else:
                    primary_values = item.get(field["name"], []) or []
                    if not isinstance(primary_values, list):
                        primary_values = [primary_values]
                    iter_items = [("value", str(v)) for v in primary_values if v not in (None, "")]

                for kind, key in iter_items:
                    src = by_esid.get(key) if kind == "esid" else by_value.get(key)
                    if src:
                        display, tooltip = FormService._format_display_value(src, field["display_format"])
                    else:
                        # Source doc missing (e.g. orphan page_id with no
                        # de_asset_fb_pages entry). Surface the raw key so
                        # the cell isn't silently empty — admins can then
                        # see which references are unresolved.
                        display, tooltip = (str(key), "")
                    if display:
                        display_items.append({"display": display, "tooltip": tooltip})
                item[f"{field['name']}__display_items"] = display_items
        return items

    @staticmethod
    def _format_display_value(source_doc, display_format):
        """Pull the display field + tooltip text from a source document.

        Returns ``(display, tooltip)`` where ``display`` is the visible label
        (e.g. ``fb_page_slug``) and ``tooltip`` is a newline-joined string of
        ``Label: value`` pairs for the configured tooltip_fields. Empty
        source values are skipped from the tooltip so the popup isn't padded
        with blank rows for missing data.
        """
        display_field = display_format.get("field")
        display = source_doc.get(display_field, "") if display_field else ""
        tooltip_lines = []
        for spec in display_format.get("tooltip_fields", []):
            value = source_doc.get(spec.get("field", ""), "")
            if value:
                tooltip_lines.append(f"{spec.get('label', '')}: {value}".strip())
        return display, "\n".join(tooltip_lines)

    @staticmethod
    def get_select_options(config, current_item=None, allowed_workspaces=None):
        """
        Get options for all select fields.

        Args:
            config: Asset configuration
            current_item: Current item data (for filtering)
            allowed_workspaces: optional list/set of workspace_esid
                strings the caller is allowed to see. ``None`` means "no
                workspace filter" (same convention as
                ``RoleFilterService.get_allowed_workspaces`` returning None
                for non-privileged users with no workspace scope). When
                provided, source docs whose ``workspace_esid`` is outside
                this set are dropped from the option list — so the picker
                dropdown for a multi-tenant user can't enumerate another
                tenant's Chatwoot accounts, FB accounts, etc.

        Returns:
            Dict mapping field names to lists of options {id, title, value, display, tooltip}.
            ``display`` and ``tooltip`` are populated only for fields with a
            ``display_format`` block (used by the multi-select chip + dropdown
            to show e.g. fb_page_slug with a hover tooltip).
        """
        options = {}
        # pre-compute the filter set once so the inner loop is
        # cheap. None propagates through as "no filter applied".
        allowed_ws_set = set(allowed_workspaces) if allowed_workspaces is not None else None

        for field in config["fields"]:
            if field.get("type") not in ["select", "multi_select"]:
                continue

            field_name = field["name"]

            # Static options
            if "options" in field:
                options[field_name] = [{"id": opt, "title": opt} for opt in field["options"]]
                # Sort static options
                options[field_name] = FormService._sort_options(options[field_name])
                continue

            # Dynamic options from Elasticsearch
            if "source" not in field:
                continue

            # Get all items from the source index
            hits = AssetService.get_all(field["source"], None)
            value_field_name = field.get("value_field")  # Optional: specific field to store
            display_format = field.get("display_format")
            # narrow tenant-owned sources fail-CLOSED; skip narrowing
            # for global reference-data sources (see _REFERENCE_DATA_SOURCES).
            narrow_this_source = allowed_ws_set is not None and field["source"] not in _REFERENCE_DATA_SOURCES
            # The workspace index keys ownership by its own ``_id``, not by a
            # ``workspace_esid`` field (see _WORKSPACE_SOURCE_INDEX). Read the
            # narrowing key from the right place for this source.
            ws_key_is_id = field["source"] == _WORKSPACE_SOURCE_INDEX
            built = []
            for hit in hits:
                src = hit["_source"]
                # drop source-index docs the caller may not
                # see. Fail-CLOSED — a doc whose workspace_esid is missing/null
                # has unknown ownership and is dropped for a workspace-scoped
                # caller, exactly like the connector picker
                # (connector_wizard_service.py) and the Asset Explorer count
                # (app.py). This previously failed OPEN (an
                # ``is not None`` guard kept unstamped docs), which contradicted
                # those two surfaces and could disclose another tenant's
                # unstamped asset in a form picker. fixed the write side
                # (wizard assets are now stamped) and added the GA4 index to the
                # backfill, so the unstamped population is small; closing this
                # reader aligns all three.
                if narrow_this_source:
                    narrow_key = hit["_id"] if ws_key_is_id else src.get("workspace_esid")
                    if narrow_key not in allowed_ws_set:
                        continue
                opt = {
                    "id": hit["_id"],
                    "title": src.get(field["source_field"], "N/A"),
                    "value": (src.get(value_field_name, "") if value_field_name else ""),
                }
                if display_format:
                    display, tooltip = FormService._format_display_value(src, display_format)
                    opt["display"] = display or opt["title"]
                    opt["tooltip"] = tooltip
                elif field["source"] == "de_asset_youtube_accounts":
                    # a fourth label surface. One Google login can
                    # front many Brand Account identities, each needing its own
                    # doc (one doc = one token = one identity), and
                    # youtube_account_title is IDENTICAL across them — so the
                    # "YouTube Account" select on the Edit Channel form listed
                    # indistinguishable options. Set only ``display``: ``title``
                    # is the value written back to youtube_account_title on save,
                    # so suffixing it would corrupt the stored field.
                    try:
                        from web.services.connector_wizard_service import _youtube_picker_label
                    except ImportError:
                        from .connector_wizard_service import _youtube_picker_label
                    labelled = _youtube_picker_label(opt["title"], src)
                    if labelled != opt["title"]:
                        opt["display"] = labelled
                built.append(opt)
            options[field_name] = FormService._sort_options(built)

        return options

    @staticmethod
    def _sort_options(options_list):
        """
        Sort dropdown options alphabetically/numerically while keeping special values at top.

        Special values like "None", "-- Select --", "-- Select to add --" stay at the beginning.
        Numbers are sorted numerically (smallest to largest).
        Text is sorted alphabetically (case-insensitive).

        Sorting uses the option's ``display`` label when present (the friendly
        value shown in the dropdown for display_format fields), falling back to
        ``title`` otherwise. See.

        Args:
            options_list: List of dicts with 'id' and 'title' keys

        Returns:
            Sorted list of options
        """
        # Define special values that should stay at the top (in this order)
        special_values = ["None", "-- Select --", "-- Select to add --"]

        # Separate special and regular options
        special_options = []
        regular_options = []

        for opt in options_list:
            title = opt.get("title", "")
            if title in special_values:
                special_options.append(opt)
            else:
                regular_options.append(opt)

        # Sort special options by their position in special_values list
        special_options.sort(key=lambda x: (special_values.index(x["title"]) if x["title"] in special_values else 999))

        # Sort regular options
        def sort_key(opt):
            # sort by the label the user actually reads. For
            # display_format fields (Countries, FB Pages, GA4 Properties,
            # IG Accounts, YouTube Channels) ``title`` is the raw
            # source_field value (an ISO code / id / handle) while
            # ``display`` is the friendly label shown in the dropdown —
            # sorting by ``title`` there ordered Countries by ISO code
            # (Germany before Denmark). Fields without a display_format
            # have no ``display`` key, so this falls back to ``title`` and
            # their ordering is unchanged.
            label = opt.get("display") or opt.get("title", "")
            # Try to parse as number
            try:
                # Handle numeric strings
                return (0, float(label))  # 0 = numeric, sorts first
            except (ValueError, TypeError):
                # Not a number, sort alphabetically (case-insensitive).
                # strip diacritics first so accented leading
                # letters fold to their base letter — otherwise "Åland
                # Islands" (Å = U+00C5) sorts after "Zimbabwe" because its
                # code point is above ASCII z. Same NFKD fold the
                # update_v16 country migration uses for title matching.
                decomposed = unicodedata.normalize("NFKD", str(label).lower())
                folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
                return (1, folded)  # 1 = text, sorts after numbers

        regular_options.sort(key=sort_key)

        # Combine: special options first, then regular options
        return special_options + regular_options

    @staticmethod
    def get_computed_display_fields(item, config, item_id=None):
        """
        Generate computed display fields (like OAuth URLs).

        Args:
            item: Document data
            config: Asset configuration
            item_id: Document _id, exposed to templates as {esid} so configs can
                build URLs that reference the asset's own primary key.

        Returns:
            Dict of computed field values
        """
        computed = {}

        if "computed_display" not in config:
            return computed

        from ..config import config as app_config

        for field_name, field_config in config["computed_display"].items():
            template = field_config.get("template", "")

            template_vars = dict(item)
            template_vars["fboauth_hostname"] = app_config.FBOAUTH_HOSTNAME or "localhost"
            if item_id is not None:
                template_vars["esid"] = item_id

            entry = {
                "label": field_config.get("label", field_name),
                "help_text": field_config.get("help_text", ""),
            }
            try:
                entry["value"] = template.format(**template_vars)
            except KeyError:
                entry["value"] = "N/A"
            computed[field_name] = entry

        return computed
