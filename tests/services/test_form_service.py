"""
Tests for the Form Service.

Covers:
  validate_required_fields — required fields, conditional, edit-only, encrypted in edit
  generate_random_string   — length, character set
  generate_unique_value    — retries until unique
  process_form_data        — encrypted, ESID select, multi-select, list, auto-generate
  prepare_for_display      — masks encrypted, filters ESID fields
  prepare_for_edit         — list to newline, multi-select with ESID lookup
  get_select_options       — static, dynamic from ES, sorting
  get_computed_display_fields — template rendering, missing variable
"""

from unittest import mock

import pytest

from asset_management.web.services.form_service import FormService


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _simple_config(fields, **extras):
    """Build a minimal asset config with the given field list."""
    cfg = {"index": "test_index", "fields": fields}
    cfg.update(extras)
    return cfg


# ---------------------------------------------------------------------------
# validate_required_fields
# ---------------------------------------------------------------------------

class TestValidateRequiredFields:

    def test_all_present(self):
        config = _simple_config([
            {"name": "title", "label": "Title", "required": True},
        ])
        valid, err = FormService.validate_required_fields({"title": "Hello"}, config)
        assert valid is True
        assert err is None

    def test_missing_required(self):
        config = _simple_config([
            {"name": "title", "label": "Title", "required": True},
        ])
        valid, err = FormService.validate_required_fields({"title": ""}, config)
        assert valid is False
        assert "Title is required" in err

    def test_multiple_missing(self):
        config = _simple_config([
            {"name": "a", "label": "A", "required": True},
            {"name": "b", "label": "B", "required": True},
        ])
        valid, err = FormService.validate_required_fields({}, config)
        assert valid is False
        assert "A is required" in err
        assert "B is required" in err

    def test_default_max_length(self):
        # text fields cap at the type default (255) with no explicit limit.
        config = _simple_config([{"name": "t", "label": "T", "type": "text"}])
        valid, err = FormService.validate_required_fields({"t": "x" * 300}, config)
        assert valid is False
        assert "T must be 255 characters or fewer" in err

    def test_name_no_pattern_accepts_unicode(self):
        # plain text fields carry no pattern -> accents/apostrophes pass.
        config = _simple_config([{"name": "t", "label": "T", "type": "text"}])
        valid, err = FormService.validate_required_fields({"t": "Niño O'Brien & Co"}, config)
        assert valid is True and err is None

    def test_list_validated_per_item(self):
        # list pattern is checked per entry, not against the joined blob.
        config = _simple_config([
            {"name": "urls", "label": "URLs", "type": "list", "pattern": r"https?://.+"},
        ])
        valid, err = FormService.validate_required_fields({"urls": "https://ok.org\nbad"}, config)
        assert valid is False
        assert "URLs format is invalid" in err

    def test_skips_section_divider(self):
        config = _simple_config([
            {"name": "divider", "label": "Divider", "type": "section_divider", "required": True},
        ])
        valid, err = FormService.validate_required_fields({}, config)
        assert valid is True

    def test_skips_data_source_checkbox(self):
        config = _simple_config([
            {"name": "ds", "label": "DS", "type": "data_source_checkbox", "required": True},
        ])
        valid, err = FormService.validate_required_fields({}, config)
        assert valid is True

    def test_skips_edit_only_in_add_mode(self):
        config = _simple_config([
            {"name": "token", "label": "Token", "required": True, "edit_only": True},
        ])
        valid, err = FormService.validate_required_fields({}, config, mode="add")
        assert valid is True

    def test_checks_edit_only_in_edit_mode(self):
        config = _simple_config([
            {"name": "token", "label": "Token", "required": True, "edit_only": True},
        ])
        valid, err = FormService.validate_required_fields({}, config, mode="edit")
        assert valid is False

    def test_skips_conditional_when_condition_not_met(self):
        config = _simple_config([
            {"name": "extra", "label": "Extra", "required": True,
             "condition": {"field": "type", "value": "premium"}},
        ])
        valid, err = FormService.validate_required_fields({"type": "basic"}, config)
        assert valid is True

    def test_checks_conditional_when_condition_met(self):
        config = _simple_config([
            {"name": "extra", "label": "Extra", "required": True,
             "condition": {"field": "type", "value": "premium"}},
        ])
        valid, err = FormService.validate_required_fields({"type": "premium", "extra": ""}, config)
        assert valid is False

    def test_skips_encrypted_required_in_edit_mode(self):
        config = _simple_config([
            {"name": "secret", "label": "Secret", "required": True, "encrypted": True},
        ])
        valid, err = FormService.validate_required_fields({"secret": ""}, config, mode="edit")
        assert valid is True


# ---------------------------------------------------------------------------
# generate_random_string
# ---------------------------------------------------------------------------

class TestGenerateRandomString:

    def test_correct_length(self):
        result = FormService.generate_random_string(20)
        assert len(result) == 20

    def test_alphanumeric(self):
        result = FormService.generate_random_string(100)
        assert result.isalnum()

    def test_zero_length(self):
        result = FormService.generate_random_string(0)
        assert result == ""


# ---------------------------------------------------------------------------
# generate_unique_value
# ---------------------------------------------------------------------------

class TestGenerateUniqueValue:

    @mock.patch("asset_management.web.services.form_service.AssetService")
    def test_returns_on_first_unique(self, mock_asset_svc):
        mock_asset_svc.check_unique_value.return_value = True
        result = FormService.generate_unique_value("idx", "field", 10)
        assert len(result) == 10
        mock_asset_svc.check_unique_value.assert_called_once()

    @mock.patch("asset_management.web.services.form_service.AssetService")
    def test_retries_when_not_unique(self, mock_asset_svc):
        mock_asset_svc.check_unique_value.side_effect = [False, False, True]
        result = FormService.generate_unique_value("idx", "field", 8)
        assert len(result) == 8
        assert mock_asset_svc.check_unique_value.call_count == 3


# ---------------------------------------------------------------------------
# process_form_data
# ---------------------------------------------------------------------------

class TestProcessFormData:

    @mock.patch("asset_management.web.services.form_service.encrypt", return_value="ENC_VALUE")
    def test_encrypts_new_value(self, mock_enc):
        config = _simple_config([
            {"name": "secret", "label": "Secret", "encrypted": True},
        ])
        result = FormService.process_form_data({"secret": "plaintext"}, config)
        mock_enc.assert_called_once_with("plaintext")
        assert result["secret"] == "ENC_VALUE"

    @mock.patch("asset_management.web.services.form_service.encrypt")
    def test_keeps_existing_encrypted_on_edit_unchanged(self, mock_enc):
        config = _simple_config([
            {"name": "secret", "label": "Secret", "encrypted": True},
        ])
        existing = {"secret": "EXISTING_ENC"}
        result = FormService.process_form_data(
            {"secret": "EXISTING_ENC"}, config, existing_data=existing, mode="edit"
        )
        mock_enc.assert_not_called()
        assert result["secret"] == "EXISTING_ENC"

    @mock.patch("asset_management.web.services.form_service.encrypt")
    def test_keeps_existing_encrypted_when_blank_on_edit(self, mock_enc):
        config = _simple_config([
            {"name": "secret", "label": "Secret", "encrypted": True},
        ])
        existing = {"secret": "EXISTING_ENC"}
        result = FormService.process_form_data(
            {"secret": ""}, config, existing_data=existing, mode="edit"
        )
        assert result["secret"] == "EXISTING_ENC"

    def test_regular_field(self):
        config = _simple_config([
            {"name": "title", "label": "Title"},
        ])
        result = FormService.process_form_data({"title": "Hello"}, config)
        assert result["title"] == "Hello"

    def test_optional_empty_field_add_mode(self):
        config = _simple_config([
            {"name": "note", "label": "Note"},
        ])
        result = FormService.process_form_data({"note": ""}, config, mode="add")
        assert "note" not in result

    def test_optional_empty_field_edit_mode_marks_delete(self):
        config = _simple_config([
            {"name": "note", "label": "Note"},
        ])
        result = FormService.process_form_data({"note": ""}, config, mode="edit")
        assert result.get("__delete__note") is True

    def test_preserve_on_empty_edit(self):
        config = _simple_config([
            {"name": "note", "label": "Note", "preserve_on_empty": True},
        ])
        result = FormService.process_form_data({"note": ""}, config, mode="edit")
        assert "__delete__note" not in result

    def test_list_field(self):
        config = _simple_config([
            {"name": "tags", "label": "Tags", "type": "list"},
        ])
        result = FormService.process_form_data({"tags": "a\nb\nc"}, config)
        assert result["tags"] == ["a", "b", "c"]

    def test_list_field_empty_edit(self):
        config = _simple_config([
            {"name": "tags", "label": "Tags", "type": "list"},
        ])
        result = FormService.process_form_data({"tags": ""}, config, mode="edit")
        assert result.get("__delete__tags") is True

    def test_list_field_primary_scalar_sync(self):
        # a list field with primary_scalar_field keeps a legacy scalar
        # in sync with the first entry (team_urls -> team_url for the importers).
        config = _simple_config([
            {"name": "team_urls", "label": "URLs", "type": "list",
             "primary_scalar_field": "team_url"},
        ])
        result = FormService.process_form_data({"team_urls": "https://a.org\nhttps://b.org"}, config)
        assert result["team_urls"] == ["https://a.org", "https://b.org"]
        assert result["team_url"] == "https://a.org"

    def test_list_field_primary_scalar_empty_edit_deletes_both(self):
        # clearing the list also marks the derived scalar for deletion.
        config = _simple_config([
            {"name": "team_urls", "label": "URLs", "type": "list",
             "primary_scalar_field": "team_url"},
        ])
        result = FormService.process_form_data({"team_urls": ""}, config, mode="edit")
        assert result.get("__delete__team_urls") is True
        assert result.get("__delete__team_url") is True

    # checkbox_group — static-option multi-checkbox stored as a
    # keyword list. Values are read via getlist on a MultiDict and filtered to
    # the field's declared options.
    def _preset_config(self):
        return _simple_config([
            {"name": "team_time_presets", "label": "Presets", "type": "checkbox_group",
             "options": [
                 {"value": "now-30d/d", "label": "Last 30 days"},
                 {"value": "now-90d/d", "label": "Last 90 days"},
                 {"value": "cal:this_month", "label": "This Month"},
             ]},
        ])

    def test_checkbox_group_collects_checked_multidict(self):
        from werkzeug.datastructures import MultiDict
        form = MultiDict([
            ("team_time_presets", "now-30d/d"),
            ("team_time_presets", "cal:this_month"),
        ])
        result = FormService.process_form_data(form, self._preset_config())
        assert result["team_time_presets"] == ["now-30d/d", "cal:this_month"]

    def test_checkbox_group_filters_unknown_values(self):
        from werkzeug.datastructures import MultiDict
        form = MultiDict([
            ("team_time_presets", "now-90d/d"),
            ("team_time_presets", "bogus"),  # not a declared option
        ])
        result = FormService.process_form_data(form, self._preset_config())
        assert result["team_time_presets"] == ["now-90d/d"]

    def test_checkbox_group_empty_add_mode_omitted(self):
        from werkzeug.datastructures import MultiDict
        result = FormService.process_form_data(MultiDict(), self._preset_config(), mode="add")
        assert "team_time_presets" not in result

    def test_checkbox_group_empty_edit_marks_delete(self):
        # Unticking every box on edit clears the field (reverts the team to the
        # default-list fallback).
        from werkzeug.datastructures import MultiDict
        result = FormService.process_form_data(MultiDict(), self._preset_config(), mode="edit")
        assert result.get("__delete__team_time_presets") is True

    def test_checkbox_group_list_display_maps_values_to_labels(self):
        # the list view must show friendly labels, not raw stored
        # values (now-30d/d -> "Last 30 days"). enrich_for_list_display attaches
        # a __display_items list mapped via the field's static options.
        items = [{"team_time_presets": ["now-30d/d", "cal:this_month"]}]
        enriched = FormService.enrich_for_list_display(items, self._preset_config())
        di = enriched[0]["team_time_presets__display_items"]
        assert [d["display"] for d in di] == ["Last 30 days", "This Month"]

    def test_checkbox_group_list_display_empty_when_unset(self):
        items = [{}]  # a team with no preset config
        enriched = FormService.enrich_for_list_display(items, self._preset_config())
        assert enriched[0]["team_time_presets__display_items"] == []

    @mock.patch("asset_management.web.services.form_service.AssetService")
    def test_select_store_esid(self, mock_asset_svc):
        mock_asset_svc.get_by_id.return_value = {"team_title": "Alpha Team"}
        config = _simple_config([
            {"name": "team_title", "label": "Team", "store_esid": True,
             "source": "de_asset_teams", "source_field": "team_title"},
        ])
        result = FormService.process_form_data({"team_title": "doc123"}, config)
        assert result["team_esid"] == "doc123"
        assert result["team_title"] == "Alpha Team"

    def test_skips_section_divider(self):
        config = _simple_config([
            {"name": "div", "label": "Div", "type": "section_divider"},
        ])
        result = FormService.process_form_data({}, config)
        assert "div" not in result

    def test_skips_logo(self):
        config = _simple_config([
            {"name": "logo", "label": "Logo", "type": "logo"},
        ])
        result = FormService.process_form_data({}, config)
        assert "logo" not in result

    @mock.patch("asset_management.web.services.form_service.AssetService")
    def test_auto_generate_unique(self, mock_asset_svc):
        mock_asset_svc.check_unique_value.return_value = True
        config = _simple_config(
            [],
            auto_generate={"api_key": {"length": 12, "unique": True}},
        )
        result = FormService.process_form_data({}, config, mode="add")
        assert "api_key" in result
        assert len(result["api_key"]) == 12

    def test_auto_generate_not_in_edit(self):
        config = _simple_config(
            [],
            auto_generate={"api_key": {"length": 12}},
        )
        result = FormService.process_form_data({}, config, mode="edit")
        assert "api_key" not in result


# ---------------------------------------------------------------------------
# prepare_for_display
# ---------------------------------------------------------------------------

class TestPrepareForDisplay:

    def test_masks_encrypted(self):
        config = _simple_config([
            {"name": "secret", "label": "Secret", "encrypted": True},
        ])
        item = {"secret": "ENC_VALUE", "title": "Test"}
        result = FormService.prepare_for_display(item, config)
        assert result["secret"] == "********"
        assert result["title"] == "Test"

    def test_filters_esid_fields(self):
        config = _simple_config([{"name": "title", "label": "Title"}])
        item = {"title": "Test", "team_esid": "abc", "country_esids": ["x"]}
        result = FormService.prepare_for_display(item, config)
        assert "team_esid" not in result
        assert "country_esids" not in result
        assert result["title"] == "Test"

    def test_handles_none_item(self):
        config = _simple_config([])
        result = FormService.prepare_for_display(None, config)
        assert result == {}


# ---------------------------------------------------------------------------
# prepare_for_edit
# ---------------------------------------------------------------------------

class TestPrepareForEdit:

    def test_list_to_newline_string(self):
        config = _simple_config([
            {"name": "tags", "label": "Tags", "type": "list"},
        ])
        item = {"tags": ["a", "b", "c"]}
        result = FormService.prepare_for_edit(item, config)
        assert result["tags"] == "a\nb\nc"

    def test_handles_none_item(self):
        config = _simple_config([])
        result = FormService.prepare_for_edit(None, config)
        assert result == {}

    def test_primary_scalar_seeds_list_for_legacy_item(self):
        # a team predating the migration has only the scalar team_url;
        # the edit form seeds team_urls from it so the Add-button widget is filled.
        config = _simple_config([
            {"name": "team_urls", "label": "URLs", "type": "list",
             "primary_scalar_field": "team_url"},
        ])
        result = FormService.prepare_for_edit({"team_url": "https://legacy.org"}, config)
        assert result["team_urls"] == "https://legacy.org"

    def test_primary_scalar_does_not_overwrite_existing_list(self):
        # when the list already exists it wins over the legacy scalar.
        config = _simple_config([
            {"name": "team_urls", "label": "URLs", "type": "list",
             "primary_scalar_field": "team_url"},
        ])
        item = {"team_url": "https://old.org", "team_urls": ["https://a.org", "https://b.org"]}
        result = FormService.prepare_for_edit(item, config)
        assert result["team_urls"] == "https://a.org\nhttps://b.org"

    @mock.patch("asset_management.web.services.form_service.AssetService")
    def test_multi_select_with_esids(self, mock_asset_svc):
        mock_asset_svc.get_by_id.return_value = {"country_title": "France"}
        config = _simple_config([
            {"name": "country_title", "label": "Countries", "type": "multi_select",
             "store_esid": True, "source": "de_asset_countries",
             "source_field": "country_title"},
        ])
        item = {"country_title": ["France"], "country_esid": ["esid1"]}
        result = FormService.prepare_for_edit(item, config)
        assert result["country_title_esids"] == "esid1"
        assert result["country_title_titles"] == "France"


# ---------------------------------------------------------------------------
# get_select_options
# ---------------------------------------------------------------------------

class TestGetSelectOptions:

    def test_static_options(self):
        config = _simple_config([
            {"name": "status", "label": "Status", "type": "select",
             "options": ["Active", "Inactive"]},
        ])
        opts = FormService.get_select_options(config)
        assert len(opts["status"]) == 2
        assert opts["status"][0]["id"] == "Active"

    @mock.patch("asset_management.web.services.form_service.AssetService")
    def test_dynamic_options(self, mock_asset_svc):
        mock_asset_svc.get_all.return_value = [
            {"_id": "id1", "_source": {"team_title": "Alpha"}},
            {"_id": "id2", "_source": {"team_title": "Beta"}},
        ]
        config = _simple_config([
            {"name": "team", "label": "Team", "type": "select",
             "source": "de_asset_teams", "source_field": "team_title"},
        ])
        opts = FormService.get_select_options(config)
        assert len(opts["team"]) == 2
        assert opts["team"][0]["title"] == "Alpha"

    def test_skips_non_select(self):
        config = _simple_config([
            {"name": "title", "label": "Title", "type": "text"},
        ])
        opts = FormService.get_select_options(config)
        assert opts == {}

    def test_sort_numbers_before_text(self):
        options_list = [
            {"id": "b", "title": "Banana"},
            {"id": "2", "title": "2"},
            {"id": "1", "title": "1"},
            {"id": "a", "title": "Apple"},
        ]
        sorted_opts = FormService._sort_options(options_list)
        assert sorted_opts[0]["title"] == "1"
        assert sorted_opts[1]["title"] == "2"
        assert sorted_opts[2]["title"] == "Apple"
        assert sorted_opts[3]["title"] == "Banana"

    def test_sort_special_values_first(self):
        options_list = [
            {"id": "a", "title": "Alpha"},
            {"id": "none", "title": "None"},
        ]
        sorted_opts = FormService._sort_options(options_list)
        assert sorted_opts[0]["title"] == "None"

    def test_sort_by_display_label_when_present(self):
        # display_format options carry a friendly ``display`` label
        # that differs from ``title`` (the raw source_field — an ISO code
        # here). The list must order by what the user reads (display), not
        # by the code, so "Denmark" (DK) precedes "Germany" (DE).
        options_list = [
            {"id": "DE", "title": "DE", "display": "Germany"},
            {"id": "DK", "title": "DK", "display": "Denmark"},
            {"id": "AF", "title": "AF", "display": "Afghanistan"},
        ]
        sorted_opts = FormService._sort_options(options_list)
        assert [o["display"] for o in sorted_opts] == ["Afghanistan", "Denmark", "Germany"]

    def test_sort_folds_leading_diacritics(self):
        # an accented leading letter (Å, U+00C5) must fold to its
        # base letter for sorting, otherwise "Åland Islands" sorts after
        # "Zimbabwe" because its code point is above ASCII z.
        options_list = [
            {"id": "ZW", "title": "ZW", "display": "Zimbabwe"},
            {"id": "AX", "title": "AX", "display": "Åland Islands"},
            {"id": "AL", "title": "AL", "display": "Albania"},
        ]
        sorted_opts = FormService._sort_options(options_list)
        assert [o["display"] for o in sorted_opts] == ["Åland Islands", "Albania", "Zimbabwe"]


# ---------------------------------------------------------------------------
# get_computed_display_fields
# ---------------------------------------------------------------------------

class TestGetComputedDisplayFields:

    def test_no_computed(self):
        config = _simple_config([])
        result = FormService.get_computed_display_fields({}, config)
        assert result == {}

    @mock.patch("asset_management.web.services.form_service.FormService.__module__", "asset_management.web.services.form_service")
    def test_template_rendering(self):
        config = _simple_config([], computed_display={
            "oauth_url": {
                "label": "OAuth URL",
                "template": "https://{fboauth_hostname}/auth/{app_id}",
            }
        })
        with mock.patch("asset_management.web.config.config") as mock_cfg:
            mock_cfg.FBOAUTH_HOSTNAME = "oauth.example.com"
            item = {"app_id": "12345"}
            result = FormService.get_computed_display_fields(item, config)
        assert result["oauth_url"]["value"] == "https://oauth.example.com/auth/12345"
        assert result["oauth_url"]["label"] == "OAuth URL"

    def test_missing_template_variable(self):
        config = _simple_config([], computed_display={
            "oauth_url": {
                "label": "OAuth URL",
                "template": "https://{fboauth_hostname}/auth/{missing_field}",
            }
        })
        with mock.patch("asset_management.web.config.config") as mock_cfg:
            mock_cfg.FBOAUTH_HOSTNAME = "oauth.example.com"
            result = FormService.get_computed_display_fields({}, config)
        assert result["oauth_url"]["value"] == "N/A"

    def test_should_expose_item_id_as_esid_template_var_when_provided(self):
        config = _simple_config([], computed_display={
            "redirect_uri": {
                "label": "OAuth Redirect URI",
                "template": "https://{fboauth_hostname}/{esid}/auth_callback",
            }
        })
        with mock.patch("asset_management.web.config.config") as mock_cfg:
            mock_cfg.FBOAUTH_HOSTNAME = "fb.example.com"
            result = FormService.get_computed_display_fields({}, config, item_id="abc123")
        assert result["redirect_uri"]["value"] == "https://fb.example.com/abc123/auth_callback"

    def test_should_return_na_when_template_uses_esid_but_item_id_is_missing(self):
        config = _simple_config([], computed_display={
            "redirect_uri": {
                "label": "OAuth Redirect URI",
                "template": "https://{fboauth_hostname}/{esid}/auth_callback",
            }
        })
        with mock.patch("asset_management.web.config.config") as mock_cfg:
            mock_cfg.FBOAUTH_HOSTNAME = "fb.example.com"
            result = FormService.get_computed_display_fields({}, config)
        assert result["redirect_uri"]["value"] == "N/A"

    def test_should_pass_help_text_through_to_rendered_field(self):
        config = _simple_config([], computed_display={
            "redirect_uri": {
                "label": "OAuth Redirect URI",
                "template": "https://{fboauth_hostname}/x/auth_callback",
                "help_text": "Register this in Meta App Dashboard",
            }
        })
        with mock.patch("asset_management.web.config.config") as mock_cfg:
            mock_cfg.FBOAUTH_HOSTNAME = "fb.example.com"
            result = FormService.get_computed_display_fields({}, config)
        assert result["redirect_uri"]["help_text"] == "Register this in Meta App Dashboard"

    def test_should_default_help_text_to_empty_string_when_not_specified(self):
        config = _simple_config([], computed_display={
            "oauth_url": {
                "label": "OAuth URL",
                "template": "https://{fboauth_hostname}/x",
            }
        })
        with mock.patch("asset_management.web.config.config") as mock_cfg:
            mock_cfg.FBOAUTH_HOSTNAME = "fb.example.com"
            result = FormService.get_computed_display_fields({}, config)
        assert result["oauth_url"]["help_text"] == ""
