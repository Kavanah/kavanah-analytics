"""
Web Asset Manager - Flask Application
A web-based interface for managing Digital Engagement Dashboard assets.
"""

import sys
import os
import re
import uuid
import secrets
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# the elasticsearch client logs EVERY request at INFO
# ("GET https://...:9200/... [status:200 duration:0.001s]"). Measured on prod
# 2026-08-06 that was 82.5% of a ~23 MB/day error log, while the AUDIT: lines we
# actually investigate with were 0.1% of it — during the open-signup
# investigation the signup evidence was buried under this chatter. WARNING keeps
# every genuine ES problem (connection failures, retries, timeouts) and drops
# only the per-call success noise. a teammate confirmed he reads errors, not info.
# NOTE the logger name: elasticsearch-py 9.x emits the per-request line from
# **elastic_transport.transport**, NOT "elasticsearch". Damping "elasticsearch"
# alone changes nothing — verified live on staging 2026-08-07, where the noise
# continued unchanged after that one-liner. Setting the elastic_transport
# PARENT covers .transport and any sibling the library adds later. Both names
# are damped so this keeps working across a major-version change in either
# direction.
logging.getLogger("elastic_transport").setLevel(logging.WARNING)
logging.getLogger("elasticsearch").setLevel(logging.WARNING)

# Add bin/python to the import path so `from lib.*` and
# `from asset_management.*` resolve at module load time.
# (The previous 3-dirname computation pointed at /app, which contains no
#  Python packages — harmless while only deferred lib imports existed, but
# added a top-level `from lib.log_filters` that broke the load.)
_current_dir = os.path.dirname(os.path.abspath(__file__))
_python_dir = os.path.dirname(os.path.dirname(_current_dir))
sys.path.insert(0, _python_dir)

# Phase 3: scrub CR/LF/NUL from every log message before it reaches a
# handler. Mitigates py/log-injection across the codebase via a single
# centralized filter rather than per-call-site %s formatting churn.
from lib.email_format import is_plausible_email  # noqa: E402
from lib.errors import InvalidRequestError, RequestedItemNotFoundError  # noqa: E402
from lib.log_filters import install_filter_on_all_handlers  # noqa: E402

install_filter_on_all_handlers()

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    abort,
    make_response,
    session,
    send_file,
    g,
)
from werkzeug.middleware.proxy_fix import ProxyFix
import threading
import time
import calendar
from urllib.parse import quote

# Handle both direct execution and module import
if __name__ == "__main__" or __package__ is None or __package__ == "":
    # Direct execution or gunicorn loading - use absolute imports
    sys.path.insert(0, os.path.dirname(_current_dir))
    from web.config import config
    from web.secret_validation import (
        InvalidSecretError,
        is_test_mode,
        validate_secret_key,
        validate_test_auth_secret_from_environ,
    )
    from web.asset_configs import ASSET_CONFIGS, ASSET_CATEGORIES
    from web.services.asset_service import AssetService
    from web.services.form_service import FormService
    from web.services.log_service import LogService
    from web.services.import_service import ImportService
    from web.services.asset_import_service import AssetImportService
    from web.services.kibana_service import KibanaService, kibana_tls_verify
    from web.services.dashboard_service import DashboardService
    from web.services.role_filter_service import RoleFilterService
    from web.services.index_pattern_service import validate_index_access
    from web.services.mfa_service import MFAService
    from web.services.mfa_recovery_token_service import MfaRecoveryTokenService
    from web.services.mfa_recovery_email_service import MfaRecoveryEmailService
    from web.services.backup_service import BackupService
    from web.services.role_management_service import RoleManagementService
    from web.services.user_provisioning_service import UserProvisioningService
    from web.services.audit_log_service import AuditLogService
    from web.services.login_lockout_service import LoginLockoutService
    from web.services.kibana_gate_service import KibanaGateService
    from web.services.presence_service import PresenceService
    from web.services.registration_service import RegistrationService
    from web.services.user_management_service import UserManagementService
    from web.services.user_profile_service import UserProfileService
    from web.services.feedback_service import FeedbackService
    from web.services.maintenance_alert_service import MaintenanceAlertService
    from web.services.role_permissions_service import (
        ROLE_DISPLAY_NAMES,
        RolePermissionsService,
        role_display_name,
    )
    from web.services.orphan_sweep_service import OrphanSweepService
    from web.services.wizard_session_service import WizardSessionService
    from web.services.feature_flag_service import FeatureFlagService
    from web.services.notify_list_service import NotifyListService
    from web.services.connection_service import ConnectionService
    from web.services.report_schedule_service import ReportScheduleService
    from web.services.org_billing_service import OrgBillingService
    from web.services.permissions_index_service import PermissionsIndexService
    from web.services.permissions_seeder_service import PermissionsSeederService
    from web.services.user_role_assignment_service import UserRoleAssignmentService
    from web.services.data_quality_service import get_data_quality_report
    from web.dashboards_config import (
        DASHBOARD_CONFIGS,
        get_dashboard_url,
        get_enabled_dashboards,
    )
    from web import auth as auth_module
    from web.auth import init_auth, verify_auth, get_current_user, check_csrf_for_state_change
    from web.blueprints.dashboard_builder import dashboard_bp
    from web.blueprints.data_connectors import data_connectors_bp
    from web.blueprints.data_explorer import data_explorer_bp
    from web.blueprints.restore_backup import restore_backup_bp
    from web.blueprints.analytics import analytics_bp
    from web.blueprints.public_report import public_report_bp
    from web.blueprints.internal_auth import internal_auth_bp
    from web.blueprints.connections import connections_bp
    from web.blueprints.report_scheduler import report_scheduler_bp
    from web.blueprints.field_catalog import field_catalog_bp
    from web.blueprints.signup_links import signup_links_bp
    from web.blueprints.publish_global_church import publish_global_church_bp
else:
    # Module import - use relative imports
    from .config import config
    from .secret_validation import (
        InvalidSecretError,
        is_test_mode,
        validate_secret_key,
        validate_test_auth_secret_from_environ,
    )
    from .asset_configs import ASSET_CONFIGS, ASSET_CATEGORIES
    from .services.asset_service import AssetService
    from .services.form_service import FormService
    from .services.log_service import LogService
    from .services.import_service import ImportService
    from .services.asset_import_service import AssetImportService
    from .services.kibana_service import KibanaService, kibana_tls_verify
    from .services.dashboard_service import DashboardService
    from .services.role_filter_service import RoleFilterService
    from .services.index_pattern_service import validate_index_access
    from .services.mfa_service import MFAService
    from .services.mfa_recovery_token_service import MfaRecoveryTokenService
    from .services.mfa_recovery_email_service import MfaRecoveryEmailService
    from .services.backup_service import BackupService
    from .services.role_management_service import RoleManagementService
    from .services.user_provisioning_service import UserProvisioningService
    from .services.audit_log_service import AuditLogService
    from .services.login_lockout_service import LoginLockoutService
    from .services.kibana_gate_service import KibanaGateService
    from .services.presence_service import PresenceService
    from .services.registration_service import RegistrationService
    from .services.user_management_service import UserManagementService
    from .services.user_profile_service import UserProfileService
    from .services.feedback_service import FeedbackService
    from .services.maintenance_alert_service import MaintenanceAlertService
    from .services.role_permissions_service import (
        ROLE_DISPLAY_NAMES,
        RolePermissionsService,
        role_display_name,
    )
    from .services.orphan_sweep_service import OrphanSweepService
    from .services.wizard_session_service import WizardSessionService
    from .services.feature_flag_service import FeatureFlagService
    from .services.notify_list_service import NotifyListService
    from .services.connection_service import ConnectionService
    from .services.report_schedule_service import ReportScheduleService
    from .services.org_billing_service import OrgBillingService
    from .services.permissions_index_service import PermissionsIndexService
    from .services.permissions_seeder_service import PermissionsSeederService
    from .services.user_role_assignment_service import UserRoleAssignmentService
    from .services.data_quality_service import get_data_quality_report
    from .dashboards_config import (
        DASHBOARD_CONFIGS,
        get_dashboard_url,
        get_enabled_dashboards,
    )
    from . import auth as auth_module
    from .auth import init_auth, verify_auth, get_current_user, check_csrf_for_state_change
    from .blueprints.dashboard_builder import dashboard_bp
    from .blueprints.data_connectors import data_connectors_bp
    from .blueprints.data_explorer import data_explorer_bp
    from .blueprints.restore_backup import restore_backup_bp
    from .blueprints.analytics import analytics_bp
    from .blueprints.public_report import public_report_bp
    from .blueprints.internal_auth import internal_auth_bp
    from .blueprints.connections import connections_bp
    from .blueprints.report_scheduler import report_scheduler_bp
    from .blueprints.field_catalog import field_catalog_bp
    from .blueprints.signup_links import signup_links_bp
    from .blueprints.publish_global_church import publish_global_church_bp

# Initialize Flask app
app = Flask(__name__, static_url_path="/static", static_folder="static")
app.secret_key = config.SECRET_KEY

# refuse to start with a missing, sentinel, or short
# SECRET_KEY in production. The 2026-04-23 prod incident ran with the
# literal string "null" because every previous check was a logging warning
# only. Tests bypass this gate (pytest sets a known-good test key in
# conftest, but is_test_mode() guards against future test-key changes).
if not is_test_mode():
    try:
        validate_secret_key(app.secret_key)
    except InvalidSecretError as exc:
        if app.debug:
            logging.warning("SECURITY: %s", exc)
        else:
            raise RuntimeError(str(exc)) from exc

    # refuse to start when the X-Test-Persona header-auth secret is
    # live on anything that identifies as production (DEPLOY_ENV=prod or an
    # APP_PUBLIC_HOSTNAME on the prod list). The path skips MFA by design and
    # is meant to be safe by ABSENCE of the secret; this makes that promise
    # enforced rather than assumed. Refuse-only — a marker can never enable
    # the path — and no debug downgrade: a backdoor secret on a prod
    # host is wrong in every mode.
    try:
        validate_test_auth_secret_from_environ()
    except InvalidSecretError as exc:
        raise RuntimeError(str(exc)) from exc

# Initialize authentication system
init_auth(app)

# Register blueprints
app.register_blueprint(dashboard_bp, url_prefix="/dashboard-builder")
app.register_blueprint(data_connectors_bp, url_prefix="/data-connectors")
app.register_blueprint(data_explorer_bp, url_prefix="/data-explorer")
# per-connection detail / manage page reachable from the
# Data Connectors card's "Manage connection" button. Replaces the
# legacy "Manage OAuth tokens" link that routed to the empty
# /assets/facebook_app_users list view for wizard-cohort users.
app.register_blueprint(connections_bp)
# Report Scheduler UI (Schedules list; create/edit + history
# in later phases). Gated on mc_dashboard_builder.
app.register_blueprint(report_scheduler_bp)
app.register_blueprint(restore_backup_bp, url_prefix="/restore-backup")
# per-tenant Field Catalog picker (owner-only via mc_field_catalog).
app.register_blueprint(field_catalog_bp, url_prefix="/field-catalog")
# superuser page for personal signup links (webinar onboarding).
app.register_blueprint(signup_links_bp)
# Publish to Global.Church dialog — one organisation's country x month
# release product, owner-only (workspace-scoped WORKSPACE_DELETE); the
# route path is absolute (/publish-global-church/<org_esid>).
app.register_blueprint(publish_global_church_bp)
app.register_blueprint(analytics_bp)
app.register_blueprint(public_report_bp)
# internal auth-backend for nginx auth_request. Exposes
# GET /api/internal/kibana-gate at the imports_container:8084 layer;
# nginx's /_kibana_auth internal location calls it on every /kibana
# request and proxies through only on 204. No url_prefix — the route
# path is already absolute (/api/internal/kibana-gate).
app.register_blueprint(internal_auth_bp)
# Sources blueprint was removed as a follow-up
# (2026-05-22). The page was a permission-leaky surface (no role gate
# on /sources/, exposing the per-connection org_shared toggle to every
# authenticated user) and redundant with the Data Connectors page's
# connector-tile section. The org_shared decision is still made at the
# onboarding wizard's step-8; a future surface for flipping it
# post-wizard, if needed, lives on Data Connectors with proper role
# scoping.

# Ensure MFA index exists in Elasticsearch
try:
    MFAService.ensure_index_exists()
except Exception as _mfa_init_err:
    logging.warning(f"Could not ensure MFA index exists: {_mfa_init_err}")

# Ensure audit log index exists, backfill scanner tags, and purge old entries
try:
    AuditLogService.ensure_index_exists()
    AuditLogService.backfill_scanner_tags()
    AuditLogService.delete_older_than(days=90)
except Exception as _audit_init_err:
    logging.warning(f"Could not initialize audit log: {_audit_init_err}")

# Kibana gate API-key cache — when the feature is ON (env vars set),
# ensure the index exists and drop rows whose key already expired in ES. Off by
# absence means off here too: no index, no delete_by_query. Fail-soft like the
# neighbours; the gate itself degrades to Kibana's own login if this index is
# unavailable.
try:
    KibanaGateService.startup()
except Exception as _kgate_init_err:
    logging.warning(f"Could not initialize kibana gate key index: {_kgate_init_err}")

# ensure the user-presence index exists (per-request activity
# heartbeat), and purge rows for users who've been dormant for 90+ days so
# the upsert-keyed index doesn't accumulate stale rows for deleted users.
try:
    PresenceService.ensure_index_exists()
    PresenceService.delete_older_than(days=90)
except Exception as _presence_init_err:
    logging.warning(f"Could not initialize user-presence index: {_presence_init_err}")

# Ensure registration index exists and clean up expired pending registrations
try:
    RegistrationService.ensure_index_exists()
    RegistrationService.cleanup_expired(hours=48)
except Exception as _reg_init_err:
    logging.warning(f"Could not initialize registration index: {_reg_init_err}")

# Ensure system settings index exists (feedback email, etc.)
try:
    FeedbackService.ensure_index_exists()
except Exception as _fb_init_err:
    logging.warning(f"Could not initialize system settings index: {_fb_init_err}")

# extend system settings index with maintenance-alert fields.
try:
    MaintenanceAlertService.ensure_index_exists()
except Exception as _ma_init_err:
    logging.warning(f"Could not initialize maintenance alert mapping: {_ma_init_err}")

# install the project-wide default index template BEFORE any
# ensure_index_exists / ensure_indices_exist calls below. The wide
# override outranks ES's default ``data_stream: {}`` template so the
# indices created by subsequent ensure_* calls land as regular indices,
# not data streams. Idempotent + fail-soft — a template-install
# failure logs but doesn't block startup.
try:
    from lib.es_index_template_service import ensure_project_default_template

    ensure_project_default_template()
except Exception as _tpl_init_err:
    logging.warning(f"could not install project default template: {_tpl_init_err}")

# install per-source MAPPING templates so an index auto-created by
# ES (when a doc write wins the race against create_index_if_not_exists) is born
# with correct field types instead of dynamic ``text``. Out-ranks the
# no-data-stream template and the legacy per-source no_datastream templates.
# Idempotent + fail-soft, same contract as the call above.
try:
    from lib.es_index_template_service import ensure_source_mapping_templates

    ensure_source_mapping_templates()
except Exception as _src_tpl_err:
    logging.warning(f"could not install per-source mapping templates: {_src_tpl_err}")

# align de_dashboard_templates to the versioned JSON in
# bin/python/dashboard_templates/. Before this the
# sync tool's --apply was wired into no deploy path, so a template change in
# git reached ES only when someone remembered to run it. Same contract as the
# two installs above: idempotent + fail-soft. Each overwrite of a live doc is
# logged at WARNING by the tool.
try:
    from tools.sync_dashboard_templates import sync_on_startup as _sync_dashboard_templates

    _sync_dashboard_templates()
except Exception as _dash_tpl_err:
    logging.warning(f"could not sync dashboard templates: {_dash_tpl_err}")

# Ensure role permissions index exists and seed defaults for system roles
try:
    RolePermissionsService.ensure_index_exists()
    RolePermissionsService.seed_defaults()
except Exception as _rp_init_err:
    logging.warning(f"Could not initialize role permissions index: {_rp_init_err}")

# ensure wizard_session index exists for resumable wizard flows
try:
    WizardSessionService.ensure_index_exists()
except Exception as _ws_init_err:
    logging.warning(f"Could not initialize wizard session index: {_ws_init_err}")

# ensure de_connector_notify_list index exists for the step-4
# notify-me capture surface (placeholder and coming-soon tiles).
try:
    NotifyListService.ensure_index_exists()
except Exception as _nl_init_err:
    logging.warning(f"Could not initialize notify list index: {_nl_init_err}")

# ensure feature_flags index exists and seed registered defaults.
# (The founding NEW_ONBOARDING flag was retired; the seed set
# lives in feature_flag_service._REGISTRY.)
try:
    FeatureFlagService.ensure_index_exists()
    FeatureFlagService.seed_defaults()
except Exception as _ff_init_err:
    logging.warning(f"Could not initialize feature flags index: {_ff_init_err}")

# ensure the three connection indices exist
# (de_connection, de_connection_asset, de_workspace_connection_asset).
# The workspace-link index is a stub in PR-A — mapping only, no writers
# lands behind the permissions refactor.
try:
    ConnectionService.ensure_indices_exist()
except Exception as _conn_init_err:
    logging.warning(f"Could not initialize connection indices: {_conn_init_err}")

# shared login/MFA lockout store (de_auth_lockout). Best-effort at
# start — the service also creates it lazily and degrades to in-process
# counting while ES is unreachable.
try:
    LoginLockoutService.ensure_index()
except Exception as _lockout_init_err:
    logging.warning(f"Could not initialize login lockout index: {_lockout_init_err}")

# ensure the report-scheduler indices exist
# (de_report_schedules, de_report_snapshots). P1 is backend-only — the
# poll-runner (asset_management/jobs/run_report_schedules.py) and service
# CRUD consume these; the UI section lands in a later phase.
try:
    ReportScheduleService.ensure_indices_exist()
except Exception as _report_sched_init_err:
    logging.warning(f"Could not initialize report-scheduler indices: {_report_sched_init_err}")

# Option B: bootstrap the per-team subscription index
# (de_billing_team_subscription). Phase O1 is build-only / INERT — the org
# customer + per-team subscription writers (checkout/get_state) are rewired in
# O2 behind a flag; this just makes the index/mapping ready.
try:
    OrgBillingService.ensure_index_exists()
except Exception as _org_billing_init_err:
    logging.warning(f"Could not initialize org-billing index: {_org_billing_init_err}")

# Phase 1: bootstrap the new authz indices alongside the existing
# de_role_permissions UI-flag system, then seed the closed Permission
# enum and the five default system roles. Phase 1 is build-only — no
# code path consumes these documents yet.
try:
    PermissionsIndexService.ensure_indices_exist()
    PermissionsSeederService.seed_permissions()
    PermissionsSeederService.seed_default_roles()
except Exception as _authz_init_err:
    logging.warning(f"Could not initialize authz indices: {_authz_init_err}")

# defense-in-depth orphan sweep across the workspace-tier
# cascade indices. Cleans up rows left behind by workspace deletes that
# happened cascade landed (2026-05-22). Idempotent —
# subsequent boots find zero orphans and exit clean. Safety-gated to
# refuse running when de_asset_workspace is empty / missing so a
# cold-boot before workspace seed doesn't wipe everything.
try:
    _orphan_counts = OrphanSweepService.sweep_workspace_tier()
    _total = sum(_orphan_counts.values())
    if _total:
        logging.info(
            "orphan sweep deleted %d row(s) across %d index(es): %s",
            _total,
            len([c for c in _orphan_counts.values() if c]),
            _orphan_counts,
        )
except Exception as _sweep_err:
    logging.warning(f"orphan sweep failed: {_sweep_err}")

# defense-in-depth orphan-connection sweep. A de_connection holds a
# live OAuth token keyed only by principal; one whose principal has zero
# workspace memberships is unreachable from any workspace and is swept (token
# + child asset rows). Catches connections left behind when a workspace's
# membership rows were already gone at delete time (the cascade's step 3.6
# could not enumerate the principal). Safety-gated: skips entirely when the
# membership index is empty/missing so a cold boot never wipes all tokens.
try:
    _conn_counts = OrphanSweepService.sweep_orphan_connections()
    _conn_total = sum(_conn_counts.values())
    if _conn_total:
        logging.info(
            "orphan-connection sweep deleted %d row(s): %s",
            _conn_total,
            _conn_counts,
        )
except Exception as _conn_sweep_err:
    logging.warning(f"orphan-connection sweep failed: {_conn_sweep_err}")

# scrub dead organization_esid refs from team docs. An org deleted
# by a path that didn't cascade the reference cleanup leaves teams pointing at
# a non-existent org, which dead-ends the team→org→workspace graph walk and
# hides the workspace from Asset Explorer / Data Inventory. Safety-gated: skips
# when de_asset_organization is empty/missing so a cold boot never scrubs every
# team's org refs. Idempotent — a healthy cluster touches nothing.
try:
    _team_org_counts = OrphanSweepService.sweep_orphan_team_org_refs()
    _team_org_total = sum(_team_org_counts.values())
    if _team_org_total:
        logging.info(
            "org→team sweep scrubbed dead org refs from %d team(s): %s",
            _team_org_total,
            _team_org_counts,
        )
except Exception as _team_org_sweep_err:
    logging.warning(f"org→team orphan-ref sweep failed: {_team_org_sweep_err}")

# (P3b): the membership-without-role report (added in Phase 1) is
# retired — membership indices are gone, scoped roles are the source of truth.

# Logo upload storage — within the static folder so Flask can serve them directly.
# NOTE: This directory lives inside the container. Add a Docker volume mount for
# static/uploads/ to persist logos across container rebuilds.
LOGO_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads", "logos")
_ALLOWED_LOGO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB


def _handle_logo_upload(field_name):
    """
    Process a logo file upload from request.files / request.form.

    Returns:
        (logo_relative_path, error_message)
        - logo_relative_path is relative to the static folder, e.g. 'uploads/logos/logo_abc.jpg'
        - Returns (False, None)  — no file uploaded, no change
        - Returns (None, None)   — removal was requested
        - Returns (str, None)    — new file saved, path returned
        - Returns (False, str)   — validation error
    """
    remove_flag = request.form.get(f"{field_name}_remove") == "1"
    file = request.files.get(f"{field_name}_file")

    if remove_flag:
        return None, None

    if not file or file.filename == "":
        return False, None

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in _ALLOWED_LOGO_EXTENSIONS:
        return False, "Logo must be a JPG or PNG file."

    file_data = file.read()
    if len(file_data) > _MAX_LOGO_BYTES:
        return False, "Logo file exceeds the 2 MB size limit."

    os.makedirs(LOGO_UPLOAD_DIR, exist_ok=True)
    filename = f"logo_{uuid.uuid4().hex}{ext}"
    # phase 4: ext is whitelisted above (_ALLOWED_LOGO_EXTENSIONS);
    # safe_path_join lets CodeQL see containment under LOGO_UPLOAD_DIR.
    from lib.paths import safe_path_join

    with open(safe_path_join(LOGO_UPLOAD_DIR, filename), "wb") as fh:
        fh.write(file_data)

    return f"uploads/logos/{filename}", None


def _delete_logo_file(logo_path):
    """Delete a stored logo file given its static-relative path. Silently ignores missing files."""
    if not logo_path:
        return
    abs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", logo_path)
    try:
        os.remove(abs_path)
    except OSError:
        pass


def _vet_echo_subdomain(form_data):
    """vet an Echo tenant form's subdomain.

    Returns ``(subdomain, error_message)``: the normalized label to store and
    None, or None and a user-safe message. The subdomain becomes the importers'
    request host (``https://<subdomain>.echoglobal.org``), so anything that is
    not a single DNS label is refused.
    """
    from lib.echo_base_url import EchoSubdomainError, normalize_echo_subdomain

    try:
        return normalize_echo_subdomain(form_data.get("echo_tenant_subdomain")), None
    except EchoSubdomainError as e:
        return None, e.public_message


def _vet_chatwoot_base_url(form_data, existing_item=None):
    """vet a Chatwoot account form's server URL.

    Returns ``(base_url, error_message)``: the value to store and None, or None
    and a user-safe message. On edit, moving the account to a different server
    requires re-entering the API token — the form re-submits the saved
    ciphertext (or nothing) when the token is left alone, and replaying a saved
    token to a new host would hand it to whoever runs that host.
    """
    from lib.chatwoot_base_url import ChatwootBaseUrlError, vet_account_base_url

    token_reentered = True
    if existing_item is not None:
        submitted_token = (form_data.get("chatwoot_api_token") or "").strip()
        token_reentered = bool(submitted_token) and submitted_token != existing_item.get("chatwoot_api_token")
    try:
        base_url = vet_account_base_url(
            form_data.get("chatwoot_base_url"),
            existing_doc=existing_item,
            token_reentered=token_reentered,
        )
    except ChatwootBaseUrlError as e:
        return None, e.public_message
    return base_url, None


def _verify_chatwoot_form(form_data, base_url, existing_item=None):
    """test a Chatwoot account form's server, account id and token
    before it is saved. Returns None, or a user-safe message.

    On edit the check runs only when the server, the account id or the token
    changed; an untouched token is re-submitted as the saved ciphertext (or
    left blank), so the saved token is decrypted for the check.
    """
    from cryptography.fernet import InvalidToken

    from import_scripts.chatwoot.api import ChatwootCredentialError, verify_account_access
    from lib.chatwoot_base_url import ChatwootBaseUrlError, normalize_chatwoot_base_url
    from lib.encryption import decrypt

    account_id = (form_data.get("chatwoot_account_id") or "").strip()
    submitted_token = (form_data.get("chatwoot_api_token") or "").strip()
    token = submitted_token
    if existing_item is not None:
        saved_token = existing_item.get("chatwoot_api_token") or ""
        token_reentered = bool(submitted_token) and submitted_token != saved_token
        try:
            # A legacy doc with no URL means Chatwoot Cloud; compare like with like.
            saved_base_url = normalize_chatwoot_base_url(existing_item.get("chatwoot_base_url"))
        except ChatwootBaseUrlError:
            saved_base_url = None
        changed = (
            token_reentered
            or account_id != str(existing_item.get("chatwoot_account_id") or "").strip()
            or base_url != saved_base_url
        )
        if not changed:
            return None
        if not token_reentered:
            try:
                token = decrypt(saved_token) if saved_token else ""
            except InvalidToken:  # an undecryptable saved token (e.g. a retired key) must be re-entered
                return "The saved API token could not be read. Enter the token again."
    try:
        verify_account_access(base_url, account_id, token)
    except ChatwootCredentialError as e:
        return e.public_message
    return None


# Handle reverse proxy with APPLICATION_ROOT prefix
class PrefixMiddleware:
    def __init__(self, app, prefix="", allowed_paths=None):
        self.app = app
        self.prefix = prefix
        # Paths that are allowed without the prefix even when behind a proxy
        self.allowed_paths = allowed_paths or []

    def __call__(self, environ, start_response):
        path_info = environ["PATH_INFO"]

        if self.prefix and path_info.startswith(self.prefix):
            # Strip prefix when present
            environ["PATH_INFO"] = path_info[len(self.prefix) :]
            environ["SCRIPT_NAME"] = self.prefix
            return self.app(environ, start_response)
        elif self.prefix and not path_info.startswith(self.prefix):
            # Check if path is in allowed list (for direct top-level routes)
            for allowed_path in self.allowed_paths:
                if path_info.startswith(allowed_path):
                    # Path is whitelisted, allow without prefix
                    return self.app(environ, start_response)

            # Check if we're behind proxy with X-Forwarded-For header
            # If yes, require prefix. If no (direct access), allow without prefix
            x_forwarded = environ.get("HTTP_X_FORWARDED_FOR")
            if x_forwarded:
                # Behind proxy, require prefix
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"Not Found - Access via /assets path"]
            else:
                # Direct access (e.g., docker exec), allow without prefix
                return self.app(environ, start_response)
        else:
            # No prefix configured
            return self.app(environ, start_response)


# Check if we should use prefix middleware (only when behind reverse proxy)
# Use environment variable to control this
USE_PREFIX = os.environ.get("FLASK_USE_PREFIX", "true").lower() == "true"

if USE_PREFIX:
    # Apply middleware for /assets prefix (set by Nginx)
    # Allow certain paths to be accessed without prefix
    app.wsgi_app = PrefixMiddleware(
        app.wsgi_app,
        prefix="/assets",
        allowed_paths=[
            "/",
            "/login",
            "/logout",
            "/dashboard",
            "/dashboards/login",
            "/signup",
            "/api/dashboards/",
            "/static",
            "/pricing",
            "/verify-email",
            "/public/",
            # self-service profile must be reachable from the
            # dashboards side too (some users have no management-console role).
            "/profile",
            # MFA pages are part of the sign-in surface and must
            # render at bare /mfa/* (not /assets/mfa/*) for the same reason
            # /login is canonical at the bare path. The before_request
            # canonicalise_assets_prefix_for_sign_in_surface handler bounces
            # /assets/mfa/* → /mfa/* with the right status code.
            "/mfa/",
            # follow-up: /change-password is the forced-password-change
            # route (fpc_pending session only — voluntary password change
            # lives at /profile/change-password). Same bare-URL rule as the
            # rest of the auth-flow surface.
            "/change-password",
            # bare /forbidden is the landing page for nginx's
            # /kibana 403 handler — must be reachable without the /assets
            # prefix because the nginx redirect lands here directly.
            "/forbidden",
            # /api/internal/* is the nginx auth_request backend.
            # nginx hits it as a subrequest with no /assets prefix; the
            # bare path must resolve.
            "/api/internal/",
            # /admin/elevate is the Kibana-elevation companion
            # to the auth_request gate. Bare path so it co-locates with
            # /api/internal/* under the internal_auth blueprint.
            "/admin/elevate",
            # the wizard surface is canonical at bare /onboarding/*
            # carved out /onboarding/* in the login-redirect
            # allow-list, Memory #602 registers the bare OAuth callback in
            # Meta's allowlist, and every template fetch hardcodes the bare
            # form. Without this whitelist entry, a bare /onboarding/* hit
            # behind nginx would 404 or fall through with SCRIPT_NAME empty
            # while /assets/onboarding/* would set SCRIPT_NAME=/assets — two
            # different SCRIPT_NAMEs for the same logical page, producing
            # divergent url_for(..., _external=True) results for the OAuth
            # callback. The companion ``_CANONICALISE_PREFIX_PATHS`` entry
            # below bounces /assets/onboarding/* → bare so url_for always
            # yields the form registered in the Meta app allowlist.
            "/onboarding/",
        ],
    )
    # Apply ProxyFix for handling X-Forwarded-* headers
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


def _mfa_redirect(endpoint, **kwargs):
    """Build a redirect to an MFA route at the bare /mfa/* path.

    MFA pages are canonical at bare /mfa/* (matching the bare
    /login surface). url_for under PrefixMiddleware would re-prepend
    /assets, so strip it back off before redirecting.
    """
    path = url_for(endpoint, **kwargs)
    if path.startswith("/assets"):
        path = path[len("/assets") :]
    return redirect(path)


_CANONICALISE_PREFIX_PATHS = ("/mfa/", "/change-password", "/onboarding/")


@app.before_request
def canonicalise_assets_prefix_for_sign_in_surface():
    """redirect /assets/<auth-flow-path> → bare /<auth-flow-path>
    so the URL bar always shows the canonical bare path during the
    sign-in flow.

    The PrefixMiddleware strips the /assets prefix before this handler
    runs, but it leaves SCRIPT_NAME=/assets so we can detect that the
    request originally came in with the prefix. GETs use 301 (permanent),
    POSTs use 308 so the form body survives the browser hop.

    Currently covers /mfa/*, /change-password, and /onboarding/*
    (the wizard's OAuth callback URL must match the form
    registered in Meta's allowlist regardless of entry point). /login
    has its own in-handler check because login_page / login_post need
    to short-circuit before any auth or session work.
    """
    if request.environ.get("SCRIPT_NAME") != "/assets":
        return None
    path = request.path
    if not any(
        path == p or path.startswith(p + "/") or (p.endswith("/") and path.startswith(p)) for p in _CANONICALISE_PREFIX_PATHS
    ):
        return None
    qs = request.query_string.decode("ascii", errors="replace")
    target = f"{path}?{qs}" if qs else path
    code = 308 if request.method in ("POST", "PUT", "PATCH", "DELETE") else 301
    return redirect(target, code=code)


# Verify Elasticsearch authentication and authorization
@app.before_request
def check_authentication():
    """Verify ES authentication and authorization."""
    # Skip in debug mode if ES_AUTH_REQUIRED=false
    if app.debug and not config.ES_AUTH_REQUIRED:
        return

    return verify_auth()


@app.before_request
def csrf_protection():
    """Enforce CSRF token on all state-changing requests (POST/PUT/DELETE/PATCH)."""
    return check_csrf_for_state_change()


@app.before_request
def generate_csp_nonce():
    """Phase 1: mint a fresh per-request CSP nonce.

    Stored on ``flask.g`` so both the ``inject_user`` context processor
    (which exposes it to templates as ``csp_nonce``) and the
    ``set_security_headers`` after_request hook (which embeds it in the
    ``script-src`` directive) read the SAME value for a given request.
    A new random nonce per response is what makes the nonce mechanism
    sound — a static value would be guessable and defeat the purpose.
    """
    g.csp_nonce = secrets.token_urlsafe(16)


@app.after_request
def deny_cors(response):
    """Explicitly deny cross-origin requests.

    By never setting Access-Control-Allow-Origin, browsers already block
    cross-origin JS from reading responses.  For extra safety, reject
    CORS preflight requests outright.
    """
    if request.method == "OPTIONS" and "Origin" in request.headers:
        # Reject CORS preflight — no Access-Control-Allow-* headers
        response.status_code = 403
    return response


@app.after_request
def prevent_bfcache_for_session_bearing_responses(response):
    """Prevent the browser back-forward cache (bfcache) from restoring
    authenticated pages after sign-out.

    Without ``Cache-Control: no-store`` Chrome and Safari may restore a
    logged-in page from memory when the user clicks back after signing
    out — the user sees their old dashboard as if still authenticated
    because the browser never re-hits the server, so verify_auth never
    fires. Setting no-store on any session-bearing response makes the
    page ineligible for bfcache, forcing a real HTTP request on the
    back navigation that goes through verify_auth and redirects to
    /login.

    Applied whenever the response was rendered against an authenticated,
    mfa_pending, or fpc_pending session — i.e., any page whose contents
    depend on who the user is. Public marketing pages, /static, and
    /public reports remain cacheable.
    """
    if session.get("authenticated") or session.get("mfa_pending") or session.get("fpc_pending"):
        # setdefault so individual handlers (login_page, logout) that
        # already set a stricter value (no-cache, must-revalidate, …)
        # win — we only fill in when nothing else has been set.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.after_request
def set_security_headers(response):
    """Add standard security headers to every response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Cache-Control: revalidate every load on public (session-less) responses
    # so deploys land immediately on /login, /public/report/<token>, and
    # similar shells. Authenticated responses get the stricter `no-store`
    # from prevent_bfcache_for_session_bearing_responses; that handler
    # uses setdefault and runs after this one (after_request fires LIFO),
    # so we must skip session-bearing requests here or its setdefault
    # would no-op and authenticated pages would lose bfcache protection.
    if not (session.get("authenticated") or session.get("mfa_pending") or session.get("fpc_pending")):
        response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
    # Permissive CSP that allows the inline styles/scripts already in use,
    # plus the CDN origins for ECharts, Leaflet, Font Awesome, and Google Fonts.
    # object-src/base-uri/form-action don't fall back to default-src — set
    # them explicitly. form-action also needs to
    # allow the FB OAuth host so the FB App User Authorize button can
    # cross-host redirect after form submission.
    #
    # Issue 1 (2026-05-21): the wizard step-5 Continue button
    # POSTs to /onboarding/step-7 which 302s through /start-oauth and
    # ultimately to https://www.facebook.com/v25.0/dialog/oauth?... The
    # browser evaluates form-action against every URL in the redirect
    # chain; without www.facebook.com in the allowlist the final hop
    # is blocked and the user dead-ends back on step-5 with no visible
    # error. Chrome reports the violation against the FIRST URL in the
    # chain (the form's action attribute), which makes the misleading-
    # looking message "form posted to <this host>
    # violates form-action 'self' ..." — same-origin POST appears to
    # be blocked, but the actual block is at the facebook.com hop.
    from asset_management.web.config import config as app_config

    fb_oauth_host = (app_config.FBOAUTH_HOSTNAME or "").strip()
    form_action_sources = ["'self'"]
    if fb_oauth_host:
        form_action_sources.append(f"https://{fb_oauth_host}")
    # Meta's OAuth dialog. The wizard's step-5 Continue redirects here
    # to start the Meta OAuth roundtrip. Pinned to the exact
    # host, not a wildcard, so a future XSS that builds a form-action
    # pointing at fbcdn.net or facebookmail.com is still blocked.
    form_action_sources.append("https://www.facebook.com")
    form_action = "form-action " + " ".join(form_action_sources)

    # Stripe Elements (Billing → Payment) needs three CSP allowances
    # (per Stripe's CSP guide): load js.stripe.com (script-src), render the
    # card iframe from js.stripe.com + the 3DS/redirect frames at
    # hooks.stripe.com (frame-src), and tokenize via api.stripe.com
    # (connect-src). Without these the card iframe never mounts (the expiry/
    # CVC/ZIP fields don't appear). Scoped to the exact Stripe hosts — no
    # wildcards — and only widen the three directives Stripe requires; every
    # other source list is unchanged. Applied globally for simplicity (only
    # the billing pages load Stripe, and this merely permits those hosts).
    # follow-up (2026-06-22) — Phase 1 hotfix.
    #
    # added a per-request 'nonce-<n>' to script-src while KEEPING
    # 'unsafe-inline' as an intended CSP2 fallback. But CSP Level 3 browsers
    # (every modern browser) IGNORE 'unsafe-inline' the moment a nonce source
    # is present — so the header was actually ENFORCING strict nonce mode in
    # practice. That blocks every inline event-handler attribute (onclick=,
    # onchange=, …), which can't carry a nonce. User Management alone has 112
    # such handlers, so the whole page became unclickable (reported by a teammate).
    #
    # Fix: drop the nonce from the EMITTED script-src so 'unsafe-inline' is
    # honored again app-wide, immediately restoring inline handlers. The
    # per-request nonce is still minted (generate_csp_nonce) and the
    # nonce="{{ csp_nonce }}" attributes on the 65 <script> blocks stay in
    # place as harmless no-ops — migration groundwork is preserved.
    # Re-enabling the nonce in script-src is gated on Phase 2/3/4
    # first migrating those 112 inline handlers (and the rest) to
    # addEventListener so nothing breaks under strict CSP3.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' " + "'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com https://js.stripe.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://unpkg.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "img-src 'self' data: blob: https://*.tile.openstreetmap.org https://*.basemaps.cartocdn.com https://server.arcgisonline.com; "
        "connect-src 'self' https://d2ad6b4ur7yvpq.cloudfront.net https://cdn.jsdelivr.net https://unpkg.com https://api.stripe.com; "
        "frame-src 'self' https://js.stripe.com https://hooks.stripe.com; "
        "frame-ancestors 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; " + form_action,
    )
    return response


# ============================================
# ROUTES
# ============================================


@app.route("/static/<path:filename>")
def serve_static(filename):
    """Explicitly serve static files (fallback for when Nginx doesn't handle them)."""
    from flask import send_from_directory

    return send_from_directory(app.static_folder, filename)


# prefix-safe url_for for /assets-only routes linked from
# bare-path surfaces (/billing, /profile). See url_prefix.py.
from asset_management.web.url_prefix import assets_url  # noqa: E402

app.jinja_env.globals["assets_url"] = assets_url


@app.context_processor
def inject_user():
    """Make current user, session config, and CSRF token available to all templates."""
    csrf_token = auth_module.refresh_csrf_token()
    analytics = {
        "plausible_domain": config.ANALYTICS_PLAUSIBLE_DOMAIN,
        "fathom_site_id": config.ANALYTICS_FATHOM_SITE_ID,
        "ga4_id": config.ANALYTICS_GA4_ID,
    }
    if "authenticated" in session:
        user = get_current_user()
        # follow-up: pass username so the System 2 bridge
        # in get_merged_permissions fires for wizard-signup users.
        # workspace_admin / workspace_owner hold the role as a System 2
        # grant (not an ES Security role), so session["roles"] is []; the
        # bridge is what gives them mc_data_connectors / db_management_console /
        # hdr_dashboards_link. The route-level guard at
        # data_connectors.routes._require_data_connectors already passes
        # username — without this matching call here, the sidebar link
        # was hidden even though the route itself would let them in.
        perms = RolePermissionsService.get_merged_permissions(
            user.get("roles", []),
            username=user.get("username", ""),
        )
        is_superuser = "superuser" in user.get("roles", [])
        # resolve the user's scoped Permission enum set once per
        # request. The sidebar helper closes over this frozenset so
        # Jinja can check sidebar visibility without re-querying ES on
        # every link. Super-admins get ALL_PERMISSIONS without ES queries.
        from asset_management.web.services.permission_resolver import (
            PermissionResolver,
            SIDEBAR_ITEM_PERMISSIONS,
        )

        scoped_permissions = PermissionResolver.effective_permissions_anywhere(
            user.get("username", ""), is_super_admin=is_superuser
        )

        def _sidebar_visible(perm_key: str) -> bool:
            """combine OLD _DEFAULTS flag (per-user role bag)
            with the NEW scoped permission check. OR-semantics so no
            existing role behaviour regresses.
            """
            if perms.get(perm_key):
                return True
            if perm_key not in SIDEBAR_ITEM_PERMISSIONS:
                return False
            needed = SIDEBAR_ITEM_PERMISSIONS[perm_key]
            return any(p in scoped_permissions for p in needed)

        # has_any_mc considers BOTH the OLD bag AND the
        # scoped-admin path so the management-console gear icon (which
        # hinges on it) renders for scoped admins too.
        has_any_mc = any(v for k, v in perms.items() if k.startswith("mc_"))
        if not has_any_mc:
            has_any_mc = any(_sidebar_visible(key) for key in SIDEBAR_ITEM_PERMISSIONS)

        # surface workspace.is_agency to the template so the
        # team form can decide whether to render the Organization picker
        # as a multi-select (agency / superuser) or as read-only chips
        # (non-agency workspace user). Fail-closed: if the workspace
        # lookup errors, render as non-agency so the read-only branch
        # engages — accidentally exposing the unrestricted dropdown is
        # the worse failure mode.
        try:
            from asset_management.web.services.workspace_membership_service import (
                WorkspaceMembershipService,
            )

            is_agency_workspace = WorkspaceMembershipService.is_agency_for_user(user.get("username", "") or "")
        except Exception as e:
            logging.warning("is_agency_workspace lookup failed: %s", e)
            is_agency_workspace = False

        # (follow-up): the Billing nav button is restricted to
        # superuser + workspace_owner. The helper short-circuits on the
        # superuser role and only hits ES for the workspace_owner grant
        # otherwise.
        can_access_billing = auth_module.user_can_access_billing(user.get("username", ""), user.get("roles", []))

        # the Report Scheduler nav is for the v2 audience —
        # superuser / workspace owner / org owner. Mirror the blueprint gate
        # (_user_is_report_scheduler_audience): privileged OR an org-tier role
        # (org_esids non-empty). The route gate is authoritative; this just
        # decides whether to show the nav link.
        try:
            report_scheduler_visible = bool(RoleFilterService.current_user_is_privileged())
            if not report_scheduler_visible:
                try:
                    from asset_management.web.services.role_filter_service import resolve_user_scope as _rus
                except ImportError:
                    from .services.role_filter_service import resolve_user_scope as _rus
                _rs_scope = _rus(
                    user.get("username", "") or "",
                    user.get("roles", []) or [],
                    RoleFilterService.load_config(),
                )
                report_scheduler_visible = bool(getattr(_rs_scope, "org_esids", None))
        except Exception as e:  # noqa: BLE001 — nav flag must not 500 the page
            logging.warning("report_scheduler_visible lookup failed: %s", e)
            report_scheduler_visible = bool(is_superuser)

        # broken-connection banners, driven by de_credential_health.
        # Cached 60s inside the service (this runs on EVERY page render), and
        # fail-soft: a banner lookup must never 500 a page. Empty list when
        # everything is healthy, so the partial renders nothing.
        try:
            from asset_management.web.services.credential_banner_service import build_banners
            from asset_management.web.services.workspace_membership_service import (
                WorkspaceMembershipService,
            )

            _ws = list(WorkspaceMembershipService.get_workspaces_for_user(user.get("username", "") or "") or [])
            credential_banners = build_banners(
                username=user.get("username", "") or "",
                workspace_esids=_ws,
                is_superuser=is_superuser,
            )
        except Exception as e:  # noqa: BLE001 — banners must never 500 a page
            logging.warning("credential banner lookup failed: %s", e)
            credential_banners = []

        return {
            "current_user": user,
            "user_perms": perms,
            "has_any_mc": has_any_mc,
            "is_superuser": is_superuser,
            "is_agency_workspace": is_agency_workspace,
            "can_access_billing": can_access_billing,
            "report_scheduler_visible": report_scheduler_visible,
            "credential_banners": credential_banners,
            "csrf_token": csrf_token,
            "csp_nonce": getattr(g, "csp_nonce", ""),
            "analytics": analytics,
            "sidebar_visible": _sidebar_visible,
            # the server idle window (seconds), surfaced to the
            # client idle timer in session-timeout.js. Only meaningful for
            # an authenticated session.
            "session_idle_timeout": config.SESSION_LIFETIME,
        }
    return {
        "current_user": None,
        "user_perms": {},
        "has_any_mc": False,
        "is_superuser": False,
        "is_agency_workspace": False,
        "can_access_billing": False,
        "report_scheduler_visible": False,
        # never show connection banners on unauthenticated pages.
        "credential_banners": [],
        "csrf_token": csrf_token,
        "csp_nonce": getattr(g, "csp_nonce", ""),
        "analytics": analytics,
        "sidebar_visible": lambda _key: False,
        # 0 disables the client idle timer for unauthenticated pages.
        "session_idle_timeout": 0,
    }


@app.template_filter("role_display")
def role_display(role):
    """friendly label for a single ES Security role name.

    Thin Jinja wrapper over role_permissions_service.role_display_name —
    the canonical map for the role pickers and role badges. Distinct from
    ``first_role_display`` below, which resolves org/team titles for a
    whole roles list.
    """
    return role_display_name(role)


@app.template_filter("first_role_display")
def first_role_display(roles):
    """
    Get user-friendly display name for the relevant org or team role.

    Scans all roles to find the first org_ or team_ role and resolves it.
    Superuser roles short-circuit the scan and return 'superuser'.
    For org roles, returns organization_description from de_asset_organization.
    For team roles, returns team_title from de_asset_teams.

    Args:
        roles: List of role names

    Returns:
        str: User-friendly role name or empty string if no roles
    """
    if not roles or len(roles) == 0:
        return ""

    # Superuser takes priority — skip org/team scanning
    if "superuser" in roles:
        return "superuser"

    # Scan all roles to find the first org_ or team_ role
    org_role = None
    team_role = None
    for role in roles:
        if role.startswith("org_") and org_role is None:
            org_role = role
        elif role.startswith("team_") and team_role is None:
            team_role = role

    target_role = org_role or team_role

    if target_role:
        if target_role.startswith("org_"):
            org_id = target_role[4:]  # Extract title after "org_"
            try:
                from services.role_filter_service import RoleFilterService

                descriptions = RoleFilterService.get_org_descriptions_by_ids([org_id])
                if descriptions:
                    return descriptions[0]
            except Exception as e:
                import logging

                logging.warning(f"Failed to resolve org role {target_role}: {e}")
            return org_id  # Fallback to raw ID

        if target_role.startswith("team_"):
            team_id = target_role[5:]  # Extract title after "team_"
            try:
                from services.role_filter_service import RoleFilterService

                team_titles = RoleFilterService.get_team_titles_by_ids([team_id])
                if team_titles:
                    return team_titles[0]
            except Exception as e:
                import logging

                logging.warning(f"Failed to resolve team role {target_role}: {e}")
            return team_id  # Fallback to raw ID

    # No org or team role found — return first role as-is
    return roles[0]


def _redirect_to_allowed_page():
    """Redirect to the first management console page the user has permission for.

     follow-up: falls through to the System-2 sidebar gate
    (``SIDEBAR_ITEM_PERMISSIONS``) when the legacy ``mc_*`` bag doesn't
    grant a destination. Pre-fix this only consulted
    ``get_merged_permissions``, which returns all-False for org_admin /
    team_admin (their mc_* surface comes System-2 model),
    so scoped admins clicking the dashboards-page gear icon were bounced
    back to /dashboard — even though their System-2 ``REPORT_EDIT``
    grants Dashboard Builder. The System-2 fallback mirrors the same OR
    pattern ``index()`` and ``_require_mc`` already use.
    """
    from flask import redirect, url_for

    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if perms.get("mc_data_explorer"):
        return redirect(url_for("data_explorer.index"))
    if perms.get("mc_dashboard_builder"):
        return redirect(url_for("dashboard_builder.index"))
    if perms.get("mc_user_management"):
        return redirect(url_for("user_management"))
    if perms.get("mc_import_logs"):
        return redirect(url_for("list_logs"))
    if perms.get("mc_import_status_reports"):
        return redirect(url_for("status_reports"))
    if perms.get("mc_import_data"):
        return redirect(url_for("import_data"))
    if perms.get("mc_data_inventory"):
        return redirect(url_for("data_inventory"))
    if perms.get("mc_restore_backup"):
        return redirect(url_for("restore_backup.index"))
    if perms.get("mc_testing"):
        return redirect(url_for("testing_page"))

    # System-2 fallback for scoped admins whose only mc_* surface
    # is reachable via SIDEBAR_ITEM_PERMISSIONS, not the legacy bag.
    # Order matches the legacy bag above so destination choice stays
    # stable. /assets/ (mc_asset_explorer) and the user-management /
    # logs / etc legacy-only pages aren't included — System-2 catalog
    # only grants Dashboard Builder + the workspace-tier surfaces
    # (Asset Explorer / User Management), which already match
    # the legacy bag for users who hold them.
    from asset_management.web.services.permission_resolver import (
        PermissionResolver,
        SIDEBAR_ITEM_PERMISSIONS,
    )

    held = PermissionResolver.effective_permissions_anywhere(
        session.get("username", ""),
        is_super_admin="superuser" in session.get("roles", []),
    )
    for mc_key, route in (
        ("mc_dashboard_builder", "dashboard_builder.index"),
        ("mc_asset_explorer", "index"),
        ("mc_data_inventory", "data_inventory"),
        ("mc_user_management", "user_management"),
    ):
        needed = SIDEBAR_ITEM_PERMISSIONS.get(mc_key, ())
        if needed and any(p in held for p in needed):
            return redirect(url_for(route))

    # No MC pages allowed — send to dashboards
    return redirect("/dashboard")


@app.route("/")
def index():
    """Main dashboard showing all asset categories."""
    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if not perms.get("mc_asset_explorer"):
        # fall through to the new-authz model for scoped admins
        # (workspace_admin / org_admin etc.) before redirecting. Same
        # OR-with-OLD-path pattern as _require_mc.
        from asset_management.web.services.permission_resolver import (
            PermissionResolver,
            SIDEBAR_ITEM_PERMISSIONS,
        )

        needed = SIDEBAR_ITEM_PERMISSIONS.get("mc_asset_explorer", ())
        if needed:
            held = PermissionResolver.effective_permissions_anywhere(
                session.get("username", ""),
                is_super_admin="superuser" in session.get("roles", []),
            )
            if not any(p in held for p in needed):
                return _redirect_to_allowed_page()
        else:
            return _redirect_to_allowed_page()
    return render_template("index.html", assets=ASSET_CONFIGS, categories=ASSET_CATEGORIES)


@app.route("/api/documentation-permissions")
def api_documentation_permissions():
    """Return which documentation sections the current user can see.

    The docs site is Sphinx HTML served statically by nginx at /docs/,
    so this endpoint exists purely to let the docs client-side JS hide
    sidebar and landing-page entries for sections outside the user's
    role. It is nav-filtering, not access control — direct URLs still
    reach the underlying pages.
    """
    if "authenticated" not in session:
        return jsonify({"error": "not authenticated"}), 401
    roles = session.get("roles", [])
    is_superuser = "superuser" in roles
    is_admin = is_superuser or "asset_admin" in roles
    return jsonify(
        {
            "sections": {
                "guide-dashboards": True,
                "guide-data-explorer": is_admin,
                "guide-management-console": is_admin,
                "self-hosted": is_superuser,
            }
        }
    )


@app.route("/debug/expire-session")
def debug_expire_session():
    """
    DEBUG ONLY: Instantly mark the current session as expired so the next
    page load triggers the session-expiry → login redirect flow.
    Visit /assets/debug/expire-session while logged in, then navigate
    anywhere on DEAM to reproduce the "Invalid request" login bug on demand.
    """
    import logging

    # Only allow in debug/development mode
    if not app.debug and os.environ.get("APP_ENV", "production") != "development":
        abort(404)

    if "authenticated" not in session:
        return "No active session to expire.", 400
    old_ts = session.get("auth_timestamp", 0)
    # Use 1 (epoch 1970), not 0 — 0 is falsy so verify_auth()'s
    # `session.get('auth_timestamp')` check would skip the expiry branch entirely,
    # leaving authenticated=True in the session and causing a redirect loop.
    session["auth_timestamp"] = 1
    logging.info(
        f"DEBUG: debug_expire_session called — user={session.get('username')} "
        f"old_auth_timestamp={old_ts} new_auth_timestamp=1"
    )
    return (
        "<p>Session marked as expired (auth_timestamp=1).</p>"
        "<p>Now navigate to any DEAM page to trigger the login redirect, "
        'then try logging in to reproduce the "Invalid request" error.</p>'
        '<p><a href="/assets/">Go to asset explorer</a></p>'
    ), 200


def create_kibana_session(username, password):
    """
    Create a Kibana session by calling Kibana's login API.
    Returns the sid cookie value on success, None on failure.

    This enables SSO: user logs into Flask once, and the Kibana
    embedded dashboard is also authenticated automatically.
    """
    import requests
    import logging

    from asset_management.web.config import DEFAULT_KIBANA_URL

    kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)
    login_url = f"{kibana_url}/internal/security/login"

    try:
        # Call Kibana's login API
        response = requests.post(
            login_url,
            json={
                "providerType": "basic",
                "providerName": "basic",
                "currentURL": f"{kibana_url}/",
                "params": {"username": username, "password": password},
            },
            headers={
                "kbn-xsrf": "true",
                "Content-Type": "application/json",
                "x-elastic-internal-product": "kibana",
            },
            verify=kibana_tls_verify(),  # verify Kibana TLS (was verify=False)
            allow_redirects=False,
            timeout=10,
        )

        if response.status_code == 200:
            # Extract sid cookie from response
            sid_cookie = response.cookies.get("sid")
            if sid_cookie:
                logging.info(f"AUDIT: user={username} action=kibana_session_create success=True")
                return sid_cookie
            else:
                logging.warning(f"Kibana login succeeded but no sid cookie returned for {username}")
        else:
            logging.warning(f"Kibana login failed for {username}: {response.status_code}")

    except Exception as e:
        logging.error(f"Error creating Kibana session for {username}: {e}")

    return None


def _build_fb_app_user_oauth_url(item):
    """Build the OAuth URL for an FB App User record."""
    from asset_management.web.config import config as app_config

    hostname = app_config.FBOAUTH_HOSTNAME or "localhost"
    fb_app_esid = item.get("fb_app_esid", "")
    auth_url = item.get("fb_app_user_auth_url", "")
    return f"https://{hostname}/{fb_app_esid}/{auth_url}"


def _workspaces_visible_to(session) -> bool:
    """should the user see the Workspaces asset type at all?

    The Workspaces tier is system_admin-only. Even within that, the
    visibility set is further narrowed by
    :class:`WorkspaceVisibilityService.get_visible_workspaces_for_user`
    to the per-role rule — the gate below decides "does the tier
    appear at all", not "which docs the user sees". This is the
    asymmetric privacy contract Kavanah agreed on for even
    superusers cannot enumerate every tenant's workspace.

     (2026-05-21) refined the per-role rule:
      * Non-superuser: ``(member-of) ∪ (created-by)``
      * Superuser:     ``(created-by) ∪ (active workspace-scoped grants)``

    The gate consults the sentinel-bearing system_admin check, not the
    looser ``superuser`` role flag, so a misconfigured superuser
    assignment cannot accidentally expose the tier.

    Fail-closed: any exception or unknown session resolves to False.
    The cost of hiding the tier from a legitimate admin (who can fall
    back to ``tools/bootstrap_workspaces.py`` until reconfigured) is
    lower than the cost of exposing it to a non-admin.
    """
    username = (session.get("username") or "").strip()
    if not username:
        return False
    try:
        from asset_management.web.services.role_filter_service import (
            _user_is_system_admin,
        )

        return bool(_user_is_system_admin(username))
    except Exception as e:
        logging.warning("visibility check failed for %s: %s", username, e)
        return False


def _organizations_visible_to(session) -> bool:
    """should the user see the Organizations asset type?

    The spike's UX claim: solo users never see the word
    "organization" in the navigation. Org is transparent to
    individuals — the data model carries it for hierarchy uniformity
    but the menu hides it. Agencies operate multiple client orgs, so
    they DO see the Organizations tab. Superusers always see it
    (they manage the whole cluster).

    Reads ``workspace.is_agency`` via ``WorkspaceMembershipService.is_agency_for_user``
    (F8 /). NEVER reads ``org.is_multi_workspace`` from this
    function — that field is deprecated post-F7 and stays writable
    only for back-compat readers; new code goes through the workspace
    tier.

    Fail-open: if the workspace lookup raises or the user has no
    workspace membership yet (e.g. a session that pre-dates F7 and
    hasn't been backfilled), return True. The cost of accidentally
    showing the menu is far lower than the cost of accidentally
    hiding it from a legitimate org owner — and the data filtering
    underneath still prevents any actual data leak.
    """
    roles = session.get("roles") or []
    if "superuser" in roles:
        return True
    username = (session.get("username") or "").strip()
    if not username:
        # No session = no signed-in user = no chrome decisions to make.
        # Fall through to True so the unauthenticated paths (which 302
        # to /login elsewhere) don't trip over a False here.
        return True
    try:
        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService,
        )

        return WorkspaceMembershipService.is_agency_for_user(username)
    except Exception as e:
        logging.warning("visibility check failed for %s: %s", username, e)
        return True  # fail-open


# Phase B (and) narrowed every *enumeration* surface for the
# data-source asset types to the user's workspace set: list_assets,
# get_asset_list, get_tree_structure, get_dashboard_summary, search_assets.
# But the direct-object-reference routes — reading / editing / deleting a
# single asset by ``item_id`` — were never gated. An asset_admin in
# workspace A who knows (or guesses) workspace B's facebook_ad_account _id
# could still GET /api/asset/.../details, /asset/.../edit/<id>, or POST the
# delete route and reach it. closes that IDOR gap so the boundary
# the list views enforce also holds on the direct URL.
_DATA_SOURCE_SYSTEM_TYPES = ("teams", "organizations", "workspaces")

# the two LEGACY Facebook-app asset types. They hold a shared FB
# app's credentials (App ID / Secret / Token) + per-user app tokens for the
# pre-wizard import model. New tenants authenticate per-connection via the
# Data Connectors wizard (de_connection.oauth_token_enc) and have NO
# fb_app / fb_app_user docs, so these Asset Explorer nodes are always empty
# and expose a concept those users should never see. In ``get_tree_structure``
# the node is suppressed when its (already workspace-scoped) count is zero, so
# a legacy tenant that still owns these docs keeps both nodes while a
# new-onboarded workspace sees neither.
_LEGACY_FB_APP_TYPES = ("facebook_apps", "facebook_app_users")


def _data_sources_visible_to(session) -> bool:
    """should the current user see the data-source asset types at
    all (Chatwoot, Echo, the five Facebook types, the two Google Analytics
    types, Google Ads, Instagram, the two YouTube types)?

    Only the *data-source* types — the ``company`` tier
    (``teams`` / ``organizations`` / ``workspaces``) carries its own
     gates and is NOT this helper's concern.

    The problem (, sibling of the user-list / org-dropdown
    leaks): the per-asset workspace filter admits any doc whose
    ``workspace_esid`` is in ``get_allowed_workspaces()``. For an org-tier
    owner/admin in a multi-org agency workspace, ``get_allowed_workspaces``
    infers the WHOLE workspace (org_esid → org.workspace_esid), so the
    filter admits EVERY sibling org's data sources — a cross-tenant
    disclosure. Data-source docs carry only ``workspace_esid`` (no
    org/team back-reference), so a precise org-narrow would need a reverse
    team→source lookup; per the team's call (2026-06-02) we instead HIDE the
    data-source types from org-tier users entirely. They reach their own
    sources through Data Connectors, not the Asset Explorer.

    Rule: visible only to a *privileged* operator — superuser, asset_admin,
    or a privileged workspace role (``workspace_admin`` / ``workspace_owner``)
    — exactly the ``current_user_is_privileged`` discriminator the
    family uses to tell a whole-workspace operator from an org-scoped one. A
    non-privileged user (org owner/admin, team-scoped, or no scope) sees
    none of the data-source types.

    Fail-closed: any exception resolves to False (hidden). The cost of
    hiding a data source from a legitimate admin — who still reaches every
    source through Data Connectors — is far lower than re-opening the
    cross-tenant leak this closes.
    """
    try:
        return bool(RoleFilterService.current_user_is_privileged())
    except Exception as e:  # noqa: BLE001 — security boundary, fail closed
        logging.warning("data-source visibility check failed: %s", e)
        return False


def _data_source_asset_in_user_workspace(asset_type: str, item: dict) -> bool:
    """may the current user reach this single data-source asset?

    Mirrors the ``else`` branch of the ``list_assets`` / ``get_asset_list``
    workspace filter exactly, so the per-item gate on the direct-access
    routes agrees with the list/tree narrowing — a doc the list view hides
    is also unreachable by direct URL, and one the list view shows stays
    editable.

    Returns ``True`` (allow) for the system types
    (``teams`` / ``organizations`` / ``workspaces``); those carry their own
     gates on the same routes and are not this
    helper's concern. For the data-source types the contract matches the
    list filter:

      * ``current_user_has_any_scope`` False → deny-all (Phase B.4
        floor): a bare user with no roles / memberships / grants reaches
        nothing, same as the empty list they'd see.
      * ``should_apply_filter`` False, or ``get_allowed_workspaces`` returns
        ``None`` → no workspace filter applies at this layer (privileged
        unscoped pass-through) → allow, matching the list branch where
        ``allowed_workspaces is None`` skips the filter.
      * Otherwise allow iff the doc's ``workspace_esid`` is in the user's
        allowed set. A doc with no ``workspace_esid`` (pre-backfill) is in
        no allowed set, so it's denied — the same intentional behavior as
        the list filter (run ``tools.backfill_asset_workspace_esid`` to tag
        legacy docs).

    Fail-closed: any unexpected error denies access. For a security gate the
    cost of a spurious 404 to a legitimate admin (who can retry / be
    re-scoped) is lower than the cost of leaking a cross-tenant asset.
    """
    if asset_type in _DATA_SOURCE_SYSTEM_TYPES:
        return True
    # data-source types are hidden from non-privileged (org-tier)
    # users entirely. Deny the direct-object routes too, so a known _id
    # can't reach a source the list/tree views now hide.
    if not _data_sources_visible_to(session):
        return False
    try:
        if not RoleFilterService.current_user_has_any_scope():
            return False
        if not RoleFilterService.should_apply_filter():
            return True
        allowed_workspaces = RoleFilterService.get_allowed_workspaces()
        if allowed_workspaces is None:
            return True
        return item.get("workspace_esid") in set(allowed_workspaces)
    except Exception as e:
        logging.warning("workspace boundary check failed for %s: %s", asset_type, e)
        return False


def _company_tier_asset_in_user_scope(asset_type: str, item: dict) -> bool:
    """may the current user reach this single ``teams`` /
    ``organizations`` asset by direct _id?

    The company-tier sibling of :func:`_data_source_asset_in_user_workspace`.
    That helper short-circuits to ``True`` for the system types on the premise
     that "teams/organizations carry their own gates on the
    same routes" — but the direct-object routes (edit / delete /
    check-references / bulk-delete / details) had NO per-object company-tier
    check for ``teams`` at all, and only the fail-open *type-visibility* gate
    (``_organizations_visible_to``) for ``organizations``. A scoped admin who
    knew a foreign team/org _id could therefore read or mutate it cross-tenant.

    This mirrors the ``("teams", "organizations")`` branch of
    :func:`list_assets` exactly, so a doc the list view hides is also
    unreachable by direct URL:

      * ``current_user_has_any_scope`` False → deny.
      * ``should_apply_filter`` False → no filter applies (privileged unscoped
        pass-through) → allow.
      * Otherwise allow iff the doc's title is in the allowed-titles set
        (``get_allowed_teams_for_org_users`` / ``get_allowed_organizations``)
        AND it survives the ``filter_hits_by_workspace`` tenant-boundary
        check (titles collide across workspaces).

    ``workspaces`` is intentionally NOT handled here — those routes already
    enforce a per-object check via ``WorkspaceVisibilityService`` and a delete
    block. Only ``teams`` / ``organizations`` route through this helper.

    Fail-closed: any unexpected error denies, same posture as the data-source
    sibling — a spurious 404 to a legitimate admin costs less than a leak.
    """
    if asset_type not in ("teams", "organizations"):
        # Defensive: callers gate to teams/organizations. Anything else is not
        # this helper's contract — deny rather than silently allow.
        return False
    try:
        if not RoleFilterService.current_user_has_any_scope():
            return False
        if not RoleFilterService.should_apply_filter():
            return True
        asset_config = ASSET_CONFIGS[asset_type]
        if asset_type == "teams":
            allowed_titles = RoleFilterService.get_allowed_teams_for_org_users()
        else:
            allowed_titles = RoleFilterService.get_allowed_organizations()
        if allowed_titles is not None:
            title_field = asset_config["title_field"]
            if item.get(title_field) not in set(allowed_titles):
                return False
        # tenant-boundary: titles collide across workspaces, so a
        # surviving title is not sufficient — re-run the workspace filter the
        # list view applies. filter_hits_by_workspace takes hit-shaped dicts.
        hit = {"_id": item.get("_id", ""), "_source": item}
        return bool(RoleFilterService.filter_hits_by_workspace([hit]))
    except Exception as e:  # noqa: BLE001 — a security gate must fail closed
        logging.warning("company-tier boundary check failed for %s: %s", asset_type, e)
        return False


def _asset_type_for_index(index_name: str) -> str | None:
    """reverse-resolve an ES index name to its ASSET_CONFIGS
    asset_type, or ``None`` if the index is not a known asset index.

    Used by the ``/api/options/<index_name>`` routes to reject arbitrary
    client-supplied index names (the route param is otherwise passed straight
    into a raw ES fetch) and to pick the right scope-narrowing path.
    """
    for atype, cfg in ASSET_CONFIGS.items():
        if cfg.get("index") == index_name:
            return atype
    return None


def _scope_filter_asset_hits(asset_type: str, hits: list) -> list:
    """apply the caller's RoleFilterService scope to a list of asset
    hits, mirroring the per-type narrowing in :func:`list_assets`.

    Centralizes the three branches so the ``/api/options`` routes enforce the
    exact same boundary the HTML list view does:

      * teams / organizations → no-scope deny-all, then title-allow-list +
         workspace tenant-boundary.
      * workspaces → member-of ∪ created-by ∪ grants visibility set.
      * data-source types → no-scope deny-all, then workspace_esid allow-list.

    Fail-closed: any error returns ``[]`` rather than leaking the unfiltered
    list.
    """
    try:
        if asset_type in ("teams", "organizations"):
            if not RoleFilterService.current_user_has_any_scope():
                return []
            if not RoleFilterService.should_apply_filter():
                return hits
            asset_config = ASSET_CONFIGS[asset_type]
            if asset_type == "teams":
                allowed_titles = RoleFilterService.get_allowed_teams_for_org_users()
            else:
                allowed_titles = RoleFilterService.get_allowed_organizations()
            if allowed_titles is not None:
                allowed_set = set(allowed_titles)
                title_field = asset_config["title_field"]
                hits = [h for h in hits if h.get("_source", {}).get(title_field) in allowed_set]
            return RoleFilterService.filter_hits_by_workspace(hits)
        if asset_type == "workspaces":
            from asset_management.web.services.workspace_visibility_service import (
                WorkspaceVisibilityService,
            )

            visible_ids = WorkspaceVisibilityService.get_visible_workspaces_for_user(
                session.get("username", "") or "",
                is_superuser=("superuser" in (session.get("roles") or [])),
            )
            return [h for h in hits if h.get("_id") in visible_ids]
        # data-source types: workspace_esid allow-list.
        # hides the data-source types from non-privileged
        # (org-tier) users on every other asset route; /api/options was the gap.
        if not _data_sources_visible_to(session):
            return []
        if not RoleFilterService.current_user_has_any_scope():
            return []
        if not RoleFilterService.should_apply_filter():
            return hits
        allowed_workspaces = RoleFilterService.get_allowed_workspaces()
        if allowed_workspaces is None:
            return hits
        allowed_ws_set = set(allowed_workspaces)
        return [h for h in hits if h.get("_source", {}).get("workspace_esid") in allowed_ws_set]
    except Exception as e:  # noqa: BLE001 — a security filter must fail closed
        logging.warning("_scope_filter_asset_hits failed for %s: %s", asset_type, e)
        return []


def _reference_esid_in_user_scope(source_index: str, esid: str) -> bool:
    """may the current user point an asset at ``esid`` in ``source_index``?

    ``FormService.process_form_data`` copies a posted select / multi-select
    value straight into the doc as an ``*_esid`` reference. Without this check
    a workspace owner could post a foreign tenant's org / page / account id and
    attach their asset to it. The referenced doc is loaded and run through the
    same per-object gates the direct-object routes use, so a reference is
    accepted exactly when the caller could open that doc in the Explorer.

    Global lookup tables (countries / regions) are not tenant-owned and pass.
    A missing doc, an unknown index, or any lookup error denies.
    """
    if FormService.is_reference_data_source(source_index):
        return True
    ref_type = _asset_type_for_index(source_index)
    if ref_type is None:
        return False
    try:
        if ref_type == "workspaces":
            allowed_workspaces = RoleFilterService.get_allowed_workspaces()
            return allowed_workspaces is not None and esid in set(allowed_workspaces)
        doc = AssetService.get_by_id(source_index, esid)
        if not doc:
            return False
        item = {**doc, "_id": esid}
        if ref_type in ("teams", "organizations"):
            return _company_tier_asset_in_user_scope(ref_type, item)
        return _data_source_asset_in_user_workspace(ref_type, item)
    except Exception as e:  # noqa: BLE001 — a security gate must fail closed
        logging.warning("reference scope check failed for %s/%s: %s", source_index, esid, e)
        return False


def _as_esid_list(value) -> list:
    """Normalize a stored / posted ``*_esid`` value (str or list) to a list."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v]
    return [value]


def _out_of_scope_reference(asset_config: dict, data: dict, existing: dict | None = None) -> str | None:
    """return the label of the first reference field in ``data`` that
    names a doc outside the caller's scope, or ``None`` when all are in scope.

    On edit, only esids the post ADDS are checked — a reference already stored
    on the doc was accepted when it was written, and re-checking it would lock
    a user out of saving an unrelated change to a legacy doc.
    """
    for field in asset_config["fields"]:
        if not (field.get("store_esid") and field.get("source")):
            continue
        esid_field = field.get("esid_field", field["name"].replace("_title", "_esid"))
        if esid_field not in data:
            continue
        already = set(_as_esid_list((existing or {}).get(esid_field)))
        for esid in _as_esid_list(data[esid_field]):
            if esid in already:
                continue
            if not _reference_esid_in_user_scope(field["source"], esid):
                return field.get("label") or field["name"]
    return None


def _strip_superuser_only_fields(asset_config: dict, data: dict) -> None:
    """drop superuser-only values (and their edit-mode
    ``__delete__`` markers) from processed form data, in place.

    For ``store_esid`` select fields, ``process_form_data`` writes BOTH the
    title (``name``) and the load-bearing ESID (``esid_field``); both go.
    """
    for field in asset_config["fields"]:
        if not field.get("superuser_only"):
            continue
        names = [field["name"]]
        if field.get("store_esid"):
            names.append(field.get("esid_field", field["name"].replace("_title", "_esid")))
        for name in names:
            data.pop(name, None)
            data.pop(f"__delete__{name}", None)


@app.route("/asset/<asset_type>")
def list_assets(asset_type):
    """List all assets of a specific type.

    the ``teams`` and ``organizations`` list views are
    scope-defining surfaces — they must respect the user's
    RoleFilterService scope (same as ``get_asset_list`` at
    ``/api/tree/assets/<asset_type>``). Without this, an asset_admin
    with no relevant membership or grant could enumerate every
    tenant's teams/orgs through the list view at
    ``/assets/asset/teams``.

     (F7 co-required PR): the ``organizations`` asset type
    is hidden from non-agency users — 404 here matches the tree-view
    suppression in ``get_tree_structure``.
    """
    if asset_type not in ASSET_CONFIGS:
        return "Asset type not found", 404
    if asset_type == "organizations" and not _organizations_visible_to(session):
        return "Asset type not found", 404
    # Workspaces tier is system_admin-only. 404 mirrors the
    # organizations gate so a curl against /asset/workspaces from a
    # non-admin session is indistinguishable from "wrong asset type".
    if asset_type == "workspaces" and not _workspaces_visible_to(session):
        return "Asset type not found", 404
    # data-source types (everything outside the company tier) are
    # hidden from non-privileged (org-tier) users — they reach their own
    # sources via Data Connectors. 404 matches the organizations/workspaces
    # gates so a curl is indistinguishable from "wrong asset type".
    if asset_type not in _DATA_SOURCE_SYSTEM_TYPES and not _data_sources_visible_to(session):
        return "Asset type not found", 404

    asset_config = ASSET_CONFIGS[asset_type]
    hits = AssetService.get_all(asset_config["index"])

    # Phase B.4 scope filter — mirror the get_asset_list
    # pattern at app.py:3635. Caveats:
    #   * ``current_user_has_any_scope`` False → no-scope deny-all (B.4):
    #     a bare user with no roles AND no memberships sees zero
    #     teams/orgs, not the full list. Pre-B.4 the
    #     ``should_apply_filter`` False branch leaked everything.
    #   * ``should_apply_filter`` True → narrow to ``allowed_titles``.
    #     ``allowed_titles is not None`` admits empty list (privileged
    #     user with no scope → no items).
    if asset_type in ("teams", "organizations"):
        if not RoleFilterService.current_user_has_any_scope():
            hits = []
        elif RoleFilterService.should_apply_filter():
            if asset_type == "teams":
                allowed_titles = RoleFilterService.get_allowed_teams_for_org_users()
            else:
                allowed_titles = RoleFilterService.get_allowed_organizations()
            if allowed_titles is not None:
                allowed_set = set(allowed_titles)
                title_field = asset_config["title_field"]
                hits = [h for h in hits if h.get("_source", {}).get(title_field) in allowed_set]
            # tenant-boundary by workspace_esid (titles collide
            # across workspaces). Second layer after the title filter;
            # mirrors get_asset_list (the JSON tree backend) so the HTML
            # list and the tree counts agree. Fail-closed on missing field.
            hits = RoleFilterService.filter_hits_by_workspace(hits)
    elif asset_type == "workspaces":
        # workspaces are filtered by the (member-of) ∪
        # (created-by) set, NOT by the workspace_esid field on the doc
        # (the doc IS the workspace; there is no parent workspace
        # reference). Even a system_admin sees only their own
        # workspaces — the visibility gate above already 404s
        # non-admins, so this branch is the per-admin narrowing layer.
        try:
            from asset_management.web.services.workspace_visibility_service import (
                WorkspaceVisibilityService,
            )

            # superuser tier visibility narrows to created-by ∪ grants.
            visible_ids = WorkspaceVisibilityService.get_visible_workspaces_for_user(
                session.get("username", "") or "",
                is_superuser=("superuser" in (session.get("roles") or [])),
            )
        except Exception as e:
            logging.warning("list_assets visibility lookup failed: %s", e)
            visible_ids = set()
        hits = [h for h in hits if h["_id"] in visible_ids]
    else:
        # per-tenant isolation for the 13 data-source asset
        # types. Each asset doc carries ``workspace_esid`` (Phase A);
        # filter ``hits`` to the user's allowed workspace set. Mirrors
        # the teams/organizations branch above:
        #   * No-scope user → empty hits (same deny-all semantic as
        # Phase B.4 enforces for teams/orgs).
        #   * Privileged user with no workspace → empty list (filter
        #     applied, admits nothing). ``allowed_workspaces is None``
        #     means "no workspace filter to apply" — pass through
        #     unfiltered, same as ``allowed_titles is None`` above.
        # Docs that LACK a workspace_esid value (pre-backfill) match
        # nothing under this filter — that's intentional. Run
        # ``tools.backfill_asset_workspace_esid --apply`` to tag every
        # existing doc with the Kavanah singleton; subsequent organic
        # creates carry workspace_esid automatically.
        if not RoleFilterService.current_user_has_any_scope():
            hits = []
        elif RoleFilterService.should_apply_filter():
            allowed_workspaces = RoleFilterService.get_allowed_workspaces()
            if allowed_workspaces is not None:
                allowed_ws_set = set(allowed_workspaces)
                hits = [h for h in hits if h.get("_source", {}).get("workspace_esid") in allowed_ws_set]

    # Enrich first — enrich_for_list_display reads <field>_esids arrays,
    # which prepare_for_display strips. Reversing the order would silently
    # leave display_format-driven cells empty (em-dash) on every row.
    raw_items = []
    for hit in hits:
        item = hit["_source"]
        item["_id"] = hit["_id"]
        raw_items.append(item)
    raw_items = FormService.enrich_for_list_display(raw_items, asset_config)
    # fill the Workspace column's workspace_title from workspace_esid
    # (the backfill stamps only the esid). Must run before prepare_for_display
    # so the resolved title is present when the row is rendered.
    raw_items = FormService.enrich_workspace_titles(raw_items, asset_config)

    # Now mask encrypted fields and drop ESID arrays. The enriched
    # `<field>__display_items` payloads survive (they don't end in _esid).
    items = [FormService.prepare_for_display(item, asset_config) for item in raw_items]

    return render_template("list.html", asset_type=asset_type, config=asset_config, items=items)


@app.route("/asset/<asset_type>/add", methods=["GET", "POST"])
def add_asset(asset_type):
    """Add a new asset."""
    if asset_type not in ASSET_CONFIGS:
        return "Asset type not found", 404
    # code-managed types (TikTok accounts) are projections of
    # de_connection: no Explorer add / edit.
    if ASSET_CONFIGS[asset_type].get("code_managed"):
        return "This asset type is managed by DEAM and cannot be changed here.", 403
    if asset_type == "organizations" and not _organizations_visible_to(session):
        # defense-in-depth: bypassing the tree-view by direct URL
        # to /asset/organizations/add still 404s for non-agency users.
        return "Asset type not found", 404
    # defense-in-depth on the add path. Without this, a
    # non-admin who knew the URL could POST to /asset/workspaces/add
    # and create a workspace they shouldn't have access to.
    if asset_type == "workspaces" and not _workspaces_visible_to(session):
        return "Asset type not found", 404
    # same defense-in-depth on the add path for the data-source
    # types — a non-privileged user can't GET the form or POST a create.
    if asset_type not in _DATA_SOURCE_SYSTEM_TYPES and not _data_sources_visible_to(session):
        return "Asset type not found", 404

    asset_config = ASSET_CONFIGS[asset_type]

    if request.method == "POST":
        import logging

        # non-superusers don't see the superuser_only workspace
        # field on the form, so request.form has no workspace_title /
        # workspace_esid key — validate_required_fields would then
        # produce "Workspace (superuser only) is required". Inject the
        # server-resolved default before validation runs. The
        # strip block below still deletes the values from ``data`` for
        # non-superusers, and the unconditional pre-block at
        # app.py:1190 writes the authoritative workspace_esid onto the
        # final doc — so this injection is only for validation correctness,
        # not the persisted state. (Crafted POSTs that include
        # workspace_esid still hit the strip and get overwritten.)
        _post_is_superuser = "superuser" in (session.get("roles") or [])
        if not _post_is_superuser:
            form_data = request.form.copy()
            if not form_data.get("workspace_esid") and not form_data.get("workspace_title"):
                from lib.es_helpers import (
                    get_elasticsearch_client as _post_es,
                    resolve_creator_workspace_esid as _post_resolve,
                )

                _es_client_2 = _post_es()
                if _es_client_2 is not None:
                    try:
                        _ws_3 = _post_resolve(_es_client_2, session.get("username", ""))
                    except Exception as e:
                        logging.warning("POST workspace default resolution failed: %s", e)
                        _ws_3 = None
                    if _ws_3:
                        form_data["workspace_esid"] = _ws_3
                        form_data["workspace_title"] = _ws_3
        else:
            form_data = request.form

        # Validate required fields
        is_valid, validation_error = FormService.validate_required_fields(form_data, asset_config, mode="add")
        chatwoot_base_url = None
        if is_valid and asset_type == "chatwoot_accounts":
            chatwoot_base_url, validation_error = _vet_chatwoot_base_url(form_data)
            if validation_error is None:
                validation_error = _verify_chatwoot_form(form_data, chatwoot_base_url)
            is_valid = validation_error is None
        echo_subdomain = None
        if is_valid and asset_type == "echo_tenants":
            echo_subdomain, validation_error = _vet_echo_subdomain(form_data)
            is_valid = validation_error is None
        if not is_valid:
            select_options = FormService.get_select_options(
                asset_config, allowed_workspaces=RoleFilterService.get_allowed_workspaces()
            )
            return (
                render_template(
                    "form.html",
                    asset_type=asset_type,
                    config=asset_config,
                    mode="add",
                    select_options=select_options,
                    item=dict(form_data),
                    error_message=validation_error,
                ),
                400,
            )

        data = FormService.process_form_data(form_data, asset_config, mode="add")
        if chatwoot_base_url is not None:
            data["chatwoot_base_url"] = chatwoot_base_url
        if echo_subdomain is not None:
            data["echo_tenant_subdomain"] = echo_subdomain

        # server-side gate on superuser_only fields. Strip
        # values submitted by non-superusers (the template hides the
        # field, but a crafted POST could include workspace_esid
        # otherwise and bypass the auto-tagger's creator-primary
        # resolution). Defense-in-depth alongside the template gate.
        #
        # For store_esid select fields, process_form_data writes BOTH
        # ``name`` (title) and ``esid_field`` (the load-bearing ESID)
        # into ``data``. Strip both — leaving just one would still
        # corrupt the doc (orphan title with no ESID, or ESID without
        # a display title).
        _is_superuser_2 = "superuser" in (session.get("roles") or [])
        if not _is_superuser_2:
            for _field_2 in asset_config["fields"]:
                if not _field_2.get("superuser_only"):
                    continue
                data.pop(_field_2["name"], None)
                if _field_2.get("store_esid"):
                    _esid_2 = _field_2.get(
                        "esid_field",
                        _field_2["name"].replace("_title", "_esid"),
                    )
                    data.pop(_esid_2, None)

            # every posted reference (organization, page, account…)
            # must name a doc the caller can reach — process_form_data copies
            # the select value in unchecked. Superusers keep cross-tenant writes.
            _bad_ref = _out_of_scope_reference(asset_config, data)
            if _bad_ref:
                logging.warning(
                    "%s refused add of %s: %s is outside their scope",
                    session.get("username", ""),
                    asset_type,
                    _bad_ref,
                )
                return f"The selected {_bad_ref} is not available to you.", 403

        # Handle logo upload for organizations
        if asset_type == "organizations":
            logo_path, logo_error = _handle_logo_upload("organization_logo")
            if logo_error:
                select_options = FormService.get_select_options(
                    asset_config, allowed_workspaces=RoleFilterService.get_allowed_workspaces()
                )
                return (
                    render_template(
                        "form.html",
                        asset_type=asset_type,
                        config=asset_config,
                        mode="add",
                        select_options=select_options,
                        item=dict(form_data),
                        error_message=logo_error,
                    ),
                    400,
                )
            if logo_path is not False:
                data["organization_logo"] = logo_path or ""

        # Auto-generate resource_name fields if not provided
        if asset_type == "ga_accounts":
            if not data.get("ga_account_resource_name") and data.get("ga_account_id"):
                data["ga_account_resource_name"] = f"accounts/{data['ga_account_id']}"
        elif asset_type == "ga_properties":
            if not data.get("ga_property_resource_name") and data.get("ga_property_id"):
                data["ga_property_resource_name"] = f"properties/{data['ga_property_id']}"

        # populate workspace_esid on every NEW asset doc so
        # per-tenant isolation (filter by workspace_esid) covers organic
        # creates without waiting on the backfill. resolve to
        # the CREATOR's primary workspace (not the singleton) — pre-718
        # this used resolve_default_workspace_esid which returned the
        # singleton regardless of who was adding the asset, causing a
        # user in workspace B to create assets that landed in workspace A.
        #
        # Runs for ALL asset types except workspaces itself — the 13
        # data-source indices plus teams/organizations all carry a
        # parent workspace pointer. excludes ``workspaces``: a
        # workspace doc has no parent workspace, so writing
        # workspace_esid on the new doc would be both semantically wrong
        # and would leak the creator-resolution ESID into every new
        # workspace.
        if asset_type != "workspaces":
            from lib.es_helpers import (
                get_elasticsearch_client as _get_es_3,
                resolve_creator_workspace_esid as _resolve_ws,
            )

            _es_3 = _get_es_3()
            if _es_3 is not None and "workspace_esid" not in data:
                _ws_2 = _resolve_ws(_es_3, session.get("username", ""))
                if _ws_2:
                    data["workspace_esid"] = _ws_2

        # reject create when the sanitized alias slug
        # would collide. rescopes the ORG check from cluster-wide
        # to within the parent workspace — two unrelated workspaces may
        # both contain an org titled "Acme". Team scope unchanged
        # (uniqueness within parent org). Surfaces as a 400
        # with the offending slug named.
        #
        # also populates workspace_esid + organization_slug on
        # NEW org docs so F5's backfill and the per-workspace scoping
        # work for organic creates post-backfill. Under the
        # workspace_esid write moved out of this block to cover all
        # asset types uniformly; the organization_slug write stays here.
        if asset_type in ("teams", "organizations"):
            from lib.es_helpers import (
                SlugCollisionError,
                check_org_slug_unique,
                check_team_slug_unique,
                get_elasticsearch_client,
                resolve_creator_workspace_esid,
                sanitize_team_title,
            )

            slug_check_es = get_elasticsearch_client()
            if slug_check_es is not None:
                try:
                    if asset_type == "teams":
                        raw_orgs = data.get("organization_esid") or []
                        if isinstance(raw_orgs, str):
                            raw_orgs = [raw_orgs]
                        check_team_slug_unique(
                            slug_check_es,
                            team_title=data.get("team_title", ""),
                            org_esids=[x for x in raw_orgs if x],
                        )
                    else:  # organizations
                        # scope the slug-uniqueness check to
                        # the CREATOR's workspace, not the singleton.
                        # Pre-718 every new org's slug-uniqueness was
                        # checked against the singleton's namespace,
                        # producing false collisions for orgs created
                        # under non-singleton workspaces (and false
                        # admissions for orgs that would collide within
                        # the creator's actual target workspace).
                        #
                        # prefer the workspace the form actually
                        # TARGETS (``data["workspace_esid"]`` — a superuser's
                        # selection in the superuser-only Workspace field,
                        # preserved through the pre-block) over the
                        # creator's own resolved workspace. Without this, a
                        # superuser creating an org in workspace B had its
                        # uniqueness checked against workspace A (their home),
                        # so an org named "ACME" that already exists in A
                        # falsely collided even though B has none. Symmetric
                        # with the stamp guard below, which already
                        # respects the form selection. Falls back to the
                        # creator-resolved workspace when the form set none.
                        workspace_esid = data.get("workspace_esid") or resolve_creator_workspace_esid(
                            slug_check_es, session.get("username", "")
                        )
                        check_org_slug_unique(
                            slug_check_es,
                            organization_title=data.get("organization_title", ""),
                            workspace_esid=workspace_esid,
                        )
                        # populate organization_slug on the
                        # new org doc so the per-workspace scoping works
                        # for organic creates. The workspace_esid write
                        # used to live here too; it moved
                        # to the unconditional pre-block above so all
                        # asset types share one code path. This fallback
                        # write stays as belt-and-suspenders for when the
                        # pre-block degrades (ES outage) but this slug-check
                        # ES call still succeeds.
                        #
                        # MUST respect an already-present
                        # workspace_esid. A superuser's form selection (the
                        # superuser_only Workspace field) lands in
                        # ``data["workspace_esid"]`` and survives the
                        # pre-block (guarded on ``not in data``). Without the
                        # same guard here, this line unconditionally overwrote
                        # the selection with the CREATOR's primary workspace —
                        # so a superuser creating an org in another workspace
                        # had it silently stamped to their own. (Hit during the
                        # tripwire setup: Acme org selected
                        # control-workspace, landed in kavanah-media.) Only
                        # fall back to the creator-resolved value when the form
                        # didn't already set one.
                        if workspace_esid and "workspace_esid" not in data:
                            data["workspace_esid"] = workspace_esid
                        proposed_slug = sanitize_team_title(data.get("organization_title", ""))
                        if proposed_slug and proposed_slug != "unknown":
                            data["organization_slug"] = proposed_slug
                except SlugCollisionError as exc:
                    select_options = FormService.get_select_options(
                        asset_config, allowed_workspaces=RoleFilterService.get_allowed_workspaces()
                    )
                    err_msg = (
                        f"A {asset_type[:-1]} with a similar name already exists. "
                        f"Sanitized slugs collide: {exc.slug!r}. Pick a different name."
                    )
                    return (
                        render_template(
                            "form.html",
                            asset_type=asset_type,
                            config=asset_config,
                            mode="add",
                            select_options=select_options,
                            item=dict(form_data),
                            error_message=err_msg,
                        ),
                        400,
                    )

        # workspace-create finalization. The form collected
        # workspace_title + (optional) is_agency/timezone/currency/
        # fiscal_year_start_month. We:
        #   1. Derive workspace_slug from the title (read-only, never
        #      a form field; deterministic via slugify).
        #   2. Run the availability check (RESERVED_SLUGS + collision
        #      against existing workspaces + active wizard sessions).
        #   3. Sanitize timezone/currency/month to known-good defaults
        #      via WorkspaceSettingsService.sanitize.
        #   4. Coerce is_agency to a real bool (the form sends "true"/
        #      "false" strings).
        #   5. Stamp created_by + created_at + is_multi_workspace_user_supported.
        # The membership + role-assignment writes happen AFTER the doc
        # is created (need the new ESID).
        _workspace_post_create = None  # set below when we want post-create writes
        if asset_type == "workspaces":
            from asset_management.web.services.workspace_slug_service import (
                WorkspaceSlugService,
                slugify,
            )
            from asset_management.web.services.workspace_settings_service import (
                WorkspaceSettingsService,
            )
            from datetime import datetime as _dt_2

            title = data.get("workspace_title", "").strip()
            slug = slugify(title)
            availability = WorkspaceSlugService.check_availability(title, include_suggestions=True)
            if not availability.available:
                # Same render-form-with-error pattern as the org/team
                # slug-check branch above. The form does not show a
                # slug field, so the error names the title and the
                # suggestions list.
                select_options = FormService.get_select_options(
                    asset_config, allowed_workspaces=RoleFilterService.get_allowed_workspaces()
                )
                if availability.reason == "reserved":
                    err_msg = f"That workspace name maps to a reserved slug ({slug!r}). Try one of: {', '.join(availability.suggestions) or '<no suggestions>'}."
                elif availability.reason == "taken":
                    err_msg = f"A workspace with that name already exists ({slug!r}). Try one of: {', '.join(availability.suggestions) or '<no suggestions>'}."
                elif availability.reason == "empty":
                    err_msg = "Workspace title cannot be empty."
                elif availability.reason == "too_short":
                    err_msg = "Workspace title is too short — needs at least 2 letters/numbers after slugification."
                else:
                    err_msg = f"Workspace name unavailable: {availability.reason}."
                return (
                    render_template(
                        "form.html",
                        asset_type=asset_type,
                        config=asset_config,
                        mode="add",
                        select_options=select_options,
                        item=dict(form_data),
                        error_message=err_msg,
                    ),
                    400,
                )

            settings = WorkspaceSettingsService.sanitize(
                data.get("timezone"),
                data.get("currency"),
                data.get("fiscal_year_start_month"),
            )
            # Coerce is_agency from the "true"/"false" select option.
            is_agency_raw = (data.get("is_agency") or "false").strip().lower()
            is_agency = is_agency_raw == "true"
            # same coercion for the superuser-only AI Assistant
            # toggle. Non-superuser submissions never carry the field (the
            # strip above popped it), so they land here as False.
            ai_enabled_raw = str(data.get("ai_assistant_enabled") or "false").strip().lower()
            ai_assistant_enabled = ai_enabled_raw == "true"

            creator_username = (session.get("username") or "").strip() or "unknown"

            # Replace the raw form data with the canonical doc shape.
            # Drop the inbound raw form fields and rebuild — the
            # AssetService.create_with_error call below uses this dict
            # as-is, so any field we want on the doc must live here.
            data = {
                "workspace_title": title,
                "workspace_slug": slug,
                "is_agency": is_agency,
                "ai_assistant_enabled": ai_assistant_enabled,
                "is_multi_workspace_user_supported": False,
                "timezone": settings.timezone,
                "currency": settings.currency,
                "fiscal_year_start_month": settings.fiscal_year_start_month,
                "created_by": creator_username,
                "created_at": _dt_2.utcnow().isoformat() + "Z",
            }
            # Signal to the post-create branch below that we need to
            # write the membership + role-assignment docs for the
            # creator. Carry the username through (session might
            # re-resolve to a different value post-create in theory).
            _workspace_post_create = creator_username

        # workspace docs use ``_id == workspace_slug`` as the
        # natural key (documented at permissions_index_service.py:39).
        # The bootstrap path has always honored this; the Asset Explorer
        # add path was previously auto-generating an ES id which left
        # UI-created workspaces with ``_id != workspace_slug`` and broke
        # every downstream consumer that did ``es.get(id=slug)`` or
        # treated ``workspace_esid == workspace_slug`` (notably
        # UserRoleAssignmentService.assign and api_assign_role; see
        # description for the full break list).
        create_doc_id = data["workspace_slug"] if asset_type == "workspaces" else None
        success, error_msg, new_item_id = AssetService.create_with_error(asset_config["index"], data, doc_id=create_doc_id)
        if success:
            # Auto-create GA metrics index when team is assigned to new property
            if asset_type == "ga_properties" and data.get("team_esid"):
                from lib.es_helpers import create_ga_metrics_index_for_team

                create_ga_metrics_index_for_team(data["team_esid"])

            # after a successful workspace create, write the
            # creator's membership + workspace_owner role assignment.
            # Membership uses is_primary=False — the creator already has
            # a primary workspace from their original signup; this new
            # workspace is a secondary tenant they admin. Without these
            # writes the creator would lose visibility of the workspace
            # they just made the moment a future code path filters by
            # membership only (created_by is belt-and-suspenders).
            if asset_type == "workspaces" and _workspace_post_create:
                try:
                    from lib.es_helpers import get_elasticsearch_client
                    from asset_management.web.services.onboarding_service import (
                        OnboardingService,
                    )

                    _es_4 = get_elasticsearch_client()
                    if _es_4 is not None:
                        OnboardingService._upsert_workspace_membership(
                            _es_4,
                            _workspace_post_create,
                            new_item_id,
                            is_primary=False,
                        )
                        OnboardingService._upsert_workspace_role_assignment(
                            _es_4,
                            _workspace_post_create,
                            new_item_id,
                            "workspace_owner",
                        )
                except Exception as e:
                    # The workspace doc landed; surfacing a 500 here
                    # would leave an orphan workspace doc with no
                    # creator membership. Log + continue is the right
                    # tradeoff: the operator can repair via a CLI tool
                    # later, and the workspace is at least discoverable
                    # through the created_by leg of WorkspaceVisibility.
                    logging.error(
                        "post-create membership write failed for workspace %s: %s",
                        new_item_id,
                        e,
                    )

            # legacy auto-create of org_*/team_* ES Security
            # roles on org/team creation removed. The new authz model
            # handles scope membership via de_authz_user_*_role; ES
            # Security role names are no longer the authoritative scope.

            # post-create visibility warning for superusers.
            # earlier/668 a superuser's Explorer scope is
            # memberships ∪ grants ∪ workspace-role expansion — so an
            # org/team created into a workspace they hold no role or
            # grant for lands OUTSIDE their own scope and silently
            # vanishes from the list view they're redirected to
            # ("I just created org X and it's gone"). Re-run the exact
            # list-view predicate against the new doc (create used
            # refresh=wait_for, so the workspace-expansion scans see
            # it) and carry a warning flag on the redirect when the
            # creator can't see what they just made. Superuser-only:
            # non-superusers are scoped by membership and always land
            # in their own workspace (strip +
            # auto-tagger), so the warning is irrelevant noise for them.
            _warn_params = {}
            if asset_type in ("teams", "organizations") and "superuser" in (session.get("roles") or []):
                if not _company_tier_asset_in_user_scope(asset_type, {**data, "_id": new_item_id}):
                    _warn_params = {
                        "su_hidden": "1",
                        "su_hidden_title": data.get(asset_config["title_field"], ""),
                    }

            # Save (stay) on the ADD form: the item now exists, so
            # staying means landing on its EDIT form (subsequent saves are
            # edits of the new doc). Checked before the from_tree/list
            # redirects; the FB-App-User oauth_redirect branch below still
            # wins over this (its submit carries no save_action).
            if request.form.get("save_action") == "save":
                return redirect(url_for("edit_asset", asset_type=asset_type, item_id=new_item_id, saved="1"))

            # tree explorer no longer expands asset-type rows into
            # an item list, so there's nothing for the explorer to "select"
            # after add. Send the user to the table view at /asset/<type>
            # where the new row is visible.
            from_tree = request.args.get("from_tree", "false") == "true"
            if from_tree:
                return redirect(url_for("list_assets", asset_type=asset_type, **_warn_params))
            # For FB App Users: if OAuth button clicked, save then redirect straight to Facebook auth
            if asset_type == "facebook_app_users" and request.form.get("oauth_redirect") == "1":
                saved_item = AssetService.get_by_id(asset_config["index"], new_item_id)
                if saved_item:
                    return redirect(_build_fb_app_user_oauth_url(saved_item))
            # For asset types with a computed_display block (FB App Users, YouTube
            # Accounts) redirect to the edit page so the OAuth URL banner is
            # immediately visible.
            if asset_config.get("computed_display"):
                return redirect(url_for("edit_asset", asset_type=asset_type, item_id=new_item_id))
            return redirect(url_for("list_assets", asset_type=asset_type, **_warn_params))

        logging.error(f"Failed to add {asset_type}: {error_msg}")
        return (
            render_template(
                "error.html",
                error_code=500,
                error_title="Failed to Add Asset",
                error_message=f"Could not save the {asset_config['title'].lower()}. {error_msg}",
            ),
            500,
        )

    # GET request - render form
    select_options = FormService.get_select_options(
        asset_config, allowed_workspaces=RoleFilterService.get_allowed_workspaces()
    )

    # pre-fill workspace_esid on the add form so the
    # required workspace selector lands on a sane default (creator's
    # primary workspace, falling back to the first workspace the user
    # can see). Mirrors the auto-tagger's default at app.py:1271 —
    # same resolve_creator_workspace_esid call.
    #
    # broadened this from "superuser only" to "every persona":
    # non-superusers don't see the workspace_title field (the template
    # hides it via the superuser_only gate), but the
    # validate_required_fields check at app.py:1101 still consults
    # workspace_esid on the POST. Without a pre-filled value the
    # required-field rule fires for non-superuser team creators and
    # produces "Workspace (superuser only) is required". The POST
    # branch re-applies the same default before validation runs (see
    # above) so a hand-crafted submission without the hidden input is
    # also covered.
    initial_item: dict = {}
    from lib.es_helpers import (
        get_elasticsearch_client as _get_es_4,
        resolve_creator_workspace_esid as _resolve_primary,
    )

    _es_5 = _get_es_4()
    default_ws_esid: str | None = None
    if _es_5 is not None:
        try:
            default_ws_esid = _resolve_primary(_es_5, session.get("username", ""))
        except Exception as e:
            logging.warning("resolve_creator_workspace_esid failed: %s", e)
    if not default_ws_esid:
        # Fallback: first visible workspace. Uses the same
        # visibility helper as the workspace tier itself.
        try:
            from asset_management.web.services.workspace_visibility_service import (
                WorkspaceVisibilityService,
            )

            # superuser tier visibility narrows to created-by ∪ grants.
            visible = WorkspaceVisibilityService.get_visible_workspaces_for_user(
                session.get("username", "") or "",
                is_superuser=("superuser" in (session.get("roles") or [])),
            )
            if visible:
                default_ws_esid = next(iter(visible))
        except Exception as e:
            logging.warning("visible workspace fallback failed: %s", e)
    if default_ws_esid:
        initial_item["workspace_esid"] = default_ws_esid
        initial_item["workspace_title"] = default_ws_esid

    # pre-fill organization_title / organization_esid for the
    # teams form when the current user is non-superuser AND not in an
    # agency workspace. These users see the org field as read-only
    # chips (form.html branch) and the chips need values.
    if asset_type == "teams":
        _is_superuser_4 = "superuser" in (session.get("roles") or [])
        _is_agency = False
        if not _is_superuser_4:
            try:
                from asset_management.web.services.workspace_membership_service import (
                    WorkspaceMembershipService as _wms_2,
                )

                _is_agency = _wms_2.is_agency_for_user(session.get("username", "") or "")
            except Exception as e:
                logging.warning("is_agency_for_user failed: %s", e)
        if not _is_superuser_4 and not _is_agency:
            allowed_org_titles = RoleFilterService.get_allowed_organizations()
            # ``None`` means "no filter" — only happens for genuinely
            # unscoped users (kibana_admin etc.); those don't reach the
            # team form. Treat None as empty here so we never auto-fill
            # the full org list for a privileged-but-unscoped user.
            allowed_set = set(allowed_org_titles or [])
            if allowed_set:
                org_hits = AssetService.get_all("de_asset_organization")
                matched_esids: list[str] = []
                matched_titles: list[str] = []
                for hit in org_hits:
                    src = hit.get("_source") or {}
                    title = src.get("organization_title", "")
                    if title in allowed_set:
                        matched_esids.append(hit["_id"])
                        matched_titles.append(title)
                # The multi_select widget reads from ``{name}_esids`` /
                # ``{name}_titles`` (pipe-delimited strings via
                # prepare_for_edit's convention). Match that shape so
                # the existing template + JS path works unchanged for
                # both the read-only chip branch and the legacy widget.
                initial_item["organization_esid"] = matched_esids
                initial_item["organization_title"] = matched_titles
                initial_item["organization_title_esids"] = "|||".join(matched_esids)
                initial_item["organization_title_titles"] = "|||".join(matched_titles)

    # ``is_superuser`` is already injected globally via
    # ``inject_user`` context_processor. The template's superuser_only
    # field gate consults it without an explicit kwarg here.
    return render_template(
        "form.html",
        asset_type=asset_type,
        config=asset_config,
        mode="add",
        select_options=select_options,
        item=initial_item,
    )


@app.route("/asset/<asset_type>/edit/<item_id>", methods=["GET", "POST"])
def edit_asset(asset_type, item_id):
    """Edit an existing asset."""
    if asset_type not in ASSET_CONFIGS:
        return "Asset type not found", 404
    # code-managed types (TikTok accounts) are projections of
    # de_connection: no Explorer add / edit.
    if ASSET_CONFIGS[asset_type].get("code_managed"):
        return "This asset type is managed by DEAM and cannot be changed here.", 403
    if asset_type == "organizations" and not _organizations_visible_to(session):
        return "Asset type not found", 404
    # Workspaces tier is system_admin-only. 404 mirrors the
    # organizations gate so a curl against /asset/workspaces from a
    # non-admin session is indistinguishable from "wrong asset type".
    if asset_type == "workspaces" and not _workspaces_visible_to(session):
        return "Asset type not found", 404  # defense-in-depth
    # data-source types are hidden from non-privileged (org-tier)
    # users. The per-item gate below also denies, but the type-level
    # 404 here keeps the edit path consistent with list/add and the
    # organizations/workspaces gates.
    if asset_type not in _DATA_SOURCE_SYSTEM_TYPES and not _data_sources_visible_to(session):
        return "Asset type not found", 404

    asset_config = ASSET_CONFIGS[asset_type]

    # Get existing item
    existing_item = AssetService.get_by_id(asset_config["index"], item_id)
    if not existing_item:
        return "Item not found", 404

    # a system_admin can only edit a workspace they're a
    # member of OR that they created. Without this, knowing an arbitrary
    # workspace _id (e.g. by guessing slugs or via the API) would
    # let any admin edit any workspace.
    if asset_type == "workspaces":
        try:
            from asset_management.web.services.workspace_visibility_service import (
                WorkspaceVisibilityService,
            )

            # superuser tier visibility narrows to created-by ∪ grants.
            _visible_2 = WorkspaceVisibilityService.get_visible_workspaces_for_user(
                session.get("username", "") or "",
                is_superuser=("superuser" in (session.get("roles") or [])),
            )
            if item_id not in _visible_2:
                return "Item not found", 404
        except Exception as e:
            logging.warning("edit_asset visibility check failed: %s", e)
            return "Item not found", 404

    # company-tier (teams/organizations) per-object scope check. The
    # data-source helper below short-circuits these types to allow, and
    # the only other gate is the fail-open _organizations_visible_to type check —
    # so a scoped admin who knew a foreign team/org _id could load and overwrite
    # it. Mirror the list-view narrowing. workspaces handled above.
    if asset_type in ("teams", "organizations"):
        if not _company_tier_asset_in_user_scope(asset_type, {**existing_item, "_id": item_id}):
            return "Item not found", 404

    # enforce the workspace boundary on the edit route for the
    # data-source asset types — covering BOTH the GET (render the form) and
    # the POST (persist the change), since both flow through here. Without
    # this an admin in workspace A who knew workspace B's asset _id could
    # load and overwrite it. Placed before the POST handler below so a
    # crafted POST is rejected, not just the form render. 404 matches the
    # workspaces gate directly above.
    if not _data_source_asset_in_user_workspace(asset_type, existing_item):
        return "Item not found", 404

    # org admins editing an auto-join-enabled org see a small
    # accept/decline activity panel. Only orgs that have opted in (non-
    # empty auto_join_email_domains) get the lookup, so no audit-query
    # cost is paid for orgs that haven't used the feature. Failure of
    # the lookup is non-fatal — the page renders without the panel.
    auto_join_stats = None
    if asset_type == "organizations" and existing_item.get("auto_join_email_domains"):
        try:
            from asset_management.web.services.auto_join_report_service import (
                AutoJoinReportService,
            )

            auto_join_stats = AutoJoinReportService.get_stats(item_id)
        except Exception:
            # noqa: BLE001 — panel is observability; never break the page
            auto_join_stats = None

    # Phase 4 — teams get an "Ad accounts" panel rendered from the
    # effective-dated fb_ad_account_links (which account(s) the team runs ads
    # from, and when). Operators with the Data Connectors permission also get
    # the manage controls (close / add / edit dates) — the correction path
    # that used to require Kibana Dev Tools. Fail-soft like auto_join_stats:
    # a lookup failure renders the page without the panel.
    # the ad-account link panel lives on the Edit Facebook Page form
    # ONLY (a teammate: ads attach to pages, so that is where operators look). It
    # was on the team form Phase 4 and has been removed from there.
    # Links stay TEAM-scoped, so build_panel_for_page resolves the page's owning
    # team and returns that team's links, labeled with whose they are.
    ad_account_links = None
    if asset_type == "facebook_pages":
        try:
            try:
                from web.services.team_ad_account_link_service import build_panel_for_page
            except ImportError:
                from .services.team_ad_account_link_service import (  # type: ignore[no-redef]
                    build_panel_for_page,
                )

            _perms = RolePermissionsService.get_merged_permissions(
                session.get("roles", []),
                username=session.get("username"),
            )
            ad_account_links = build_panel_for_page(
                item_id,
                existing_item,
                can_manage=bool(_perms.get("mc_data_connectors")),
            )
        except Exception:
            # noqa: BLE001 — panel is display-only; never break the form
            ad_account_links = None

    if request.method == "POST":
        # same defaulting trick as the add path — non-superusers
        # don't see the workspace_title field on the edit form, so a
        # submission carries no workspace key and validate_required_fields
        # would fire "Workspace (superuser only) is required" on edits
        # too. Inherit workspace_esid from the existing doc; the user
        # cannot reassign workspace via this surface anyway (
        # superuser_only gate).
        _edit_is_superuser = "superuser" in (session.get("roles") or [])
        if not _edit_is_superuser:
            form_data = request.form.copy()
            if not form_data.get("workspace_esid") and not form_data.get("workspace_title"):
                existing_ws_esid = existing_item.get("workspace_esid", "")
                if existing_ws_esid:
                    form_data["workspace_esid"] = existing_ws_esid
                    form_data["workspace_title"] = existing_ws_esid
        else:
            form_data = request.form

        # Validate required fields
        is_valid, validation_error = FormService.validate_required_fields(form_data, asset_config, mode="edit")
        chatwoot_base_url = None
        if is_valid and asset_type == "chatwoot_accounts":
            chatwoot_base_url, validation_error = _vet_chatwoot_base_url(form_data, existing_item)
            if validation_error is None:
                validation_error = _verify_chatwoot_form(form_data, chatwoot_base_url, existing_item)
            is_valid = validation_error is None
        echo_subdomain = None
        if is_valid and asset_type == "echo_tenants":
            echo_subdomain, validation_error = _vet_echo_subdomain(form_data)
            is_valid = validation_error is None
        if not is_valid:
            item = FormService.prepare_for_edit(existing_item, asset_config)
            select_options = FormService.get_select_options(
                asset_config, existing_item, allowed_workspaces=RoleFilterService.get_allowed_workspaces()
            )
            return (
                render_template(
                    "form.html",
                    asset_type=asset_type,
                    config=asset_config,
                    mode="edit",
                    select_options=select_options,
                    item=item,
                    item_id=item_id,
                    error_message=validation_error,
                    computed_fields=FormService.get_computed_display_fields(existing_item, asset_config, item_id),
                    auto_join_stats=auto_join_stats,
                    ad_account_links=ad_account_links,
                ),
                400,
            )

        data = FormService.process_form_data(form_data, asset_config, existing_data=existing_item, mode="edit")
        if chatwoot_base_url is not None:
            # A blank URL is stored as the explicit cloud origin, not deleted.
            data["chatwoot_base_url"] = chatwoot_base_url
        if echo_subdomain is not None:
            data["echo_tenant_subdomain"] = echo_subdomain

        # the edit path had no strip, so a non-superuser
        # could post ``workspace_title=<other workspace>`` and re-stamp the
        # asset into another tenant through the ``{**existing_item, **data}``
        # merge below. Strip the superuser-only fields and check every newly
        # posted reference against the caller's scope, as add_asset does.
        _existing_ws_2 = existing_item.get("workspace_esid")
        if not _edit_is_superuser:
            _strip_superuser_only_fields(asset_config, data)
            _bad_ref = _out_of_scope_reference(asset_config, data, existing=existing_item)
            if _bad_ref:
                logging.warning(
                    "%s refused edit of %s/%s: %s is outside their scope",
                    session.get("username", ""),
                    asset_type,
                    item_id,
                    _bad_ref,
                )
                return f"The selected {_bad_ref} is not available to you.", 403

        # the workspace ``is_agency`` flag is rendered as a
        # select with options ["false", "true"], which process_form_data
        # returns as raw strings. The create path coerces to bool at
        # app.py:1402-1404; the edit path must do the same or a flip
        # from true → false saves the literal "false" (which is truthy
        # in Python) and readers checking ``if doc.get("is_agency"):``
        # continue to treat the workspace as an agency.
        if asset_type == "workspaces" and "is_agency" in data:
            raw = data["is_agency"]
            data["is_agency"] = raw == "true" if isinstance(raw, str) else bool(raw)

        # Handle logo upload for organizations
        if asset_type == "organizations":
            logo_path, logo_error = _handle_logo_upload("organization_logo")
            if logo_error:
                item = FormService.prepare_for_edit(existing_item, asset_config)
                select_options = FormService.get_select_options(
                    asset_config, existing_item, allowed_workspaces=RoleFilterService.get_allowed_workspaces()
                )
                return (
                    render_template(
                        "form.html",
                        asset_type=asset_type,
                        config=asset_config,
                        mode="edit",
                        select_options=select_options,
                        item=item,
                        item_id=item_id,
                        error_message=logo_error,
                        computed_fields=FormService.get_computed_display_fields(existing_item, asset_config, item_id),
                        auto_join_stats=auto_join_stats,
                        ad_account_links=ad_account_links,
                    ),
                    400,
                )
            if logo_path is not False:
                # Delete the old logo file before storing the new path (or clearing it)
                _delete_logo_file(existing_item.get("organization_logo", ""))
                data["organization_logo"] = logo_path or ""

        # Handle field deletions marked with __delete__ prefix
        fields_to_delete = [k.replace("__delete__", "") for k, v in data.items() if k.startswith("__delete__") and v is True]

        for key in fields_to_delete:
            existing_item.pop(key, None)

        # Remove deletion markers from data
        data = {k: v for k, v in data.items() if not k.startswith("__delete__")}

        # Merge with existing data (preserve fields not in form)
        updated_document = {**existing_item, **data}
        # only a superuser moves an asset between workspaces; for
        # everyone else the tenant stamp is the stored one.
        if not _edit_is_superuser:
            if _existing_ws_2:
                updated_document["workspace_esid"] = _existing_ws_2
            else:
                updated_document.pop("workspace_esid", None)

        # Auto-generate resource_name fields if not provided (only if not explicitly deleted)
        if asset_type == "ga_accounts":
            if not updated_document.get("ga_account_resource_name") and updated_document.get("ga_account_id"):
                # Only auto-generate if the field wasn't explicitly cleared
                if "ga_account_resource_name" not in fields_to_delete:
                    updated_document["ga_account_resource_name"] = f"accounts/{updated_document['ga_account_id']}"
        elif asset_type == "ga_properties":
            if not updated_document.get("ga_property_resource_name") and updated_document.get("ga_property_id"):
                # Only auto-generate if the field wasn't explicitly cleared
                if "ga_property_resource_name" not in fields_to_delete:
                    updated_document["ga_property_resource_name"] = f"properties/{updated_document['ga_property_id']}"

        # reject edit when the sanitized alias slug
        # would collide. rescopes the ORG check to be per-workspace
        # — the edit reads the org's existing workspace_esid from the
        # stored doc (NOT the resolve_default_*; an admin renaming an
        # org should be checked against that org's own workspace, not the
        # current user's default). exclude_org_esid keeps self-edit a
        # no-op (renaming an org without changing its slug passes
        # through).
        if asset_type in ("teams", "organizations"):
            from lib.es_helpers import (
                SlugCollisionError,
                check_org_slug_unique,
                check_team_slug_unique,
                get_elasticsearch_client,
                sanitize_team_title,
            )

            slug_check_es = get_elasticsearch_client()
            if slug_check_es is not None:
                try:
                    if asset_type == "teams":
                        raw_orgs = updated_document.get("organization_esid") or []
                        if isinstance(raw_orgs, str):
                            raw_orgs = [raw_orgs]
                        check_team_slug_unique(
                            slug_check_es,
                            team_title=updated_document.get("team_title", ""),
                            org_esids=[x for x in raw_orgs if x],
                            exclude_team_esid=item_id,
                        )
                    else:  # organizations
                        # Read the org's existing workspace_esid from the
                        # stored doc (existing_item is the pre-edit
                        # snapshot loaded by the route handler). Falls
                        # back to None during the transition window
                        # where F5 hasn't backfilled this org yet — the
                        # check degrades to global scope in that case.
                        workspace_esid = (existing_item or {}).get("workspace_esid")
                        check_org_slug_unique(
                            slug_check_es,
                            organization_title=updated_document.get("organization_title", ""),
                            workspace_esid=workspace_esid,
                            exclude_org_esid=item_id,
                        )
                        # refresh organization_slug from the
                        # (possibly renamed) title. workspace_esid is
                        # not rewritten on edit — that's a separate
                        # "move org between workspaces" operation, not
                        # yet supported.
                        proposed_slug = sanitize_team_title(updated_document.get("organization_title", ""))
                        if proposed_slug and proposed_slug != "unknown":
                            updated_document["organization_slug"] = proposed_slug
                except SlugCollisionError as exc:
                    item = FormService.prepare_for_edit(existing_item, asset_config)
                    select_options = FormService.get_select_options(
                        asset_config, existing_item, allowed_workspaces=RoleFilterService.get_allowed_workspaces()
                    )
                    err_msg = (
                        f"A {asset_type[:-1]} with a similar name already exists. "
                        f"Sanitized slugs collide: {exc.slug!r}. Pick a different name."
                    )
                    return (
                        render_template(
                            "form.html",
                            asset_type=asset_type,
                            config=asset_config,
                            mode="edit",
                            select_options=select_options,
                            item=item,
                            item_id=item_id,
                            error_message=err_msg,
                            computed_fields=FormService.get_computed_display_fields(existing_item, asset_config, item_id),
                            auto_join_stats=auto_join_stats,
                            ad_account_links=ad_account_links,
                        ),
                        400,
                    )

        # workspace-edit sanitization. The form sends raw
        # strings for is_agency / timezone / currency /
        # fiscal_year_start_month; coerce them through the same
        # sanitizers used by add_asset (and the wizard) so the doc
        # ends up with canonical values regardless of what the user
        # typed. The immutable fields — workspace_slug, created_by,
        # created_at, is_multi_workspace_user_supported — are
        # re-stamped from existing_item to defend against form-field
        # tampering (a curl could otherwise rewrite created_by).
        if asset_type == "workspaces":
            from asset_management.web.services.workspace_settings_service import (
                WorkspaceSettingsService,
            )

            settings = WorkspaceSettingsService.sanitize(
                updated_document.get("timezone"),
                updated_document.get("currency"),
                updated_document.get("fiscal_year_start_month"),
            )
            is_agency_raw = str(updated_document.get("is_agency") or "false").strip().lower()
            updated_document["is_agency"] = is_agency_raw == "true"
            # ai_assistant_enabled is superuser-only. The template
            # hides the field for non-superusers, but the edit path has no
            # earlier generic strip — so a crafted non-superuser POST
            # could flip a spend-metered feature on. Re-stamp from the stored
            # doc unless the acting user is a superuser (same posture as the
            # immutable identity fields below).
            if _edit_is_superuser:
                _ai_raw = str(updated_document.get("ai_assistant_enabled") or "false").strip().lower()
                updated_document["ai_assistant_enabled"] = _ai_raw == "true"
            else:
                updated_document["ai_assistant_enabled"] = bool(existing_item.get("ai_assistant_enabled", False))
            updated_document["timezone"] = settings.timezone
            updated_document["currency"] = settings.currency
            updated_document["fiscal_year_start_month"] = settings.fiscal_year_start_month
            # Re-stamp immutable identity fields from the original
            # doc. workspace_slug is immutable because child orgs'
            # aliases reference it; renaming it would orphan them.
            # created_by + created_at are audit fields that never
            # change. is_multi_workspace_user_supported is governed
            # by a separate (not-yet-built) admin surface, not by
            # the Asset Explorer.
            updated_document["workspace_slug"] = existing_item.get("workspace_slug")
            updated_document["created_by"] = existing_item.get("created_by")
            updated_document["created_at"] = existing_item.get("created_at")
            updated_document["is_multi_workspace_user_supported"] = existing_item.get(
                "is_multi_workspace_user_supported", False
            )

        if AssetService.update(asset_config["index"], item_id, updated_document):
            # Auto-create GA metrics index when team is assigned to property
            if asset_type == "ga_properties":
                new_team_esid = updated_document.get("team_esid")
                old_team_esid = existing_item.get("team_esid")

                # If team was just assigned (new or changed)
                if new_team_esid and new_team_esid != old_team_esid:
                    from lib.es_helpers import create_ga_metrics_index_for_team

                    create_ga_metrics_index_for_team(new_team_esid)

            # For FB App Users: if OAuth button clicked, save then redirect straight to Facebook auth
            if asset_type == "facebook_app_users" and request.form.get("oauth_redirect") == "1":
                return redirect(_build_fb_app_user_oauth_url(updated_document))

            # Save (stay on the form) vs Save & Close (legacy
            # redirect-to-list). A missing/unknown save_action means an old
            # form or a scripted POST — keep the legacy close behavior.
            # from_tree is threaded through so a later Save & Close still
            # returns to the tree explorer.
            if request.form.get("save_action") == "save":
                _params_2 = {"saved": "1"}
                if request.args.get("from_tree", "false") == "true":
                    _params_2["from_tree"] = "true"
                return redirect(url_for("edit_asset", asset_type=asset_type, item_id=item_id, **_params_2))

            # Check if request came from tree explorer
            from_tree = request.args.get("from_tree", "false") == "true"
            if from_tree:
                return redirect(url_for("index") + "?refresh=true")
            return redirect(url_for("list_assets", asset_type=asset_type))
        return "Failed to update asset", 500

    # GET request - render form
    item = FormService.prepare_for_edit(existing_item, asset_config)
    select_options = FormService.get_select_options(
        asset_config, existing_item, allowed_workspaces=RoleFilterService.get_allowed_workspaces()
    )
    computed_fields = FormService.get_computed_display_fields(existing_item, asset_config, item_id)

    return render_template(
        "form.html",
        asset_type=asset_type,
        config=asset_config,
        mode="edit",
        select_options=select_options,
        item=item,
        item_id=item_id,
        computed_fields=computed_fields,
        auto_join_stats=auto_join_stats,
        ad_account_links=ad_account_links,
    )


def _plan_team_dashboards(team_esid):
    """read-only plan for a team's dashboards, taken before the team
    is deleted. Best-effort: None (no cleanup) on any failure."""
    try:
        from asset_management.web.services.team_dashboard_cleanup import plan_team_dashboard_cleanup
        from lib.es_helpers import get_elasticsearch_client as _get_es

        return plan_team_dashboard_cleanup(_get_es(), team_esid)
    except Exception as e:  # noqa: BLE001 — a planning failure must never block the team delete
        logging.warning("dashboard plan failed for team %s: %s", team_esid, e)
        return None


def _apply_team_dashboards(plan):
    """delete a deleted team's template dashboards per ``plan``.
    Best-effort, like the authz cascade: never fails the delete."""
    if plan is None:
        return
    try:
        from asset_management.web.services.team_dashboard_cleanup import apply_team_dashboard_cleanup

        counts = apply_team_dashboard_cleanup(plan)
        if any(counts.values()):
            logging.info("dashboard cascade for team %s: %s", plan.team_esid, counts)
    except Exception as e:  # noqa: BLE001 — never fail a completed delete on cascade bookkeeping
        logging.warning("dashboard cascade failed for team %s: %s", plan.team_esid, e)


@app.route("/asset/<asset_type>/delete/<item_id>", methods=["POST"])
def delete_asset(asset_type, item_id):
    """Delete an asset."""
    if asset_type not in ASSET_CONFIGS:
        return jsonify({"success": False, "error": "Asset type not found"}), 404
    # code-managed types (TikTok accounts) are projections of
    # de_connection: an Explorer delete would orphan a live token.
    if ASSET_CONFIGS[asset_type].get("code_managed"):
        return jsonify({"success": False, "error": "This asset type is managed by DEAM and cannot be deleted here."}), 403
    if asset_type == "organizations" and not _organizations_visible_to(session):
        # defense-in-depth — non-agency users can't delete orgs
        # by direct API call.
        return jsonify({"success": False, "error": "Asset type not found"}), 404
    # delete is intentionally NOT supported for workspaces in
    # this ticket (data-loss risk; see the ticket's "Out of scope"
    # section). Even a system_admin gets a 404 on the delete route to
    # prevent the destructive write until a follow-up ticket lands
    # with an explicit confirm-by-typing-the-slug pattern.
    if asset_type == "workspaces":
        return jsonify({"success": False, "error": "Workspace delete not supported"}), 404

    asset_config = ASSET_CONFIGS[asset_type]

    # company-tier (teams/organizations) per-object scope check before
    # the destructive delete. The data-source gate below short-circuits
    # these types to allow; the only other gate is the fail-open
    # _organizations_visible_to type check (and NONE for teams). A scoped admin
    # who knew a foreign team/org _id could delete it cross-tenant without this.
    if asset_type in ("teams", "organizations"):
        existing_item = AssetService.get_by_id(asset_config["index"], item_id)
        if not existing_item or not _company_tier_asset_in_user_scope(asset_type, {**existing_item, "_id": item_id}):
            return jsonify({"success": False, "error": "Item not found"}), 404

    # a data-source asset outside the user's workspace cannot be
    # deleted by direct POST, even with a known _id. Fetch the doc and run
    # the same workspace-boundary gate as the list/edit routes; a denied
    # request 404s ("not found") rather than confirming the asset exists.
    # System types (teams/organizations) short-circuit to allow in the
    # helper — their delete authorization is handled by the
    # company-tier check directly above, not by workspace_esid.
    if asset_type not in _DATA_SOURCE_SYSTEM_TYPES:
        existing_item = AssetService.get_by_id(asset_config["index"], item_id)
        if not existing_item:
            return jsonify({"success": False, "error": "Item not found"}), 404
        if not _data_source_asset_in_user_workspace(asset_type, existing_item):
            return jsonify({"success": False, "error": "Item not found"}), 404

    # refuse to delete an organization that still has teams.
    # The old behavior cascaded via reference cleanup, which merely emptied
    # each child team's organization_esid — leaving parentless teams that
    # stay active, keep importing, and pollute Data Connectors and the
    # continuity report (the orphaned-team case). Deleting the teams first makes
    # the operator's intent explicit; a listing failure refuses (fail
    # closed) rather than treating the org as childless.
    if asset_type == "organizations":
        from asset_management.web.services.reference_cleanup_service import (
            ReferenceCleanupService,
        )

        try:
            child_teams = ReferenceCleanupService.list_child_teams_of_org(item_id)
        except Exception as e:  # noqa: BLE001 — refusal boundary
            logging.error("child-team check failed for org %s: %s", item_id, e)
            return jsonify({"success": False, "error": "Could not verify this organization has no teams; not deleted."}), 503
        if child_teams:
            names = ", ".join(t["title"] or t["esid"] for t in child_teams[:10])
            more = f" (+{len(child_teams) - 10} more)" if len(child_teams) > 10 else ""
            return (
                jsonify(
                    {
                        "success": False,
                        "error": (
                            f"This organization still has {len(child_teams)} team(s): {names}{more}. "
                            "Move or delete them first."
                        ),
                    }
                ),
                409,
            )

    # For dashboards, also delete from Kibana before removing from ES
    if asset_type == "dashboards":
        item = AssetService.get_by_id(asset_config["index"], item_id)
        if item:
            kibana_dashboard_id = item.get("dashboard_id")
            space_id = item.get("space_id", "default")
            if kibana_dashboard_id:
                KibanaService.delete_dashboard(kibana_dashboard_id, space_id)

    # plan the team's dashboard cleanup BEFORE the delete — the delete's
    # reference cleanup strips the team from every dashboard, after which its
    # template copies can no longer be told from other private dashboards.
    _team_dashboard_plan = _plan_team_dashboards(item_id) if asset_type == "teams" else None

    if AssetService.delete(asset_config["index"], item_id):
        _apply_team_dashboards(_team_dashboard_plan)
        # cascade-purge authz rows naming the deleted scope so
        # role/membership/grant docs (and the tree counts derived from
        # them) can't outlive the team/org. Best-effort — the asset is
        # already gone; a cascade failure logs and never fails the delete.
        if asset_type in ("teams", "organizations"):
            try:
                from asset_management.web.services.permissions_index_service import (
                    cascade_purge_authz_for_scope,
                )
                from lib.es_helpers import get_elasticsearch_client as _get_es

                _scope_type = "team" if asset_type == "teams" else "org"
                _counts = cascade_purge_authz_for_scope(_get_es(), _scope_type, item_id)
                if any(_counts.values()):
                    logging.info("authz cascade for %s %s: %s", _scope_type, item_id, _counts)
            except Exception as e:  # noqa: BLE001 — never fail a completed delete on cascade bookkeeping
                logging.warning("authz cascade failed for %s %s: %s", asset_type, item_id, e)
        # the org is the billing entity, so its
        # de_billing_org record must not outlive it. Prod had three comped
        # records whose orgs had been deleted — rendering in the roster as
        # bare ESIDs with a live Clear button. Same posture as the authz
        # cascade above: best-effort, never fails the completed delete.
        if asset_type == "organizations":
            try:
                from asset_management.web.services.org_billing_service import OrgBillingService

                _billing = OrgBillingService.delete_org_billing_doc(item_id)
                if _billing.get("existed"):
                    logging.info("billing record removed for deleted org %s: %s", item_id, _billing)
            except Exception as e:  # noqa: BLE001 — never fail a completed delete on cascade bookkeeping
                logging.warning("billing cascade failed for org %s: %s", item_id, e)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Failed to delete"}), 500


@app.route("/asset/<asset_type>/check-references/<item_id>")
def check_asset_references(asset_type, item_id):
    """Check what references would be affected by deleting this asset."""
    if asset_type not in ASSET_CONFIGS:
        return jsonify({"success": False, "error": "Asset type not found"}), 404

    from asset_management.web.services.reference_cleanup_service import (
        ReferenceCleanupService,
    )

    asset_config = ASSET_CONFIGS[asset_type]

    # the dependents list of a cross-tenant team/org is itself a
    # tenant-information leak (it names the assets the team/org is wired to).
    # Company-tier per-object scope check, same as the delete route it feeds.
    if asset_type in ("teams", "organizations"):
        existing_item = AssetService.get_by_id(asset_config["index"], item_id)
        if not existing_item or not _company_tier_asset_in_user_scope(asset_type, {**existing_item, "_id": item_id}):
            return jsonify({"success": False, "error": "Item not found"}), 404

    # workspaces fall through BOTH gates on this route — the
    # branch above only covers teams/organizations, and the
    # branch below is skipped because "workspaces" is in
    # _DATA_SOURCE_SYSTEM_TYPES. The dependents list of a workspace names
    # every team, org and data-source asset wired to it, and the workspace
    # _id is its slug, so gate it with the same
    # type-level + per-object visibility the sibling routes use
    # (list_assets / get_asset_details).
    if asset_type == "workspaces":
        if not _workspaces_visible_to(session):
            return jsonify({"success": False, "error": "Item not found"}), 404
        try:
            from asset_management.web.services.workspace_visibility_service import (
                WorkspaceVisibilityService,
            )

            visible_ids = WorkspaceVisibilityService.get_visible_workspaces_for_user(
                session.get("username", "") or "",
                is_superuser=("superuser" in (session.get("roles") or [])),
            )
        except Exception as e:
            # Fail closed: an unavailable visibility service must not leak
            # cross-tenant dependents.
            logging.warning("check-references visibility lookup failed: %s", e)
            visible_ids = set()
        if item_id not in visible_ids:
            return jsonify({"success": False, "error": "Item not found"}), 404

    # the dependents list of a cross-workspace data-source asset is
    # itself a tenant-information leak (it names the orgs/teams the asset is
    # wired to). Gate it the same way as the delete route it feeds.
    if asset_type not in _DATA_SOURCE_SYSTEM_TYPES:
        existing_item = AssetService.get_by_id(asset_config["index"], item_id)
        if not existing_item or not _data_source_asset_in_user_workspace(asset_type, existing_item):
            return jsonify({"success": False, "error": "Item not found"}), 404

    dependents = ReferenceCleanupService.get_dependents(asset_config["index"], item_id)

    # also return the referencing docs BY NAME so the delete
    # confirmation can warn "this asset is linked by team X" — deleting a
    # load-bearing doc should never look consequence-free. The human type
    # label comes from the ASSET_CONFIGS entry owning each child index.
    index_labels = {cfg["index"]: cfg.get("title", key) for key, cfg in ASSET_CONFIGS.items()}
    references = [
        {
            "index": ref["index"],
            "type_label": index_labels.get(ref["index"], ref["index"]),
            "id": ref["id"],
            "title": ref["title"],
            "field": ref["field"],
        }
        for ref in ReferenceCleanupService.get_dependent_docs(asset_config["index"], item_id)
    ]
    return jsonify({"success": True, "dependents": dependents, "references": references})


@app.route("/asset/<asset_type>/bulk-delete", methods=["POST"])
def bulk_delete_assets(asset_type):
    """Delete multiple assets in a single operation."""
    if asset_type not in ASSET_CONFIGS:
        return jsonify({"success": False, "error": "Asset type not found"}), 404
    # code-managed types (TikTok accounts) are projections of
    # de_connection: an Explorer delete would orphan a live token.
    if ASSET_CONFIGS[asset_type].get("code_managed"):
        return jsonify({"success": False, "error": "This asset type is managed by DEAM and cannot be deleted here."}), 403

    # mirror the single-delete route's type-level gates, which
    # bulk-delete was missing entirely. Without these a scoped caller could
    # bulk-delete orgs they can't see, or workspaces (which single-delete
    # blocks outright for data-loss safety).
    if asset_type == "organizations" and not _organizations_visible_to(session):
        return jsonify({"success": False, "error": "Asset type not found"}), 404
    if asset_type == "workspaces":
        return jsonify({"success": False, "error": "Workspace delete not supported"}), 404

    data = request.get_json()
    item_ids = data.get("item_ids", [])

    if not item_ids:
        return jsonify({"success": False, "error": "No items specified"}), 400

    asset_config = ASSET_CONFIGS[asset_type]
    deleted_count = 0
    failed_count = 0
    failures = []

    deleted_ids = []
    # per-team dashboard plans, taken before each delete (see the
    # single-delete route) and applied only for teams that were deleted.
    team_dashboard_plans = {}
    for item_id in item_ids:
        try:
            # company-tier (teams/organizations) per-item scope check.
            # A crafted bulk-delete payload could mix in foreign team/org _ids;
            # an out-of-scope id is recorded as a failure and skipped, never
            # deleted. (workspaces are blocked outright above.)
            if asset_type in ("teams", "organizations"):
                existing_item = AssetService.get_by_id(asset_config["index"], item_id)
                if not existing_item or not _company_tier_asset_in_user_scope(asset_type, {**existing_item, "_id": item_id}):
                    failed_count += 1
                    failures.append({"item_id": item_id, "error": "Item not found"})
                    continue

            # a crafted bulk-delete payload could mix in _ids from
            # another workspace. Gate every id with the same boundary check
            # as the single delete route; an out-of-workspace id is recorded
            # as a failure ("Item not found") and skipped, never deleted.
            if asset_type not in _DATA_SOURCE_SYSTEM_TYPES:
                existing_item = AssetService.get_by_id(asset_config["index"], item_id)
                if not existing_item or not _data_source_asset_in_user_workspace(asset_type, existing_item):
                    failed_count += 1
                    failures.append({"item_id": item_id, "error": "Item not found"})
                    continue

            # per-item refuse gate — an org with live child teams
            # is recorded as a failure and skipped, never deleted (same
            # contract as the single-delete route; fail closed on error).
            if asset_type == "organizations":
                from asset_management.web.services.reference_cleanup_service import (
                    ReferenceCleanupService,
                )

                try:
                    _children = ReferenceCleanupService.list_child_teams_of_org(item_id)
                except Exception as e:  # noqa: BLE001 — refusal boundary
                    logging.error("bulk child-team check failed for org %s: %s", item_id, e)
                    failed_count += 1
                    failures.append({"item_id": item_id, "error": "Could not verify the organization has no teams"})
                    continue
                if _children:
                    failed_count += 1
                    failures.append(
                        {
                            "item_id": item_id,
                            "error": f"Organization still has {len(_children)} team(s); move or delete them first",
                        }
                    )
                    continue

            # For dashboards, also delete from Kibana before removing from ES
            if asset_type == "dashboards":
                item = AssetService.get_by_id(asset_config["index"], item_id)
                if item:
                    kibana_dashboard_id = item.get("dashboard_id")
                    space_id = item.get("space_id", "default")
                    if kibana_dashboard_id:
                        KibanaService.delete_dashboard(kibana_dashboard_id, space_id)

            if asset_type == "teams":
                team_dashboard_plans[item_id] = _plan_team_dashboards(item_id)
            if AssetService.delete_only(asset_config["index"], item_id):
                deleted_count += 1
                deleted_ids.append(item_id)
            else:
                failed_count += 1
                failures.append({"item_id": item_id, "error": "Delete failed"})
        except Exception:  # noqa: BLE001 — one item failing must not abort the batch
            logging.exception("bulk_delete_assets: delete failed for %s %s", asset_type, item_id)
            failed_count += 1
            failures.append({"item_id": item_id, "error": "Delete failed"})

    # Clean up ESID references in child assets in a single bulk pass
    if deleted_ids:
        try:
            from asset_management.web.services.reference_cleanup_service import (
                ReferenceCleanupService,
            )

            ReferenceCleanupService.cleanup_references_bulk(asset_config["index"], deleted_ids)
        except Exception as e:
            logging.error(f"Bulk reference cleanup failed for {asset_type}: {e}")

        for _id in deleted_ids:
            _apply_team_dashboards(team_dashboard_plans.get(_id))

        # same authz cascade as the single-delete route, per
        # deleted scope. Best-effort — never fails a completed delete.
        if asset_type in ("teams", "organizations"):
            try:
                from asset_management.web.services.permissions_index_service import (
                    cascade_purge_authz_for_scope,
                )
                from lib.es_helpers import get_elasticsearch_client as _get_es

                _es = _get_es()
                _scope_type = "team" if asset_type == "teams" else "org"
                for _id in deleted_ids:
                    _counts = cascade_purge_authz_for_scope(_es, _scope_type, _id)
                    if any(_counts.values()):
                        logging.info("authz cascade for %s %s: %s", _scope_type, _id, _counts)
            except Exception as e:  # noqa: BLE001 — never fail a completed delete on cascade bookkeeping
                logging.warning(f"bulk authz cascade failed for {asset_type}: {e}")

    return jsonify(
        {
            "success": failed_count < len(item_ids),
            "deleted_count": deleted_count,
            "failed_count": failed_count,
            "failures": failures,
            "message": f"Deleted {deleted_count} of {len(item_ids)} items",
        }
    )


# ============================================
# IMPORT LOGS ROUTES
# ============================================


@app.route("/logs")
def list_logs():
    """List recent import log files."""
    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if not perms.get("mc_import_logs"):
        return _redirect_to_allowed_page()
    try:
        logs = LogService.get_recent_logs(limit=20)
        return render_template("logs_list.html", logs=logs)
    except Exception as e:
        import logging

        logging.error(f"Failed to list logs: {e}")
        return (
            render_template(
                "error.html",
                error_code=500,
                error_title="Failed to Load Logs",
                error_message="Something went wrong loading this page. Try again in a moment; if it keeps happening, contact support.",
            ),
            500,
        )


@app.route("/logs/view/<filename>")
def view_log(filename):
    """View a specific log file with virtual scrolling."""
    # same gate as /logs. The global route gate's carve-out
    # admits scoped admins everywhere; raw import logs are cross-tenant.
    _require_mc("mc_import_logs", "Import Logs")
    try:
        content = LogService.read_log_file(filename)
        lines = content.split("\n")
        total_lines = len(lines)

        # Apply syntax highlighting server-side
        highlighted_lines = []
        for line in lines:
            css_class = LogService.get_line_class(line)
            highlighted_lines.append({"text": line, "class": css_class})

        return render_template(
            "log_viewer.html",
            filename=filename,
            lines=highlighted_lines,
            total_lines=total_lines,
        )
    except FileNotFoundError:
        return (
            render_template(
                "error.html",
                error_code=404,
                error_title="Log File Not Found",
                error_message=f"The log file '{filename}' could not be found.",
            ),
            404,
        )
    except Exception as e:
        import logging

        logging.error(f"Failed to read log file {filename}: {e}")
        return (
            render_template(
                "error.html",
                error_code=500,
                error_title="Failed to Read Log",
                error_message="Something went wrong loading this page. Try again in a moment; if it keeps happening, contact support.",
            ),
            500,
        )


@app.route("/logs/delete/<filename>", methods=["POST"])
def delete_log(filename):
    """Delete a log file."""
    _require_mc("mc_import_logs", "Import Logs")
    try:
        LogService.delete_log_file(filename)
        return jsonify({"success": True})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to delete log file",
            log_detail=f"delete_log: failed to delete '{filename}'",
            exc=e,
        )


@app.route("/logs/download/<filename>")
def download_log(filename):
    """Download a log file."""
    _require_mc("mc_import_logs", "Import Logs")
    try:
        filepath = LogService.get_log_filepath(filename)
        return send_file(filepath, as_attachment=True, download_name=filename)
    except FileNotFoundError:
        return (
            render_template(
                "error.html",
                error_code=404,
                error_title="Log File Not Found",
                error_message=f"The log file '{filename}' could not be found.",
            ),
            404,
        )
    except Exception as e:
        import logging

        logging.error(f"Failed to download log file {filename}: {e}")
        return (
            render_template(
                "error.html",
                error_code=500,
                error_title="Failed to Download Log",
                error_message="Something went wrong loading this page. Try again in a moment; if it keeps happening, contact support.",
            ),
            500,
        )


# ============================================
# STATUS REPORTS ROUTES
# ============================================


@app.route("/status-reports")
def status_reports():
    """Display status report with errors from the most recent log."""
    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if not perms.get("mc_import_status_reports"):
        return _redirect_to_allowed_page()
    try:
        LogService.cleanup_old_logs(keep_days=7)
        available_dates = LogService.get_available_log_dates(limit=7)
        selected_date = request.args.get("date")

        # Find the log file for the selected date, or use most recent
        target_filename = None
        if selected_date:
            for d in available_dates:
                if d["date"] == selected_date:
                    target_filename = d["filename"]
                    break

        if target_filename:
            # Load report for the selected date
            try:
                content = LogService.read_log_file(target_filename)
            except Exception:
                content = ""

            # Parse errors from content
            lines = content.split("\n")
            error_lines = [line for line in lines if line.strip() and LogService.get_line_class(line) == "log-error"]
            report = {
                "filename": target_filename,
                "date": selected_date,
                "errors": error_lines,
                "has_errors": len(error_lines) > 0,
            }
        else:
            report = LogService.get_most_recent_log_errors()
            # Set selected_date from the most recent log filename
            if report.get("filename"):
                m = re.match(r"imports_(\d{4})(\d{2})(\d{2})\.log$", report["filename"])
                if m:
                    selected_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        summary = []
        if report.get("filename") and selected_date:
            summary = LogService.get_import_summary_for_date(selected_date, report["filename"])
        return render_template(
            "status_reports.html",
            report=report,
            summary=summary,
            available_dates=available_dates,
            selected_date=selected_date,
        )
    except Exception as e:
        import logging

        logging.error(f"Failed to generate status report: {e}")
        return (
            render_template(
                "error.html",
                error_code=500,
                error_title="Failed to Load Status Report",
                error_message="Something went wrong loading this page. Try again in a moment; if it keeps happening, contact support.",
            ),
            500,
        )


@app.route("/api/admin/data-quality")
def api_data_quality():
    """data-quality audit — teams without country, team_country=
    'unknown' counts per index, fb_insights without location_countries,
    and ISO2 codes in active targeting that aren't in _COUNTRY_CENTROIDS.

    Same checks as the one-off CLI audit scripts (private, under
    internal/tools/debug/), served live so the Status Reports page can
    render them. Five-minute in-process cache.

    Gated on mc_import_status_reports — same permission as the rest of
    the Status Reports surface. No new permission needed.
    """
    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if not perms.get("mc_import_status_reports"):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    try:
        report = get_data_quality_report()
    except Exception:
        logging.exception("data quality endpoint failed")
        return jsonify({"ok": False, "error": "Data quality report failed."}), 500

    status = 200 if report.get("ok") else 503
    return jsonify(report), status


@app.route("/api/dashboards/<dashboard_key>")
def get_dashboard_info(dashboard_key):
    """Get dashboard configuration and URL (for AJAX dashboard switching)."""
    try:
        if dashboard_key not in DASHBOARD_CONFIGS:
            return jsonify({"success": False, "error": "Dashboard not found"}), 404

        config = DASHBOARD_CONFIGS[dashboard_key]
        if not config.get("enabled", True):
            return jsonify({"success": False, "error": "Dashboard is disabled"}), 403

        dashboard_url = get_dashboard_url(dashboard_key)

        return jsonify(
            {
                "success": True,
                "dashboard_key": dashboard_key,
                "title": config["title"],
                "description": config.get("description", ""),
                "url": dashboard_url,
            }
        )
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to get dashboard info",
            log_detail="get_dashboard_info failed",
            exc=e,
        )


@app.route("/api/dashboards/space/<space_id>")
def get_dashboards_in_space(space_id):
    """Get list of dashboards in a Kibana space."""
    # (phase 03 guard) — the /api/dashboards/ prefix is open to every
    # signed-in user, and this lists saved objects in ANY Kibana space with the
    # app's service credential. No UI calls it; it is an operator tool.
    _require_superuser()
    try:
        dashboards = KibanaService.get_dashboards_in_space(space_id)
        return jsonify({"success": True, "space_id": space_id, "dashboards": dashboards})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to fetch dashboards for space",
            log_detail=f"get_dashboards_in_space: failed for space '{space_id}'",
            exc=e,
        )


def _org_team_titles_in_workspace(workspace_esid):
    """return ``(org_titles_set, team_titles_set)`` for one workspace.

    Looks up ``de_asset_organization`` / ``de_asset_teams`` by
    ``workspace_esid``. reindexed ``de_asset_teams.workspace_esid`` to
    keyword; confirmed ``de_asset_organization.workspace_esid`` is
    likewise pure keyword, so BOTH queries now use the bare field (the prior
    org-side ``.keyword`` term was a silent no-op). Used to narrow the Builder's
    topbar org/team dropdowns to the workspace the user selected, so a
    multi-workspace user doesn't see other workspaces' orgs/teams.

    Returns ``(None, None)`` on no workspace / ES error so the caller skips the
    workspace narrowing (degrade to the role-scoped list, never widen).
    """
    if not workspace_esid:
        return None, None
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None, None

        def _titles(index, title_field, ws_field):
            resp = es.search(
                index=index,
                body={
                    "query": {"term": {ws_field: workspace_esid}},
                    "_source": [title_field],
                    "size": 10000,
                },
            )
            out = set()
            for hit in resp.get("hits", {}).get("hits", []):
                val = (hit.get("_source") or {}).get(title_field)
                if isinstance(val, list):
                    out.update(v for v in val if v)
                elif val:
                    out.add(val)
            return out

        # de_asset_teams.workspace_esid was reindexed to keyword, so
        # the bare field matches hyphenated slugs.
        # de_asset_organization.workspace_esid is ALSO pure keyword
        # (ORGANIZATION_MAPPING) with no ``.keyword`` subfield — the prior
        # ``workspace_esid.keyword`` term was a silent no-op (zero org hits),
        # the secondary-finding bug class. Use the bare field for both;
        # both indices are keyword-mapped per the workspace-scope convention.
        return (
            _titles("de_asset_organization", "organization_title", "workspace_esid"),
            _titles("de_asset_teams", "team_title", "workspace_esid"),
        )
    except Exception:  # noqa: BLE001 — never widen; degrade to role scope
        logging.warning("_org_team_titles_in_workspace(%s) failed", workspace_esid)
        return None, None


def _org_team_esids_in_workspace(workspace_esid):
    """return ``(org_esids_set, team_esids_set)`` for one workspace.

    The ESID-keyed companion to ``_org_team_titles_in_workspace``. Org/team
    TITLES are NOT unique across workspaces — two wizard signups can both name
    an org "Kavanah" or a team "General" (the exact collision class
    moved the scope walks off titles for). Narrowing the Builder filter-options
    dropdowns by title therefore lets a FOREIGN-workspace org/team with the
    same title leak through (the duplicate-"Kavanah" bug: kavanah-media's
    "Kavanah" is allowed, so kavanah-workspace's same-titled "Kavanah" passes
    too). The ``_id`` of each doc IS globally unique, so narrowing by ESID
    admits only the docs that genuinely live in this workspace.

    Returns ``(None, None)`` on no workspace / ES error so the caller skips the
    workspace narrowing (degrade to the role-scoped list, never widen) — same
    contract as ``_org_team_titles_in_workspace``.
    """
    if not workspace_esid:
        return None, None
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None, None

        def _esids(index):
            resp = es.search(
                index=index,
                body={
                    "query": {"term": {"workspace_esid": workspace_esid}},
                    "_source": False,
                    "size": 10000,
                },
            )
            return {hit["_id"] for hit in resp.get("hits", {}).get("hits", [])}

        return (
            _esids("de_asset_organization"),
            _esids("de_asset_teams"),
        )
    except Exception:  # noqa: BLE001 — never widen; degrade to role scope
        logging.warning("_org_team_esids_in_workspace(%s) failed", workspace_esid)
        return None, None


def _scope_filter_options_to_user(org_items, team_items, workspace_esid=None):
    """Restrict filter-options orgs/teams to what the current user's roles allow.

    - Privileged users (superuser / asset_admin / unrestricted) whose
      resolved scope is non-empty: orgs/teams narrowed to the resolver's
      union of memberships, grants, and workspace tier.
    - Legacy org users: orgs narrowed to their org_* roles; teams narrowed
      to teams belonging to those orgs.
    - Legacy team users: teams narrowed to their team_* roles only. Orgs
      narrowed to orgs those teams belong to (looked up from de_asset_teams).
    - **No scope from any source**: returns empty
      lists. The earlier path here returned input-unchanged for any
      user where ``should_apply_filter`` was False, which leaks all
      orgs/teams to a non-privileged user with no roles AND no
      memberships. In the membership era that's a real privacy gap,
      not a "misconfigured account" edge case.

    ``org_items`` and ``team_items`` are lists of ``{"title": ...}`` dicts.
    """
    try:
        from asset_management.web.services.role_filter_service import RoleFilterService
    except ImportError:
        from .services.role_filter_service import RoleFilterService

    # Phase B.2 / B.4: deny-all floor. A user with no resolvable
    # scope from ANY source (legacy regex, memberships, grants, workspace)
    # must see empty filter dropdowns — NOT the unfiltered list. Without
    # this branch, the earlier `if not should_apply_filter: return
    # as-is` path leaked the full org/team list to bare users. The
    # ``current_user_has_any_scope`` helper consults the resolver directly
    # — kept separate from ``should_apply_filter`` because other callers
    # (asset tree, get_filter_for_current_user) rely on the legacy
    # semantic where False means "no filter, pass through" for
    # genuinely-unscoped legacy roles like ``kibana_admin``.
    if not RoleFilterService.current_user_has_any_scope():
        return [], []

    if not RoleFilterService.should_apply_filter():
        return org_items, team_items

    allowed_orgs = RoleFilterService.get_allowed_organizations()
    allowed_teams = RoleFilterService.get_allowed_teams()

    # Team-only user: derive the allowed orgs from the teams they can see
    if allowed_orgs is None and allowed_teams:
        team_docs = AssetService.get_all("de_asset_teams")
        derived_orgs = set()
        for hit in team_docs:
            src = hit.get("_source", {})
            if src.get("team_title") not in allowed_teams:
                continue
            org = src.get("organization_title")
            if isinstance(org, list):
                derived_orgs.update(org)
            elif isinstance(org, str) and "," in org:
                derived_orgs.update(o.strip() for o in org.split(","))
            elif isinstance(org, str):
                derived_orgs.add(org)
        allowed_orgs = list(derived_orgs)

    # Org user (no team_* roles): expand allowed teams to all teams in those orgs
    if allowed_orgs is not None and allowed_teams is None:
        allowed_teams = RoleFilterService.get_team_titles_by_organization(allowed_orgs)

    if allowed_orgs is not None:
        allowed_org_set = set(allowed_orgs)
        org_items = [o for o in org_items if o.get("title") in allowed_org_set]

    if allowed_teams is not None:
        allowed_team_set = set(allowed_teams)
        team_items = [t for t in team_items if t.get("title") in allowed_team_set]

    # when a workspace is selected (multi-workspace / superuser),
    # narrow to that workspace's orgs/teams as a final layer ON TOP of the
    # role scope. Degrades to the role-scoped list if the workspace lookup
    # returns nothing (None) — never widens.
    #
    # narrow by ESID, not title. Org/team titles are NOT unique
    # across workspaces (two workspaces can each hold a "Kavanah" org / a
    # "General" team), so a title-membership filter let a foreign-workspace
    # org/team with the same title leak into the dropdown (the duplicate-
    # "Kavanah" bug). ESIDs are globally unique, so this admits only the docs
    # that actually live in the selected workspace. Items missing an esid (a
    # legacy shape) fall back to the title match so they are never dropped.
    if workspace_esid:
        ws_org_esids, ws_team_esids = _org_team_esids_in_workspace(workspace_esid)
        ws_org_titles, ws_team_titles = _org_team_titles_in_workspace(workspace_esid)
        if ws_org_esids is not None:
            org_items = [
                o
                for o in org_items
                if (o.get("esid") in ws_org_esids)
                or (o.get("esid") is None and ws_org_titles is not None and o.get("title") in ws_org_titles)
            ]
        if ws_team_esids is not None:
            team_items = [
                t
                for t in team_items
                if (t.get("esid") in ws_team_esids)
                or (t.get("esid") is None and ws_team_titles is not None and t.get("title") in ws_team_titles)
            ]

    return org_items, team_items


@app.route("/api/dashboards/filter-options")
def get_dashboard_filter_options():
    """Get available organizations and teams for dashboard filtering.

    Query params:
        platform (optional): Platform name (e.g. 'Facebook Ads'). When provided,
            returns only the org/team values that exist in that platform's data view.
            When omitted, returns all orgs/teams from the asset indices.
        org (optional): Organization title. When provided, filters the teams
            aggregation to only return teams belonging to that organization.
        filter_type (optional): Filter type for partnership users ('all', 'primary', 'partnerships').
            When 'primary', only returns teams from single-org documents.
            When 'partnerships', only returns teams from multi-org documents.
            When 'all' or omitted, returns teams from all documents.
    """
    import logging

    platform = request.args.get("platform", "")
    org_filter = request.args.get("org", "")
    orgs_filter = request.args.get("orgs", "")  # Comma-separated list of orgs
    # the Builder topbar workspace selector passes the active workspace
    # so the org/team dropdowns narrow to it (multi-workspace / superuser).
    workspace_esid = request.args.get("workspace_esid", "")
    print(
        f"[FILTER-OPTIONS] Called: platform={platform!r} org={org_filter!r} orgs={orgs_filter!r} args={dict(request.args)}",
        flush=True,
    )
    filter_type = request.args.get("filter_type", "all")

    # Parse multiple orgs if provided
    org_list = []
    if orgs_filter:
        org_list = [org.strip() for org in orgs_filter.split(",") if org.strip()]
    elif org_filter:
        org_list = [org_filter]

    logging.info(
        f"Filter options API called: platform={platform}, org={org_filter}, orgs={orgs_filter}, filter_type={filter_type}, org_list={org_list}"
    )

    try:
        if platform and platform in PLATFORM_DATA_VIEW_MAP:
            # Resolve data view to index and query for distinct values
            index_pattern = resolve_data_view_index(platform)
            logging.info(f"Filter options: platform='{platform}' resolved to index='{index_pattern}' org_list={org_list}")
            if index_pattern:
                from lib.es_helpers import get_elasticsearch_client

                es = get_elasticsearch_client()
                if es:
                    # Try without .keyword first (fields may be keyword type directly),
                    # fall back to .keyword sub-field if that fails
                    for suffix in ["", ".keyword"]:
                        try:
                            # Build aggregation body
                            aggs_body = {
                                "orgs": {
                                    "terms": {
                                        "field": f"organization_title{suffix}",
                                        "size": 500,
                                    }
                                },
                            }

                            # If org filter is set, use a filtered aggregation for teams.
                            # Use match query (not terms/term) so that multi-org teams
                            # stored as comma-separated strings or text-analyzed fields
                            # are found correctly.
                            if org_list:
                                # Determine if using multiple orgs or single org with filter_type
                                if len(org_list) > 1 or (len(org_list) == 1 and not filter_type) or filter_type == "all":
                                    # Multiple orgs or single org without document-type filtering
                                    if len(org_list) == 1:
                                        filter_query = {"match": {f"organization_title{suffix}": org_list[0]}}
                                    else:
                                        filter_query = {
                                            "bool": {
                                                "should": [
                                                    {"match": {f"organization_title{suffix}": org}} for org in org_list
                                                ],
                                                "minimum_should_match": 1,
                                            }
                                        }
                                else:
                                    # Single org with document-type filtering (primary/partnerships)
                                    org = org_list[0]
                                    if filter_type == "primary":
                                        # Single-org documents only (organization_title array size == 1)
                                        filter_query = {
                                            "bool": {
                                                "must": [
                                                    {"match": {f"organization_title{suffix}": org}},
                                                    {
                                                        "script": {
                                                            "script": f"doc['{f'organization_title{suffix}'}'].size() == 1"
                                                        }
                                                    },
                                                ]
                                            }
                                        }
                                    elif filter_type == "partnerships":
                                        # Multi-org documents only (organization_title array size > 1)
                                        filter_query = {
                                            "bool": {
                                                "must": [
                                                    {"match": {f"organization_title{suffix}": org}},
                                                    {
                                                        "script": {
                                                            "script": f"doc['{f'organization_title{suffix}'}'].size() > 1"
                                                        }
                                                    },
                                                ]
                                            }
                                        }
                                    else:
                                        # Default: match any docs with this org
                                        filter_query = {"match": {f"organization_title{suffix}": org}}

                                aggs_body["teams"] = {
                                    "filter": filter_query,
                                    "aggs": {
                                        "team_names": {
                                            "terms": {
                                                "field": f"team_title{suffix}",
                                                "size": 500,
                                            }
                                        }
                                    },
                                }
                            else:
                                aggs_body["teams"] = {
                                    "terms": {
                                        "field": f"team_title{suffix}",
                                        "size": 500,
                                    }
                                }

                            search_body = {"aggs": aggs_body}
                            logging.info(f"Filter options: executing ES query on '{index_pattern}' with body: {search_body}")

                            result = es.search(
                                index=RoleFilterService.scoped_index_pattern(index_pattern),
                                size=0,
                                body=search_body,
                                ignore_unavailable=True,
                            )
                            aggs = result.get("aggregations", {})
                            logging.info(f"Filter options: ES returned aggregations: {aggs}")
                            org_buckets = aggs.get("orgs", {}).get("buckets", [])

                            # Extract team buckets from the right location depending on filter
                            if org_list:
                                team_buckets = aggs.get("teams", {}).get("team_names", {}).get("buckets", [])
                            else:
                                team_buckets = aggs.get("teams", {}).get("buckets", [])

                            org_list = [{"title": b["key"]} for b in org_buckets if b["key"] and b["key"].lower() != "unknown"]
                            team_list = [
                                {"title": b["key"]} for b in team_buckets if b["key"] and b["key"].lower() != "unknown"
                            ]
                            logging.info(
                                f"Filter options: found {len(org_list)} orgs, {len(team_list)} teams "
                                f"in '{index_pattern}' using suffix='{suffix}'"
                            )

                            # Return whatever we got (even if empty - the data view
                            # simply doesn't have these fields, e.g. Disciple Tools)
                            org_list, team_list = _scope_filter_options_to_user(org_list, team_list, workspace_esid)
                            return jsonify(
                                {
                                    "success": True,
                                    "platform": platform,
                                    "organizations": sorted(org_list, key=lambda x: x["title"]),
                                    "teams": sorted(team_list, key=lambda x: x["title"]),
                                }
                            )
                        except Exception as agg_err:
                            logging.warning(f"Filter options: aggregation failed with suffix='{suffix}': {agg_err}")
                            continue

                    # Both suffixes failed (fields don't exist in this index) - return empty
                    logging.info(f"Filter options: fields not found in index '{index_pattern}', returning empty")
                    return jsonify(
                        {
                            "success": True,
                            "platform": platform,
                            "organizations": [],
                            "teams": [],
                        }
                    )

            # Fall through to asset-based lookup if resolution failed
            logging.warning(f"Could not resolve data view for platform '{platform}', falling back to asset indices")

        # Default: return all orgs/teams from asset indices
        orgs = AssetService.get_all("de_asset_organization")
        org_result_list = [
            {
                "title": hit["_source"]["organization_title"],
                # carry the org ESID so the workspace-narrowing layer
                # in _scope_filter_options_to_user can match by unique ESID
                # instead of non-unique title (the duplicate-"Kavanah" leak).
                "esid": hit.get("_id"),
                "logo": hit["_source"].get("organization_logo", ""),
                "description": hit["_source"].get("organization_description", ""),
            }
            for hit in orgs
            if hit["_source"].get("organization_title")
        ]

        # organization_title can be stored as:
        #   - array with one element:  ["ACME"]                 → single-org team
        #   - comma-separated string:  "ACME, BETA, GAMMA"      → multi-org team
        def org_titles(team_hit):
            """Return list of org titles for a team regardless of storage format."""
            org = team_hit["_source"].get("organization_title")
            if isinstance(org, list):
                return org
            if isinstance(org, str) and "," in org:
                return [o.strip() for o in org.split(",") if o.strip()]
            if isinstance(org, str):
                return [org]
            return []

        def is_single_org(team_hit):
            return len(org_titles(team_hit)) == 1

        def is_multi_org(team_hit):
            return len(org_titles(team_hit)) > 1

        # Filter teams by organization if org_list is provided
        # Use match query on text field to handle comma-separated strings
        if org_list:
            team_query = {
                "bool": {
                    "should": [{"match": {"organization_title": org}} for org in org_list],
                    "minimum_should_match": 1,
                }
            }
            teams = AssetService.get_all("de_asset_teams", query=team_query)
            logging.info(f"Filter options: filtered teams by orgs={org_list}, filter_type={filter_type} from asset index")
        else:
            teams = AssetService.get_all("de_asset_teams")

        if filter_type == "primary":
            teams = [t for t in teams if is_single_org(t)]
            logging.info(f"Filter options: filter_type=primary, kept {len(teams)} single-org teams")
        elif filter_type == "partnerships":
            teams = [t for t in teams if is_multi_org(t)]
            logging.info(f"Filter options: filter_type=partnerships, kept {len(teams)} multi-org teams")

        # carry the team ESID additively alongside the title. The
        # title stays the dropdown value (5 dashboard endpoints + the
        # title-keyed scope filter below depend on it); the esid lets the
        # import-run path send a collision-safe team filter.
        team_list = [
            {"title": hit["_source"]["team_title"], "esid": hit.get("_id")}
            for hit in teams
            if hit["_source"].get("team_title")
        ]

        org_result_list, team_list = _scope_filter_options_to_user(org_result_list, team_list, workspace_esid)
        result = {
            "success": True,
            "organizations": sorted(org_result_list, key=lambda x: x["title"]),
            "teams": sorted(team_list, key=lambda x: x["title"]),
        }
        logging.info(f"Filter options API returning: {len(result['organizations'])} orgs, {len(result['teams'])} teams")
        if result["teams"]:
            logging.info(f"Team titles: {[t['title'] for t in result['teams']]}")
        return jsonify(result)
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to get filter options",
            log_detail="get_filter_options failed",
            exc=e,
        )


# Mapping from sidebar platform group names to Kibana data view names
PLATFORM_DATA_VIEW_MAP = {
    # the by-team data view (create-data-views.sh) replaces the
    # retired cross-tenant alias's "Disciple Tools" view.
    "Disciple Tools": "Disciple Tools (by team)",
    "Echo": "Echo Conversations data view",
    "Meta": ["Facebook data view", "Facebook Page Insights data view", "Instagram Data View"],
    "Google": ["Google Ads data view", "YouTube data view", "Google Analytics data view"],
    "Web": "Google Analytics data view",
    "Apps": "Google Analytics data view",
    "Chat Woot": "Chat Woot data view",
}

# Maps platform names (PLATFORM_DATA_VIEW_MAP keys) to their governing ds_* fields.
# A platform is disabled when ALL its ds_* fields are "false".
PLATFORM_DS_FIELDS = {
    "Meta": ["ds_facebook", "ds_instagram"],
    "Google": ["ds_google_analytics", "ds_google_ads", "ds_youtube"],
    "Web": ["ds_google_analytics"],
    "Apps": ["ds_google_analytics"],
    "Echo": ["ds_echo"],
    "Chat Woot": ["ds_chatwoot"],
}

# Dashboard menu values that differ from PLATFORM_DATA_VIEW_MAP keys
_DASHBOARD_MENU_ALIASES = {"Chatwoot": "Chat Woot"}

# Maps dashboard data_view index patterns to their governing ds_* field.
# A dashboard is hidden when ANY of its patterns' ds_* flags are disabled.
INDEX_PATTERN_DS_MAP = {
    "*fb_insights": "ds_facebook",
    "*fb_page_insights": "ds_facebook",
    "*ig_insights": "ds_instagram",
    "*google_ads": "ds_google_ads",
    "*youtube_analytics": "ds_youtube",
    "*ga_metrics": "ds_google_analytics",
    "*echo_conversations": "ds_echo",
    "*chatwoot_insights": "ds_chatwoot",
    # TikTok organic dashboards hide when ds_tiktok is off.
    "*tiktok_organic": "ds_tiktok",
}

# allowlist + ds_* mapping for CALLER-SUPPLIED data-view patterns.
# The canva Summary Report sends its per-card dv bindings via repeated
# ?pattern= params (they are metric_overrides-resolvable client-side, so the
# server cannot know them in advance, and their spelling varies:
# '*_fb_page_insights' there vs '*fb_page_insights' in INDEX_PATTERN_DS_MAP).
# Keyed by the NORMALIZED source suffix (leading '*'/'_' stripped); doubles as
# the validation allowlist so the endpoint can never be turned into an
# arbitrary-index existence probe. Single home for the map is
# summary_visibility_service.PATTERN_SOURCE_DS (the canvas seeder's
# seed-time check reads it too); aliased here so this module's callers and
# test pins keep their names.
try:
    from web.services.summary_visibility_service import PATTERN_SOURCE_DS as _REQUESTED_PATTERN_SOURCE_DS
except ImportError:
    from asset_management.web.services.summary_visibility_service import (
        PATTERN_SOURCE_DS as _REQUESTED_PATTERN_SOURCE_DS,
    )
_REQUESTED_PATTERN_MAX = 30


def _validate_requested_patterns(raw_patterns):
    """sanitize repeated ``?pattern=`` params.

    Returns ``(patterns, source_by_pattern)`` where every pattern passed the
    charset check AND normalizes (leading ``*``/``_`` stripped) to exactly a
    key of ``_REQUESTED_PATTERN_SOURCE_DS``. Anything else is dropped, not
    errored — a stale client naming an unknown source degrades to "no
    visibility answer for that pattern", which the client treats as visible.
    """
    import re

    out: list = []
    sources: dict = {}
    for p in raw_patterns[:_REQUESTED_PATTERN_MAX]:
        p = (p or "").strip()
        if not p or len(p) > 64 or p in sources:
            continue
        if not re.fullmatch(r"[a-z0-9_*]+", p):
            continue
        suffix = p.lstrip("*_")
        if suffix in _REQUESTED_PATTERN_SOURCE_DS:
            out.append(p)
            sources[p] = suffix
    return out, sources


def resolve_data_view_index(platform):
    """Resolve a platform's data view name(s) to Elasticsearch index pattern(s).

    PLATFORM_DATA_VIEW_MAP values may be a single string or a list of strings.
    When multiple data views are mapped (e.g. Meta has both FB ads and page
    insights), the resolved index patterns are joined with commas so ES can
    query all of them in a single request.

    Returns a comma-separated index pattern string, or None if resolution fails.
    """
    import logging
    import requests

    # scoped service account / API key, falling back to elastic.
    try:
        from web.services.kibana_service import kibana_auth
    except ImportError:
        from asset_management.web.services.kibana_service import kibana_auth

    entry = PLATFORM_DATA_VIEW_MAP.get(platform)
    if not entry:
        return None

    # Normalise to a list so the rest of the logic is uniform
    names = entry if isinstance(entry, list) else [entry]

    from asset_management.web.config import DEFAULT_KIBANA_URL

    kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)

    space_id = "default"
    api_url = f"{kibana_url}/s/{space_id}/api/data_views"
    headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
    auth, configured = kibana_auth(headers)
    if not configured:
        logging.warning("No Kibana credential configured (KIBANA_APP_* / ES01_PASSWORD), cannot resolve data view")
        return None
    resp = requests.get(api_url, headers=headers, auth=auth, verify=kibana_tls_verify())

    if resp.status_code != 200:
        logging.error(f"Kibana data_views API returned {resp.status_code}")
        return None

    all_views = resp.json().get("data_view", [])
    patterns = []
    for name in names:
        for dv in all_views:
            if dv.get("name") == name or dv.get("title") == name:
                patterns.append(dv.get("title"))
                break
        else:
            logging.warning(f"Data view '{name}' not found in space '{space_id}'")

    if not patterns:
        return None
    return ",".join(patterns)


# Cache for resolved platform → index pattern mappings
_platform_index_cache = None
_platform_index_cache_time = 0
PLATFORM_INDEX_CACHE_TTL = 300  # 5 minutes
# lazy-init TTL caches get a lock each — the build runs inside the
# lock so concurrent gthread requests on an expired cache don't stampede
# Kibana/ES with duplicate rebuilds (builds are sub-second).
_platform_index_lock = threading.Lock()

# Cache for Kibana dashboard ID → ES index patterns mapping
_dashboard_index_cache = None
_dashboard_index_cache_time = 0
_dashboard_index_lock = threading.Lock()


def _get_platform_index_map():
    """Resolve all platforms to ES index patterns, with TTL caching.

    Returns a dict mapping platform names to their resolved index patterns.
    Caches the result for PLATFORM_INDEX_CACHE_TTL seconds to avoid
    repeated Kibana data views API calls.
    """
    global _platform_index_cache, _platform_index_cache_time
    import time

    now = time.time()
    if _platform_index_cache is not None and (now - _platform_index_cache_time) < PLATFORM_INDEX_CACHE_TTL:
        return _platform_index_cache

    with _platform_index_lock:
        if _platform_index_cache is not None and (time.time() - _platform_index_cache_time) < PLATFORM_INDEX_CACHE_TTL:
            return _platform_index_cache

        index_map = {}
        for platform_name in PLATFORM_DATA_VIEW_MAP:
            index_pattern = resolve_data_view_index(platform_name)
            if index_pattern:
                index_map[platform_name] = index_pattern

        _platform_index_cache = index_map
        _platform_index_cache_time = time.time()
        return index_map


def _get_dashboard_index_map():
    """Map each dashboard ID to the ES index patterns it queries.

    Reads from de_asset_echarts_dashboards and extracts index patterns
    from echarts_config.charts[].data_view AND from per-column data_view
    overrides in table_columns (used by multi-data-view tables).

    Returns: { "dashboard-uuid": ["*fb_insights", "*ig_insights"], ... }
    Only includes dashboards that reference at least one index pattern.
    Caches the result for PLATFORM_INDEX_CACHE_TTL seconds.
    """
    global _dashboard_index_cache, _dashboard_index_cache_time
    import time, logging

    now = time.time()
    if _dashboard_index_cache is not None and (now - _dashboard_index_cache_time) < PLATFORM_INDEX_CACHE_TTL:
        return _dashboard_index_cache

    with _dashboard_index_lock:
        if _dashboard_index_cache is not None and (time.time() - _dashboard_index_cache_time) < PLATFORM_INDEX_CACHE_TTL:
            return _dashboard_index_cache

        try:
            from lib.es_helpers import get_all_documents as _get_all_docs

            result = {}

            # Collect data_view from chart-level AND from table_columns
            for doc in _get_all_docs("de_asset_echarts_dashboards"):
                src = doc.get("_source", {})
                if src.get("visibility_mode") == "hide":
                    continue
                dash_id = src.get("dashboard_id", "").strip()
                if not dash_id:
                    continue
                dvs = set()
                for c in src.get("echarts_config", {}).get("charts", []):
                    if c.get("data_view"):
                        dvs.add(c["data_view"])
                    for col in c.get("table_columns", []):
                        if col.get("data_view"):
                            dvs.add(col["data_view"])
                patterns = sorted(dvs)
                if patterns:
                    result[dash_id] = patterns

            _dashboard_index_cache = result
            _dashboard_index_cache_time = time.time()
            logging.info(f"Dashboard index map built: {len(result)} dashboards mapped to index patterns")
            return result

        except Exception as e:
            logging.error(f"Failed to build dashboard index map: {e}")
            return {}


_dashboard_menu_cache = None
_dashboard_menu_cache_time = 0
_dashboard_menu_lock = threading.Lock()


def _get_dashboard_menu_map():
    """Map each dashboard ID to its menu group name.

    Returns: { "dashboard-uuid": "Echo", ... }
    Caches the result for PLATFORM_INDEX_CACHE_TTL seconds.
    """
    global _dashboard_menu_cache, _dashboard_menu_cache_time
    import time, logging

    now = time.time()
    if _dashboard_menu_cache is not None and (now - _dashboard_menu_cache_time) < PLATFORM_INDEX_CACHE_TTL:
        return _dashboard_menu_cache

    with _dashboard_menu_lock:
        if _dashboard_menu_cache is not None and (time.time() - _dashboard_menu_cache_time) < PLATFORM_INDEX_CACHE_TTL:
            return _dashboard_menu_cache

        try:
            from lib.es_helpers import get_all_documents as _get_all_docs

            result = {}
            for doc in _get_all_docs("de_asset_echarts_dashboards"):
                src = doc.get("_source", {})
                if src.get("visibility_mode") == "hide":
                    continue
                dash_id = src.get("dashboard_id", "").strip()
                menu = (src.get("menu") or "").strip()
                if dash_id and menu:
                    result[dash_id] = menu

            _dashboard_menu_cache = result
            _dashboard_menu_cache_time = time.time()
            logging.info(f"Dashboard menu map built: {len(result)} dashboards")
            return result

        except Exception as e:
            logging.error(f"Failed to build dashboard menu map: {e}")
            return {}


_DS_FIELDS = [
    "ds_chatwoot",
    "ds_echo",
    "ds_facebook",
    "ds_google_analytics",
    "ds_google_ads",
    "ds_instagram",
    "ds_tiktok",
    "ds_youtube",
]


def _get_team_data_source_flags(es, team_titles):
    """Return aggregated ds_* flags for the given team titles.

    Queries de_asset_teams and returns a dict like {"ds_echo": True, ...}.
    For multiple teams, a flag is True if ANY team has it set to "true".
    Missing/absent fields are treated as False (disabled) — matching the
    unchecked state shown in the UI.
    """
    import logging

    if not team_titles:
        return {k: True for k in _DS_FIELDS}

    try:
        should_clauses = []
        for t in team_titles:
            should_clauses.append({"term": {"team_title.keyword": t}})
            should_clauses.append({"term": {"team_title": t}})

        result = es.search(
            index="de_asset_teams",
            body={
                "query": {"bool": {"should": should_clauses, "minimum_should_match": 1}},
                "size": len(team_titles) * 2,
                "_source": _DS_FIELDS,
            },
            ignore_unavailable=True,
        )

        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            return {k: True for k in _DS_FIELDS}

        # Aggregate: True if ANY team has the field explicitly set to "true".
        # Missing/absent fields are treated as False (disabled), matching the
        # unchecked checkbox the user sees in the edit form.
        flags = {}
        for field in _DS_FIELDS:
            flags[field] = any(hit["_source"].get(field) == "true" for hit in hits)
        return flags

    except Exception as e:
        logging.error(f"Error fetching team data source flags: {e}")
        return {k: True for k in _DS_FIELDS}


def _get_team_titles_for_org(es, org, filter_type):
    """Look up team titles from de_asset_teams for the given org and filter_type.

    Args:
        es: Elasticsearch client
        org: Primary org name (e.g. 'ACME')
        filter_type: 'primary' (single-org teams), 'partnerships' (multi-org teams),
                     or '' / 'all' (all teams for this org)

    Returns:
        list of team_title strings
    """
    import logging

    try:
        query = {"match": {"organization_title": org}}
        result = es.search(
            index="de_asset_teams",
            body={
                "query": query,
                "size": 1000,
                "_source": ["team_title", "organization_title"],
            },
            ignore_unavailable=True,
        )

        teams = []
        for hit in result.get("hits", {}).get("hits", []):
            src = hit["_source"]
            team_title = src.get("team_title")
            if not team_title:
                continue

            org_title = src.get("organization_title")
            # Determine single-org vs multi-org
            if isinstance(org_title, list):
                is_multi = len(org_title) > 1
            elif isinstance(org_title, str):
                is_multi = "," in org_title
            else:
                is_multi = False

            if filter_type == "primary" and not is_multi:
                teams.append(team_title)
            elif filter_type == "partnerships" and is_multi:
                teams.append(team_title)
            elif filter_type in ("", "all"):
                teams.append(team_title)

        logging.info(f"_get_team_titles_for_org: org={org}, filter_type={filter_type}, found {len(teams)} teams")
        return teams

    except Exception as e:
        logging.error(f"Error looking up teams for org {org}: {e}")
        return []


@app.route("/api/dashboards/platform-availability")
def get_platform_availability():
    """Check which platforms have data for the given org/team filter.

    Uses Elasticsearch _msearch to batch count queries for all platforms
    into a single HTTP round-trip for optimal performance.

    Query params:
        org (optional, repeatable): Organization title to filter by. May be
            passed multiple times (e.g. ``org=A&org=B``) so that multi-org
            users in "All Organizations" mode see availability across every
            org they belong to.
        team (optional, repeatable): Team title to filter by. May be passed
            multiple times (e.g. ``team=A&team=B``) so that multi-team users
            in "All Teams" mode can check availability across every team they
            belong to. When multiple teams are given, a platform has data if
            ANY team has data (union).

    Returns JSON:
        { success: true, platforms: { "Facebook Ads": { has_data: true }, ... } }
    """
    import logging

    # ``org`` is repeatable like ``team`` — multi-org users in
    # "All Organizations" mode need every org's teams unioned together,
    # not just the alphabetically-first one.
    org_filters = [o for o in request.args.getlist("org") if o]
    # Back-compat: callers that still send a single ``org=`` are handled by
    # getlist returning a one-element list, so no separate path needed.
    org_filter = org_filters[0] if len(org_filters) == 1 else ""
    team_filters = [t for t in request.args.getlist("team") if t]
    filter_type = request.args.get("filter_type", "")  # 'primary', 'partnerships', or ''

    # scoped users must not be able to probe platform availability
    # for teams they have no role for. Any client-supplied ``team`` / ``org``
    # values that fall outside the session's allowed set are silently dropped
    # and the request is evaluated against the user's own scope instead.
    # Admins / unrestricted users see ``allowed_teams is None`` and keep the
    # pre-existing behaviour (free choice of team via the dropdown).
    allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
    if allowed_teams is not None:
        if team_filters:
            team_filters = [t for t in team_filters if t in allowed_teams]
        if not team_filters:
            team_filters = list(allowed_teams)
        # Once teams are locked to the allowed set, ``org`` would only serve
        # to re-expand scope via ``_get_team_titles_for_org`` — drop both
        # the single-string and list forms.
        org_filter = ""
        org_filters = []

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Cannot connect to Elasticsearch"}),
                500,
            )

        # Resolve which team titles to filter by.
        # If specific teams are selected, use them directly.
        # Otherwise look up teams from de_asset_teams based on org(s) + filter_type.
        # multiple orgs union their resolved teams (deduped).
        if team_filters:
            team_titles = team_filters
        elif org_filters:
            seen: set[str] = set()
            team_titles = []
            for o in org_filters:
                for t in _get_team_titles_for_org(es, o, filter_type):
                    if t not in seen:
                        seen.add(t)
                        team_titles.append(t)
        else:
            team_titles = []

        logging.info(
            f"Platform availability: orgs={org_filters}, teams={team_filters}, filter_type={filter_type}, resolved {len(team_titles)} teams: {team_titles}"
        )

        # Resolve all platform data view names to index patterns (cached)
        # the platform/dashboard maps resolve data views
        # through KIBANA; the caller-supplied ?pattern= checks below need only
        # ES. Staging 2026-08-27 (live evidence): a ZAP CI run took
        # kib01 down and this endpoint 500'd wholesale, killing Summary-Report
        # section visibility along with the menus. Degrade instead: Kibana
        # down => empty platform/dashboard maps (menus stay visible — the
        # fail-open posture), pattern visibility still answered.
        try:
            index_map = _get_platform_index_map()
        except Exception as e:  # noqa: BLE001 — Kibana outage must not 500 the endpoint
            logging.warning("Platform availability: platform index map unavailable (Kibana down?): %s", e)
            index_map = {}

        # Build the team filter query once — reused for every msearch entry.
        # Query both team_title and team_title.keyword to handle either mapping.
        if team_titles:
            should_clauses = []
            for t in team_titles:
                should_clauses.append({"term": {"team_title.keyword": t}})
                should_clauses.append({"term": {"team_title": t}})
            team_query = {"bool": {"should": should_clauses, "minimum_should_match": 1}}
        else:
            team_query = {"match_all": {}}

        # --- Part 1: platform-group queries (combined index patterns per platform) ---
        search_body = []
        platform_order = []
        for platform_name, index_pattern in index_map.items():
            search_body.append({"index": index_pattern, "ignore_unavailable": True})
            search_body.append({"size": 0, "query": team_query})
            platform_order.append(platform_name)

        # --- Part 2: per-dashboard individual-pattern queries ---
        # Collect unique individual patterns from the dashboard index map so we can
        # determine availability at the single-dashboard level (e.g. hide only
        # "Meta Instagram Insights" when *ig_insights has no data, while leaving
        # other Meta dashboards visible).
        try:
            dashboard_index_map = _get_dashboard_index_map()
        except Exception as e:  # noqa: BLE001 — same Kibana-outage degrade as index_map above
            logging.warning("Platform availability: dashboard index map unavailable (Kibana down?): %s", e)
            dashboard_index_map = {}
        individual_patterns = sorted({p for patterns in dashboard_index_map.values() for p in patterns})
        for pattern in individual_patterns:
            search_body.append({"index": pattern, "ignore_unavailable": True})
            search_body.append({"size": 0, "query": team_query})

        # -- Part 2b: caller-supplied data-view patterns ---
        # The canva Summary Report asks about its per-card dv bindings so it
        # can auto-hide sections/cards with no data for the selected team(s).
        # Existence check (any docs at all), NOT range-scoped — a quiet week
        # must still render zeros; only true absence hides (same deliberate
        # semantics as the dashboard-level check above).
        requested_patterns, requested_sources = _validate_requested_patterns(request.args.getlist("pattern"))
        for pattern in requested_patterns:
            search_body.append({"index": pattern, "ignore_unavailable": True})
            search_body.append({"size": 0, "query": team_query})

        if not search_body:
            return jsonify({"success": True, "platforms": {}, "dashboards": {}, "patterns": {}})

        # Execute all queries in a single HTTP round-trip
        results = es.msearch(body=search_body)

        def _doc_count(response):
            if "error" in response:
                return 0
            total = response.get("hits", {}).get("total", {})
            return total.get("value", 0) if isinstance(total, dict) else total

        # Parse platform results (first len(platform_order) responses)
        platforms = {}
        for i, platform_name in enumerate(platform_order):
            platforms[platform_name] = {"has_data": _doc_count(results["responses"][i]) > 0}

        # Mark platforms with no resolved data view as no-data
        for platform_name in PLATFORM_DATA_VIEW_MAP:
            if platform_name not in platforms:
                platforms[platform_name] = {"has_data": False}

        # Parse individual-pattern results (responses after the platform ones)
        offset = len(platform_order)
        pattern_has_data = {}
        for i, pattern in enumerate(individual_patterns):
            pattern_has_data[pattern] = _doc_count(results["responses"][offset + i]) > 0

        # Map individual-pattern results to per-dashboard availability.
        # changed from all -> any. A dashboard with multiple
        # data_views becomes visible as soon as ANY of its data views has
        # data (originally motivated by the first-look template mixing the
        # daily index with the retired weekly index, whose cron only ran on
        # Sundays — retired weekly, but the any semantics stay).
        # Acceptable trade-off: a multi-data-view dashboard whose
        # primary source is broken stays visible — the user sees partial
        # data rather than nothing, which matches the wizard-cohort's
        # "show me something" expectation.
        dashboards = {
            dash_id: {"has_data": any(pattern_has_data.get(p, False) for p in patterns)}
            for dash_id, patterns in dashboard_index_map.items()
        }

        # parse caller-supplied pattern results (responses after the
        # dashboard individual-pattern block). "visible" starts as raw data
        # existence; Part 3 folds the ds_* toggles in below.
        requested_offset = offset + len(individual_patterns)
        requested = {}
        for i, pattern in enumerate(requested_patterns):
            requested[pattern] = {"visible": _doc_count(results["responses"][requested_offset + i]) > 0}
        # Tier 2 — filled below only when a team scope exists AND
        # patterns were requested; {} means "no explicit opinions" (automatic).
        unit_overrides: dict = {}

        # --- Part 3: Override based on team ds_* flags ---
        # When a team/org is selected, check which data sources are disabled
        # and hide the corresponding platform groups and individual dashboards.
        if team_titles:
            ds_flags = _get_team_data_source_flags(es, team_titles)

            # 3a: Per-dashboard override using index pattern → ds_* mapping.
            # A dashboard is hidden when ANY of its index patterns belongs to
            # a disabled data source (consistent with the all() data check).
            for dash_id, patterns in dashboard_index_map.items():
                for pattern in patterns:
                    ds_field = INDEX_PATTERN_DS_MAP.get(pattern)
                    if ds_field and not ds_flags.get(ds_field, False):
                        dashboards[dash_id] = {"has_data": False}
                        break

            # fold ds_* toggles into caller-supplied pattern
            # visibility: source toggled off => hidden even when stale docs
            # exist (Tier 2's "explicit control wins over data presence").
            for pattern, info in requested.items():
                ds_field = _REQUESTED_PATTERN_SOURCE_DS.get(requested_sources.get(pattern, ""))
                if ds_field and not ds_flags.get(ds_field, False):
                    info["visible"] = False

            # Tier 2 — per-team card overrides (explicit human choice,
            # stored on the team doc; 'show'/'hide' per unit key, merged across
            # the selected teams with show-wins). Only computed when patterns
            # were requested — this is Summary-Report-only payload.
            if requested_patterns:
                try:
                    from web.services import summary_visibility_service as _svs
                except ImportError:
                    from asset_management.web.services import summary_visibility_service as _svs
                unit_overrides = _svs.merged_overrides_for_titles(es, team_titles)

            # 3b: Dashboards without index patterns — fall back to menu match.
            # Also catches dashboards whose patterns aren't in INDEX_PATTERN_DS_MAP.
            disabled_platforms = set()
            for platform_name, ds_keys in PLATFORM_DS_FIELDS.items():
                if all(not ds_flags.get(k, False) for k in ds_keys):
                    disabled_platforms.add(platform_name)

            if disabled_platforms:
                logging.info(f"Disabling platforms based on ds_* flags: {disabled_platforms}")
                for p in disabled_platforms:
                    if p in platforms:
                        platforms[p]["has_data"] = False

                dash_menu_map = _get_dashboard_menu_map()
                for dash_id, menu in dash_menu_map.items():
                    resolved = _DASHBOARD_MENU_ALIASES.get(menu, menu)
                    if resolved in disabled_platforms:
                        dashboards[dash_id] = {"has_data": False}

        # ``debug_teams`` was the aid that confirmed the scope bypass;
        # now that scope is session-enforced, echoing the resolved team list
        # back to the client adds no useful information. Logged server-side
        # at line 1914 for operators.
        return jsonify(
            {
                "success": True,
                "platforms": platforms,
                "dashboards": dashboards,
                # caller-supplied pattern visibility (existence AND
                # ds_* toggle); absent patterns mean "no answer" and the canva
                # client fails open (shows the section).
                "patterns": requested,
                # Tier 2 — explicit per-team card overrides
                # ({unit_key: 'show'|'hide'}); explicit wins over automatic.
                "units": unit_overrides,
            }
        )

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to check platform availability",
            log_detail="check_platform_availability failed",
            exc=e,
        )


@app.route("/api/dashboards/data-pulled-as-of")
def get_data_pulled_as_of():
    """Get the latest @timestamp from the data view backing a platform's dashboards."""
    import logging

    platform = request.args.get("platform", "")
    if platform not in PLATFORM_DATA_VIEW_MAP:
        return jsonify({"success": True, "timestamp": None})

    try:
        index_pattern = resolve_data_view_index(platform)
        if not index_pattern:
            return jsonify({"success": True, "timestamp": None})

        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": True, "timestamp": None})

        result = es.search(
            index=RoleFilterService.scoped_index_pattern(index_pattern),
            size=0,
            body={"aggs": {"max_ts": {"max": {"field": "@timestamp"}}}},
            ignore_unavailable=True,
        )

        max_ts = result.get("aggregations", {}).get("max_ts", {}).get("value_as_string")
        return jsonify({"success": True, "timestamp": max_ts, "platform": platform})

    except Exception as e:
        logging.error(f"Failed to get data-pulled-as-of for {platform}: {e}")
        return jsonify({"success": True, "timestamp": None})


@app.route("/api/dashboards/data-pulled-as-of-echarts")
def get_data_pulled_as_of_echarts():
    """Get the latest @timestamp for an ECharts dashboard's data source.

    Returns a timestamp only when all charts in the dashboard use a single
    data view (single data source).  When multiple data views are present the
    timestamp is ambiguous, so we return None and the frontend hides the field.

     Slice A — also returns ``is_meta`` (any chart binds a
    ``fb_*`` / ``fb_insights*`` data view) so the dashboard viewer can
    decide whether the Custom Range Apply path needs the "pull new
    data" modal. When ``from`` and ``to`` query args (YYYY-MM-DD) are
    passed, also returns ``has_data_in_range`` — a count > 0 of
    documents whose ``@timestamp`` (or ``kibana_date`` fallback) falls
    inside the requested window. The modal is skipped when this is
    False — there's no existing data to render against, so we kick off
    the import directly.
    """
    import logging

    dashboard_id = request.args.get("dashboard_id", "")
    range_from = (request.args.get("from") or "").strip()
    range_to = (request.args.get("to") or "").strip()
    range_args_present = bool(range_from and range_to)

    if not dashboard_id:
        return jsonify({"success": True, "timestamp": None, "is_meta": False})

    try:
        from lib.es_helpers import get_elasticsearch_client, get_document_by_id

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": True, "timestamp": None, "is_meta": False})

        # Try both dashboard indices (builder saves to de_asset_dashboards,
        # the dashboards page reads from de_asset_echarts_dashboards)
        source = get_document_by_id("de_asset_echarts_dashboards", dashboard_id) or get_document_by_id(
            "de_asset_dashboards", dashboard_id
        )
        if not source:
            return jsonify({"success": True, "timestamp": None, "is_meta": False})

        # Collect distinct data_view values across all charts
        charts = source.get("echarts_config", {}).get("charts", [])
        data_views: set[str] = set()
        for chart in charts:
            dv = chart.get("data_view")
            if dv:
                data_views.add(dv)
            # Trend line charts store data views inside sources
            for src in chart.get("sources", []):
                sdv = src.get("data_view")
                if sdv:
                    data_views.add(sdv)

        # Meta dashboard if any data_view contains the ``fb_``
        # token. Data views in this codebase carry a per-tenant wildcard
        # prefix (``*fb_insights``, ``*_fb_insights``, ``*fb_page_insights``)
        # rather than a bare ``fb_*`` shape, so an ``includes`` check is
        # the right primitive.
        # Instagram (``ig_insights``) is Meta-owned. Phase 1
        # originally EXCLUDED it because the importer couldn't be told to
        # run IG-only (it always ran ad → page → IG). added the
        # ``--stages`` selector + data-view→stage resolution, so an
        # IG-only dashboard now resolves to ``--stages=ig`` and the pull
        # is meaningful. Include ig_insights so the custom-range "pull new
        # data" flow fires for IG-only dashboards (without this gate the
        # front-end's _currentDashboardIsMeta is False and the kickoff
        # never POSTs — the symptom: selecting a custom range on an
        # IG-only dashboard does nothing).
        # resolve which connector backs this dashboard (meta_ads /
        # google_analytics_4 / google_ads / None). ``is_meta`` is kept as a
        # backward-compatible alias (the frontend still reads it) but now derives
        # from the resolver so the FB behavior is identical.
        connector = _resolve_dashboard_connector(data_views)
        is_meta = connector == "meta_ads"

        if len(data_views) != 1:
            # Multi-data-view dashboards still surface connector + an
            # optional has_data_in_range (over the union of indices) so
            # the modal can render — only the single-source timestamp is
            # ambiguous.
            response = {"success": True, "timestamp": None, "is_meta": is_meta, "connector": connector}
            if range_args_present:
                response["has_data_in_range"] = _has_data_in_range_for_views(es, data_views, range_from, range_to)
            return jsonify(response)

        index_pattern = data_views.pop()

        # Try @timestamp first, fall back to kibana_date
        scoped_pattern = RoleFilterService.scoped_index_pattern(index_pattern)
        max_ts = None
        for ts_field in ("@timestamp", "kibana_date"):
            try:
                result = es.search(
                    index=scoped_pattern,
                    size=0,
                    body={"aggs": {"max_ts": {"max": {"field": ts_field}}}},
                    # scoped_index_pattern returns an EXPLICIT index
                    # list (not a wildcard); allowlisted-but-never-imported teams
                    # contribute index names that don't exist, which makes ES
                    # throw index_not_found_exception and the except below
                    # silently null the timestamp. ignore_unavailable skips the
                    # missing ones (scope already enforces tenancy).
                    ignore_unavailable=True,
                    allow_no_indices=True,
                )
                agg = result.get("aggregations", {}).get("max_ts", {})
                max_ts = agg.get("value_as_string")
                if max_ts:
                    break
            except Exception:
                continue

        response = {
            "success": True,
            "timestamp": max_ts,
            "data_view": index_pattern,
            "is_meta": is_meta,
            "connector": connector,
        }
        if range_args_present:
            response["has_data_in_range"] = _has_data_in_range_for_views(es, {index_pattern}, range_from, range_to)
        return jsonify(response)

    except Exception as e:
        logging.error(f"Failed to get data-pulled-as-of for dashboard {dashboard_id}: {e}")
        return jsonify({"success": True, "timestamp": None, "is_meta": False})


def _has_data_in_range_for_views(es, data_views: set[str], range_from: str, range_to: str) -> bool:
    """Slice A — does any document in ``data_views`` fall inside
    [range_from, range_to]? Used by the Custom Range modal to decide
    whether to prompt or kick off the import directly. False on ES
    outage / bad input — the caller treats False as "no overlap" and
    routes through the pull path, which is the safer default (running
    an extra import is recoverable; suppressing the modal with stale
    data isn't).
    """
    import logging

    if not data_views:
        return False
    try:
        for ts_field in ("@timestamp", "kibana_date"):
            for dv in data_views:
                try:
                    scoped = RoleFilterService.scoped_index_pattern(dv)
                    result = es.count(
                        index=scoped,
                        # see get_data_pulled_as_of_echarts: the scoped
                        # pattern is an explicit index list that may name
                        # never-imported (non-existent) indices; without these,
                        # ES throws on the first missing one and the except below
                        # wrongly reports "no data in range" (suppressing the
                        # re-import modal even when in-range data exists).
                        ignore_unavailable=True,
                        allow_no_indices=True,
                        body={
                            "query": {
                                "range": {
                                    ts_field: {
                                        "gte": range_from,
                                        "lte": range_to,
                                        "format": "yyyy-MM-dd",
                                    }
                                }
                            }
                        },
                    )
                    if int(result.get("count") or 0) > 0:
                        return True
                except Exception:
                    continue
        return False
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning("has_data_in_range raised: %s", e)
        return False


@app.route("/api/dashboards/<dashboard_id>/report-snapshots")
def get_dashboard_report_snapshots(dashboard_id):
    """past report runs whose schedule references this dashboard.

    Backs the dashboard toolbar's Reports dropdown. Deliberately NOT behind the
    Report Scheduler gate (org owners/admins) — the ticket's audience is EVERY
    user who can open the dashboard, and the /api/dashboards/ prefix is already
    role-authz-exempt for authenticated users (auth.check_route_authorization).
    Visibility scoping still applies: the caller's UserScope filters the
    snapshot list exactly as it does on the scheduler's own history page, so a
    user only sees runs for orgs/teams they can access.
    """
    import logging

    try:
        from asset_management.web.services.report_schedule_service import ReportScheduleService
        from asset_management.web.services.role_filter_service import resolve_user_scope
    except ImportError:
        from .services.report_schedule_service import ReportScheduleService
        from .services.role_filter_service import resolve_user_scope

    try:
        # Same trust model as the scheduler blueprint's _effective_scope
        #: a privileged user (superuser / asset_admin /
        # workspace_admin / workspace_owner) resolves to EMPTY UserScope
        # *_esids, so the unconditional scope filter dropped every snapshot
        # and the Reports button never appeared for the very users who create
        # schedules. Privileged → None (the service treats it as see-all);
        # everyone else keeps their real UserScope.
        if RoleFilterService.current_user_is_privileged():
            scope = None
        else:
            scope = resolve_user_scope(
                session.get("username", ""),
                session.get("roles", []) or [],
                RoleFilterService.load_config(),
            )
        snapshots = ReportScheduleService.list_snapshots_for_dashboard(dashboard_id, scope=scope)
        return jsonify({"success": True, "snapshots": snapshots})
    except Exception as e:  # noqa: BLE001 — viewer surface must never 500 the toolbar
        logging.error(f"report-snapshots for dashboard {dashboard_id}: {e}")
        return jsonify({"success": True, "snapshots": []})


# Slice B — Meta Marketing API hard limit on
# Insights historical reads. Requested start_dates older than this are
# clamped to (today − 37 months) and the response surfaces
# ``clamped_start_date`` so the UI can tell the user we trimmed.
_META_API_MAX_LOOKBACK_MONTHS = 37


# on-demand custom-range import was Meta-only. This resolver
# generalizes the dashboard→connector decision so the same "pull new data" flow
# fires for GA4 and Google Ads dashboards too. Keyed on data-view SOURCE SUFFIX
# (the per-tenant wildcard/ESID prefix is irrelevant), matching the same
# ``includes``-style primitive the old ``is_meta`` check used. Order matters:
# the Meta family (fb_/ig_) is checked first so a hypothetical mixed dashboard
# stays Meta. Returns the connector_id key into _CONNECTOR_IMPORTER_REGISTRY, or
# None when no known connector backs the dashboard (caller renders read-only,
# no kickoff). Use the FULL canonical suffixes — NOT the Canva editor's loose
# ``*ga`` / ``*youtube`` prefixes, which are substring traps.
def _resolve_dashboard_connector(data_views) -> str | None:
    """Map a dashboard's data views to the connector_id that backs it.

    Returns one of ``"meta_ads"`` / ``"google_analytics_4"`` / ``"google_ads"``,
    or ``None`` when no recognized connector data view is present. YouTube is
    intentionally absent (no dashboard pack yet).
    """
    dvs = [dv for dv in (data_views or []) if isinstance(dv, str)]
    # Meta family first (fb ads / fb pages / instagram are all the meta_ads importer).
    if any(("fb_insights" in dv) or ("fb_page_insights" in dv) or ("ig_insights" in dv) for dv in dvs):
        return "meta_ads"
    if any("ga_metrics" in dv for dv in dvs):  # covers *_ga_metrics and *_ga_metrics_window
        return "google_analytics_4"
    if any("google_ads" in dv for dv in dvs):
        return "google_ads"
    return None


def _dashboard_data_views(dashboard_id):
    """load the set of data views a dashboard binds, from
    ``de_asset_echarts_dashboards``. Returns an empty set on miss / ES outage.

    Shared by ``_resolve_dashboard_connector_by_id`` and ``_resolve_meta_stages``;
    uses the same dual-match (.keyword + bare term) the rest of this module uses
    for de_asset_* text fields (the.keyword trap).
    """
    if not dashboard_id:
        return set()
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return set()
        resp = es.search(
            index="de_asset_echarts_dashboards",
            body={
                "size": 1,
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"dashboard_id.keyword": dashboard_id}},
                            {"term": {"dashboard_id": dashboard_id}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning("_dashboard_data_views lookup raised for %r: %s", dashboard_id, e)
        return set()

    hits = (resp.get("hits") or {}).get("hits") or []
    if not hits:
        return set()
    src = hits[0].get("_source") or {}
    data_views = set()
    for chart in (src.get("echarts_config") or {}).get("charts", []) or []:
        if chart.get("data_view"):
            data_views.add(chart["data_view"])
        for col in chart.get("table_columns", []) or []:
            if col.get("data_view"):
                data_views.add(col["data_view"])
        for s in chart.get("sources", []) or []:
            if s.get("data_view"):
                data_views.add(s["data_view"])
    return data_views


def _resolve_dashboard_connector_by_id(dashboard_id) -> str | None:
    """server-side connector resolution from a dashboard_id.

    Authoritative for import routing (the client's probe value is not trusted).
    Returns the connector_id or None (caller defaults None to meta_ads to
    preserve earlier behavior).
    """
    return _resolve_dashboard_connector(_dashboard_data_views(dashboard_id))


def _resolve_nonmeta_kickoff(connector_id, team_src):
    """resolve a GA4 / Google Ads on-demand kickoff for a team.

    Returns ``(connection_esid, scope_filter)``:
      * ``(esid, None)``  — an ACTIVE OAuth connection serves this team; dispatch
        --connection_esid and reuse the importer path.
      * ``(None, "id,id")`` — no OAuth connection (service-account team); the
        caller dispatches a synthetic legacy_team: key + this provider-asset
        scope filter (property_ids for GA4, customer_ids for Google Ads).
      * ``(None, None)``  — the team has no provider assets at all; nothing to import.

    Resolution prefers the OAuth connection (so we get per-user token + progress
    keyed on the real connection); the service-account fallback keeps the
    on-demand pull working for the (today majority) operator-seeded teams without
    touching the cron path.
    """
    try:
        from asset_management.web.services.connection_service import ConnectionService
    except Exception as e:  # noqa: BLE001
        logging.warning("ConnectionService import failed: %s", e)
        ConnectionService = None  # type: ignore[assignment]

    if connector_id == "google_analytics_4":
        # union BOTH team↔property conventions: team_ga4_properties
        # (bare ids, legacy) AND team_ga4_property_esids (asset esids, what the
        # wizard writes). Reading only the former missed every wizard-connected
        # property → "no properties connected" banner. Resolve esids → bare ids
        # via de_asset_ga_properties.
        property_ids = [str(p).split("/")[-1].strip() for p in (team_src.get("team_ga4_properties") or []) if p]
        property_ids += _resolve_team_ga4_property_esids(team_src.get("team_ga4_property_esids") or [])
        property_ids = sorted({p for p in property_ids if p})
        if ConnectionService is not None:
            for pid in property_ids:
                try:
                    resolved = ConnectionService.get_ga4_oauth_connection_for_property(pid)
                except Exception as e:  # noqa: BLE001
                    logging.warning("GA4 connection resolve raised for %r: %s", pid, e)
                    resolved = None
                if resolved:
                    return (resolved[0], None)  # (connection_esid, _account_esid)
        return (None, ",".join(property_ids) if property_ids else None)

    if connector_id == "google_ads":
        # The team carries the customer ESID; resolve it to the bare customer id.
        customer_ids = _resolve_team_google_ads_customer_ids(team_src)
        if ConnectionService is not None:
            for cid in customer_ids:
                try:
                    esid = ConnectionService.get_google_ads_oauth_connection_esid(cid)
                except Exception as e:  # noqa: BLE001
                    logging.warning("Google Ads connection resolve raised for %r: %s", cid, e)
                    esid = None
                if esid:
                    return (esid, None)
        return (None, ",".join(customer_ids) if customer_ids else None)

    return (None, None)


def _resolve_team_google_ads_customer_ids(team_src):
    """resolve a team's Google Ads customer id(s) from its
    ``google_ads_customer_esid`` reference(s) via de_asset_google_ads_customers.

    The team doc carries the customer ESID (asset reference), not the bare
    customer id the importer's --customer_ids filter expects. Returns a list of
    bare customer ids (empty on miss / outage — caller treats empty as "no assets").
    """
    raw = team_src.get("google_ads_customer_esid")
    esids = [raw] if isinstance(raw, str) and raw else (list(raw) if isinstance(raw, list) else [])
    esids = [e for e in esids if e]
    if not esids:
        return []
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return []
        resp = es.search(
            index="de_asset_google_ads_customers",
            body={"size": len(esids) + 5, "query": {"ids": {"values": esids}}},
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("google_ads customer id resolve raised: %s", e)
        return []
    out = []
    for hit in (resp.get("hits") or {}).get("hits") or []:
        cid = (hit.get("_source") or {}).get("google_ads_customer_id")
        if cid:
            out.append(str(cid))
    return out


def _resolve_team_ga4_property_esids(esids):
    """resolve a team's ``team_ga4_property_esids`` (de_asset_ga_properties
    asset esids, what the wizard writes) to bare ``ga_property_id`` values (what the
    importer's --property_ids filter expects). Returns a list of bare ids (empty on
    miss / outage). Mirrors _resolve_team_google_ads_customer_ids.
    """
    esids = [e for e in (esids or []) if e]
    if not esids:
        return []
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return []
        resp = es.search(
            index="de_asset_ga_properties",
            body={"size": len(esids) + 5, "query": {"ids": {"values": esids}}},
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("ga4 property id resolve raised: %s", e)
        return []
    out = []
    for hit in (resp.get("hits") or {}).get("hits") or []:
        pid = (hit.get("_source") or {}).get("ga_property_id")
        if pid:
            out.append(str(pid).split("/")[-1].strip())
    return out


def _resolve_meta_stages(dashboard_id):
    """KAN selective dispatch (B) — map a dashboard's data views to the Meta
    importer stage(s) it needs: a subset of ``["ad", "page", "ig"]``.

    Loads the dashboard's ``echarts_config.charts[].data_view`` (+ table-column
    overrides) from ``de_asset_echarts_dashboards`` and maps each data view's
    SOURCE SUFFIX to a stage:

      * ``*_ig_insights``        -> ig
      * ``*_fb_page_insights``   -> page
      * ``*_fb_insights``        -> ad

    Prefix-agnostic (wildcard / team-slug / ESID forms all match on suffix).

    Returns:
      * a non-empty list of stages when the dashboard resolves to ≥1 Meta stage
      * ``None`` when the dashboard_id is empty/unknown, has no Meta data views,
        or ES is unavailable — the caller MUST treat None as "run all stages"
        (the safety fallback; never import nothing).
    """
    if not dashboard_id:
        return None
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None
        # Dual-match dashboard_id.keyword + bare dashboard_id. de_asset_*
        # text fields are mapped text+keyword; a bare term on the analyzed
        # field silently returns 0 for hyphenated/uuid values (the recurring
        # .keyword trap). The.keyword subfield is the exact
        # match; the bare term is a fallback for any mapping without it.
        resp = es.search(
            index="de_asset_echarts_dashboards",
            body={
                "size": 1,
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"dashboard_id.keyword": dashboard_id}},
                            {"term": {"dashboard_id": dashboard_id}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001 — boundary; caller falls back to all stages
        logging.warning("KAN selective-dispatch: _resolve_meta_stages lookup raised for %r: %s", dashboard_id, e)
        return None

    hits = (resp.get("hits") or {}).get("hits") or []
    if not hits:
        return None
    src = hits[0].get("_source") or {}

    data_views = set()
    for chart in (src.get("echarts_config") or {}).get("charts", []) or []:
        if chart.get("data_view"):
            data_views.add(chart["data_view"])
        for col in chart.get("table_columns", []) or []:
            if col.get("data_view"):
                data_views.add(col["data_view"])

    stages = set()
    for dv in data_views:
        if not isinstance(dv, str):
            continue
        if "_ig_insights" in dv:
            stages.add("ig")
        elif "_fb_page_insights" in dv:
            stages.add("page")
        elif "_fb_insights" in dv:
            stages.add("ad")
        # non-Meta data views (echo / GA / etc.) contribute no Meta stage.

    if not stages:
        return None
    # Deterministic order for stable cmd + tests.
    return [s for s in ("ad", "page", "ig") if s in stages]


def _kickoff_team_in_caller_scope(team_esid) -> bool:
    """authorize the on-demand range-import kickoff for a resolved team.

    The ``team_filter`` → ``de_asset_teams`` lookup in
    :func:`kickoff_meta_range_import` is GLOBAL — any team title in the cluster
    resolves — so without this gate any authenticated user could dispatch an
    importer against ANOTHER tenant's connected accounts (third-party API spend
    / quota burn; same trust-the-client class as). Mirrors
    the report scheduler's write gate (``_effective_scope``):

      * privileged callers (superuser / asset_admin / system_admin /
        workspace_admin / workspace_owner) pass — they already passed the page
        gate, and admins legitimately trigger pulls on other tenants'
        dashboards (the cross-tenant-admin path this route exists for).
      * everyone else must have the team's ESID in their workspace-widened
        UserScope: direct team/org scope plus every team in their allowed
        workspace(s) — the same team universe the report scheduler authorizes
        schedule saves against (user decision 2026-06-15: the tenancy boundary
        is the workspace).

    ESID-based, not title-based: team titles collide across workspaces
    , so a title-membership check would still admit a same-named
    foreign team. Fail-closed — any error resolving scope denies the kickoff.
    """
    try:
        if RoleFilterService.current_user_is_privileged():
            return True
    except Exception:  # noqa: BLE001 — fall through to the scoped check
        pass
    try:
        from asset_management.web.blueprints.report_scheduler.routes import (
            _get_user_scope,
            _workspace_widened_scope,
        )

        scope = _workspace_widened_scope(_get_user_scope())
        allowed = getattr(scope, "team_esids", frozenset()) or frozenset()
        return team_esid in allowed
    except Exception as e:  # noqa: BLE001 — an authz gate never fails open
        logging.warning("kickoff scope check failed; denying: %s", e)
        return False


@app.route("/api/dashboards/meta/kickoff-range-import", methods=["POST"])
def kickoff_meta_range_import():
    """Slice B — kick off an on-demand Meta backfill for a
    specific date range, driven by the dashboard viewer's Custom Range
    "pull new data" flow.

    Resolves the connection_esid via the dashboard's selected TEAM —
    the client sends ``team_filter`` (the team_title from the
    team-filter dropdown), the server queries
    ``de_asset_teams[term: team_title == X]`` and reads
    ``connection_esid`` off the team doc (set bridge at
    wizard finish for fb_import_mode='account_wide' teams). This
    works for admins viewing other tenants' dashboards — they have
    no de_connection of their own, but the team they're scoped to
    carries the right connection.

    Then clamps ``start_date`` to today − 37 months (Meta Marketing
    API hard limit) and dispatches into ``_kickoff_on_demand_backfill``
    with the range override.

    Returns (200) ``{ok: True, run_id, clamped_start_date?}`` on a clean
    kickoff. Returns (200) ``{ok: False, reason}`` for fall-through
    conditions the UI surfaces inline:
      * ``team_not_found`` — team_filter does not match any team doc, OR
         it resolves to a team outside the caller's allowed
        scope — deliberately the same response, so the route is not an
        existence oracle for foreign team titles
      * ``team_not_account_wide`` — Phase 1 supports only the
        bridged (account-wide) teams; legacy page-filtered teams must
        re-run the wizard to migrate
      * ``team_missing_connection_esid`` — team doc has no
        connection_esid (data integrity issue; flag for investigation)
      * ``fb_import_in_flight`` — cluster lock held by another importer
      * ``no_importer_for_connector`` / ``popen_failed`` / etc — from
        the underlying helper, surfaced verbatim.

    Returns 400 on ``missing_range`` / ``missing_team_filter`` /
    ``invalid_range``. Returns 401 ``{reason: 'unauthenticated'}`` on
    anonymous request.
    """
    import logging
    from datetime import datetime, timedelta, timezone

    username = session.get("username", "")
    if not username:
        return jsonify({"ok": False, "reason": "unauthenticated"}), 401

    body = request.get_json(silent=True) or {}
    raw_start = (body.get("start_date") or "").strip()
    raw_end = (body.get("end_date") or "").strip()
    if not raw_start or not raw_end:
        return jsonify({"ok": False, "reason": "missing_range"}), 400

    # KAN selective dispatch (B) — the dashboard the pull was triggered from.
    # Optional + best-effort: when present and it resolves to a Meta stage
    # subset, only those importer stages run (e.g. an IG-only dashboard runs
    # only the IG stage). Absent / unresolvable -> stages=None -> the importer
    # runs all three (the safe fallback; never import nothing).
    dashboard_id = (body.get("dashboard_id") or "").strip()

    # dedup-only refresh. The dashboard "Refresh" button sends
    # ``scope="dedup_only"`` so the importer pulls ONLY the windowed
    # de-duplicated field (reach/frequency) for the range, skipping the full
    # per-day pipeline. Any other / absent scope is the existing custom-range
    # "Pull new data?" behavior (full pull). FB-only in this phase.
    dedup_only = (body.get("scope") or "").strip() == "dedup_only"

    try:
        sd = datetime.strptime(raw_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        ed = datetime.strptime(raw_end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"ok": False, "reason": "invalid_range"}), 400

    if sd > ed:
        return jsonify({"ok": False, "reason": "invalid_range"}), 400

    # Clamp end_date forward to today if a caller passes a future date —
    # the importer would silently return zero docs otherwise, which is
    # a worse UX than a visible clamp.
    today = datetime.now(tz=timezone.utc)
    if ed > today:
        ed = today

    # Clamp start_date to (today − 37 months). Using 37*30 days as the
    # cap — slightly conservative vs the calendar-month math but safe
    # against month-length variance, and the UI shows the clamped value
    # so the user can see what changed.
    earliest = today - timedelta(days=_META_API_MAX_LOOKBACK_MONTHS * 30)
    clamped_start_date: str | None = None
    if sd < earliest:
        clamped_start_date = earliest.strftime("%Y-%m-%d")
        sd = earliest

    start_date = sd.strftime("%Y-%m-%d")
    end_date = ed.strftime("%Y-%m-%d")

    # follow-up (a teammate, 2026-05-22 staging) — resolve the
    # connection_esid via the DASHBOARD'S TEAM, not the requesting
    # user's own connections. Admins reading other tenants' dashboards
    # have no de_connection of their own — that's by design. The team
    # doc the dashboard scopes to carries connection_esid +
    # fb_ad_account_esid directly when fb_import_mode == "account_wide"
    # (the bridge writes them at wizard finish).
    #
    # Resolution chain:
    #   client sends team_filter (the team_title from the team-filter
    #     dropdown — same value the iframe URL uses) →
    #   de_asset_teams[term: team_title == X] → team_doc →
    #   team_doc["connection_esid"] → de_connection[id] (the token)
    team_filter = (body.get("team_filter") or "").strip()

    # Phase 2 — the multi-select viewer sends the checked team TITLES in
    # ``team_filters`` (a list) for a dedup Refresh with 2+ teams checked. Resolve
    # exactly that subset and dispatch one comma-joined run over those teams (NOT
    # the all-scoped superset). Single team continues to use the scalar
    # ``team_filter`` path; 0 checked (empty list AND empty scalar) falls through
    # to the all-teams path below. Ignored for a full pull (see the loop client-
    # side — a full multi-team pull is a serial per-team kickoff, not one run).
    raw_team_filters = body.get("team_filters")
    team_filters = [str(t).strip() for t in raw_team_filters if str(t).strip()] if isinstance(raw_team_filters, list) else []
    if dedup_only and len(team_filters) >= 2:
        return jsonify(
            _kickoff_dedup_for_team_titles(
                team_titles=team_filters,
                username=username,
                dashboard_id=dashboard_id,
                start_date=start_date,
                end_date=end_date,
                clamped_start_date=clamped_start_date,
            )
        )

    # multi-team dedup Refresh. An empty team_filter is the dashboard's
    # "All Teams" selection. For a FULL pull we still require an explicit team
    # (unchanged), but for a dedup-only Refresh we pull dedup reach for EVERY team
    # the dashboard covers: resolve the caller's in-scope teams (the same set the
    # team-filter dropdown is built from), dedupe to distinct ad accounts, and
    # kick off each SEQUENTIALLY behind the import lock (throttle-safe; the user
    # can Cancel). One aggregate run_id; the table reloads when the run finishes.
    if not team_filter:
        if not dedup_only:
            return jsonify({"ok": False, "reason": "missing_team_filter"}), 400
        return jsonify(
            _kickoff_dedup_all_scoped_teams(
                username=username,
                dashboard_id=dashboard_id,
                start_date=start_date,
                end_date=end_date,
                clamped_start_date=clamped_start_date,
            )
        )

    return jsonify(
        _kickoff_range_import_for_team(
            team_filter,
            username=username,
            dashboard_id=dashboard_id,
            dedup_only=dedup_only,
            start_date=start_date,
            end_date=end_date,
            clamped_start_date=clamped_start_date,
        )
    )


def _kickoff_range_import_for_team(
    team_filter,
    *,
    username,
    dashboard_id,
    dedup_only,
    start_date,
    end_date,
    clamped_start_date=None,
    team_ids_override=None,
):
    """Resolve ONE team_title → connection → dispatch an on-demand backfill.

    Returns the result dict (``{ok, run_id | reason, ...}``) — the caller
    ``jsonify``s it. Extracted from the endpoint body so the multi-team dedup
    Refresh can call it per team without duplicating the
    resolution chain. Behavior for a single explicit team is byte-identical to
    the earlier inline path.

    ``team_ids_override`` (KAN1241-03) — a list of already-resolved, in-scope FB
    team ESIDs. When given, SKIP the single-team title resolution and dispatch ONE
    Meta dedup pull scoped to all of them (page-filtered surface: comma-joined
    ``--team_ids``, connector-global lock). This is the "All Teams" path — the
    importer's per-account loop covers every team in one run, so no per-team
    kickoff loop (which would self-contend on the global lock). Only valid with
    ``dedup_only=True``; ignored otherwise.
    """
    import logging

    if team_ids_override and dedup_only:
        return _dispatch_multi_team_dedup(
            team_ids_override,
            username=username,
            dashboard_id=dashboard_id,
            start_date=start_date,
            end_date=end_date,
        )

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "reason": "es_unavailable"}
        # follow-up (2026-05-27) — match BOTH ``team_title.keyword``
        # and the raw ``team_title``. ``team_title`` is an analyzed text
        # field, so a bare ``term`` query against it tokenizes the stored
        # value (e.g. "test-account" → ["test", "account"]) and never
        # matches the literal string — every hyphenated / multi-word team
        # title (like the individual-wizard "test-account") fell through to
        # ``team_not_found`` even though the team exists and the dashboard
        # renders its data fine. The .keyword subfield holds the un-analyzed
        # value for exact match. Mirrors the dual-match pattern
        # already uses in role_filter_service.py (~line 1263) for the same
        # reason; the bare-term form is kept in the should as a fallback for
        # any index whose mapping lacks the .keyword subfield.
        result = es.search(
            index="de_asset_teams",
            body={
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"team_title.keyword": team_filter}},
                            {"term": {"team_title": team_filter}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                "size": 1,
            },
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("kickoff: team lookup raised for %r: %s", team_filter, e)
        return {"ok": False, "reason": "team_lookup_failed"}

    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        return {"ok": False, "reason": "team_not_found", "team_filter": team_filter}

    team_doc = hits[0]
    team_src = team_doc.get("_source", {}) or {}
    team_esid = team_doc.get("_id")

    # the lookup above is GLOBAL, so a foreign tenant's team title
    # resolves too. Deny before any dispatch unless the resolved team is in
    # the caller's scope (privileged callers pass — see the helper). The
    # response is byte-identical to the unknown-team branch so this route is
    # not an existence oracle for foreign team titles.
    if not _kickoff_team_in_caller_scope(team_esid):
        logging.info(
            "range-import kickoff for team %r (%s) denied — outside caller %r scope",
            team_filter,
            team_esid,
            username,
        )
        return {"ok": False, "reason": "team_not_found", "team_filter": team_filter}

    # resolve which connector this dashboard is backed by so the
    # SAME kickoff route serves Meta, GA4, and Google Ads. Server-side
    # resolution (from the dashboard's data views) is authoritative — the
    # client's probe value is not trusted for import routing. Unknown / no
    # dashboard -> default to meta_ads to preserve the earlier behavior
    # for any Meta dashboard whose id doesn't resolve (the safe historical
    # default; the importer still scopes to the resolved team).
    connector_id = _resolve_dashboard_connector_by_id(dashboard_id) or "meta_ads"

    # Per-connector team -> dispatch resolution. Each branch sets:
    #   connection_esid   — real de_connection esid, OR synthetic legacy_team:<esid>
    #   legacy_team_esid  — team esid when dispatching via --team_ids (no real conn)
    #   scope_filter      — comma-joined provider asset ids for the synthetic path
    legacy_team_esid: str | None = None
    scope_filter: str | None = None

    if connector_id == "meta_ads":
        # Unchanged FB behavior. account_wide teams carry
        # connection_esid directly; page_filtered teams dispatch by team_ids
        # with a synthetic legacy_team: progress key.
        import_mode = team_src.get("fb_import_mode") or "page_filtered"
        if import_mode == "account_wide":
            connection_esid = team_src.get("connection_esid") or ""
            if not connection_esid:
                return {
                    "ok": False,
                    "reason": "team_missing_connection_esid",
                    "team_filter": team_filter,
                    "team_esid": team_esid,
                }
        else:
            legacy_team_esid = team_esid
            connection_esid = f"legacy_team:{team_esid}"
    else:
        # GA4 / Google Ads. Resolve the team's provider assets to an
        # ACTIVE OAuth connection when one exists (dispatch --connection_esid,
        # reusing the proven importer path). When the team has no
        # OAuth connection (operator-seeded, service-account teams — the
        # no-regression path), fall back to a synthetic
        # legacy_team: key + a provider-asset scope filter so the on-demand
        # pull still scopes to THIS team's properties/customers and still
        # writes progress. The nightly cron (no flags) is unaffected by either
        # branch — it never passes a connection or a team.
        connection_esid, scope_filter = _resolve_nonmeta_kickoff(connector_id, team_src)
        if connection_esid is None and not scope_filter:
            # Team has neither an OAuth connection nor any resolvable provider
            # assets — nothing to import for this connector. Surface inline.
            return {
                "ok": False,
                "reason": "team_has_no_connector_assets",
                "team_filter": team_filter,
                "team_esid": team_esid,
                "connector": connector_id,
            }
        if connection_esid is None:
            # Service-account team: synthetic progress key + team_ids dispatch.
            legacy_team_esid = team_esid
            connection_esid = f"legacy_team:{team_esid}"

    try:
        from asset_management.web.blueprints.analytics.routes import _kickoff_on_demand_backfill
    except Exception as e:  # noqa: BLE001
        logging.warning("kickoff: helper import failed: %s", e)
        return {"ok": False, "reason": "kickoff_helper_unavailable"}

    # KAN selective dispatch (B) — Meta-only stage subset. None => all stages /
    # ignored for non-Meta connectors.
    selected_stages = _resolve_meta_stages(dashboard_id) if connector_id == "meta_ads" else None
    if selected_stages is not None:
        logging.info(
            "KAN selective-dispatch: dashboard_id=%r -> stages=%s",
            dashboard_id,
            selected_stages,
        )

    result = _kickoff_on_demand_backfill(
        username=username,
        connection_esid=connection_esid,
        connector_id=connector_id,
        asset_count=0,
        start_date=start_date,
        end_date=end_date,
        legacy_team_esid=legacy_team_esid,
        scope_team_esid=team_esid,
        stages=selected_stages,
        # provider-asset scope filter for the GA4/GAds service-account
        # (synthetic-key) path; None for Meta and for the real-connection path.
        scope_filter=scope_filter,
        # dedup-only refresh (Refresh button). FB-only this phase;
        # the helper only appends --dedup_only for the facebook source.
        dedup_only=dedup_only,
    )

    if clamped_start_date and result.get("ok"):
        result["clamped_start_date"] = clamped_start_date
    return result


def _dispatch_multi_team_dedup(team_esids, *, username, dashboard_id, start_date, end_date):
    """KAN1241-03 — ONE windowed dedup pull scoped to every in-scope FB team.

    Dispatches via the page-filtered (legacy_team) surface with comma-joined
    ``--team_ids`` so the importer takes a SINGLE connector-global lock acquire
    and its per-account loop writes per-ad-set window docs for every team in one
    run. This is why the multi-team path does NOT loop per-team kickoffs — those
    would each spawn a subprocess contending on the same global lock, so only the
    first would win and the rest would bounce ``fb_import_in_flight`` (the staging
    bug this replaces). ``connection_esid`` is a stable per-dashboard synthetic
    key so the dup-Popen guard + progress doc behave.
    """
    import logging

    try:
        from asset_management.web.blueprints.analytics.routes import _kickoff_on_demand_backfill
    except Exception as e:  # noqa: BLE001
        logging.warning("KAN1241-03 multi-team dedup: helper import failed: %s", e)
        return {"ok": False, "reason": "kickoff_helper_unavailable"}

    joined = ",".join(team_esids)
    # Meta-only stage subset for this dashboard (dedup_only ignores stages, but
    # pass it through for parity with the single-team path).
    selected_stages = _resolve_meta_stages(dashboard_id)

    return _kickoff_on_demand_backfill(
        username=username,
        connection_esid=f"legacy_team:{dashboard_id}",
        connector_id="meta_ads",
        asset_count=0,
        start_date=start_date,
        end_date=end_date,
        legacy_team_esid=joined,  # → --team_ids=A,B,C ; lock scope "" (global)
        scope_team_esid=None,
        stages=selected_stages,
        scope_filter=None,
        dedup_only=True,
    )


def _kickoff_dedup_all_scoped_teams(*, username, dashboard_id, start_date, end_date, clamped_start_date=None):
    """dedup Refresh for ALL teams the dashboard covers ("All Teams").

    Resolves the caller's in-scope FB teams (the same universe the team-filter
    dropdown is built from — role/membership/workspace scope, via the
    kickoff scope helper, intersected with ds_facebook) and dispatches ONE
    windowed dedup pull scoped to ALL of them at once (comma-joined --team_ids).

    Why one run, not a loop (KAN1241-03, 2026-07-09): the page-filtered dashboard
    teams all take the connector-GLOBAL ``facebook`` lock (legacy dispatch → lock
    scope ""), so N per-team subprocesses would all contend on the SAME lock — the
    first wins and the rest bounce with ``fb_import_in_flight``, silently leaving
    the other teams unpulled (staging: only team A + team B got window docs; team C
    + team D got none). The importer already handles the whole set in one run:
    ``resolve_team_filter_scope`` parses comma-joined team_ids, ``_import_window``
    loops every scoped team per ad account, and ``_import_shared_account_per_team``
    writes per-ad-set docs for EVERY scoped team reaching a shared account. One
    global-lock acquire, one run_id, one poller — no self-contention.

    Returns ``{ok, run_id, teams_total, ...}`` or ``{ok: False, reason}`` when no
    FB team could be resolved (fail-closed — never fan out cluster-wide).
    """
    team_esids = _scoped_fb_team_esids_for_kickoff()
    if not team_esids:
        return {"ok": False, "reason": "no_scoped_teams"}

    # ONE importer run scoped to every in-scope FB team. Dispatched via the
    # page-filtered (legacy_team) surface with comma-joined esids so the importer
    # takes a single global-lock acquire and its per-account loop covers them all.
    res = _kickoff_range_import_for_team(
        ",".join(team_esids),
        username=username,
        dashboard_id=dashboard_id,
        dedup_only=True,
        start_date=start_date,
        end_date=end_date,
        clamped_start_date=clamped_start_date,
        team_ids_override=team_esids,
    )
    if not res.get("ok"):
        return res

    res["teams_total"] = len(team_esids)
    if clamped_start_date:
        res["clamped_start_date"] = clamped_start_date
    return res


def _kickoff_dedup_for_team_titles(*, team_titles, username, dashboard_id, start_date, end_date, clamped_start_date=None):
    """Phase 2 — dedup Refresh for a SPECIFIC checked-team SUBSET.

    The multi-select viewer sends the checked team titles; resolve them to
    in-scope FB team ESIDs (:func:`_scoped_fb_team_esids_for_titles`, same
    ds_facebook + gate as the all-teams path) and dispatch ONE
    comma-joined ``--team_ids`` run over exactly those teams — the same
    single-global-lock model as :func:`_kickoff_dedup_all_scoped_teams`, so no
    per-team self-contention. Distinct from the all-teams path only in WHICH
    teams: exactly the checked subset, not the caller's whole FB universe.

    Falls back to ``no_scoped_teams`` when none of the titles resolve to an
    in-scope FB team (fail-closed — never dispatches an unbounded pull).
    """
    team_esids = _scoped_fb_team_esids_for_titles(team_titles)
    if not team_esids:
        return {"ok": False, "reason": "no_scoped_teams"}

    res = _kickoff_range_import_for_team(
        ",".join(team_esids),
        username=username,
        dashboard_id=dashboard_id,
        dedup_only=True,
        start_date=start_date,
        end_date=end_date,
        clamped_start_date=clamped_start_date,
        team_ids_override=team_esids,
    )
    if not res.get("ok"):
        return res

    res["teams_total"] = len(team_esids)
    if clamped_start_date:
        res["clamped_start_date"] = clamped_start_date
    return res


def _scoped_fb_team_esids_for_kickoff() -> list[str]:
    """The FB team ESIDs the current caller may pull — the dashboard's team universe.

    Reuses the report-scheduler workspace-widened UserScope (same set the
    kickoff scope check authorizes against), intersected with ds_facebook teams
    ONLY, and gated per-team through ``_kickoff_team_in_caller_scope`` so the
    multi-team pull authorizes exactly like a single-team kickoff would. Empty
    list on any failure (fail-closed — a blank scope must not fan out to the whole
    cluster).

    ds_facebook intersection is load-bearing: the scope is ALL teams the user can
    see (incl. non-FB teams like a bare "test" team, or GA4/Echo-only teams).
    Dispatching the Meta window pull at a non-FB team makes get_insights_window's
     strict mode resolve to ZERO teams and exit 1 -> the whole refresh
    reported "Import failed" even though the FB teams were fine (staging
    2026-07-09: team "test" sank the DJI* Reach dashboard refresh)."""
    import logging

    try:
        from asset_management.web.blueprints.report_scheduler.routes import (
            _get_user_scope,
            _workspace_widened_scope,
        )
        from lib.es_helpers import get_elasticsearch_client

        scope = _workspace_widened_scope(_get_user_scope())
        team_esids = sorted(getattr(scope, "team_esids", frozenset()) or frozenset())
        if not team_esids:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        resp = es.search(
            index="de_asset_teams",
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"ids": {"values": team_esids}},
                            {"term": {"ds_facebook": "true"}},
                        ]
                    }
                },
                "_source": False,
                "size": len(team_esids),
            },
        )
        fb_esids = [hit.get("_id") for hit in resp.get("hits", {}).get("hits", []) if hit.get("_id")]
        # Per-team scope gate — byte-parity with the single-team kickoff's
        # authorization (these esids came FROM the caller's scope, so this is a
        # belt-and-braces re-check, not a new trust boundary).
        return [e for e in fb_esids if _kickoff_team_in_caller_scope(e)]
    except Exception as e:  # noqa: BLE001 — fail-closed: never fan out on error
        logging.warning("scoped-team resolution failed; refusing multi-team pull: %s", e)
        return []


def _scoped_fb_team_esids_for_titles(team_titles: list[str]) -> list[str]:
    """Phase 2 — the FB team ESIDs for a SPECIFIC set of checked team
    titles, scope-gated exactly like the all-teams resolver.

    The multi-select dedup Refresh sends the checked team TITLES; this resolves
    them to distinct in-scope FB team ESIDs so the one-run comma-joined dispatch
    pulls EXACTLY the checked subset (not the all-scoped superset). Same
    ds_facebook intersection + per-team ``_kickoff_team_in_caller_scope`` gate as
    :func:`_scoped_fb_team_esids_for_kickoff`, so authorization is byte-identical
    — a title the caller can't see (or that isn't a FB team) is silently dropped,
    never dispatched. Fail-closed: empty list on any error / no matches.

    Title match mirrors the single-team resolver's dual ``team_title.keyword`` /
    bare ``team_title`` should (the field is analyzed text; the .keyword subfield
    holds the exact value, the bare term is the fallback for indices missing it).
    """
    import logging

    titles = [t.strip() for t in (team_titles or []) if t and t.strip()]
    if not titles:
        return []
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return []
        resp = es.search(
            index="de_asset_teams",
            body={
                "query": {
                    "bool": {
                        "filter": [{"term": {"ds_facebook": "true"}}],
                        "must": [
                            {
                                "bool": {
                                    "should": [
                                        {"terms": {"team_title.keyword": titles}},
                                        {
                                            "bool": {
                                                "should": [{"match_phrase": {"team_title": t}} for t in titles],
                                                "minimum_should_match": 1,
                                            }
                                        },
                                    ],
                                    "minimum_should_match": 1,
                                }
                            }
                        ],
                    }
                },
                "_source": False,
                "size": len(titles) * 4,  # headroom for same-title collisions across workspaces
            },
        )
        # De-dupe ESIDs while preserving discovery order, then per-team scope gate
        # (a resolved foreign same-titled team is denied here).
        seen: set[str] = set()
        ordered: list[str] = []
        for hit in resp.get("hits", {}).get("hits", []):
            eid = hit.get("_id")
            if eid and eid not in seen:
                seen.add(eid)
                ordered.append(eid)
        return [e for e in ordered if _kickoff_team_in_caller_scope(e)]
    except Exception as e:  # noqa: BLE001 — fail-closed: never fan out on error
        logging.warning("title-subset team resolution failed; refusing pull: %s", e)
        return []


# ============================================
# IMPORT DATA ROUTES
# ============================================


@app.route("/import-data")
def import_data():
    """Display the import data page."""
    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if not perms.get("mc_import_data"):
        return _redirect_to_allowed_page()
    return render_template("import_data.html")


def _require_index_in_scope(data_view):
    """central index authorisation for routes that take a data_view.

    Returns a ``(json, 403)`` pair when the requested index / pattern has no
    overlap with the caller's scope (``validate_index_access``), else
    None. The routes below already query the SCOPED rewrite of ``data_view``,
    so a foreign index was never readable — but it answered with an empty 200,
    which is indistinguishable from "no data" and invisible in logs. A 403 with
    an AUDIT line makes the horizontal probe visible. An empty ``data_view`` is
    left to each route's own "required" check.
    """
    if not data_view:
        return None
    if validate_index_access(data_view):
        return None
    logging.warning(
        "AUDIT: user=%s action=index_scope_denied data_view=%r path=%s ip=%s",
        session.get("username", ""),
        data_view,
        request.path,
        request.remote_addr,
    )
    return jsonify({"success": False, "error": "Access denied: index outside your scope."}), 403


def _require_import_data_permission():
    """permission-tier gate for the /api/import/* surface.

    The /import-data PAGE checks ``mc_import_data`` but the API routes it
    calls sat behind login only, so a logged-in user without the permission
    (a plain dashboard viewer) could hit import-status internals and — worse —
    the operational POSTs (run / cancel / delete-log). Every route below is
    consumed exclusively by import_data.html (verified 2026-09-01: no other
    template or JS references them), so gating the whole surface on the same
    permission the page uses breaks no legitimate caller. Reads within scope
    were never a tenant leak — this closes the TIER
    violation. Returns a (json, 403) pair to return, or None to proceed.
    """
    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if not perms.get("mc_import_data"):
        return jsonify({"success": False, "error": "You do not have permission to use Import Data."}), 403
    return None


@app.route("/api/import/run", methods=["POST"])
def run_imports():
    """Start data imports for selected platforms."""
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        imports = data.get("imports", [])
        if not imports:
            return jsonify({"success": False, "error": "No imports selected"}), 400

        # Validate imports
        valid_sources = [
            "facebook_ads",
            "facebook_pages",
            "echo",
            "google_ads",
            "google_analytics",
            "instagram",
            "youtube",
            "chatwoot",
            "disciple_tools",
        ]
        for imp in imports:
            source = imp.get("source")
            if source not in valid_sources:
                return (
                    jsonify({"success": False, "error": f"Invalid source: {source}"}),
                    400,
                )

            # Check that exactly one of: days, date range, or explicit dates list
            has_days = "days" in imp and imp.get("days")
            has_dates = "start_date" in imp and "end_date" in imp
            has_specific_dates = "dates" in imp and imp.get("dates")

            provided = sum([bool(has_days), bool(has_dates), bool(has_specific_dates)])
            if provided == 0:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": f"{source}: Must provide days, date range, or dates list",
                        }
                    ),
                    400,
                )

            if provided > 1:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": f"{source}: Provide only one of: days, date range, or dates list",
                        }
                    ),
                    400,
                )

            # Validate days if provided
            if has_days:
                days = int(imp.get("days"))
                if days < 1 or days > 365:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": f"{source}: Days must be between 1 and 365",
                            }
                        ),
                        400,
                    )

            # validate team_ids before it flows into a subprocess argv
            # via _build_import_command. team_ids is a comma-separated list of
            # team titles; restrict to a conservative character set so a value
            # like "; rm -rf /" can't reach the import scripts even though cmd
            # is a list (no shell). CodeQL py/command-line-injection.
            team_ids_raw = imp.get("team_ids")
            if team_ids_raw is not None:
                if not isinstance(team_ids_raw, str):
                    return (
                        jsonify({"success": False, "error": f"{source}: team_ids must be a string"}),
                        400,
                    )
                if len(team_ids_raw) > 1024:
                    return (
                        jsonify({"success": False, "error": f"{source}: team_ids too long"}),
                        400,
                    )
                # Allow alphanumerics, common name punctuation, commas, spaces.
                if not re.fullmatch(r"[A-Za-z0-9_\-., ]*", team_ids_raw):
                    return (
                        jsonify({"success": False, "error": f"{source}: team_ids contains invalid characters"}),
                        400,
                    )

            # Validate dates if provided
            if has_dates:
                try:
                    from datetime import datetime

                    start = datetime.strptime(imp["start_date"], "%Y-%m-%d")
                    end = datetime.strptime(imp["end_date"], "%Y-%m-%d")

                    if start > end:
                        return (
                            jsonify(
                                {
                                    "success": False,
                                    "error": f"{source}: Start date must be <= end date",
                                }
                            ),
                            400,
                        )

                    if end > datetime.now():
                        return (
                            jsonify(
                                {
                                    "success": False,
                                    "error": f"{source}: End date cannot be in the future",
                                }
                            ),
                            400,
                        )

                    days_diff = (end - start).days + 1
                    if days_diff > 365:
                        return (
                            jsonify(
                                {
                                    "success": False,
                                    "error": f"{source}: Date range cannot exceed 365 days",
                                }
                            ),
                            400,
                        )

                except ValueError as e:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": f"{source}: Invalid date format (use YYYY-MM-DD)",
                            }
                        ),
                        400,
                    )

            # Validate specific dates list if provided
            if has_specific_dates:
                try:
                    from datetime import datetime

                    dates_str = imp["dates"]
                    date_list = [d.strip() for d in dates_str.split(",") if d.strip()]
                    if not date_list:
                        return (
                            jsonify({"success": False, "error": f"{source}: dates list is empty"}),
                            400,
                        )
                    if len(date_list) > 365:
                        return (
                            jsonify({"success": False, "error": f"{source}: dates list cannot exceed 365 entries"}),
                            400,
                        )
                    for d in date_list:
                        parsed = datetime.strptime(d, "%Y-%m-%d")
                        if parsed > datetime.now():
                            return (
                                jsonify({"success": False, "error": f"{source}: Date {d} is in the future"}),
                                400,
                            )
                except ValueError:
                    return (
                        jsonify({"success": False, "error": f"{source}: Invalid date in dates list (use YYYY-MM-DD)"}),
                        400,
                    )

        # Echo's GraphQL API is tenant-scoped: ANY echo import is a
        # full-tenant pull (whole rate-limit budget + the per-connector importer
        # lock, which aborts a concurrent run instead of queuing it). Kickoff is
        # therefore restricted to workspace owners (superuser included),
        # enforced server-side — hiding the option in the UI is not a gate.
        if any(imp.get("source") == "echo" for imp in imports):
            if not auth_module.user_is_workspace_owner(session.get("username", ""), session.get("roles", []) or []):
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Echo imports are tenant-wide pulls; only workspace owners can trigger them.",
                        }
                    ),
                    403,
                )

        # Delete manual import log if requested (cron logs are never touched)
        delete_log = data.get("delete_log", False)
        if delete_log:
            log_delete_result = ImportService.delete_manual_log()
            import logging

            logging.info(f"Delete log result: {log_delete_result}")

        # Delete recent data if requested
        delete_data = data.get("delete_data", False)
        if delete_data:
            delete_result = ImportService.delete_recent_data(imports)
            import logging

            logging.info(
                f"Delete recent data result: {delete_result}"  # nosemgrep: tainted-sql-string - not SQL, logging ES operation result
            )

        # Run the imports
        result = ImportService.run_imports(imports)

        return jsonify(
            {
                "success": True,
                "import_id": result["import_id"],
                "started": result["started"],
                # follow-up — sources skipped because the selected team
                # has no linked account (google_ads/GA4); surfaced so the UI can
                # explain them instead of showing a silent green no-op.
                "skipped": result.get("skipped", []),
                "message": result["message"],
            }
        )

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to start imports",
            log_detail="start_imports failed",
            exc=e,
        )


@app.route("/api/import/status/<import_id>")
def get_import_status(import_id):
    """Get the status of a running import."""
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    try:
        status = ImportService.get_import_status(import_id)
        return jsonify(status)
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to get import status",
            log_detail=f"get_import_status: failed for import_id '{import_id}'",
            exc=e,
            body={"found": False},
        )


@app.route("/api/import/cancel/<import_id>", methods=["POST"])
def cancel_import(import_id):
    """Cancel a running import by killing its processes."""
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    try:
        result = ImportService.cancel_import(import_id)
        return jsonify(result)
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to cancel import",
            log_detail=f"cancel_import: failed for import_id '{import_id}'",
            exc=e,
        )


@app.route("/api/import/delete-log", methods=["POST"])
def delete_todays_log():
    """Delete the manual import log file."""
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    try:
        result = ImportService.delete_manual_log()
        return jsonify(result)
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to delete manual log",
            log_detail="delete_todays_log failed",
            exc=e,
        )


@app.route("/api/import/log-exists")
def check_todays_log():
    """Check if the manual import log exists."""
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    try:
        exists = os.path.exists(ImportService.MANUAL_LOG_FILE)
        filename = os.path.basename(ImportService.MANUAL_LOG_FILE)
        return jsonify({"exists": exists, "filename": filename})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to check log existence",
            log_detail="check_todays_log failed",
            exc=e,
            body={"exists": False},
        )


@app.route("/api/import/log-content")
def get_log_content():
    """Get manual import log content, optionally from a byte offset for live tailing."""
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    try:
        offset = request.args.get("offset", 0, type=int)
        log_path = ImportService.MANUAL_LOG_FILE

        if not os.path.exists(log_path):
            return jsonify({"exists": False, "content": "", "offset": 0, "size": 0})

        file_size = os.path.getsize(log_path)

        # If offset is beyond file size, return empty (file may have been truncated)
        if offset > file_size:
            offset = 0

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            content = f.read()
            new_offset = f.tell()

        return jsonify(
            {
                "exists": True,
                "content": content,
                "offset": new_offset,
                "size": file_size,
            }
        )

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to read log content",
            log_detail="get_log_content failed",
            exc=e,
            body={"exists": False},
        )


# ============================================
# DATA SOURCE IMPORT STATUS ROUTE
# ============================================


@app.route("/api/import/data-source-status")
def get_data_source_status():
    """
    Return document stats and monthly coverage for a given Elasticsearch index (data view).
    Query params:
      data_view  - required; ES index name or pattern
      team       - optional; filter by team_title
      from_date  - optional; YYYY-MM-DD lower bound on kibana_date
      to_date    - optional; YYYY-MM-DD upper bound on kibana_date
    """
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    data_view = request.args.get("data_view", "").strip()
    _gate_2 = _require_index_in_scope(data_view)
    if _gate_2 is not None:
        return _gate_2
    if not data_view:
        return jsonify({"success": False, "error": "data_view is required"}), 400

    team = request.args.get("team", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        # Build bool query filters
        must_filters = []
        if team:
            # team_title is a pure keyword in most schemas (Facebook, Instagram, Echo, GA)
            # but a text+keyword subfield in YouTube and Google Ads.
            # Using a should clause handles both without needing to know the schema.
            must_filters.append(
                {
                    "bool": {
                        "should": [
                            {"term": {"team_title": team}},
                            {"term": {"team_title.keyword": team}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        # match BOTH DT spellings — the legacy cross-tenant alias
        # `disciple-tools` (hyphen) and the team-keyed `*_disciple_tools`
        # (underscore, "Disciple Tools (by team)" data view). The hyphen-only
        # check silently dropped the DT date-field handling for the team-keyed
        # view (empty monthly table on Data Source Import Status). Mirrors
        # charts.js _isDtDataView.
        is_disciple_tools = "disciple-tools" in data_view.lower() or "disciple_tools" in data_view.lower()

        if from_date or to_date:
            # Explicitly declare the input format so ES doesn't try to parse
            # plain yyyy-MM-dd strings using the field's stored format (e.g.
            # Echo's "MMMM dd, yyyy HH:mm:ss 'UTC'" would otherwise reject them).
            range_clause = {"format": "yyyy-MM-dd"}
            if from_date:
                range_clause["gte"] = from_date
            if to_date:
                range_clause["lte"] = to_date
            # Disciple Tools has one doc per contact; timestamp is updated on every
            # import. Filter by when contacts were created: the legacy plugin
            # docs carry a top-level first_contact_date; the team-keyed
            # docs don't — they use date_created_ms (epoch_millis
            # date; the yyyy-MM-dd format clause parses the bounds).
            if is_disciple_tools:
                date_field = "first_contact_date" if "disciple-tools" in data_view.lower() else "date_created_ms"
            else:
                date_field = "kibana_date"
            must_filters.append({"range": {date_field: range_clause}})

        query = {"bool": {"must": must_filters}} if must_filters else {"match_all": {}}

        # Build aggregations
        aggs_body = {"max_timestamp": {"max": {"field": "@timestamp"}}}
        if is_disciple_tools:
            # date_modified is a text/keyword field — use terms agg to get all
            # unique values so we can build the monthly summary table.
            # the team-keyed *_disciple_tools indices map
            # date_modified as a bare keyword (schema'd), so .keyword is
            # unmapped there and a terms agg on it returns zero buckets (the
            # empty-table symptom). The legacy dynamic-mapped alias is text
            # and DOES need the .keyword subfield.
            dm_field = "date_modified.keyword" if "disciple-tools" in data_view.lower() else "date_modified"
            aggs_body["by_date_modified"] = {"terms": {"field": dm_field, "size": 10000, "order": {"_key": "asc"}}}
        else:
            # Use a day-level histogram so that full-datetime kibana_date fields
            # (e.g. Echo's "MMMM dd, yyyy HH:mm:ss UTC" format) are bucketed by
            # calendar day rather than having cardinality count every unique timestamp.
            aggs_body["by_day"] = {
                "date_histogram": {
                    "field": "kibana_date",
                    "calendar_interval": "day",
                    "format": "yyyy-MM-dd",
                    "min_doc_count": 1,
                },
                "aggs": {"no_data_count": {"filter": {"term": {"no_data": True}}}},
            }

        result = es.search(
            index=RoleFilterService.scoped_index_pattern(data_view),
            body={
                "query": query,
                "aggs": aggs_body,
                "size": 0,
                "track_total_hits": True,
            },
            ignore_unavailable=True,
        )

        total_docs = result["hits"]["total"]["value"]

        # when the search matches zero indices (e.g. a data view that
        # resolves to the empty-scope sentinel `*__empty_scope__`,
        # as the cross-tenant `disciple-tools` alias does for every user), ES
        # returns `hits` zeroed but omits the `aggregations` key entirely. Read
        # it defensively so this endpoint fails silent (empty summary) like the
        # dashboard panels do, rather than raising KeyError into the broad except.
        aggregations = result.get("aggregations", {})

        # Extract last pulled timestamp
        max_ts = aggregations.get("max_timestamp", {}).get("value_as_string")
        last_pulled = max_ts  # ISO string or None

        # Build monthly summary by grouping daily buckets into year/month
        monthly_summary = []
        from collections import defaultdict

        if is_disciple_tools:
            # date_modified values are keyword strings like "2025-03-15" —
            # extract unique dates and group by month.
            dm_buckets = aggregations.get("by_date_modified", {}).get("buckets", [])
            monthly_dates = defaultdict(dict)  # (year, month) -> {date_str: doc_count}
            for bucket in dm_buckets:
                date_str = bucket.get("key", "").strip()
                if not date_str:
                    continue
                # Take first 10 chars in case of datetime strings
                date_str = date_str[:10]
                doc_count = bucket.get("doc_count", 1)
                try:
                    year, month, _day = [int(x) for x in date_str.split("-")]
                    monthly_dates[(year, month)][date_str] = monthly_dates[(year, month)].get(date_str, 0) + doc_count
                except ValueError:
                    continue
        else:
            day_buckets = aggregations.get("by_day", {}).get("buckets", [])
            monthly_dates = defaultdict(dict)  # (year, month) -> {date_str: {doc_count, no_data_count}}
            for bucket in day_buckets:
                date_str = bucket.get("key_as_string", "")  # "2025-02-15"
                doc_count = bucket.get("doc_count", 1)
                no_data_count = bucket.get("no_data_count", {}).get("doc_count", 0)
                if not date_str:
                    continue
                try:
                    year, month, _day = [int(x) for x in date_str.split("-")]
                    monthly_dates[(year, month)][date_str] = {
                        "doc_count": doc_count,
                        "no_data_count": no_data_count,
                    }
                except ValueError:
                    continue

        # Both DT and non-DT paths populate monthly_dates — build the summary
        for year, month in sorted(monthly_dates.keys()):
            date_counts = monthly_dates[(year, month)]
            dates_list = []
            for d in sorted(date_counts.keys()):
                info = date_counts[d]
                if isinstance(info, dict):
                    entry = {"date": d, "doc_count": info["doc_count"]}
                    # Mark as no_data if ALL docs in this bucket are no_data markers
                    if info["no_data_count"] > 0 and info["no_data_count"] >= info["doc_count"]:
                        entry["no_data"] = True
                else:
                    # DT path stores plain doc_count integers
                    entry = {"date": d, "doc_count": info}
                dates_list.append(entry)
            # Count dates with real data (exclude no_data-only dates)
            dates_pulled = sum(1 for d in dates_list if not d.get("no_data"))
            no_data_dates = sum(1 for d in dates_list if d.get("no_data"))
            total_days = calendar.monthrange(year, month)[1]
            entry = {
                "year": year,
                "month": month,
                "month_name": calendar.month_name[month],
                "dates_pulled": dates_pulled,
                "total_days": total_days,
                "dates": dates_list,
            }
            if no_data_dates:
                entry["no_data_dates"] = no_data_dates
            monthly_summary.append(entry)

        # When a date range is provided, fill in months that had no data
        # so the full requested range always appears in the table.
        if not is_disciple_tools and from_date and to_date:
            from datetime import date as _date

            try:
                range_start = _date.fromisoformat(from_date)
                range_end = _date.fromisoformat(to_date)
                found = {(r["year"], r["month"]) for r in monthly_summary}
                cy, cm = range_start.year, range_start.month
                ey, em = range_end.year, range_end.month
                while (cy, cm) <= (ey, em):
                    if (cy, cm) not in found:
                        monthly_summary.append(
                            {
                                "year": cy,
                                "month": cm,
                                "month_name": calendar.month_name[cm],
                                "dates_pulled": 0,
                                "total_days": calendar.monthrange(cy, cm)[1],
                                "dates": [],
                            }
                        )
                    cm += 1
                    if cm > 12:
                        cy += 1
                        cm = 1
                monthly_summary.sort(key=lambda r: (r["year"], r["month"]))
            except ValueError:
                pass

        resp_data = {
            "success": True,
            "document_count": total_docs,
            "last_pulled": last_pulled,
            "monthly_summary": monthly_summary,
        }

        # Flag so frontend knows to disable import button
        if is_disciple_tools:
            resp_data["is_disciple_tools"] = True

        return jsonify(resp_data)

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to get data source status",
            log_detail="get_data_source_status failed",
            exc=e,
        )


@app.route("/api/import/data-view-teams")
def get_data_view_teams():
    """
    Return the set of team_title values that have at least one document
    in the given data view (ES index / index pattern).
    Query params:
      data_view - required; ES index name or pattern
    """
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    data_view = request.args.get("data_view", "").strip()
    _gate_2 = _require_index_in_scope(data_view)
    if _gate_2 is not None:
        return _gate_2
    if not data_view:
        return jsonify({"success": False, "error": "data_view is required"}), 400

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        # Wildcard data views can span indices with mixed mappings:
        # some have team_title as pure "keyword", others as "text" with a
        # .keyword subfield.  Always run both aggregations and merge so
        # teams aren't missed when mappings differ across indices.
        # Exclude no_data marker documents so teams with only markers
        # are not falsely counted as having real data.
        teams_with_data = set()
        no_data_filter = {"bool": {"must_not": [{"term": {"no_data": True}}]}}

        # Query 1: team_title.keyword (text+keyword subfield mappings)
        scoped_dv = RoleFilterService.scoped_index_pattern(data_view)
        result = es.search(
            index=scoped_dv,
            body={
                "size": 0,
                "query": no_data_filter,
                "aggs": {"teams": {"terms": {"field": "team_title.keyword", "size": 500}}},
            },
            ignore_unavailable=True,
        )
        teams_with_data.update(b["key"] for b in result["aggregations"].get("teams", {}).get("buckets", []))

        # Query 2: team_title (pure keyword mappings).
        # Running terms on a text field without fielddata raises a 400,
        # so isolate in its own try/except.
        try:
            result2 = es.search(
                index=scoped_dv,
                body={
                    "size": 0,
                    "query": no_data_filter,
                    "aggs": {"teams": {"terms": {"field": "team_title", "size": 500}}},
                },
                ignore_unavailable=True,
            )
            teams_with_data.update(b["key"] for b in result2["aggregations"].get("teams", {}).get("buckets", []))
        except Exception:
            pass  # text field without fielddata — already covered by .keyword query

        return jsonify({"success": True, "teams_with_data": sorted(teams_with_data)})

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to get data view teams",
            log_detail="get_data_view_teams failed",
            exc=e,
        )


@app.route("/api/import/field-values")
def get_field_values():
    """
    Return field values per team for a specific date in a data view.
    Query params:
      data_view - required; ES index name or pattern
      date      - required; YYYY-MM-DD
      field     - required; field name to retrieve
      team      - optional; filter to a specific team
    """
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    data_view = request.args.get("data_view", "").strip()
    _gate_2 = _require_index_in_scope(data_view)
    if _gate_2 is not None:
        return _gate_2
    date = request.args.get("date", "").strip()
    field = request.args.get("field", "").strip()
    team = request.args.get("team", "").strip()

    if not data_view or not date or not field:
        return (
            jsonify({"success": False, "error": "data_view, date, and field are required"}),
            400,
        )

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        # match BOTH DT spellings — the legacy cross-tenant alias
        # `disciple-tools` (hyphen) and the team-keyed `*_disciple_tools`
        # (underscore, "Disciple Tools (by team)" data view). The hyphen-only
        # check silently dropped the DT date-field handling for the team-keyed
        # view (empty monthly table on Data Source Import Status). Mirrors
        # charts.js _isDtDataView.
        is_disciple_tools = "disciple-tools" in data_view.lower() or "disciple_tools" in data_view.lower()

        filters = []
        if is_disciple_tools:
            # date_modified is a keyword field — use prefix match since the
            # stored value may include a time component (e.g. "2025-03-15T00:00:00")
            # but the UI passes just the date portion "2025-03-15".
            # team-keyed *_disciple_tools maps date_modified as a bare
            # keyword (.keyword is unmapped there); the legacy alias is text
            # and needs the subfield.
            filters.append(
                {"prefix": {("date_modified.keyword" if "disciple-tools" in data_view.lower() else "date_modified"): date}}
            )
        else:
            filters.append(
                {
                    "range": {
                        "kibana_date": {
                            "gte": date,
                            "lte": date,
                            "format": "yyyy-MM-dd",
                        }
                    }
                }
            )
        if team:
            filters.append({"match_phrase": {"team_title": team}})

        base_query = {"size": 0, "query": {"bool": {"filter": filters}}}

        def build_agg(team_field):
            return {
                "by_team": {
                    "terms": {"field": team_field, "size": 500},
                    "aggs": {"field_sample": {"top_hits": {"size": 1, "_source": [field]}}},
                }
            }

        def extract_values(result):
            values = []
            for bucket in result["aggregations"].get("by_team", {}).get("buckets", []):
                team = bucket["key"]
                hits = bucket.get("field_sample", {}).get("hits", {}).get("hits", [])
                value = None
                if hits:
                    src = hits[0].get("_source", {})
                    # Support dotted field paths (e.g. "metrics.impressions")
                    parts = field.split(".")
                    val = src
                    for part in parts:
                        if isinstance(val, dict):
                            val = val.get(part)
                        else:
                            val = None
                            break
                    value = val
                values.append({"team_title": team, "value": value})
            return values

        # Step 1: try team_title.keyword (text+keyword subfield mappings)
        body = {**base_query, "aggs": build_agg("team_title.keyword")}
        scoped_dv = RoleFilterService.scoped_index_pattern(data_view)
        result = es.search(index=scoped_dv, body=body, ignore_unavailable=True)
        values = extract_values(result)

        # Step 2: fall back to base team_title field if no results
        if not values:
            try:
                body2 = {**base_query, "aggs": build_agg("team_title")}
                result2 = es.search(index=scoped_dv, body=body2, ignore_unavailable=True)
                values = extract_values(result2)
            except Exception:
                pass

        return jsonify({"success": True, "values": sorted(values, key=lambda v: v["team_title"])})

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to get field values",
            log_detail="get_field_values failed",
            exc=e,
        )


@app.route("/api/import/all-field-values")
def get_all_field_values():
    """
    Return all field values from documents matching team + date.
    Query params:
      data_view - required; ES index name or pattern
      date      - required; YYYY-MM-DD
      team      - required; team title
      doc_index - optional; 0-based index to select a specific document (default: 0)
    Returns: { success: true, fields: [...], total_docs: N, doc_index: I }
    """
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    data_view = request.args.get("data_view", "").strip()
    _gate_2 = _require_index_in_scope(data_view)
    if _gate_2 is not None:
        return _gate_2
    date = request.args.get("date", "").strip()
    team = request.args.get("team", "").strip()
    doc_index = request.args.get("doc_index", "0").strip()

    if not data_view or not date or not team:
        return (
            jsonify({"success": False, "error": "data_view, date, and team are required"}),
            400,
        )

    try:
        doc_index = max(0, int(doc_index))
    except ValueError:
        doc_index = 0

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        # match BOTH DT spellings — the legacy cross-tenant alias
        # `disciple-tools` (hyphen) and the team-keyed `*_disciple_tools`
        # (underscore, "Disciple Tools (by team)" data view). The hyphen-only
        # check silently dropped the DT date-field handling for the team-keyed
        # view (empty monthly table on Data Source Import Status). Mirrors
        # charts.js _isDtDataView.
        is_disciple_tools = "disciple-tools" in data_view.lower() or "disciple_tools" in data_view.lower()

        filters = []
        if is_disciple_tools:
            # date_modified is a keyword field — use prefix match since the
            # stored value may include a time component (e.g. "2025-03-15T00:00:00")
            # but the UI passes just the date portion "2025-03-15".
            # team-keyed *_disciple_tools maps date_modified as a bare
            # keyword (.keyword is unmapped there); the legacy alias is text
            # and needs the subfield.
            filters.append(
                {"prefix": {("date_modified.keyword" if "disciple-tools" in data_view.lower() else "date_modified"): date}}
            )
        else:
            filters.append(
                {
                    "range": {
                        "kibana_date": {
                            "gte": date,
                            "lte": date,
                            "format": "yyyy-MM-dd",
                        }
                    }
                }
            )
        filters.append({"match_phrase": {"team_title": team}})

        # First, count total docs for this date+team
        # scope the count to the caller's allowed indices, same as the
        # es.search below. Counting over the raw client-supplied data_view let a
        # caller probe document counts for an arbitrary team in a cross-tenant
        # index (the count alone is an information leak, and a >0 count let the
        # request fall through to the scoped fetch).
        count_body = {
            "query": {"bool": {"filter": filters}},
        }
        count_result = es.count(
            index=RoleFilterService.scoped_index_pattern(data_view), body=count_body, ignore_unavailable=True
        )
        total_docs = count_result.get("count", 0)

        if total_docs == 0:
            return jsonify({"success": True, "fields": [], "total_docs": 0, "doc_index": 0})

        # Fetch the requested document by index (using from/size pagination)
        body = {
            "from": doc_index,
            "size": 1,
            "query": {"bool": {"filter": filters}},
            "_source": True,
            "sort": [{"_doc": "asc"}],
        }

        result = es.search(index=RoleFilterService.scoped_index_pattern(data_view), body=body, ignore_unavailable=True)
        hits = result.get("hits", {}).get("hits", [])

        if not hits:
            return jsonify({"success": True, "fields": [], "total_docs": total_docs, "doc_index": doc_index})

        source = hits[0].get("_source", {})

        def flatten(obj, prefix=""):
            """Flatten nested dicts into dotted keys."""
            items = []
            for k, v in obj.items():
                key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
                if isinstance(v, dict):
                    items.extend(flatten(v, key))
                else:
                    items.append({"name": key, "value": v})
            return items

        fields = flatten(source)
        fields.sort(key=lambda f: f["name"])

        return jsonify(
            {
                "success": True,
                "fields": fields,
                "total_docs": total_docs,
                "doc_index": doc_index,
            }
        )

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to get all field values",
            log_detail="get_all_field_values failed",
            exc=e,
        )


@app.route("/api/import/field-values-across-docs")
def get_field_values_across_docs():
    """
    Return the value of one field across every doc for a (team, date) pair.

    Powers the field-value popup on the Data Import Status page:
    when there is more than one doc for a given team+date, clicking a
    field name shows that field's value for each doc side-by-side instead
    of forcing the user to flip through the doc-selector dropdown.

    Query params:
      data_view - required; ES index name or pattern
      date      - required; YYYY-MM-DD
      team      - required; team title
      field     - required; dotted field path (e.g. "insight_spend",
                  "creative_details.actor_id")

    Returns: { success, values: [{doc_index, value}], total_docs }
    """
    _gate_3 = _require_import_data_permission()
    if _gate_3:
        return _gate_3
    data_view = request.args.get("data_view", "").strip()
    _gate_2 = _require_index_in_scope(data_view)
    if _gate_2 is not None:
        return _gate_2
    date = request.args.get("date", "").strip()
    team = request.args.get("team", "").strip()
    field = request.args.get("field", "").strip()

    if not data_view or not date or not team or not field:
        return (
            jsonify({"success": False, "error": "data_view, date, team, and field are required"}),
            400,
        )

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        # match BOTH DT spellings — the legacy cross-tenant alias
        # `disciple-tools` (hyphen) and the team-keyed `*_disciple_tools`
        # (underscore, "Disciple Tools (by team)" data view). The hyphen-only
        # check silently dropped the DT date-field handling for the team-keyed
        # view (empty monthly table on Data Source Import Status). Mirrors
        # charts.js _isDtDataView.
        is_disciple_tools = "disciple-tools" in data_view.lower() or "disciple_tools" in data_view.lower()

        filters = []
        if is_disciple_tools:
            # team-keyed *_disciple_tools maps date_modified as a bare
            # keyword (.keyword is unmapped there); the legacy alias is text
            # and needs the subfield.
            filters.append(
                {"prefix": {("date_modified.keyword" if "disciple-tools" in data_view.lower() else "date_modified"): date}}
            )
        else:
            filters.append(
                {
                    "range": {
                        "kibana_date": {
                            "gte": date,
                            "lte": date,
                            "format": "yyyy-MM-dd",
                        }
                    }
                }
            )
        filters.append({"match_phrase": {"team_title": team}})

        # Pull every matching doc — capped at 1000 so a misclick on a busy
        # day can't fetch a giant payload. The popup only renders one row
        # per doc, so 1000 is comfortably more than any realistic case.
        body = {
            "size": 1000,
            "query": {"bool": {"filter": filters}},
            "_source": [field],
            "sort": [{"_doc": "asc"}],
        }

        result = es.search(index=RoleFilterService.scoped_index_pattern(data_view), body=body, ignore_unavailable=True)
        hits = result.get("hits", {}).get("hits", [])

        parts = field.split(".")

        def resolve_dotted(src):
            val = src
            for part in parts:
                if isinstance(val, dict):
                    val = val.get(part)
                else:
                    return None
            return val

        values = [{"doc_index": idx, "value": resolve_dotted(hit.get("_source", {}))} for idx, hit in enumerate(hits)]

        return jsonify(
            {
                "success": True,
                "values": values,
                "total_docs": len(values),
            }
        )

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to get field values across docs",
            log_detail="get_field_values_across_docs failed",
            exc=e,
        )


# ============================================
# DATA INVENTORY ROUTES
# ============================================
#
# A workspace-tier, read-only page for seeing at a glance what data a team
# (or group of teams) has, what dates it covers, and what its fields contain.
# Gated on mc_data_inventory. Unlike the /api/import/* family, every endpoint
# here narrows to a single CHOSEN workspace via
# RoleFilterService.workspace_scoped_index_pattern — the workspace switcher.
#
# Canonical data-view registry, mirroring deployment/create-data-views.sh.
# Each entry is the wildcard ES pattern + a human label. The scoped list
# endpoint hides any view whose pattern resolves to the empty-scope sentinel
# for the active workspace (i.e. the workspace has no matching indices).
# Canonical data views, mirroring deployment/create-data-views.sh. Excludes
# de_dashboard_templates (not a data view) and the retired legacy
# `disciple-tools` hyphen alias — the team-keyed `*_disciple_tools` is the
# live one. Keep in sync with create-data-views.sh.
# kept in alphabetical order by label (the dropdown renders them in
# this order). The two deduplicated-window views (*_fb_insights_window,
# *_ga_metrics_window) are intentionally NOT listed — they are internal dedup
# indices, not user-facing data views, and confused users in the picker.
_DATA_INVENTORY_VIEWS = [
    {"pattern": "*_chatwoot_conversations", "label": "Chatwoot Conversations"},
    {"pattern": "*_chatwoot_insights", "label": "Chatwoot Insights"},
    {"pattern": "*_disciple_tools", "label": "Disciple Tools (by team)"},
    {"pattern": "*_echo_conversations", "label": "Echo Conversations"},
    {"pattern": "*_fb_insights", "label": "Facebook Ad Insights"},
    {"pattern": "*_fb_page_insights", "label": "Facebook Page Insights"},
    {"pattern": "*_fb_page_posts", "label": "Facebook Page Posts"},
    {"pattern": "*_google_ads", "label": "Google Ads"},
    {"pattern": "*_ga_metrics", "label": "Google Analytics"},
    {"pattern": "*_ig_insights", "label": "Instagram Insights"},
    {"pattern": "*_tiktok_organic", "label": "TikTok Organic"},
    {"pattern": "*_youtube_analytics", "label": "YouTube Analytics"},
]

# The no-match sentinel scoped_index_pattern returns for an empty allowlist.
_EMPTY_SCOPE_MARKER = "empty_scope_2"


def _require_data_inventory():
    """Permission gate for Data Inventory routes.

    Returns None when allowed, or a Flask redirect/JSON-403 when not.
    MUST pass username to get_merged_permissions so the System-2
    bridge fires — workspace owners/admins have empty session["roles"] and
    reach mc_data_inventory only through the bridge, never the legacy bag.
    """
    perms = RolePermissionsService.get_merged_permissions(
        session.get("roles", []),
        username=session.get("username", "") or None,
    )
    if not perms.get("mc_data_inventory"):
        return False
    return True


@app.route("/data-inventory")
def data_inventory():
    """Display the Data Inventory page."""
    if not _require_data_inventory():
        return _redirect_to_allowed_page()
    return render_template("data_inventory.html")


@app.route("/api/data-inventory/workspaces")
def data_inventory_workspaces():
    """List the workspaces the current user may inspect — populates the
    workspace switcher. Returns esid + title, sorted by title.

     (item 1): scope is narrowed to data-grant + primary workspace
    only (WorkspaceMembershipService.data_grant_and_primary_workspaces) —
    NOT RoleFilterService.get_allowed_workspaces(), whose broad union let a
    superuser see every workspace. Always a concrete list (never None), so
    there is no match-all-everything path.
    """
    if not _require_data_inventory():
        return jsonify({"success": False, "error": "forbidden"}), 403

    try:
        from lib.es_helpers import get_elasticsearch_client
        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService,
        )

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        allowed = WorkspaceMembershipService.data_grant_and_primary_workspaces(session.get("username", ""))
        if not allowed:
            return jsonify({"success": True, "workspaces": []})

        resp = es.search(
            index="de_asset_workspace",
            body={"query": {"terms": {"_id": list(allowed)}}, "_source": ["workspace_title", "title"], "size": 1000},
            ignore_unavailable=True,
        )
        workspaces = []
        for hit in resp.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            title = (src.get("workspace_title") or src.get("title") or "").strip() or hit["_id"]
            workspaces.append({"workspace_esid": hit["_id"], "workspace_title": title})
        workspaces.sort(key=lambda w: w["workspace_title"].lower())
        return jsonify({"success": True, "workspaces": workspaces})

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to list workspaces",
            log_detail="data_inventory_workspaces failed",
            exc=e,
        )


@app.route("/api/data-inventory/data-views")
def data_inventory_data_views():
    """List data views that have ≥1 index in the chosen workspace's scope.

    Query params:
      workspace - required; the active workspace_esid from the switcher.

    A view is included only if workspace_scoped_index_pattern resolves it
    to a concrete (non-sentinel) index list for this user + workspace.
    """
    if not _require_data_inventory():
        return jsonify({"success": False, "error": "forbidden"}), 403

    workspace = request.args.get("workspace", "").strip()
    if not workspace:
        return jsonify({"success": False, "error": "workspace is required"}), 400

    try:
        views = []
        for entry in _DATA_INVENTORY_VIEWS:
            scoped = RoleFilterService.workspace_scoped_index_pattern(entry["pattern"], workspace)
            if _EMPTY_SCOPE_MARKER in scoped:
                continue  # no indices for this view in this workspace's scope
            views.append({"id": entry["pattern"], "label": entry["label"]})
        return jsonify({"success": True, "data_views": views})

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to list data views",
            log_detail="data_inventory_data_views failed",
            exc=e,
        )


@app.route("/api/data-inventory/teams")
def data_inventory_teams():
    """Org→team tree for the chosen workspace, limited to teams with data
    in the given view. Powers the report-scheduler-style checkbox tree.

    Query params:
      workspace - required; active workspace_esid.
      data_view - required; the wildcard pattern from the data-views list.

    Returns {orgs: [{org_title, teams: [{title}]}]}. Team checkbox VALUE is
    the team_title (the field the grid aggregates on). Only teams with ≥1
    non-no_data document in this view are included, grouped under their org.
    """
    if not _require_data_inventory():
        return jsonify({"success": False, "error": "forbidden"}), 403

    workspace = request.args.get("workspace", "").strip()
    data_view = request.args.get("data_view", "").strip()
    _gate_2 = _require_index_in_scope(data_view)
    if _gate_2 is not None:
        return _gate_2
    if not workspace or not data_view:
        return jsonify({"success": False, "error": "workspace and data_view are required"}), 400

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        scoped = RoleFilterService.workspace_scoped_index_pattern(data_view, workspace)
        if _EMPTY_SCOPE_MARKER in scoped:
            return jsonify({"success": True, "orgs": []})

        # Titles that actually have data in this view (the existing dual-field agg).
        no_data_filter = {"bool": {"must_not": [{"term": {"no_data": True}}]}}
        titles_with_data: set[str] = set()
        for field in ("team_title.keyword", "team_title"):
            try:
                resp = es.search(
                    index=scoped,
                    body={
                        "size": 0,
                        "query": no_data_filter,
                        "aggs": {"teams": {"terms": {"field": field, "size": 1000}}},
                    },
                    ignore_unavailable=True,
                )
                titles_with_data.update(b["key"] for b in resp.get("aggregations", {}).get("teams", {}).get("buckets", []))
            except Exception:
                pass  # text field without fielddata — covered by .keyword

        if not titles_with_data:
            return jsonify({"success": True, "orgs": []})

        # Group those titles by org via the workspace's org→team walk.
        # de_asset_organization (workspace_esid) → de_asset_teams (organization_esid).
        org_titles: dict[str, str] = {}
        try:
            orgs_resp = es.search(
                index="de_asset_organization",
                body={
                    "query": {"term": {"workspace_esid": workspace}},
                    "_source": ["organization_title", "title"],
                    "size": 1000,
                },
                ignore_unavailable=True,
            )
            for h in orgs_resp.get("hits", {}).get("hits", []):
                src = h.get("_source") or {}
                org_titles[h["_id"]] = (src.get("organization_title") or src.get("title") or "").strip() or "(Unassigned)"
        except Exception:
            pass

        groups: dict[str, list[str]] = {}
        if org_titles:
            teams_resp = es.search(
                index="de_asset_teams",
                body={
                    # bare organization_esid (explicit keyword post-reindex).
                    "query": {"terms": {"organization_esid": list(org_titles.keys())}},
                    "_source": ["team_title", "organization_esid"],
                    "size": 1000,
                },
                ignore_unavailable=True,
            )
            for h in teams_resp.get("hits", {}).get("hits", []):
                src = h.get("_source") or {}
                title = (src.get("team_title") or "").strip()
                if title not in titles_with_data:
                    continue
                org_esid_field = src.get("organization_esid")
                org_esid = org_esid_field[0] if isinstance(org_esid_field, list) and org_esid_field else org_esid_field
                org_label = org_titles.get(org_esid, "(Unassigned)")
                groups.setdefault(org_label, []).append(title)

        # Any data-bearing title not matched to an org falls under (Unassigned).
        matched = {t for tms in groups.values() for t in tms}
        for title in titles_with_data - matched:
            groups.setdefault("(Unassigned)", []).append(title)

        orgs = [
            {"org_title": org, "teams": [{"title": t} for t in sorted(set(tms), key=str.lower)]}
            for org, tms in sorted(groups.items(), key=lambda kv: kv[0].lower())
        ]
        return jsonify({"success": True, "orgs": orgs})

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to list teams",
            log_detail="data_inventory_teams failed",
            exc=e,
        )


def _simplify_inventory_field_type(es_type: str) -> str:
    """Collapse an ES mapping type to inventory categories. Mirrors the
    dashboard-builder `_simplify_es_type` so numeric detection matches the
    field picker the rest of the app uses."""
    if es_type == "date":
        return "date"
    if es_type == "boolean":
        return "boolean"
    if es_type in ("text",):
        return "text"
    if es_type in ("keyword", "constant_keyword", "wildcard"):
        return "keyword"
    if es_type in (
        "long",
        "integer",
        "short",
        "byte",
        "float",
        "double",
        "half_float",
        "scaled_float",
        "unsigned_long",
    ):
        return "numeric"
    return "keyword"


def _is_summable_field_name(name: str) -> bool:
    """(item 8): a numeric-TYPED field is only summable if it's a real
    metric, not a date stored as epoch (date_created_ms, *_date_ms,
    baptism_date_ms) or an identifier (ID, *_id). Summing a date/epoch is
    meaningless and ES 400s on a date subfield. Exclude by name; the ES type
    check already drops `date`-typed fields, this catches the long/epoch ones.
    """
    leaf = name.rsplit(".", 1)[-1].lower()
    if leaf in ("id",) or leaf.endswith("_id") or leaf.endswith("_ms"):
        return False
    if "date" in leaf or "timestamp" in leaf or leaf.endswith("_at"):
        return False
    return True


def _flatten_inventory_numeric_fields(props: dict, prefix: str = "") -> list[dict]:
    """Walk an ES mapping and return the SUMMABLE numeric leaf fields as
    [{name, type}]. Sub-objects recurse; multi-fields (.keyword etc.) are
    skipped. Date-epoch and identifier fields are excluded (see
    _is_summable_field_name) so the grid never offers a column whose sum
    is meaningless or errors."""
    out: list[dict] = []
    for fname, fmeta in props.items():
        full = f"{prefix}.{fname}" if prefix else fname
        ftype = fmeta.get("type", "")
        if ftype and _simplify_inventory_field_type(ftype) == "numeric" and _is_summable_field_name(full):
            out.append({"name": full, "type": "numeric"})
        if "properties" in fmeta:
            out.extend(_flatten_inventory_numeric_fields(fmeta["properties"], full))
    return out


def _aggregatable_keyword_target(fmeta: dict, simplified: str) -> str | None:
    """the aggregatable field to run a terms agg on for a non-numeric
    leaf, or None when the field cannot be aggregated (a `text`-only field with
    no `.keyword` multi-field — a terms agg 400s on it).

    * keyword / boolean / date are aggregatable as-is.
    * text is aggregatable only via a `.keyword` (or any keyword-typed) sub-field
      declared under `fields` — mirror the mixed text+keyword mapping the totals
      grid already contends with."""
    if simplified in ("keyword", "boolean", "date"):
        return "__self__"
    if simplified == "text":
        subfields = fmeta.get("fields", {})
        # Prefer the conventional `.keyword` name, else the first keyword subfield.
        if isinstance(subfields.get("keyword"), dict) and subfields["keyword"].get("type") == "keyword":
            return "keyword"
        for sub_name, sub_meta in subfields.items():
            if isinstance(sub_meta, dict) and sub_meta.get("type") == "keyword":
                return sub_name
    return None


# the deep-union mapping merge moved to lib.es_helpers so
# the dashboard-builder /fields endpoint and the NLQ service can share it
# (this module can't be imported from the blueprint — circular). Re-exported
# under the original private name for existing in-module callers and the
# release-plan tests that import it from here.
from lib.es_helpers import (  # noqa: E402
    merge_index_mapping_properties as _merge_index_mapping_properties,
)


def _flatten_inventory_nonnumeric_fields(props: dict, prefix: str = "") -> list[dict]:
    """sibling of _flatten_inventory_numeric_fields: return the
    NON-numeric leaf fields (text / keyword / date / boolean) as
    [{name, type, agg_field}] where agg_field is the fully-qualified field a
    terms agg should target (the `.keyword` multi-field for text, else the base
    field) or None when the field has no aggregatable form. Sub-objects recurse;
    multi-field sub-keys under `fields` are NOT emitted as their own rows (the
    base field carries them via agg_field)."""
    out: list[dict] = []
    for fname, fmeta in props.items():
        full = f"{prefix}.{fname}" if prefix else fname
        ftype = fmeta.get("type", "")
        if ftype:
            simplified = _simplify_inventory_field_type(ftype)
            if simplified in ("text", "keyword", "date", "boolean"):
                target = _aggregatable_keyword_target(fmeta, simplified)
                if target == "__self__":
                    agg_field: str | None = full
                elif target:
                    agg_field = f"{full}.{target}"
                else:
                    agg_field = None
                out.append({"name": full, "type": simplified, "agg_field": agg_field})
        if "properties" in fmeta:
            out.extend(_flatten_inventory_nonnumeric_fields(fmeta["properties"], full))
    return out


@app.route("/api/data-inventory/fields")
def data_inventory_fields():
    """List the fields of a data view, for the chosen workspace's scope.

    Query params:
      workspace - required; active workspace_esid.
      data_view - required; the wildcard pattern from the data-views list.
      kind      - optional; "numeric" (default) or "non-numeric".

    kind=numeric returns only summable numeric leaf fields — the columns the
    grid can total. kind=non-numeric returns text/keyword/date/boolean leaves
    with the aggregatable field for each.
    Reads the mapping of the workspace-scoped index set.
    """
    if not _require_data_inventory():
        return jsonify({"success": False, "error": "forbidden"}), 403

    workspace = request.args.get("workspace", "").strip()
    data_view = request.args.get("data_view", "").strip()
    _gate_2 = _require_index_in_scope(data_view)
    if _gate_2 is not None:
        return _gate_2
    kind = (request.args.get("kind", "") or "numeric").strip().lower()
    if not workspace or not data_view:
        return jsonify({"success": False, "error": "workspace and data_view are required"}), 400

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        scoped = RoleFilterService.workspace_scoped_index_pattern(data_view, workspace)
        if _EMPTY_SCOPE_MARKER in scoped:
            return jsonify({"success": True, "fields": []})

        mapping = es.indices.get_mapping(index=scoped, ignore_unavailable=True)
        merged_props = _merge_index_mapping_properties(mapping)

        if kind == "non-numeric":
            fields = _flatten_inventory_nonnumeric_fields(merged_props)
        else:
            fields = _flatten_inventory_numeric_fields(merged_props)
        fields.sort(key=lambda f: f["name"])
        return jsonify({"success": True, "fields": fields})

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to list fields",
            log_detail="data_inventory_fields failed",
            exc=e,
        )


@app.route("/api/data-inventory/totals", methods=["POST"])
def data_inventory_totals():
    """Per-team sums of the requested numeric fields, for the chosen
    workspace + date range.

    JSON body (POST — a wide view's field list makes a GET query
    string long enough to be rejected at the proxy before reaching the app):
      workspace - required; active workspace_esid.
      data_view - required; the wildcard pattern.
      fields    - list of numeric field names to sum (may be empty: a
                  contact/conversation view shows only the record count).
      teams     - optional list of team_title to filter (default: all in scope).
      from_date - optional; YYYY-MM-DD lower bound.
      to_date   - optional; YYYY-MM-DD upper bound.

    Returns rows = [{team_title, record_count, values: {field: sum}}] plus
    grand totals. The aggregation is a single `terms` on team × `sum` per
    field over the workspace-scoped index set — no row can belong to a
    foreign tenant.
    """
    if not _require_data_inventory():
        return jsonify({"success": False, "error": "forbidden"}), 403

    body = request.get_json(silent=True) or {}
    workspace = (body.get("workspace") or "").strip()
    data_view = (body.get("data_view") or "").strip()
    from_date = (body.get("from_date") or "").strip()
    to_date = (body.get("to_date") or "").strip()

    if not workspace or not data_view:
        return jsonify({"success": False, "error": "workspace and data_view are required"}), 400

    # (item 3): fields may be empty — a contact/conversation view (DT,
    # Echo, Chatwoot) has no summable numerics and shows only the record count.
    fields = [str(f).strip() for f in (body.get("fields") or []) if str(f).strip()]
    teams = [str(t).strip() for t in (body.get("teams") or []) if str(t).strip()]

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        scoped = RoleFilterService.workspace_scoped_index_pattern(data_view, workspace)
        if _EMPTY_SCOPE_MARKER in scoped:
            return jsonify({"success": True, "rows": [], "totals": {}, "fields": fields})

        # DT date field differs (mirrors get_data_source_status / _isDtDataView).
        is_dt = "disciple-tools" in data_view.lower() or "disciple_tools" in data_view.lower()
        date_field = (
            ("first_contact_date" if "disciple-tools" in data_view.lower() else "date_created_ms") if is_dt else "kibana_date"
        )

        must = [{"bool": {"must_not": [{"term": {"no_data": True}}]}}]
        if teams:
            # (item 6): a bare `terms` on team_title silently drops
            # multi-word titles ("Kavanah Team") on text-mapped schemas — the
            # text analyzer tokenizes the field, so the exact phrase never
            # matches and only single-token teams survived. Match on the
            # keyword subfield (exact) OR a per-team match_phrase (handles the
            # text-only schemas where .keyword is unmapped). minimum_should=1.
            team_should = [{"terms": {"team_title.keyword": teams}}]
            for t in teams:
                team_should.append({"match_phrase": {"team_title": t}})
            must.append({"bool": {"should": team_should, "minimum_should_match": 1}})
        if from_date or to_date:
            rng = {"format": "yyyy-MM-dd"}
            if from_date:
                rng["gte"] = from_date
            if to_date:
                rng["lte"] = to_date
            must.append({"range": {date_field: rng}})

        # Sanity bound on column count. The real "Could not load" cause was the
        # GET URL length (now POST), not ES — 106 sum sub-aggs run fine — so
        # this is just a guard against a pathological request, set well above
        # the widest real view (FB Ad Insights ~106 summable fields).
        _MAX_FIELDS = 150
        truncated = len(fields) > _MAX_FIELDS
        fields = fields[:_MAX_FIELDS]

        sum_aggs = {f"sum_{i}": {"sum": {"field": f}} for i, f in enumerate(fields)}

        def run(team_field: str) -> list:
            body = {
                "size": 0,
                "query": {"bool": {"filter": must}},
                "aggs": {"by_team": {"terms": {"field": team_field, "size": 1000}, "aggs": sum_aggs}},
            }
            resp = es.search(index=scoped, body=body, ignore_unavailable=True)
            return resp.get("aggregations", {}).get("by_team", {}).get("buckets", [])

        # (item 2 / item 6): UNION both team-field aggs, do NOT fall
        # back only. team_title is keyword on some indices and text+keyword on
        # others, and the two aggs return DISJOINT team sets (a keyword index
        # has no .keyword subfield; a text index needs it). Falling back only
        # when .keyword is empty meant the pure-keyword teams were dropped
        # whenever any .keyword team existed — so only some teams showed. Merge
        # by team_title (a team lives in exactly one mapping, so no double count).
        merged: dict = {}
        for team_field in ("team_title.keyword", "team_title"):
            try:
                for b in run(team_field):
                    key = b["key"]
                    if key in merged:
                        continue  # same team can't be in both mappings; first wins
                    merged[key] = b
            except Exception:
                # text field without fielddata (no .keyword on a pure-text
                # index) raises 400 — the other field covers those teams.
                pass

        rows = []
        totals = {f: 0.0 for f in fields}
        # (item 3): always carry a per-team record count. For
        # conversation/contact views (Disciple Tools, Echo, Chatwoot) there are
        # no summable numeric fields — the COUNT of records (contacts,
        # conversations) is the meaningful number — and it's a free byproduct
        # of the team terms agg's doc_count.
        record_total = 0
        for key, b in merged.items():
            values = {}
            for i, f in enumerate(fields):
                v = b.get(f"sum_{i}", {}).get("value") or 0.0
                values[f] = v
                totals[f] += v
            count = b.get("doc_count", 0)
            record_total += count
            rows.append({"team_title": key, "record_count": count, "values": values})
        rows.sort(key=lambda r: r["team_title"].lower())

        out = {
            "success": True,
            "rows": rows,
            "totals": totals,
            "record_total": record_total,
            "fields": fields,
        }
        if truncated:
            out["truncated_to"] = _MAX_FIELDS
        return jsonify(out)

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to compute totals",
            log_detail="data_inventory_totals failed",
            exc=e,
        )


def _resolve_top_values_agg_field(merged_props: dict, field: str) -> str | None:
    """given the merged mapping and a base (non-numeric) field name,
    return the aggregatable field a terms agg should target, or None when the
    field is a `text`-only field with no keyword sub-field (a terms agg 400s on
    it). Walks the dotted path through nested `properties`. This is re-derived
    server-side (never trusted from the client) so scoping and aggregatability
    are decided by the mapping, not the request."""
    node = merged_props
    fmeta: dict = {}
    for part in field.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        fmeta = node[part]
        node = fmeta.get("properties", {})
    simplified = _simplify_inventory_field_type(fmeta.get("type", ""))
    if simplified == "numeric":
        # Not a non-numeric field — refuse (this endpoint is for categorical/
        # text/date/boolean values, not metrics).
        return None
    target = _aggregatable_keyword_target(fmeta, simplified)
    if target == "__self__":
        return field
    if target:
        return f"{field}.{target}"
    return None


@app.route("/api/data-inventory/top-values", methods=["POST"])
def data_inventory_top_values():
    """top values (by doc count) of ONE non-numeric field, scoped to
    the chosen workspace + selected teams + date range.

    JSON body (POST — mirrors data_inventory_totals; keeps the field/team lists
    off the query string):
      workspace - required; active workspace_esid.
      data_view - required; the wildcard pattern.
      field     - required; the base (non-numeric) field name to break down.
      teams     - optional list of team_title to filter (default: all in scope).
      from_date - optional; YYYY-MM-DD lower bound.
      to_date   - optional; YYYY-MM-DD upper bound.
      size      - optional; top-N (default 20, capped 200).

    Returns {values: [{value, count}], other_count, agg_field}. A text field
    with no aggregatable keyword sub-field returns success=True,
    aggregatable=False so the UI can disable/explain the link rather than error.
    Every query runs against the workspace-scoped index set — no value can leak
    from a foreign tenant.
    """
    if not _require_data_inventory():
        return jsonify({"success": False, "error": "forbidden"}), 403

    body = request.get_json(silent=True) or {}
    workspace = (body.get("workspace") or "").strip()
    data_view = (body.get("data_view") or "").strip()
    field = (body.get("field") or "").strip()
    from_date = (body.get("from_date") or "").strip()
    to_date = (body.get("to_date") or "").strip()

    if not workspace or not data_view or not field:
        return jsonify({"success": False, "error": "workspace, data_view and field are required"}), 400

    try:
        size = int(body.get("size") or 20)
    except (TypeError, ValueError):
        size = 20
    size = max(1, min(size, 200))

    teams = [str(t).strip() for t in (body.get("teams") or []) if str(t).strip()]

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        scoped = RoleFilterService.workspace_scoped_index_pattern(data_view, workspace)
        if _EMPTY_SCOPE_MARKER in scoped:
            return jsonify({"success": True, "values": [], "other_count": 0, "aggregatable": True})

        mapping = es.indices.get_mapping(index=scoped, ignore_unavailable=True)
        merged_props = _merge_index_mapping_properties(mapping)

        agg_field = _resolve_top_values_agg_field(merged_props, field)
        if not agg_field:
            # text-only (no keyword subfield) or unknown/numeric field — the UI
            # disables the link; report it explicitly rather than 400-ing.
            return jsonify({"success": True, "values": [], "other_count": 0, "aggregatable": False, "field": field})

        date_field = _inventory_date_field(data_view)

        must = [{"bool": {"must_not": [{"term": {"no_data": True}}]}}]
        if teams:
            # Mixed keyword/text+keyword team_title mapping (mirrors
            # data_inventory_totals / _backfill_report): keyword subfield exact
            # OR per-team match_phrase, minimum_should_match=1.
            team_should = [{"terms": {"team_title.keyword": teams}}]
            for t in teams:
                team_should.append({"match_phrase": {"team_title": t}})
            must.append({"bool": {"should": team_should, "minimum_should_match": 1}})
        if from_date or to_date:
            rng = {"format": "yyyy-MM-dd"}
            if from_date:
                rng["gte"] = from_date
            if to_date:
                rng["lte"] = to_date
            must.append({"range": {date_field: rng}})

        agg_body = {
            "size": 0,
            "query": {"bool": {"filter": must}},
            "aggs": {
                "top": {
                    "terms": {"field": agg_field, "size": size},
                }
            },
        }
        resp = es.search(index=scoped, body=agg_body, ignore_unavailable=True)
        top = resp.get("aggregations", {}).get("top", {})
        buckets = top.get("buckets", [])
        values = [{"value": b.get("key"), "count": b.get("doc_count", 0)} for b in buckets]
        # sum_other_doc_count: how many docs fell outside the top-N (so the UI
        # can honestly say "and N more values").
        other_count = top.get("sum_other_doc_count", 0)
        return jsonify(
            {
                "success": True,
                "values": values,
                "other_count": other_count,
                "aggregatable": True,
                "field": field,
                "agg_field": agg_field,
            }
        )

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to load field values",
            log_detail="data_inventory_top_values failed",
            exc=e,
        )


@app.route("/api/data-inventory/coverage")
def data_inventory_coverage():
    """Phase 3 — date coverage for ONE team in a data view: how far
    back the data goes and which days are missing.

    Query params:
      workspace - required; active workspace_esid.
      data_view - required; the wildcard pattern.
      team      - required; team_title.
      from_date - optional; YYYY-MM-DD lower bound (default: earliest data).
      to_date   - optional; YYYY-MM-DD upper bound (default: latest data).

    Returns:
      first_date / last_date  - the span of real data,
      last_pulled             - max @timestamp (when the importer last wrote),
      days: [{date, doc_count, state}] where state is one of
            'data'     — ≥1 real document that day,
            'no_data'  — no real document that day.
      A day-level date_histogram with min_doc_count:0 makes interior gaps
      explicit; the strip spans the full requested date range.

      Coverage reports only what it can prove — data present or absent. It does
      NOT claim WHY a day is empty: no_data markers were retired and
      without a per-run record we cannot tell "source had nothing" apart from
      "the import failed for that team/day". Surfacing a recorded source-empty
      / error state would need a per-team import-run index (deferred).
    """
    if not _require_data_inventory():
        return jsonify({"success": False, "error": "forbidden"}), 403

    workspace = request.args.get("workspace", "").strip()
    data_view = request.args.get("data_view", "").strip()
    _gate_2 = _require_index_in_scope(data_view)
    if _gate_2 is not None:
        return _gate_2
    team = request.args.get("team", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()

    if not workspace or not data_view or not team:
        return jsonify({"success": False, "error": "workspace, data_view, and team are required"}), 400

    try:
        from datetime import datetime, timedelta

        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        scoped = RoleFilterService.workspace_scoped_index_pattern(data_view, workspace)
        if _EMPTY_SCOPE_MARKER in scoped:
            return jsonify({"success": True, "days": [], "first_date": None, "last_date": None, "last_pulled": None})

        # Date field per view (mirrors data_inventory_totals / get_data_source_status).
        is_dt = "disciple-tools" in data_view.lower() or "disciple_tools" in data_view.lower()
        date_field = (
            ("first_contact_date" if "disciple-tools" in data_view.lower() else "date_created_ms") if is_dt else "kibana_date"
        )

        # Team filter handling the mixed keyword/text+keyword mapping.
        team_filter = {
            "bool": {
                "should": [
                    {"term": {"team_title.keyword": team}},
                    {"match_phrase": {"team_title": team}},
                ],
                "minimum_should_match": 1,
            }
        }

        date_range = {}
        if from_date:
            date_range["gte"] = from_date
        if to_date:
            date_range["lte"] = to_date
        range_clause = {"range": {date_field: {**date_range, "format": "yyyy-MM-dd"}}} if date_range else None

        # Real-data histogram. Markers are dead (stopped writing them;
        # is a deliberate decision, not a bug), so coverage no longer
        # tries to read a no_data:true marker — without a per-run record we
        # cannot prove WHY a day is empty (source genuinely had nothing vs the
        # import failed for that team/day). So coverage reports only what it can
        # prove: a day has real data, or it does not. min_doc_count:0 fills the
        # interior gaps between the first and last real doc as zero-count days.
        must = [team_filter, {"bool": {"must_not": [{"term": {"no_data": True}}]}}]
        if range_clause:
            must.append(range_clause)
        body = {
            "size": 0,
            "query": {"bool": {"filter": must}},
            "aggs": {
                "by_day": {
                    "date_histogram": {
                        "field": date_field,
                        "calendar_interval": "day",
                        "format": "yyyy-MM-dd",
                        "min_doc_count": 0,
                    }
                },
                "first": {"min": {"field": date_field}},
                "last": {"max": {"field": date_field}},
                "pulled": {"max": {"field": "@timestamp"}},
            },
        }
        real = es.search(index=scoped, body=body, ignore_unavailable=True)
        real_aggs = real.get("aggregations", {})
        real_days = {b["key_as_string"]: b["doc_count"] for b in real_aggs.get("by_day", {}).get("buckets", [])}

        first_date = real_aggs.get("first", {}).get("value_as_string")
        last_date = real_aggs.get("last", {}).get("value_as_string")
        last_pulled = real_aggs.get("pulled", {}).get("value_as_string")

        # Build the day strip. When the caller supplied a date range, span the
        # FULL requested range — date_histogram with min_doc_count:0 only emits
        # buckets between the first and last MATCHED doc, so empty days outside
        # the data span (e.g. before the first real-data day) would otherwise be
        # dropped from the strip entirely. Falling back to first_date..last_date
        # keeps the no-range case bounded to where data actually exists.
        strip_lo = from_date or (first_date[:10] if first_date else None)
        strip_hi = to_date or (last_date[:10] if last_date else None)
        days = []
        if strip_lo and strip_hi and strip_lo <= strip_hi:
            cur = datetime.strptime(strip_lo, "%Y-%m-%d").date()
            last = datetime.strptime(strip_hi, "%Y-%m-%d").date()
            while cur <= last:
                key = cur.isoformat()
                count = real_days.get(key, 0)
                days.append({"date": key, "doc_count": count, "state": "data" if count > 0 else "no_data"})
                cur += timedelta(days=1)

        return jsonify(
            {
                "success": True,
                "team": team,
                "days": days,
                "first_date": first_date,
                "last_date": last_date,
                "last_pulled": last_pulled,
            }
        )

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to compute coverage",
            log_detail="data_inventory_coverage failed",
            exc=e,
        )


def _inventory_date_field(data_view: str) -> str:
    """The per-view date field used for coverage/backfill (mirrors the inline
    logic in data_inventory_totals / data_inventory_coverage). DT contact
    indices carry first_contact_date; the other DT shape carries
    date_created_ms; every other view uses kibana_date."""
    dv = data_view.lower()
    is_dt = "disciple-tools" in dv or "disciple_tools" in dv
    if is_dt:
        return "first_contact_date" if "disciple-tools" in dv else "date_created_ms"
    return "kibana_date"


def _inventory_view_label(pattern: str) -> str:
    """Human label for a data-view pattern (falls back to the pattern)."""
    for v in _DATA_INVENTORY_VIEWS:
        if v["pattern"] == pattern:
            return v["label"]
    return pattern


def _backfill_report(workspace: str, view_patterns: list[str], teams: list[str], from_date: str, to_date: str) -> dict:
    """Core of the Backfill report. For each requested data view,
    runs ONE aggregation over the workspace-scoped indices: a terms agg on
    team_title (both keyword + text mappings, merged) with two sub-aggs —
    a day date_histogram (min_doc_count:1, so the bucket count == days WITH
    data) and a max on the view's date field (last date imported). Respects
    the from/to date range (empty == full history).

    Returns:
      {
        "views":  [{id, label}, ...]   # the selected views, in request order
        "teams":  ["Team A", ...]      # union of teams that have data, or the
                                       # requested team list, sorted
        "cells":  {team_title: {view_pattern: {days, last_date}}}
      }
    A team/view with no data simply has no cell entry (the caller renders 0 /
    em-dash). No row can belong to a foreign tenant — every query runs against
    the workspace-scoped index set.
    """
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        raise RuntimeError("Elasticsearch unavailable")

    requested_teams = {t for t in teams if t}
    cells: dict[str, dict[str, dict]] = {}
    teams_with_data: set[str] = set()
    views_out: list[dict] = []

    for pattern in view_patterns:
        views_out.append({"id": pattern, "label": _inventory_view_label(pattern)})
        scoped = RoleFilterService.workspace_scoped_index_pattern(pattern, workspace)
        if _EMPTY_SCOPE_MARKER in scoped:
            continue  # no indices for this view in this workspace's scope

        date_field = _inventory_date_field(pattern)

        must: list = [{"bool": {"must_not": [{"term": {"no_data": True}}]}}]
        if requested_teams:
            # Mixed keyword/text+keyword mapping (mirrors data_inventory_totals):
            # match the keyword subfield (exact) OR a per-team match_phrase.
            team_should: list = [{"terms": {"team_title.keyword": sorted(requested_teams)}}]
            for t in sorted(requested_teams):
                team_should.append({"match_phrase": {"team_title": t}})
            must.append({"bool": {"should": team_should, "minimum_should_match": 1}})
        if from_date or to_date:
            rng = {"format": "yyyy-MM-dd"}
            if from_date:
                rng["gte"] = from_date
            if to_date:
                rng["lte"] = to_date
            must.append({"range": {date_field: rng}})

        sub_aggs = {
            "by_day": {
                "date_histogram": {
                    "field": date_field,
                    "calendar_interval": "day",
                    "min_doc_count": 1,
                }
            },
            "last": {"max": {"field": date_field}},
        }

        # team_title is keyword on some indices and text+keyword on others; the
        # two aggs return DISJOINT team sets, so UNION them (a team lives in
        # exactly one mapping — first write wins, no double count). Same merge
        # rationale as data_inventory_totals.
        for team_field in ("team_title.keyword", "team_title"):
            try:
                resp = es.search(
                    index=scoped,
                    body={
                        "size": 0,
                        "query": {"bool": {"filter": must}},
                        "aggs": {"by_team": {"terms": {"field": team_field, "size": 1000}, "aggs": sub_aggs}},
                    },
                    ignore_unavailable=True,
                )
            except Exception:
                # text field without fielddata (no .keyword on a pure-text
                # index) raises 400 — the other field covers those teams.
                continue
            for b in resp.get("aggregations", {}).get("by_team", {}).get("buckets", []):
                team = b["key"]
                view_cell = cells.setdefault(team, {})
                if pattern in view_cell:
                    continue  # already counted from the other mapping
                days = len(b.get("by_day", {}).get("buckets", []))
                last_raw = b.get("last", {}).get("value_as_string")
                last_date = last_raw[:10] if last_raw else None
                view_cell[pattern] = {"days": days, "last_date": last_date}
                teams_with_data.add(team)

    # Row set: the teams the caller asked for (so an explicitly-selected team
    # with no data anywhere still gets a row), else every team that surfaced.
    row_teams = requested_teams or teams_with_data
    teams_sorted = sorted(row_teams, key=str.lower)

    return {"views": views_out, "teams": teams_sorted, "cells": cells}


@app.route("/api/data-inventory/backfill", methods=["POST"])
def data_inventory_backfill():
    """per-team × per-view backfill report: how many days each
    selected team has imports in each selected data view, and the last date
    imported. POST (the view + team selection can be long).

    JSON body:
      workspace - required; active workspace_esid.
      views     - required non-empty list of data-view patterns (subset of
                  _DATA_INVENTORY_VIEWS); "__ALL__" is expanded to every view.
      teams     - optional list of team_title (default: all teams with data).
      from_date / to_date - optional YYYY-MM-DD bounds (empty == full history).

    Returns {success, views:[{id,label}], teams:[...], cells:{team:{view:{days,last_date}}}}.
    """
    if not _require_data_inventory():
        return jsonify({"success": False, "error": "forbidden"}), 403

    body = request.get_json(silent=True) or {}
    workspace = (body.get("workspace") or "").strip()
    raw_views = [str(v).strip() for v in (body.get("views") or []) if str(v).strip()]
    teams = [str(t).strip() for t in (body.get("teams") or []) if str(t).strip()]
    from_date = (body.get("from_date") or "").strip()
    to_date = (body.get("to_date") or "").strip()

    if not workspace:
        return jsonify({"success": False, "error": "workspace is required"}), 400

    # Expand the "All data views" sentinel; otherwise keep only known patterns
    # (a forged pattern can't widen scope past the per-view workspace scoping,
    # but there's no reason to query something not on the list).
    known = {v["pattern"] for v in _DATA_INVENTORY_VIEWS}
    if "__ALL__" in raw_views:
        view_patterns = [v["pattern"] for v in _DATA_INVENTORY_VIEWS]
    else:
        view_patterns = [v for v in raw_views if v in known]

    if not view_patterns:
        return jsonify({"success": False, "error": "at least one data view is required"}), 400

    try:
        report = _backfill_report(workspace, view_patterns, teams, from_date, to_date)
        report["success"] = True
        return jsonify(report)
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to build backfill report",
            log_detail="data_inventory_backfill failed",
            exc=e,
        )


def _backfill_table(report: dict) -> dict:
    """Shape a backfill report into the {headers, rows} table the shared
    report_export_service consumes (and the same shape the on-screen table
    uses). Header: Team | <view> # days | <view> Last date … per selected view.
    """
    views = report.get("views", [])
    teams = report.get("teams", [])
    cells = report.get("cells", {})

    headers = ["Team"]
    for v in views:
        headers.append(f"{v['label']} — # days of imports")
        headers.append(f"{v['label']} — Last date imported")

    rows = []
    for team in teams:
        row = [team]
        team_cells = cells.get(team, {})
        for v in views:
            cell = team_cells.get(v["id"]) or {}
            row.append(cell.get("days", 0))
            row.append(cell.get("last_date") or "")
        rows.append(row)
    return {"headers": headers, "rows": rows}


@app.route("/api/data-inventory/backfill/export", methods=["POST"])
def data_inventory_backfill_export():
    """download the Backfill report as csv or xlsx (?format=csv|xlsx).
    Same JSON body as /api/data-inventory/backfill. The selection is recomputed
    server-side (never trust a client-supplied table) so the file matches the
    workspace-scoped data exactly."""
    if not _require_data_inventory():
        return jsonify({"success": False, "error": "forbidden"}), 403

    body = request.get_json(silent=True) or {}
    workspace = (body.get("workspace") or "").strip()
    raw_views = [str(v).strip() for v in (body.get("views") or []) if str(v).strip()]
    teams = [str(t).strip() for t in (body.get("teams") or []) if str(t).strip()]
    from_date = (body.get("from_date") or "").strip()
    to_date = (body.get("to_date") or "").strip()
    fmt = (request.args.get("format") or "csv").strip().lower()

    if not workspace:
        return jsonify({"success": False, "error": "workspace is required"}), 400

    known = {v["pattern"] for v in _DATA_INVENTORY_VIEWS}
    if "__ALL__" in raw_views:
        view_patterns = [v["pattern"] for v in _DATA_INVENTORY_VIEWS]
    else:
        view_patterns = [v for v in raw_views if v in known]
    if not view_patterns:
        return jsonify({"success": False, "error": "at least one data view is required"}), 400

    try:
        import re

        from flask import Response

        try:
            from web.services import report_export_service as exp
        except ImportError:
            from asset_management.web.services import report_export_service as exp

        report = _backfill_report(workspace, view_patterns, teams, from_date, to_date)
        title = "Data Inventory Backfill Report"
        # One section, one table — the section/table shape export_excel/export_csv expect.
        sections = [{"query": title, "narrative": "", "table": _backfill_table(report)}]
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", title) or "backfill"

        if fmt == "xlsx":
            buf = exp.export_excel(title, sections)
            return Response(
                buf.getvalue(),
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="{safe}.xlsx"'},
            )
        buf = exp.export_csv(title, sections)
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'},
        )
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to export backfill report",
            log_detail="data_inventory_backfill_export failed",
            exc=e,
        )


# ============================================
# ASSET CSV IMPORT ROUTES
# ============================================


@app.route("/api/assets/import", methods=["POST"])
def import_assets_csv():
    """Import assets from a CSV file upload."""
    import logging

    # same tier gate as the rest of the Import Data page's API
    #. This route sits outside /api/import/* and had no gate, so any
    # scoped admin could overwrite other tenants' asset docs by global id.
    denied = _require_import_data_permission()
    if denied is not None:
        return denied

    # Validate asset_type
    asset_type = request.form.get("asset_type")
    if not asset_type:
        return jsonify({"success": False, "error": "No asset_type specified"}), 400

    if asset_type not in AssetImportService.ASSET_IMPORT_CONFIGS:
        return (
            jsonify({"success": False, "error": f"Invalid asset_type: {asset_type}"}),
            400,
        )

    # Validate file upload
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "error": "No file selected"}), 400

    if not file.filename.lower().endswith(".csv"):
        return jsonify({"success": False, "error": "File must be a CSV file"}), 400

    try:
        # Read file content (handle UTF-8 BOM)
        file_content = file.read().decode("utf-8-sig")

        # Call import service
        result = AssetImportService.import_from_csv(file_content, asset_type)

        logging.info(
            f"Asset import completed: type={asset_type}, imported={result.get('imported_count', 0)}, skipped={result.get('skipped_count', 0)}, errors={result.get('error_count', 0)}"
        )

        return jsonify(result)

    except UnicodeDecodeError:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "File encoding error. Please ensure the file is UTF-8 encoded.",
                }
            ),
            400,
        )
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to import assets",
            log_detail="import_assets failed",
            exc=e,
        )


# ============================================
# API ENDPOINTS
# ============================================


# secret-bearing keys on asset docs. The config's ``encrypted`` flag
# covers the form fields (fb_app_secret, chatwoot_api_token, …), but OAuth
# callbacks also write keys no form declares (youtube_account_token,
# ga4_account_token, *_auth_state). Match those by name so a select-options
# response never carries one.
_OPTIONS_SECRET_KEY_RE = re.compile(r"token|secret|password|api_key|auth_state|_enc$")


def _options_is_secret_key(asset_type: str, key: str) -> bool:
    for field in ASSET_CONFIGS[asset_type]["fields"]:
        if field["name"] == key and field.get("encrypted"):
            return True
    return bool(_OPTIONS_SECRET_KEY_RE.search(key))


def _options_filterable_fields(asset_type: str) -> set:
    """the fields ``/api/options/<index>/filtered`` may filter on —
    the asset type's declared fields and their ``*_esid`` reference columns,
    minus anything secret."""
    config = ASSET_CONFIGS[asset_type]
    names = {config.get("title_field")} - {None}
    for field in config["fields"]:
        names.add(field["name"])
        if field.get("store_esid"):
            names.add(field.get("esid_field", field["name"].replace("_title", "_esid")))
    return {n for n in names if not _options_is_secret_key(asset_type, n)}


def _options_payload(asset_type: str, hits: list) -> list:
    """shape select options with every secret key removed from ``source``."""
    return [
        {
            "id": hit["_id"],
            "source": {k: v for k, v in (hit.get("_source") or {}).items() if not _options_is_secret_key(asset_type, k)},
        }
        for hit in hits
    ]


@app.route("/api/options/<index_name>")
def get_options(index_name):
    """Get options for select fields (AJAX endpoint).

    ``index_name`` is a client-supplied path param that was passed
    straight into a raw, unscoped ``AssetService.get_all`` — letting any
    authenticated caller enumerate every tenant's teams/orgs/workspaces/
    connections with full _source. Restrict to known asset indices and apply
    the same RoleFilterService scope the list view enforces.
    """
    asset_type = _asset_type_for_index(index_name)
    if asset_type is None:
        return jsonify({"error": "Unknown index"}), 404
    hits = AssetService.get_all(index_name)
    hits = _scope_filter_asset_hits(asset_type, hits)
    return jsonify(_options_payload(asset_type, hits))


@app.route("/api/options/<index_name>/filtered")
def get_filtered_options(index_name):
    """Get filtered options for select fields.

    same hardening as :func:`get_options` — restrict ``index_name``
    to a known asset index and scope the results to the caller before
    returning. The client-supplied ``filter_field``/``filter_value`` only
    narrows further within that already-scoped set.
    """
    asset_type = _asset_type_for_index(index_name)
    if asset_type is None:
        return jsonify({"error": "Unknown index"}), 404

    filter_field = request.args.get("filter_field")
    filter_value = request.args.get("filter_value")

    query = None
    if filter_field and filter_value:
        # only a declared, non-secret field of this asset type may be
        # filtered on — a term query on a secret field is a match oracle.
        if filter_field not in _options_filterable_fields(asset_type):
            return jsonify({"error": "Unknown filter field"}), 400
        query = {"term": {filter_field: filter_value}}

    hits = AssetService.get_all(index_name, query)
    hits = _scope_filter_asset_hits(asset_type, hits)
    return jsonify(_options_payload(asset_type, hits))


# ============================================
# TREE EXPLORER API ENDPOINTS
# ============================================


@app.route("/api/tree/structure")
def get_tree_structure():
    """Get tree structure with asset type counts.

    ``teams`` and ``organizations`` counts are scoped to the
    user's allowed set so the badge in the left tree matches what
    ``get_asset_list`` / ``list_assets`` actually return. Without
    this, an asset_admin with no scope sees "Teams (35)" in the tree
    even though clicking through yields zero items — confusing and a
    minor information leak (reveals how many teams exist).
    """
    # Phase B.4: resolve scoped counts for
    # teams/organizations once per request.
    #   * ``None``  = no scoping applies (legacy unscoped user)
    #   * ``set()`` = scoped to nothing (no-scope user OR privileged user
    #                 whose union resolves to zero teams/orgs)
    #   * ``set(titles)`` = scoped to the allowed set
    #
    # B.4: a bare user with no roles AND no memberships now falls into
    # ``set()`` via ``current_user_has_any_scope``, not into the legacy
    # ``None`` pass-through. Without this, the tree showed
    # ``Teams (35)`` to a user who would see zero on click-through.
    scoped_team_titles: set[str] | None = None
    scoped_org_titles: set[str] | None = None
    # per-tenant count for the 13 data-source asset types. Same
    # convention as the team/org sets above: None = no scope filter
    # applies; set() = scoped to nothing (deny-all); set(esids) = scoped
    # to the allowed workspaces. The badge in the left tree drives the
    # filter applied by ``list_assets`` / ``get_asset_list`` so the count
    # MUST match what those routes will actually return.
    scoped_workspace_esids: set[str] | None = None
    if not RoleFilterService.current_user_has_any_scope():
        scoped_team_titles = set()
        scoped_org_titles = set()
        scoped_workspace_esids = set()
    elif RoleFilterService.should_apply_filter():
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
        if allowed_teams is not None:
            scoped_team_titles = set(allowed_teams)
        allowed_orgs = RoleFilterService.get_allowed_organizations()
        if allowed_orgs is not None:
            scoped_org_titles = set(allowed_orgs)
        allowed_workspaces = RoleFilterService.get_allowed_workspaces()
        if allowed_workspaces is not None:
            scoped_workspace_esids = set(allowed_workspaces)

    # hide Organizations menu unless workspace.is_agency=true
    # (or the user is a superuser). Computed once per request so the
    # per-asset loop doesn't re-issue the workspace lookup per category.
    organizations_visible = _organizations_visible_to(session)
    # Workspaces tier is system_admin-only AND further scoped
    # to (member-of) ∪ (created-by). Compute both the gate (visibility
    # of the tier at all) and the visible-set (badge count + scoping
    # for list_assets) once per request.
    workspaces_visible = _workspaces_visible_to(session)
    # data-source asset types are hidden from non-privileged
    # (org-tier) users — they reach their own sources via Data Connectors.
    # Computed once per request; the per-asset loop skips the data-source
    # rows when False so the categories collapse out of the tree (the
    # empty-category gate then drops the now-empty category).
    data_sources_visible = _data_sources_visible_to(session)
    visible_workspace_ids_for_tier: set[str] = set()
    if workspaces_visible:
        try:
            from asset_management.web.services.workspace_visibility_service import (
                WorkspaceVisibilityService,
            )

            # superuser tier visibility narrows to created-by ∪ grants.
            visible_workspace_ids_for_tier = WorkspaceVisibilityService.get_visible_workspaces_for_user(
                session.get("username", "") or "",
                is_superuser=("superuser" in (session.get("roles") or [])),
            )
        except Exception as e:
            logging.warning("visible workspace lookup failed: %s", e)

    structure = {"categories": []}

    # hide data-source categories whose summed asset_type count
    # is zero for the current user. The ``company`` category (workspaces /
    # teams / organizations) stays visible regardless — without it a fresh
    # tenant has no way to bootstrap a team or org, even when counts are
    # zero. Superusers see every category unconditionally (they manage
    # the whole cluster). still suppress the per-asset
    # rows inside ``company`` based on agency / system_admin status.
    _is_superuser = "superuser" in (session.get("roles") or [])

    for cat_key, cat_data in ASSET_CATEGORIES.items():
        category = {"key": cat_key, "title": cat_data["title"], "asset_types": []}

        for asset_key in cat_data["assets"]:
            # skip the organizations entry entirely for non-agency
            # users. Solo + org-owner accounts never see "Organizations" in
            # the tree, matching the spike's UX claim that individuals don't
            # encounter the word "organization" during routine navigation.
            if asset_key == "organizations" and not organizations_visible:
                continue
            # skip the workspaces entry entirely for non-admins.
            # Same suppression model as but governed by
            # system_admin status rather than workspace.is_agency.
            if asset_key == "workspaces" and not workspaces_visible:
                continue
            # skip the data-source entries (everything outside the
            # company tier) for non-privileged users. Mirrors the
            # row suppression above; the trailing
            # gate then removes the emptied data-source category.
            if asset_key not in _DATA_SOURCE_SYSTEM_TYPES and not data_sources_visible:
                continue
            if asset_key in ASSET_CONFIGS:
                config = ASSET_CONFIGS[asset_key]
                if asset_key in ("teams", "organizations") and scoped_workspace_esids is not None:
                    # badge count must MATCH what list_assets /
                    # get_asset_list actually return. follow-up: the
                    # list narrows teams/orgs by the TITLE allow-list FIRST
                    # (get_allowed_teams_for_org_users / get_allowed_organizations),
                    # THEN by workspace_esid. The prior count applied ONLY the
                    # workspace layer, so an org-tier user (whose title set is
                    # their org's teams, a subset of the workspace) saw a badge
                    # of the whole workspace's teams while the list showed only
                    # their org's — "Teams (34)" with 3 rows on click-through.
                    # Compute the count by applying the SAME two-layer filter
                    # the list uses, in memory, so badge == list by construction
                    # (deny-all set → 0 without an ES round trip).
                    if not scoped_workspace_esids:
                        count = 0
                    else:
                        title_set = scoped_team_titles if asset_key == "teams" else scoped_org_titles
                        title_field = config["title_field"]
                        rows = AssetService.get_all(config["index"])
                        if title_set is not None:
                            rows = [h for h in rows if h.get("_source", {}).get(title_field) in title_set]
                        rows = RoleFilterService.filter_hits_by_workspace(rows)
                        count = len(rows)
                elif asset_key == "teams" and scoped_team_titles is not None:
                    count = len(scoped_team_titles)
                elif asset_key == "organizations" and scoped_org_titles is not None:
                    count = len(scoped_org_titles)
                elif asset_key == "workspaces":
                    # badge count is the size of the visibility
                    # set, NOT the global de_asset_workspace count. Even
                    # a system_admin must not see a "Workspaces (40)"
                    # badge when they can only access 2 of them — that
                    # would be the exact cross-tenant info-leak the
                    # tier was added to prevent.
                    count = len(visible_workspace_ids_for_tier)
                elif scoped_workspace_esids is not None and asset_key not in ("teams", "organizations", "workspaces"):
                    # workspace-scoped count for the 13 data-source
                    # asset types. ``scoped_workspace_esids == set()`` (deny-all
                    # user) short-circuits to a count of 0 without an ES round
                    # trip. The terms-filter count below matches exactly what
                    # ``list_assets`` will return for the same user.
                    #
                    # bare ``workspace_esid`` — the asset schemas map
                    # it explicit keyword on every de_asset_* index. The
                    # era ``.keyword`` subfield no longer exists post-reindex
                    # (a terms query on it silently matches nothing → 0 badges).
                    if not scoped_workspace_esids:
                        count = 0
                    else:
                        count = AssetService.count(
                            config["index"],
                            query={"terms": {"workspace_esid": list(scoped_workspace_esids)}},
                        )
                else:
                    count = AssetService.count(config["index"])
                # hide the two LEGACY Facebook-app nodes when the
                # caller has no fb_app / fb_app_user docs in scope. ``count``
                # here is already the workspace-scoped count for a scoped user
                # (terms on workspace_esid.keyword) and the global count for a
                # privileged/unscoped user, so a legacy tenant that still owns
                # these docs keeps both nodes while a new-onboarded workspace
                # sees neither. Presentation-only — no data is touched.
                if asset_key in _LEGACY_FB_APP_TYPES and count == 0:
                    continue
                category["asset_types"].append(
                    {
                        "key": asset_key,
                        "title": config["title"],
                        "count": count,
                        "no_add": config.get("no_add", False),
                        "code_managed": config.get("code_managed", False),
                    }
                )

        # Sort asset types alphabetically by title (case-insensitive)
        category["asset_types"].sort(key=lambda x: x["title"].lower())

        # skip categories that have nothing to show. The
        # ``company`` category is exempt — it carries the bootstrap
        # tiers (workspaces / teams / organizations) and an empty
        # tenant must still be able to add their first team. Superusers
        # bypass the gate entirely. ``asset_types == []`` covers the
        # case where every entry was suppressed.
        if not _is_superuser and cat_key != "company":
            if not category["asset_types"] or sum(a.get("count", 0) for a in category["asset_types"]) == 0:
                continue

        structure["categories"].append(category)

    return jsonify(structure)


@app.route("/api/tree/assets/<asset_type>")
def get_asset_list(asset_type):
    """Get list of assets for a specific type.

    ``teams`` and ``organizations`` are scope-defining indices —
    their visibility must respect the user's RoleFilterService scope, so
    an asset_admin without the relevant membership or grant cannot
    enumerate every tenant's teams/orgs through this endpoint.
    Other asset types pass through unfiltered (they're org-derived
    assets that surface only via the parent org/team — see the Asset
    Explorer tree UI).
    """
    if asset_type not in ASSET_CONFIGS:
        return jsonify({"error": "Asset type not found"}), 404
    if asset_type == "organizations" and not _organizations_visible_to(session):
        # defense-in-depth: the API endpoint behind the Asset
        # Explorer tree also 404s for non-agency users so a curl
        # against the JSON route can't enumerate orgs.
        return jsonify({"error": "Asset type not found"}), 404
    # same defense-in-depth on the JSON tree route. A
    # non-admin who curls /api/tree/assets/workspaces gets 404 instead
    # of the (already empty under the visibility filter) list.
    if asset_type == "workspaces" and not _workspaces_visible_to(session):
        return jsonify({"error": "Asset type not found"}), 404
    # data-source types are hidden from non-privileged (org-tier)
    # users on the JSON tree route too, so a curl can't enumerate them.
    if asset_type not in _DATA_SOURCE_SYSTEM_TYPES and not _data_sources_visible_to(session):
        return jsonify({"error": "Asset type not found"}), 404

    asset_config = ASSET_CONFIGS[asset_type]
    hits = AssetService.get_all(asset_config["index"])

    # Phase B.4 scope filter: drop hits whose title is
    # outside the user's allowed set for the two scope-defining indices.
    # Branches:
    #   * ``current_user_has_any_scope`` False → no-scope deny-all: a
    #     bare user with no roles AND no memberships sees zero items.
    #     Pre-B.4 the ``should_apply_filter`` False branch leaked the
    #     full list.
    #   * ``should_apply_filter`` True → narrow to allowed titles. None
    #     = admin-style unscoped pass-through (defensive — privileged
    #     users land here only when their union resolves non-empty).
    #     [] = privileged user with no memberships + no grants → zero
    #     items.
    if asset_type in ("teams", "organizations"):
        if not RoleFilterService.current_user_has_any_scope():
            hits = []
        elif RoleFilterService.should_apply_filter():
            if asset_type == "teams":
                allowed_titles = RoleFilterService.get_allowed_teams_for_org_users()
            else:
                allowed_titles = RoleFilterService.get_allowed_organizations()
            if allowed_titles is not None:
                allowed_set = set(allowed_titles)
                title_field = asset_config["title_field"]
                hits = [h for h in hits if h.get("_source", {}).get(title_field) in allowed_set]
            # enforce the tenant boundary by workspace_esid, not
            # just title. Team/org titles collide across workspaces, so the
            # title filter alone leaked same-titled foreign-workspace rows
            # into the Asset Explorer tree. Apply the workspace filter as a
            # second, authoritative layer (fail-closed on missing field).
            hits = RoleFilterService.filter_hits_by_workspace(hits)
    elif asset_type == "workspaces":
        # same visibility filter as list_assets (HTML route).
        # The JSON tree backend must agree with the HTML list — otherwise
        # the tree-count and the table-row-count diverge.
        try:
            from asset_management.web.services.workspace_visibility_service import (
                WorkspaceVisibilityService,
            )

            # superuser tier visibility narrows to created-by ∪ grants.
            visible_ids = WorkspaceVisibilityService.get_visible_workspaces_for_user(
                session.get("username", "") or "",
                is_superuser=("superuser" in (session.get("roles") or [])),
            )
        except Exception as e:
            logging.warning("get_asset_list visibility lookup failed: %s", e)
            visible_ids = set()
        hits = [h for h in hits if h["_id"] in visible_ids]
    else:
        # per-tenant isolation for the 13 data-source asset
        # types on the Asset Explorer tree route. Mirrors the
        # ``list_assets`` (HTML route) filter so the tree and the table
        # view return the same set. Reading either surface without the
        # filter would let an asset_admin enumerate every tenant's
        # data-source assets through the JSON route alone.
        if not RoleFilterService.current_user_has_any_scope():
            hits = []
        elif RoleFilterService.should_apply_filter():
            allowed_workspaces = RoleFilterService.get_allowed_workspaces()
            if allowed_workspaces is not None:
                allowed_ws_set = set(allowed_workspaces)
                hits = [h for h in hits if h.get("_source", {}).get("workspace_esid") in allowed_ws_set]

    items = []
    for hit in hits:
        item_data = hit["_source"]
        title = item_data.get(asset_config["title_field"], "Untitled")
        items.append({"id": hit["_id"], "title": title})

    items.sort(key=lambda x: x["title"].lower())

    return jsonify({"asset_type": asset_type, "items": items})


@app.route("/api/asset/<asset_type>/<item_id>/details")
def get_asset_details(asset_type, item_id):
    """Get detailed information about a specific asset."""
    if asset_type not in ASSET_CONFIGS:
        return jsonify({"error": "Asset type not found"}), 404

    asset_config = ASSET_CONFIGS[asset_type]
    item = AssetService.get_by_id(asset_config["index"], item_id)

    if not item:
        return jsonify({"error": "Item not found"}), 404

    # company-tier (teams/organizations) per-object scope check — the
    # helper below short-circuits these types to allow, so this JSON
    # details route leaked a foreign team/org's full _source to any scoped admin
    # who knew its _id. Mirror the list-view narrowing.
    if asset_type in ("teams", "organizations"):
        if not _company_tier_asset_in_user_scope(asset_type, {**item, "_id": item_id}):
            return jsonify({"error": "Item not found"}), 404
    # workspaces details route through the per-object visibility set
    # (member-of ∪ created-by ∪ grants), same gate as edit_asset/list_assets.
    elif asset_type == "workspaces":
        try:
            from asset_management.web.services.workspace_visibility_service import (
                WorkspaceVisibilityService,
            )

            _visible = WorkspaceVisibilityService.get_visible_workspaces_for_user(
                session.get("username", "") or "",
                is_superuser=("superuser" in (session.get("roles") or [])),
            )
            if item_id not in _visible:
                return jsonify({"error": "Item not found"}), 404
        except Exception as e:  # noqa: BLE001 — security gate fails closed
            logging.warning("get_asset_details workspace visibility check failed: %s", e)
            return jsonify({"error": "Item not found"}), 404

    # a data-source asset outside the user's workspace is 404'd
    # here even though the list views already hide it — otherwise a known
    # _id would let an admin in workspace A read workspace B's connected
    # account via this JSON route. 404 (not 403) mirrors the /
    # convention on the sibling routes and avoids confirming the
    # asset exists.
    if not _data_source_asset_in_user_workspace(asset_type, item):
        return jsonify({"error": "Item not found"}), 404

    display_item = FormService.prepare_for_display(item, asset_config)
    computed_fields = FormService.get_computed_display_fields(item, asset_config, item_id)

    return jsonify(
        {
            "id": item_id,
            "asset_type": asset_type,
            "asset_config": {
                "title": asset_config["title"],
                "title_field": asset_config["title_field"],
                "fields": asset_config["fields"],
                "code_managed": asset_config.get("code_managed", False),
            },
            "data": display_item,
            "computed_fields": computed_fields,
        }
    )


@app.route("/api/dashboard/summary")
def get_dashboard_summary():
    """Get dashboard summary statistics.

    counts are workspace-scoped for the 13 data-source asset
    types so a tenant's dashboard widget doesn't leak the global total.
    Teams/orgs counts are scoped via the title-allow-list resolved by
    ``get_allowed_teams_for_org_users`` / ``get_allowed_organizations``.
    A no-scope user sees zeros across the board.
    """
    # same scope resolution as get_tree_structure.
    scoped_team_titles: set[str] | None = None
    scoped_org_titles: set[str] | None = None
    scoped_workspace_esids: set[str] | None = None
    if not RoleFilterService.current_user_has_any_scope():
        scoped_team_titles = set()
        scoped_org_titles = set()
        scoped_workspace_esids = set()
    elif RoleFilterService.should_apply_filter():
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
        if allowed_teams is not None:
            scoped_team_titles = set(allowed_teams)
        allowed_orgs = RoleFilterService.get_allowed_organizations()
        if allowed_orgs is not None:
            scoped_org_titles = set(allowed_orgs)
        allowed_workspaces = RoleFilterService.get_allowed_workspaces()
        if allowed_workspaces is not None:
            scoped_workspace_esids = set(allowed_workspaces)

    # data-source types are hidden from non-privileged (org-tier)
    # users — their counts must not appear in the summary either.
    data_sources_visible = _data_sources_visible_to(session)

    total_assets = 0
    by_category = {}
    by_type = {}

    for cat_key, cat_data in ASSET_CATEGORIES.items():
        category_total = 0

        for asset_key in cat_data["assets"]:
            if asset_key not in _DATA_SOURCE_SYSTEM_TYPES and not data_sources_visible:
                # skip data-source counts for non-privileged users.
                continue
            if asset_key in ASSET_CONFIGS:
                config = ASSET_CONFIGS[asset_key]
                if asset_key in ("teams", "organizations") and scoped_workspace_esids is not None:
                    # follow-up: count what the list returns,
                    # applying the title allow-list FIRST then workspace (see
                    # get_tree_structure for the full rationale). A workspace-
                    # only count over-counts for an org-tier user whose title
                    # set is a subset of the workspace.
                    if not scoped_workspace_esids:
                        count = 0
                    else:
                        title_set = scoped_team_titles if asset_key == "teams" else scoped_org_titles
                        title_field = config["title_field"]
                        rows = AssetService.get_all(config["index"])
                        if title_set is not None:
                            rows = [h for h in rows if h.get("_source", {}).get(title_field) in title_set]
                        rows = RoleFilterService.filter_hits_by_workspace(rows)
                        count = len(rows)
                elif asset_key == "teams" and scoped_team_titles is not None:
                    count = len(scoped_team_titles)
                elif asset_key == "organizations" and scoped_org_titles is not None:
                    count = len(scoped_org_titles)
                elif scoped_workspace_esids is not None and asset_key not in ("teams", "organizations"):
                    # bare ``workspace_esid`` — explicit keyword on
                    # every de_asset_* index post-reindex; the era
                    # ``.keyword`` subfield is gone (see get_tree_structure).
                    if not scoped_workspace_esids:
                        count = 0
                    else:
                        count = AssetService.count(
                            config["index"],
                            query={"terms": {"workspace_esid": list(scoped_workspace_esids)}},
                        )
                else:
                    count = AssetService.count(config["index"])
                by_type[asset_key] = {"title": config["title"], "count": count}
                category_total += count
                total_assets += count

        by_category[cat_key] = {"title": cat_data["title"], "count": category_total}

    return jsonify({"total_assets": total_assets, "by_category": by_category, "by_type": by_type})


# /api/search pattern bounds (see search_assets).
_SEARCH_MAX_QUERY_LEN = 64
_SEARCH_MAX_STARS = 3


@app.route("/api/search")
def search_assets():
    """Search across all assets using wildcard pattern.

    scoped per tenant. A wildcard like ``acme*`` must not let a
    tenant in Workspace A discover that Workspace B has an asset named
    ``acme-corp-fb``. The scope resolution mirrors ``list_assets``:
    teams/orgs by allowed-title set, the 13 data-source types by
    allowed-workspace set, no-scope user → empty results everywhere.
    """
    query = request.args.get("q", "").strip()

    if not query:
        return jsonify({"error": "Query parameter required"}), 400

    # bound the pattern. Every field of every scoped asset doc is
    # matched against it, so an unbounded one was a cheap CPU pin for any
    # signed-in user. Runs of stars collapse (``**`` == ``*``) before the
    # star cap so a harmless double star is not refused.
    if len(query) > _SEARCH_MAX_QUERY_LEN:
        return jsonify({"error": f"Query is too long (max {_SEARCH_MAX_QUERY_LEN} characters)"}), 400
    wildcard_pattern = re.sub(r"\*+", "*", query)
    if wildcard_pattern.count("*") > _SEARCH_MAX_STARS:
        return jsonify({"error": f"Query has too many * wildcards (max {_SEARCH_MAX_STARS})"}), 400

    # resolve scope once and reuse across every asset type.
    # The three sets follow the same None/empty-set/populated convention
    # as list_assets and get_tree_structure.
    has_any_scope = RoleFilterService.current_user_has_any_scope()
    should_filter = RoleFilterService.should_apply_filter() if has_any_scope else False
    # data-source types are hidden from non-privileged (org-tier)
    # users — a wildcard search must not surface them either. Resolved once
    # and applied as a skip in the per-type loop below.
    data_sources_visible = _data_sources_visible_to(session)
    allowed_teams_set: set[str] | None = None
    allowed_orgs_set: set[str] | None = None
    allowed_ws_set: set[str] | None = None
    if has_any_scope and should_filter:
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
        if allowed_teams is not None:
            allowed_teams_set = set(allowed_teams)
        allowed_orgs = RoleFilterService.get_allowed_organizations()
        if allowed_orgs is not None:
            allowed_orgs_set = set(allowed_orgs)
        allowed_workspaces = RoleFilterService.get_allowed_workspaces()
        if allowed_workspaces is not None:
            allowed_ws_set = set(allowed_workspaces)

    results = {}

    # Search through each asset type
    for asset_key, asset_config in ASSET_CONFIGS.items():
        # skip data-source types entirely for non-privileged users.
        if asset_key not in _DATA_SOURCE_SYSTEM_TYPES and not data_sources_visible:
            continue

        matches = []

        # Get all documents for this asset type
        hits = AssetService.get_all(asset_config["index"])

        # per-asset scope filter — same convention as list_assets.
        if not has_any_scope:
            hits = []
        elif should_filter:
            title_field = asset_config["title_field"]
            if asset_key == "teams" and allowed_teams_set is not None:
                hits = [h for h in hits if h.get("_source", {}).get(title_field) in allowed_teams_set]
            elif asset_key == "organizations" and allowed_orgs_set is not None:
                hits = [h for h in hits if h.get("_source", {}).get(title_field) in allowed_orgs_set]
            elif asset_key not in ("teams", "organizations") and allowed_ws_set is not None:
                hits = [h for h in hits if h.get("_source", {}).get("workspace_esid") in allowed_ws_set]

        for hit in hits:
            item_data = hit["_source"]
            item_id = hit["_id"]

            # Check if any searchable field matches the wildcard pattern
            matched = False
            matched_fields = []

            # Search through all fields defined in config
            for field in asset_config.get("fields", []):
                field_name = field.get("name")
                if not field_name:
                    continue

                field_value = item_data.get(field_name, "")

                # Handle list fields - check each item individually
                if isinstance(field_value, list):
                    for item in field_value:
                        item_str = str(item) if item else ""
                        if wildcard_match(item_str, wildcard_pattern):
                            matched = True
                            matched_fields.append(
                                {
                                    "field": field.get("label", field_name),
                                    "value": item_str,
                                }
                            )
                            break  # Only add the field once even if multiple items match
                else:
                    # Convert to string for comparison
                    field_value_str = str(field_value) if field_value else ""

                    # Check for wildcard match (simple implementation)
                    if wildcard_match(field_value_str, wildcard_pattern):
                        matched = True
                        matched_fields.append(
                            {
                                "field": field.get("label", field_name),
                                "value": field_value_str,
                            }
                        )

            if matched:
                title = item_data.get(asset_config["title_field"], "Untitled")
                matches.append({"id": item_id, "title": title, "matched_fields": matched_fields})

        if matches:
            results[asset_key] = {
                "asset_type_title": asset_config["title"],
                "matches": matches,
            }

    return jsonify({"query": query, "results": results})


def wildcard_match(text: str, pattern: str) -> bool:
    """
    Simple wildcard matching function.
    Supports * (any characters) and ? (single character).
    Case-insensitive.

    a greedy two-pointer matcher, not a compiled regex. The regex
    (each ``*`` -> ``.*``) backtracked polynomially — three stars against a
    3,000-character field took ~14 s — so ``/api/search`` could pin a worker.
    This walk is O(len(text) * len(pattern)) whatever the pattern holds.
    """
    text = text.lower()
    pattern = pattern.lower()
    t = p = 0
    star_p = -1  # pattern index just past the last ``*`` seen
    star_t = 0  # text index that ``*`` is currently stretched to
    while t < len(text):
        if p < len(pattern) and pattern[p] == "*":
            p += 1
            star_p, star_t = p, t
        elif p < len(pattern) and pattern[p] in ("?", text[t]):
            t += 1
            p += 1
        elif star_p != -1:
            star_t += 1
            t, p = star_t, star_p
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


# ============================================
# ERROR HANDLERS
# ============================================


@app.errorhandler(401)
def unauthorized(error):
    """Handle unauthorized access."""
    return (
        render_template(
            "error.html",
            error_code=401,
            error_title="Authentication Required",
            error_message="Invalid credentials. Please try again.",
        ),
        401,
    )


# ============================================
# ============================================
# FORCED PASSWORD CHANGE ROUTES
# ============================================


@app.route("/change-password", methods=["GET"])
def change_password_page():
    """Forced password change — only reachable with an fpc_pending session."""
    if not session.get("fpc_pending"):
        return redirect(url_for("analytics.login_page"))
    csrf_token = auth_module.generate_csrf_token()
    return render_template(
        "change_password.html",
        username=session.get("fpc_username", ""),
        csrf_token=csrf_token,
        error_message=request.args.get("error"),
    )


@app.route("/change-password", methods=["POST"])
def change_password_post():
    """Process the forced password change form."""
    if not session.get("fpc_pending"):
        return redirect(url_for("analytics.login_page"))

    # CSRF guard
    if not auth_module.validate_csrf_token(request.form.get("csrf_token", "")):
        return redirect(url_for("change_password_page", error="Invalid request. Please try again."))

    username = session.get("fpc_username", "")
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password:
        return redirect(url_for("change_password_page", error="Please enter your temporary password."))

    # Verify the temporary password before allowing the change
    from lib.es_auth import authenticate_user

    auth_check = authenticate_user(username, current_password)
    if not auth_check["success"]:
        return redirect(url_for("change_password_page", error="Temporary password is incorrect."))

    if not new_password or len(new_password) < 8:
        return redirect(url_for("change_password_page", error="New password must be at least 8 characters."))

    # bound the upper length server-side too. The client maxlength=128
    # on change_password.html is advisory; this matches profile_change_password's
    # >128 guard so an over-long password can't reach es.security.change_password.
    if len(new_password) > 128:
        return redirect(url_for("change_password_page", error="New password is too long (max 128 characters)."))

    if new_password == current_password:
        return redirect(url_for("change_password_page", error="New password must be different from your temporary password."))

    if new_password != confirm_password:
        return redirect(url_for("change_password_page", error="Passwords do not match."))

    # Change the password in Elasticsearch
    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

    es = get_elasticsearch_client()
    if es is None:
        return redirect(
            url_for("change_password_page", error="Could not connect to authentication service. Please try again.")
        )

    try:
        get_es_admin_client(es).security.change_password(username=username, body={"password": new_password})
    except Exception as e:
        logging.error(f"change_password_post: ES password change failed for '{username}': {e}")
        return redirect(url_for("change_password_page", error="Failed to update password. Please try again."))

    # Clear the forced-change flag now that the user has set a real password
    UserProvisioningService.clear_force_password_change(username)

    # stamp credential_changed_at BEFORE the session below is built,
    # so this route's own fresh session["auth_timestamp"] (set further down)
    # lands after the stamp and is never denied by session_denial_reason /
    # verify_auth — while any OTHER live session for this account is denied
    # within the 30s account-state cache TTL.
    UserProvisioningService.stamp_credential_changed(username)

    # Promote to a full authenticated session
    roles = session.get("fpc_roles", [])
    full_name = session.get("fpc_full_name", username)
    email = session.get("fpc_email", "")

    session.clear()
    session["authenticated"] = True
    session["username"] = username
    session["roles"] = roles
    session["is_admin"] = "superuser" in roles or config.REQUIRED_ROLE in roles
    session["full_name"] = full_name
    session["email"] = email
    session["auth_timestamp"] = time.time()
    session["last_activity"] = time.time()
    session["auth_method"] = "form_login"
    session.permanent = True

    logging.info(f"AUDIT: user={username} action=force_password_change success=True ip={request.remote_addr}")

    # write a "login" audit event when the forced-password-change
    # flow promotes the user to an authenticated session. Without this the
    # Login Activity graph misses every first-login for new accounts.
    AuditLogService.log_event(
        username,
        "login",
        True,
        auth_method="form_login_after_force_change",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    # run the login gate here too. Both routes into a forced
    # password change return to /change-password BEFORE their own gate call
    # (the form-login POST's FPC branch and _complete_mfa_login's), so this
    # promotion was the one authenticated entry point left ungated.
    # Reachable when onboarding_required and force_password_change are set
    # together: an admin password reset of a not-yet-onboarded user, or the
    # signup partial-failure path. One session, but it is the setup session.
    #
    # Before the Kibana cookie, matching _complete_mfa_login: a gated user is
    # going to the wizard, not a dashboard.
    from asset_management.web.login_gate_redirect import gate_redirect_after_auth

    if gate_response := gate_redirect_after_auth(
        username,
        roles,
        auth_method="form_login_after_force_change",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    ):
        return gate_response

    # Create Kibana SSO session using the new password
    kibana_sid_cookie = create_kibana_session(username, new_password)

    response = make_response(redirect("/dashboard"))
    if kibana_sid_cookie:
        response.set_cookie(
            "sid",
            kibana_sid_cookie,
            secure=True,
            httponly=True,
            samesite="Lax",
            path="/kibana",
        )
    return response


# ============================================
# USER PROFILE (self-service) —
# ============================================
#
# Every authenticated user can manage their own full name, email, password,
# and MFA from /profile. Routes here intentionally do NOT use any admin
# guard — auth.py whitelists /profile* in check_route_authorization so
# regular team users can reach it.


def _profile_redirect(success: str = "", error: str = ""):
    """Redirect back to /profile with a flash-style query param."""
    target = url_for("profile_page")
    if success:
        return redirect(f"{target}?success={quote(success)}")
    if error:
        return redirect(f"{target}?error={quote(error)}")
    return redirect(target)


def _build_time_zone_options() -> list:
    """Return a sorted IANA-zone list for the profile time-zone <select>.

    Pulls from ``zoneinfo.available_timezones`` so the option list tracks
    the host's tzdata package (refreshed via the OS, not bundled in the
    app). Filters out the ``SystemV/``, ``US/``, and three-letter legacy
    zones — they're aliases for the canonical Region/City names and
    showing both would just make the picker noisier. The canonical name
    survives.
    """
    try:
        from zoneinfo import available_timezones
    except ImportError:  # pragma: no cover — Python 3.9+ ships zoneinfo
        return ["UTC"]
    zones = available_timezones()
    # Drop legacy / alias namespaces. Keep ``Etc/`` because those are
    # the canonical fixed-offset zones (Etc/GMT, Etc/UTC, etc.) and
    # users on terminals without a region zone sometimes pick them.
    filtered = [
        z for z in zones if "/" in z and not z.startswith(("SystemV/", "US/", "Canada/", "Brazil/", "Chile/", "Mexico/"))
    ]
    filtered.sort()
    # UTC sits at the top so the default pick is obvious.
    return ["UTC", *filtered]


def _profile_default_back_url(roles: list, username: str = "") -> str:
    """Pick the Back-link target when no entry referrer is known.

    Users with any management-console permission (mc_*) land on /assets/;
    everyone else lands on /dashboard. Keeps the default sensible whether
    the user lives on the MC side or the dashboards side of the app.

     follow-up: pass ``username`` so the System 2 bridge
    fires for wizard-signup users (workspace_admin / workspace_owner
    hold WORKSPACE_MANAGE in System 2 and get ``mc_data_connectors``
    via the bridge). Without the kwarg, those users would land on
    /dashboard after a profile edit even though /assets/ is their
    natural home.
    """
    perms = RolePermissionsService.get_merged_permissions(roles or [], username=username)
    if any(v for k, v in perms.items() if k.startswith("mc_")):
        return "/assets/"
    return "/dashboard"


def _capture_profile_back_url() -> None:
    """Stash the Referer that brought the user to /profile, for the Back link.

    Same-origin only (defends against open-redirect via the Referer header).
    Skipped when the Referer is itself a /profile URL so the original entry
    point survives form-submit redirects (POST /profile/* → 302 → /profile).
    """
    referer = request.headers.get("Referer", "")
    if not referer:
        return
    same_origin = referer.startswith(request.host_url) or (referer.startswith("/") and not referer.startswith("//"))
    if not same_origin:
        return
    if "/profile" in referer:
        return
    # Never stash the sibling Billing pages as the profile back-target, and
    # never re-capture once a back-url is already stashed. Both guards break
    # the /profile ↔ /billing back-button ping-pong: hopping to the sibling
    # no longer clobbers the real entry point (dashboard / console).
    if "/billing" in referer:
        return
    if session.get("_profile_back_url"):
        return
    session["_profile_back_url"] = referer


@app.route("/profile", methods=["GET"])
def profile_page():
    """User profile page — view and update name, email, password, MFA."""
    # Drop any in-flight TOTP setup secret — landing on /profile means the
    # user is no longer mid-setup, so a stale staged secret should not bleed
    # into the next setup attempt.
    session.pop("_totp_setup_secret_enc", None)

    _capture_profile_back_url()
    back_url = session.get("_profile_back_url") or _profile_default_back_url(
        session.get("roles", []),
        username=session.get("username", ""),
    )

    username = session.get("username", "")
    full_name = session.get("full_name", "") or ""
    email = session.get("email", "") or ""

    mfa_record = MFAService.get_mfa_record(username) or {}
    mfa_method = mfa_record.get("mfa_method") or ""
    if mfa_method == "totp":
        mfa_setup_complete = bool(mfa_record.get("totp_confirmed"))
    elif mfa_method == "yubikey":
        mfa_setup_complete = bool(mfa_record.get("yubikey_confirmed"))
    else:
        mfa_setup_complete = False
    mfa_state = {
        "enabled": bool(mfa_record.get("mfa_enabled")),
        "method": mfa_method,
        "setup_complete": mfa_setup_complete,
        "last_used": mfa_record.get("last_used") or "",
        # unused recovery codes remaining. Drives the count display +
        # "Regenerate codes" button on the security tile. recovery_used_remaining
        # is a one-shot nudge set when this login used a recovery code — surface
        # it once on the next profile view, then drop it.
        "recovery_remaining": len(mfa_record.get("recovery_code_hashes") or []),
        # the stored set predates the 80-bit keyed-hash format → prompt.
        "recovery_legacy": MFAService.recovery_set_is_legacy(mfa_record),
        "recovery_just_used_remaining": session.pop("mfa_recovery_used_remaining", None),
        # Tier 2: recovery email + verified state for the security tile,
        # plus a one-shot banner when this session was reached via email recovery
        # (MFA was disabled, so the user must re-enroll).
        "recovery_email": mfa_record.get("recovery_email") or "",
        "recovery_email_verified": bool(mfa_record.get("recovery_email_verified")),
        "recovery_email_just_used": bool(session.pop("mfa_recovery_email_used", False)),
    }

    # Account type. Closes the "Reversible from
    # Settings later" AC on. ``is_default`` is True
    # when the user has never explicitly picked a value, so the form
    # can render a "(default)" hint next to the pre-selected option
    # rather than implying the user already chose org_owner deliberately.
    account_type_detail = UserProfileService.get_account_type_detailed(username)
    account_type_state = {
        "current": account_type_detail.get("account_type"),
        "is_default": bool(account_type_detail.get("is_default")),
    }

    # per-user time zone. The stored value comes from
    # ``user.metadata.time_zone`` (UserProfileService); falls back to
    # ``UTC`` for users who predate the rollout. The template's <select>
    # builds its option list from the runtime tzdata at render time so a
    # new zone added to the OS's tzdata is automatically pickable.
    # ``is_default`` is True when no value is stored — the template's JS
    # uses this to opt-in to browser ``Intl`` auto-detection so the
    # picker pre-selects a sensible default before the user saves.
    time_zone_detail = UserProfileService.get_time_zone_detailed(username)
    time_zone_current = time_zone_detail["time_zone"]
    time_zone_is_default = time_zone_detail["is_default"]
    time_zone_options = _build_time_zone_options()

    return render_template(
        "profile.html",
        username=username,
        full_name=full_name,
        email=email,
        mfa=mfa_state,
        account_type=account_type_state,
        time_zone_current=time_zone_current,
        time_zone_is_default=time_zone_is_default,
        time_zone_options=time_zone_options,
        enforce_mfa=config.ENFORCE_MFA,
        back_url=back_url,
        success=request.args.get("success", ""),
        error=request.args.get("error", ""),
    )


@app.route("/profile/update-name", methods=["POST"])
def profile_update_name():
    """Self-service: update full_name on the current user's ES record."""
    username = session.get("username", "")
    full_name = request.form.get("full_name", "").strip()
    if not full_name:
        return _profile_redirect(error="Display name cannot be empty.")
    if len(full_name) > 200:
        return _profile_redirect(error="Display name is too long (max 200 characters).")

    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

    es = get_elasticsearch_client()
    if es is None:
        return _profile_redirect(error="Could not connect to Elasticsearch. Please try again.")

    try:
        response = es.security.get_user(username=username)
        user_data = dict(response).get(username, {})
        if not user_data:
            return _profile_redirect(error="Account not found.")
        get_es_admin_client(es).security.put_user(
            username=username,
            roles=user_data.get("roles", []),
            full_name=full_name,
            email=user_data.get("email", ""),
            # ES writes enabled=true when it is omitted.
            enabled=user_data.get("enabled", True),
            metadata=user_data.get("metadata", {}),
        )
    except Exception as e:
        logging.error(f"profile_update_name: failed for '{username}': {e}")
        return _profile_redirect(error="Failed to update display name. Please try again.")

    session["full_name"] = full_name
    AuditLogService.log_event(
        username,
        "profile_update_name",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    return _profile_redirect(success="name")


@app.route("/profile/update-email", methods=["POST"])
def profile_update_email():
    """Self-service: update email on the current user's ES record."""
    username = session.get("username", "")
    email = request.form.get("email", "").strip()
    if not email:
        return _profile_redirect(error="Email cannot be empty.")
    if len(email) > 254:
        return _profile_redirect(error="Email address is too long (max 254 characters).")
    # RFC-5322-lite: must look like local@domain.tld with no whitespace.
    # Matches the client-side check in profile.html.
    if not is_plausible_email(email):
        return _profile_redirect(error="Please enter a valid email address.")

    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

    es = get_elasticsearch_client()
    if es is None:
        return _profile_redirect(error="Could not connect to Elasticsearch. Please try again.")

    try:
        response = es.security.get_user(username=username)
        user_data = dict(response).get(username, {})
        if not user_data:
            return _profile_redirect(error="Account not found.")

        # reject an email already owned by a DIFFERENT account.
        # Self-service posture is GENERIC (no username revealed) — a logged-in
        # non-admin must not be able to probe which emails map to accounts.
        # exclude_username skips the user's own record so re-saving their
        # current address (or a case-only change) is not a false collision.
        if UserProvisioningService.email_in_use(email, exclude_username=username):
            return _profile_redirect(error="That email address is already in use.")

        get_es_admin_client(es).security.put_user(
            username=username,
            roles=user_data.get("roles", []),
            full_name=user_data.get("full_name", ""),
            email=email,
            # ES writes enabled=true when it is omitted.
            enabled=user_data.get("enabled", True),
            metadata=user_data.get("metadata", {}),
        )
    except Exception as e:
        logging.error(f"profile_update_email: failed for '{username}': {e}")
        return _profile_redirect(error="Failed to update email. Please try again.")

    session["email"] = email
    AuditLogService.log_event(
        username,
        "profile_update_email",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"email={email}",
    )
    return _profile_redirect(success="email")


@app.route("/profile/change-password", methods=["POST"])
def profile_change_password():
    """Self-service: change password. Requires current password."""
    username = session.get("username", "")
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password:
        return _profile_redirect(error="Please enter your current password.")
    if not new_password or len(new_password) < 8:
        return _profile_redirect(error="New password must be at least 8 characters.")
    if len(new_password) > 128:
        return _profile_redirect(error="New password is too long (max 128 characters).")
    if new_password == current_password:
        return _profile_redirect(error="New password must be different from your current password.")
    if new_password != confirm_password:
        return _profile_redirect(error="New passwords do not match.")

    # Re-verify current password against ES — defends against stolen sessions.
    from lib.es_auth import authenticate_user

    auth_check = authenticate_user(username, current_password)
    if not auth_check.get("success"):
        AuditLogService.log_event(
            username,
            "profile_change_password",
            False,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            failure_reason="current_password_invalid",
        )
        return _profile_redirect(error="Current password is incorrect.")

    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

    es = get_elasticsearch_client()
    if es is None:
        return _profile_redirect(error="Could not connect to authentication service. Please try again.")

    try:
        get_es_admin_client(es).security.change_password(username=username, body={"password": new_password})
    except Exception as e:
        logging.error(f"profile_change_password: ES password change failed for '{username}': {e}")
        return _profile_redirect(error="Failed to update password. Please try again.")

    # stamp credential_changed_at so any OTHER live session for this
    # account is denied within the 30s account-state cache TTL. Then advance
    # THIS session's own auth_timestamp to now (intentionally restarting the
    # absolute timer) so this request — which necessarily reached
    # verify_auth with an auth_timestamp older than the stamp just written —
    # is not the very next request denied by session_denial_reason.
    UserProvisioningService.stamp_credential_changed(username)
    session["auth_timestamp"] = time.time()

    AuditLogService.log_event(
        username,
        "profile_change_password",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    # Refresh the Kibana SSO cookie with the new password so the embedded
    # dashboards keep working without re-login.
    kibana_sid_cookie = create_kibana_session(username, new_password)
    response = make_response(_profile_redirect(success="password"))
    if kibana_sid_cookie:
        response.set_cookie(
            "sid",
            kibana_sid_cookie,
            secure=True,
            httponly=True,
            samesite="Lax",
            path="/kibana",
        )
    return response


@app.route("/settings/account-type", methods=["POST"])
def settings_set_account_type():
    """Self-service: flip the current user's account_type.

    The acceptance criterion "Reversible from Settings later"
    requires a way for a user to change their entry-point cohort after
    sign-up. This endpoint is the server side of that. The Profile-page
    UI control (under "Account type") is the primary caller; the route
    also accepts a JSON body so a future programmatic client (admin
    tooling, scripted backfill) can call it without scraping forms.

    Self-edit only — there is no ``target_user`` parameter. Admin flips
    on someone else's behalf belong in user-management, not here, and
    would need a separate auth check.

    The ``invited`` value is rejected: it's a wizard-only signup entry
    point that only makes sense when paired with a live invite token.
    A post-hoc flip to ``invited`` has nothing to anchor a membership
    to and would muddy the entry-point cohort label. Existing users who
    already have ``account_type=invited`` (because they signed up via
    an invite) keep that value — the rejection is only on incoming
    writes.
    """
    try:
        from web.services.user_profile_service import ACCOUNT_TYPE_INVITED
    except ImportError:
        from .services.user_profile_service import ACCOUNT_TYPE_INVITED

    # Detect form-submit vs JSON-API by Content-Type. The Profile-page
    # form posts application/x-www-form-urlencoded; everything else is
    # treated as the JSON API. Form posts redirect to /profile with a
    # flash-style query param (success=account_type / error=...) so
    # the Profile page renders the new value on the next render.
    is_form_submit = request.form and request.content_type and "form-urlencoded" in (request.content_type or "")

    username = session.get("username", "")
    if not username:
        if is_form_submit:
            return _profile_redirect(error="You must be signed in to update your account type.")
        return jsonify({"success": False, "error": "Not signed in"}), 401

    # CSRF: upstream ``check_csrf_for_state_change`` validates the
    # ``X-CSRF-Token`` header OR the form ``csrf_token`` field — either
    # one passing lets the request through. The form path adds an
    # explicit inline check so that a request whose header is present
    # (e.g. fetch() set it) but whose form body has a stale token still
    # bounces, rather than silently flipping the cohort label. JSON
    # callers rely on the header check alone.
    if is_form_submit:
        csrf_token = request.form.get("csrf_token", "")
        if not auth_module.validate_csrf_token(csrf_token):
            return _profile_redirect(error="Your session has expired. Please try again.")

    if is_form_submit:
        account_type = (request.form.get("account_type") or "").strip()
    else:
        data = request.get_json(silent=True) or {}
        account_type = (data.get("account_type") or "").strip()

    if not account_type:
        if is_form_submit:
            return _profile_redirect(error="Pick an account type to continue.")
        return jsonify({"success": False, "error": "account_type required"}), 400

    if account_type == ACCOUNT_TYPE_INVITED:
        # Defensive — the Profile-page form omits the invited tile
        # entirely (follow-up: the user-facing control is
        # individual / org_owner / agency only). A forged form post or
        # a JSON client that hand-rolls the value still has to bounce
        # off this guard or we'd let any user re-tag themselves with
        # the wizard-only sentinel. The check is route-specific:
        # ``invited`` is a legitimate value at the service layer because
        # the wizard step-2 POST writes it for genuinely-invited users.
        err = "Account type 'invited' can only be set by signing up through an invite link."
        if is_form_submit:
            return _profile_redirect(error=err)
        return jsonify({"success": False, "error": err}), 400

    # Broader enum validation is delegated to UserProfileService — the
    # service is the single source of truth for VALID_ACCOUNT_TYPES and
    # short-circuits before any ES write on an unknown value. Pinned by
    # the SETTINGS-001 test.
    result = UserProfileService.set_account_type(username, account_type)
    if not result.get("success"):
        if is_form_submit:
            return _profile_redirect(error=result.get("error") or "Could not update account type.")
        return jsonify(result), 400

    AuditLogService.log_event(
        username,
        "settings_set_account_type",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"{result.get('old_value') or ''} -> {result.get('new_value')}",
    )

    if is_form_submit:
        return _profile_redirect(success="account_type")
    return jsonify(result)


@app.route("/profile/update-time-zone", methods=["POST"])
def profile_update_time_zone():
    """Self-service: update the current user's per-user time zone.

    "User opens their profile page and is able to see current
    time zone and update it. Time zone should be specific to user account."
    The write target is ``user.metadata.time_zone`` via
    ``UserProfileService.set_time_zone`` — independent of the
    workspace-level ``timezone`` field on the de_asset_workspace doc.
    Validation is done by ``zoneinfo`` so an invalid zone surfaces as a
    user-facing error rather than corrupting the record.
    """
    username = session.get("username", "")
    if not username:
        return _profile_redirect(error="You must be signed in to update your time zone.")

    # CSRF: upstream ``check_csrf_for_state_change`` validates the
    # ``X-CSRF-Token`` header OR the form ``csrf_token`` field — either
    # one passing lets the request through. We add the same inline form
    # check the account-type route uses so a request whose header is
    # present (e.g. fetch() set it) but whose form body has a stale
    # token still bounces.
    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return _profile_redirect(error="Your session has expired. Please try again.")

    time_zone = (request.form.get("time_zone") or "").strip()
    if not time_zone:
        return _profile_redirect(error="Pick a time zone to continue.")

    result = UserProfileService.set_time_zone(username, time_zone)
    if not result.get("success"):
        return _profile_redirect(error=result.get("error") or "Could not update time zone.")

    # No-op writes (user re-saved the same value) succeed silently
    # without an audit-log row, matching the account-type route's
    # client-side button-gating contract. A real change always audits.
    if not result.get("noop"):
        AuditLogService.log_event(
            username,
            "profile_update_time_zone",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"{result.get('old_value') or ''} -> {result.get('new_value')}",
        )

    return _profile_redirect(success="time_zone")


@app.route("/profile/mfa/totp/setup", methods=["POST"])
def profile_mfa_totp_setup():
    """Self-service: render TOTP QR. The secret is staged in the Flask session;
    no ES write happens until the user confirms with a valid code. So clicking
    Cancel on the setup page leaves the user's existing MFA record untouched.
    """
    username = session.get("username", "")
    payload = MFAService.build_totp_setup_payload(username)
    session["_totp_setup_secret_enc"] = payload["secret_enc"]
    return render_template(
        "profile_mfa_totp_setup.html",
        qr_code_b64=payload["qr_code_b64"],
        totp_secret=payload["totp_secret"],
        error_message=request.args.get("error"),
    )


@app.route("/profile/mfa/totp/confirm", methods=["POST"])
def profile_mfa_totp_confirm():
    """Self-service: verify the TOTP code against the session-staged secret,
    then commit the full MFA record atomically. On failure, re-render with the
    SAME secret (the user's authenticator app already loaded it — regenerating
    would invalidate their scan).
    """
    username = session.get("username", "")
    code = request.form.get("code", "").strip()
    secret_enc = session.get("_totp_setup_secret_enc")

    if not secret_enc:
        return _profile_redirect(error="Your two-factor setup session expired. Please start again.")

    if not MFAService.verify_totp_code_against_secret(code, secret_enc):
        try:
            payload = MFAService.build_totp_setup_payload(username, existing_secret_enc=secret_enc)
        except Exception:
            session.pop("_totp_setup_secret_enc", None)
            return _profile_redirect(error="Your two-factor setup session expired. Please start again.")
        return render_template(
            "profile_mfa_totp_setup.html",
            qr_code_b64=payload["qr_code_b64"],
            totp_secret=payload["totp_secret"],
            error_message="Invalid code. Please try again with the current code from your authenticator app.",
        )

    commit = MFAService.commit_self_mfa_setup(username, "totp", totp_secret_enc=secret_enc)
    if not commit.get("success"):
        return _profile_redirect(error="Could not save your two-factor setup. Please try again.")

    session.pop("_totp_setup_secret_enc", None)
    AuditLogService.log_event(
        username,
        "profile_mfa_setup",
        True,
        target_user=username,
        mfa_method="totp",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    # surface one-time recovery codes after first enrollment, then
    # return to /profile. Skips the display (and runs the redirect directly)
    # if the user already has codes from a prior enrollment.
    return _show_recovery_codes_then("totp", return_to="profile")


@app.route("/profile/mfa/yubikey/setup", methods=["POST"])
def profile_mfa_yubikey_setup():
    """Self-service: render the YubiKey OTP entry form. No ES write happens
    until the user successfully registers an OTP — so Cancel leaves any
    existing MFA record untouched.
    """
    return render_template(
        "profile_mfa_yubikey_setup.html",
        error_message=request.args.get("error"),
    )


@app.route("/profile/mfa/yubikey/register", methods=["POST"])
def profile_mfa_yubikey_register():
    """Self-service: validate a YubiKey OTP and commit the full MFA record atomically."""
    username = session.get("username", "")
    otp = request.form.get("otp", "").strip()

    validation = MFAService.validate_yubikey_otp_for_registration(
        otp=otp,
        yubico_client_id=config.YUBICO_CLIENT_ID,
        yubico_secret_key=config.YUBICO_SECRET_KEY,
        allow_insecure_dev_mode=config.MFA_ALLOW_INSECURE_DEV,
    )
    if not validation.get("success"):
        return render_template(
            "profile_mfa_yubikey_setup.html",
            error_message=validation.get("error") or "YubiKey registration failed.",
        )

    commit = MFAService.commit_self_mfa_setup(username, "yubikey", yubikey_public_id=validation["public_id"])
    if not commit.get("success"):
        return _profile_redirect(error="Could not save your two-factor setup. Please try again.")

    AuditLogService.log_event(
        username,
        "profile_mfa_setup",
        True,
        target_user=username,
        mfa_method="yubikey",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    # surface one-time recovery codes after first enrollment, then
    # return to /profile.
    return _show_recovery_codes_then("yubikey", return_to="profile")


@app.route("/profile/mfa/disable", methods=["POST"])
def profile_mfa_disable():
    """Self-service: disable MFA. Requires current password and respects ENFORCE_MFA."""
    if config.ENFORCE_MFA:
        return _profile_redirect(error="MFA is required for all accounts and cannot be disabled.")

    username = session.get("username", "")
    current_password = request.form.get("current_password", "")
    if not current_password:
        return _profile_redirect(error="Please enter your current password to disable MFA.")

    from lib.es_auth import authenticate_user

    auth_check = authenticate_user(username, current_password)
    if not auth_check.get("success"):
        AuditLogService.log_event(
            username,
            "profile_mfa_disable",
            False,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            failure_reason="current_password_invalid",
        )
        return _profile_redirect(error="Current password is incorrect.")

    result = MFAService.admin_disable_mfa(username)
    if not result.get("success"):
        return _profile_redirect(error="Failed to disable MFA. Please try again.")

    AuditLogService.log_event(
        username,
        "profile_mfa_disable",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    return _profile_redirect(success="mfa-disabled")


@app.route("/profile/mfa/recovery/regenerate", methods=["POST"])
def profile_mfa_recovery_regenerate():
    """Self-service: regenerate recovery codes from Account Settings.

    Requires an active MFA session: the user must be authenticated AND have a
    confirmed second factor. has_any_factor is the source of truth for "MFA is
    set up" (it ignores the cluster ENFORCE_MFA flag and break-glass bypass).
    Regenerating replaces any existing codes, so it's also the user's recovery
    path after a recovery-code login consumed some.
    """
    username = session.get("username", "")
    if not MFAService.has_any_factor(username):
        return _profile_redirect(error="Set up two-factor authentication before generating recovery codes.")

    result = MFAService.generate_recovery_codes(username)
    if not result.get("success"):
        return _profile_redirect(error="Could not generate recovery codes. Please try again.")

    AuditLogService.log_event(
        username,
        "recovery_codes_regenerated",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    # Display the new codes once via the shared screen, then return to /profile.
    session["_recovery_codes_plaintext"] = result["plaintext"]
    session["_recovery_codes_method"] = "totp"  # unused on the profile return path
    session["_recovery_codes_return_to"] = "profile"
    # Clear any stale post-recovery-login nudge now that codes are refreshed.
    session.pop("mfa_recovery_used_remaining", None)
    return _mfa_redirect("mfa_recovery_codes_page")


@app.route("/profile/mfa/recovery-email/set", methods=["POST"])
def profile_mfa_recovery_email_set():
    """Self-service: set/change the recovery email.

    Stores it UNVERIFIED and dispatches a verification link. The address can't
    receive a recovery-login link until the user clicks that verification link.
    """
    username = session.get("username", "")
    email = (request.form.get("recovery_email") or "").strip()

    result = MFAService.set_recovery_email(username, email)
    if not result.get("success"):
        return _profile_redirect(error=result.get("error") or "Could not save your recovery email.")

    token = MfaRecoveryTokenService.generate_verify_email_token(username, email, config.SECRET_KEY)
    link = _external_mfa_url("profile_mfa_recovery_email_verify", token=token)
    send = MfaRecoveryEmailService.send("recovery_email_verify", MFAService._normalize_email(email), {"link": link})

    AuditLogService.log_event(
        username,
        "recovery_email_set",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    if not send.get("success"):
        # The address was saved; only the email send failed. Tell the user so
        # they can retry rather than waiting on an inbox that won't get a link.
        return _profile_redirect(error="Recovery email saved, but we couldn't send the verification link. Please try again.")
    return _profile_redirect(success="recovery-email-sent")


@app.route("/profile/mfa/recovery-email/verify/<token>", methods=["GET"])
def profile_mfa_recovery_email_verify(token):
    """Verify the recovery email by clicking the link from the inbox.

    No session is required — the signed token (24h, purpose-scoped, bound to
    username+address) is the proof. mark_recovery_email_verified refuses if the
    stored address changed since the link was issued.
    """
    result = MfaRecoveryTokenService.verify_email_token(token, config.SECRET_KEY)
    if not result["valid"]:
        return render_template("mfa_recovery_email_result.html", ok=False, message=result["error"])

    marked = MFAService.mark_recovery_email_verified(result["username"], result["email"])
    if not marked.get("success"):
        return render_template(
            "mfa_recovery_email_result.html",
            ok=False,
            message=marked.get("error") or "We couldn't verify your recovery email.",
        )

    # If the user has a live session, bounce them back to /profile with a success
    # flash; otherwise show a standalone confirmation.
    if session.get("authenticated"):
        return _profile_redirect(success="recovery-email-verified")
    return render_template(
        "mfa_recovery_email_result.html",
        ok=True,
        message="Your recovery email is verified. You can now use it to recover account access.",
    )


# ============================================
# USER MANAGEMENT ROUTES (admin only)
# ============================================


def _require_superuser():
    """Abort with 403 if the current user is not superuser."""
    roles = session.get("roles", [])
    if "superuser" not in roles:
        abort(403, description="This action requires superuser access.")


_SCOPED_ADMIN_ROLE_ESIDS_FOR_GATE = frozenset({"workspace_owner", "workspace_admin", "org_owner", "org_admin"})

# the single permission the four destructive
# ``/api/user-management/*`` routes gate on. WORKSPACE_DELETE is owner-only
# per ``workspace_owner``'s ``list(Permission)`` bundle holds it,
# ``workspace_admin``'s explicit bundle does not — so this makes User
# Management owner-only (AC#3), uniform across create/delete/update/assign,
# matching the ``mc_user_management`` page route + sidebar (both already
# WORKSPACE_DELETE-gated, see SIDEBAR_ITEM_PERMISSIONS).
from asset_management.web.services.permissions_enum import (  # noqa: E402
    Permission as _Permission,
)

_USER_MGMT_GATE_PERMISSION = _Permission.WORKSPACE_DELETE

# Cluster roles that only a superuser may grant through the user-management
# write surface. The role pickers hide these from non-superusers (see the
# user_management view), but the create-user / update-roles endpoints accept
# arbitrary ``system_roles[]`` strings, so this set is also enforced server
# side to close the privilege-escalation path a hidden-but-POSTable role
# would otherwise leave open.
_SUPERUSER_ONLY_SYSTEM_ROLES = frozenset({"superuser", "asset_admin", "data_explorer"})

# roles hidden from the Create User / Edit Roles pickers for ALL
# viewers, superusers included — both are dead UI there. ``superuser`` is
# grantable only from Kibana; ``asset_admin`` rendered as a permanently
# disabled checkbox. ``data_explorer`` is NOT here: the picker is the only
# way to assign it, so it stays visible to superusers (see the
# user_management view). api_update_roles re-adds a target's held
# picker-hidden roles so hiding them never strips an existing holder.
_PICKER_HIDDEN_SYSTEM_ROLES = frozenset({"superuser", "asset_admin"})


def _superuser_only_role_grant_error(requested_roles: list[str]) -> str | None:
    """Return an error message if a non-superuser is granting a superuser-only
    cluster role, else ``None``.

    ``requested_roles`` is the caller-supplied ``system_roles[]`` list. The
    matching role pickers hide :data:`_SUPERUSER_ONLY_SYSTEM_ROLES` from
    non-superusers; this is the server-side enforcement of the same boundary
    so a crafted request can't grant them. Superusers pass unconditionally.
    """
    if "superuser" in session.get("roles", []):
        return None
    blocked = sorted(r for r in requested_roles if isinstance(r, str) and r in _SUPERUSER_ONLY_SYSTEM_ROLES)
    if not blocked:
        return None
    return f"Only superusers can grant the role(s): {', '.join(blocked)}."


def _user_has_scoped_admin_role(username: str) -> bool:
    """True iff the user holds any scoped admin role on any
    scope (workspace / org). Used by ``_require_admin`` to extend the
    cluster-admin check to scoped admins.

    Best-effort; ES outage returns False (fail-closed for the gate).
    """
    if not username:
        return False
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return False
    for index in ("de_authz_user_workspace_role", "de_authz_user_org_role"):
        try:
            resp = es.search(
                index=index,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"username": username}},
                                {"terms": {"role_esid": sorted(_SCOPED_ADMIN_ROLE_ESIDS_FOR_GATE)}},
                            ]
                        }
                    },
                    "size": 1,
                    "_source": False,
                },
            )
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                continue
            logging.warning("scoped-admin lookup failed for %s on %s: %s", username, index, e)
            continue
        if resp.get("hits", {}).get("hits", []):
            return True
    return False


def _require_admin():
    """Abort with 403 if the current user is not admin.

     (C1 policy): \"admin\" now includes cluster admins
    (``asset_admin`` / ``superuser`` from ES Security) AND scoped
    admins (``workspace_owner`` / ``workspace_admin`` / ``org_owner``
    / ``org_admin`` from the new authz model).

    earlier only cluster admins passed. Letting scoped admins
    through unlocks every ``_require_admin`` API endpoint for them,
    which is wider than strictly necessary — a ``workspace_admin``
    for workspace X can in principle call admin endpoints affecting
    workspace Y, because the endpoints don't currently scope-check.

    Mitigation: WARNING log on every scoped-admin call so a follow-up
    ticket (split into ``_require_cluster_admin`` +
    ``_require_scoped_admin(scope)``) has real data to drive the
    callsite audit.
    """
    roles = session.get("roles", [])
    is_cluster_admin = "asset_admin" in roles or "superuser" in roles
    if is_cluster_admin:
        return
    username = session.get("username", "")
    if _user_has_scoped_admin_role(username):
        # C1 policy + log. The path is included so the audit
        # follow-up can see which endpoints are actually being reached.
        logging.warning(
            "scope-violation candidate: user=%s scoped_admin_called=%s",
            username,
            request.path if request else "<unknown>",
        )
        return
    abort(403, description="This page requires admin access.")


def _require_cluster_admin():
    """Abort with 403 unless the session holds ``superuser`` or ``asset_admin``.

    the split ``_require_admin``'s docstring called for. Use it
    for platform-level actions that belong to no tenant (test runners on the
    prod box, approving registrations into ES roles): scoped admins
    (workspace / org owners and admins — every self-signup user) must never
    reach them, and there is no scope to bind them to.
    """
    roles = session.get("roles", [])
    if "superuser" in roles or "asset_admin" in roles:
        return
    abort(403, description="This action requires platform admin access.")


def _require_permission(permission, *, scope_type: str = "workspace") -> None:
    """abort 403 unless the current user holds ``permission``.

    The per-scope replacement for ``_require_admin`` on the destructive
    ``/api/user-management/*`` routes. Where ``_require_admin`` (post
    ) passed any cluster admin OR any scoped admin *anywhere* —
    and logged a ``scope-violation candidate`` WARNING precisely because
    it did not scope-check — this gate asks the typed, owner-only
    question the policy actually requires.

    Two layers compose on every gated route:

    * **This helper** is the *capability* gate. It checks that the
      operator holds ``permission`` (for User Management:
      ``WORKSPACE_DELETE`` — owner-only, since ``workspace_owner``'s
      ``list(Permission)`` bundle has it while ``workspace_admin``'s
      explicit bundle does not). Cluster admins (``asset_admin`` /
      ``superuser``) short-circuit through with no ES read.
    * ``_require_target_in_workspace_scope`` /
      ``_require_memberships_in_workspace_scope`` (, still wired on
      these routes) are the *scope* gate — they confine the operator to
      their own workspace boundary, so a workspace-A owner cannot act on
      a workspace-B target. The pair is strictly tighter than the
      ``_require_admin`` it replaces.

    On denial, writes a ``permission.denied`` audit event with the
    ``{username, permission, scope_type, scope_esid, route}`` tuple and
    aborts 403. ``scope_esid`` is the operator's resolved workspace
    (best-effort; the field is for triage, not enforcement — the
    layer is what enforces scope).
    """
    from asset_management.web.services.permission_resolver import PermissionResolver

    roles = session.get("roles", [])
    if "asset_admin" in roles or "superuser" in roles:
        return  # Cluster-admin bypass — no ES read (AC#4).

    username = session.get("username", "")
    held = PermissionResolver.effective_permissions_anywhere(username)
    if permission in held:
        return

    # Denied — resolve the operator's workspace (best-effort) for the
    # audit row, then 403.
    scope_esid = ""
    try:
        allowed_ws = RoleFilterService.get_allowed_workspaces()
        if allowed_ws:
            scope_esid = sorted(allowed_ws)[0]
    except Exception as e:  # noqa: BLE001 — audit context is best-effort
        logging.warning("workspace resolution for audit failed: %s", e)
    AuditLogService.log_permission_denied(
        username,
        permission=getattr(permission, "value", str(permission)),
        scope_type=scope_type,
        scope_esid=scope_esid,
        route=request.path if request else "<unknown>",
        ip_address=request.remote_addr if request else "",
        user_agent=request.headers.get("User-Agent", "") if request else "",
    )
    abort(403, description="You do not have permission to perform this action.")


def _require_mc(perm: str, page_name: str) -> None:
    """Abort with 403 if the user lacks the given mc_* permission.

    page-level MC routes gate on their corresponding mc_* perm so
    the Role Settings checkboxes actually control visibility. The 403
    handler redirects authenticated page requests to an allowed page (see
    ``forbidden`` below), so a friendly redirect is preserved.

    for the three migrated perm keys
    (``mc_asset_explorer`` / ``mc_user_management`` / ``mc_dashboard_builder``),
    also consult the new-authz model so users holding scoped admin
    roles (``workspace_admin``, ``org_admin``, etc.) can navigate without
    400. Non-migrated keys (``mc_data_connectors``, ``mc_import_*``,
    ``mc_restore_backup``, ``mc_testing``, ``mc_grants_management``)
    keep their OLD-system-only check.
    """
    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if perms.get(perm):
        return
    # fall through to the new-authz check for migrated keys.
    from asset_management.web.services.permission_resolver import (
        PermissionResolver,
        SIDEBAR_ITEM_PERMISSIONS,
    )

    if perm in SIDEBAR_ITEM_PERMISSIONS:
        username = session.get("username", "")
        is_superuser = "superuser" in session.get("roles", [])
        held = PermissionResolver.effective_permissions_anywhere(username, is_super_admin=is_superuser)
        if any(p in held for p in SIDEBAR_ITEM_PERMISSIONS[perm]):
            return
    abort(403, description=f"This page requires the {page_name} permission.")


def _filter_users_to_current_workspace_scope(
    users: list[dict],
    *,
    override_workspace_esid: str | None = None,
) -> list[dict]:
    """#2: scope the User Management user list to the current
    operator's workspace boundary.

    Returns ``users`` unchanged when ``RoleFilterService.get_allowed_workspaces()``
    is ``None`` (privileged operator — superuser / asset_admin with no
    workspace filter). For scoped operators, returns only the users
    whose workspace memberships intersect with the operator's allowed
    workspace set.

     follow-up (2026-05-21): when ``override_workspace_esid`` is
    set AND the operator is a superuser, narrow to that single
    workspace's users instead of computing the allowed set from scope.
    Used by the /user-management workspace filter dropdown — superusers
    default to their own primary workspace but can switch to any
    workspace via the picker for account recovery. Empty string means
    "All workspaces" (no filter applied — superusers see everyone).

    The intersection is computed at three tiers (matches
    ``_workspace_esids_from_scope_titles`` in role_filter_service): a
    user belongs to a workspace if they have ANY of:

    * a direct ``de_authz_workspace_membership`` row for it, OR
    * an org-tier role on an org whose ``workspace_esid`` is in the set, OR
    * a team-tier role on a team whose org is in a workspace in the set.

    All three tiers are loaded with bulk queries so the helper scales
    to ~1000 users with one round trip per tier. Best-effort: an ES
    error on any tier silently drops that tier's contributions rather
    than aborting (the operator still sees the users from the other
    tiers).

    The current operator is ALWAYS included in the result regardless
    of membership state — operators editing their own roles need to
    see their own row.
    """
    from lib.es_helpers import get_elasticsearch_client

    # superuser override — pick exactly the requested
    # workspace (or all workspaces when override is empty string).
    _is_superuser_3 = "superuser" in (session.get("roles") or [])
    if _is_superuser_3 and override_workspace_esid is not None:
        if override_workspace_esid == "":
            return users  # "All workspaces" — no filter applied
        allowed_ws_set: set[str] = {override_workspace_esid}
    else:
        allowed_ws = RoleFilterService.get_allowed_workspaces()
        if allowed_ws is None:
            return users  # Privileged operator — no scope filter.
        allowed_ws_set = set(allowed_ws)
    if not allowed_ws_set:
        # Scoped operator with no workspace access — show only themselves.
        own_username = session.get("username", "") or ""
        return [u for u in users if u.get("username") == own_username]

    es = get_elasticsearch_client()
    if es is None:
        # ES outage — fail closed: return only the operator's own row.
        own_username = session.get("username", "") or ""
        return [u for u in users if u.get("username") == own_username]

    # is the operator privileged (workspace_admin/owner, superuser,
    # asset_admin)? A privileged operator sees the WHOLE workspace (all tiers,
    # all orgs); a non-privileged ORG-tier operator (org owner/admin) must see
    # only their own org(s). Resolved once and reused by Stage 1's gate and the
    # Stage 2 org-narrowing below. Fail-CLOSED on error: treat as NON-privileged
    # so a transient backend error can never expand an org-tier operator's view
    # to the whole workspace (the cross-org disclosure this fix prevents). The
    # empty-list concern does NOT apply here — the operator's own row is
    # always added at the tail of this function regardless of privilege, so
    # failing closed never yields a truly empty list.
    try:
        operator_is_privileged = RoleFilterService.current_user_is_privileged()
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning("privilege check failed (%s); treating as NON-privileged", e)
        operator_is_privileged = False

    # Stage 1: direct workspace members.
    #
    # (2026-06-02) FIX: read ``de_authz_user_workspace_role`` (the
    # workspace ROLE doc), NOT the retired ``de_authz_workspace_membership``
    # index. P3b emptied the membership indices and moved the
    # "user belongs to this workspace" signal onto the role doc (same move as
    # the P3a WorkspaceMembershipService re-key). This reader was missed, so a
    # workspace_admin/owner whose ONLY scope is a workspace-tier role had no
    # membership doc AND isn't caught by the org/team stages below → the User
    # Management list collapsed to just themselves (the reported empty-list
    # bug). The role index is keyed by ``workspace_esid`` the same way, so the
    # query shape is unchanged.
    #
    # gate Stage 1 on privilege. Workspace-tier members are in-scope
    # only for a privileged operator (who sees the whole workspace). A
    # non-privileged org-tier operator must NOT pick up workspace-tier users
    # here — their visible set comes solely from the org/team/grant stages,
    # narrowed to their own org(s) below.
    in_workspace_users: set[str] = set()
    if operator_is_privileged:
        try:
            resp = es.search(
                index="de_authz_user_workspace_role",
                body={
                    "query": {"terms": {"workspace_esid": list(allowed_ws_set)}},
                    "_source": ["username"],
                    "size": 10000,
                },
            )
            for hit in resp.get("hits", {}).get("hits", []):
                uname = (hit.get("_source") or {}).get("username")
                if uname:
                    in_workspace_users.add(uname)
        except Exception as e:
            logging.warning("#2: workspace role load failed: %s", e)

    # Stage 2: orgs in the allowed workspaces -> users via org membership.
    org_esids_in_ws: set[str] = set()
    try:
        resp = es.search(
            index="de_asset_organization",
            body={
                "query": {"terms": {"workspace_esid": list(allowed_ws_set)}},
                "_source": [],
                "size": 10000,
            },
        )
        org_esids_in_ws = {hit["_id"] for hit in resp.get("hits", {}).get("hits", []) if hit.get("_id")}
    except Exception as e:
        logging.warning("#2: org-by-workspace load failed: %s", e)

    # an ORG-tier operator (org owner/admin — NOT a workspace-tier
    # admin/owner, NOT superuser/asset_admin) must see only users in THEIR OWN
    # org(s), not every org in the workspace. get_allowed_workspaces() resolves
    # an org role UP to its parent workspace, so allowed_ws_set above is the
    # whole workspace and org_esids_in_ws is every org in it — the cross-org
    # leak. Narrow org_esids_in_ws to the operator's own resolved org_esids
    # UNLESS the operator is privileged (privileged keeps the whole-workspace
    # view: a workspace admin/owner/superuser legitimately sees all orgs).
    # Stages 2/3/4 all derive from org_esids_in_ws, so narrowing it here scopes
    # the whole user list.
    #
    # FAIL CLOSED (security review 2026-06-03): for a non-privileged operator we
    # DENY by default — if resolve_user_scope raises, OR returns no own_org_esids,
    # we clear org_esids_in_ws to set() rather than leaving it workspace-wide.
    # Leaving it workspace-wide on error/empty would re-open the exact cross-org
    # leak this fix closes. The operator's own row is still added at the tail, so
    # denying here never yields a misleadingly-empty result for the operator.
    if org_esids_in_ws and not operator_is_privileged:
        try:
            from asset_management.web.services.role_filter_service import resolve_user_scope

            _scope = resolve_user_scope(
                session.get("username", ""), session.get("roles", []) or [], RoleFilterService.load_config()
            )
            own_org_esids = set(_scope.org_esids or [])
            # Intersect with the operator's own orgs. Empty own set → empty
            # intersection → deny (correct: a non-privileged operator with no
            # resolvable org scope sees no other org's users, only self).
            org_esids_in_ws = org_esids_in_ws & own_org_esids
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning("org-tier narrowing failed (%s); failing closed (deny cross-org)", e)
            org_esids_in_ws = set()

    if org_esids_in_ws:
        try:
            resp = es.search(
                index="de_authz_user_org_role",
                body={
                    "query": {"terms": {"org_esid": list(org_esids_in_ws)}},
                    "_source": ["username"],
                    "size": 10000,
                },
            )
            for hit in resp.get("hits", {}).get("hits", []):
                uname = (hit.get("_source") or {}).get("username")
                if uname:
                    in_workspace_users.add(uname)
        except Exception as e:
            logging.warning("#2: user-org-role load failed: %s", e)

    # Stage 3: teams under those orgs -> users via team role.
    team_esids_in_ws: set[str] = set()
    if org_esids_in_ws:
        try:
            resp = es.search(
                index="de_asset_teams",
                body={
                    # bare organization_esid (explicit keyword post-reindex).
                    "query": {"terms": {"organization_esid": list(org_esids_in_ws)}},
                    "_source": [],
                    "size": 10000,
                },
            )
            team_esids_in_ws = {hit["_id"] for hit in resp.get("hits", {}).get("hits", []) if hit.get("_id")}
        except Exception as e:
            logging.warning("#2: team-by-org load failed: %s", e)

        if team_esids_in_ws:
            try:
                resp = es.search(
                    index="de_authz_user_team_role",
                    body={
                        "query": {"terms": {"team_esid": list(team_esids_in_ws)}},
                        "_source": ["username"],
                        "size": 10000,
                    },
                )
                for hit in resp.get("hits", {}).get("hits", []):
                    uname = (hit.get("_source") or {}).get("username")
                    if uname:
                        in_workspace_users.add(uname)
            except Exception as e:
                logging.warning("#2: user-team-role load failed: %s", e)

    # Stage 4: users with a data grant whose scope falls
    # inside the allowed workspace set. Three sub-paths union together
    # in one bool/should: workspace-scoped, org-scoped (when there are
    # orgs in scope), team-scoped (when there are teams in scope).
    # Without this leg, a superuser who holds only a workspace-scoped
    # grant on kavanah-media did not appear in another kavanah-media-
    # scoped superuser's User Management list — failing the
    # acceptance line "superuser should also see any users in User
    # Management that have the same workspace-scoped data grant."
    # Expiry is intentionally NOT filtered here: surfacing the row is
    # the right call even for an expired grant (the admin may want to
    # renew). RoleFilterService.get_allowed_workspaces enforces expiry
    # downstream when the user actually queries data.
    # the workspace-scoped grant leg admits holders of a
    # WHOLE-WORKSPACE data grant — in-scope only for a privileged operator. A
    # non-privileged org-tier operator must not pick those up (cross-org leak,
    # same as Stage 1); their grant visibility is limited to the org/team legs
    # below, which use the narrowed org/team sets.
    grant_scope_filters: list[dict] = []
    if operator_is_privileged:
        grant_scope_filters.append(
            {
                "bool": {
                    "must": [
                        {"term": {"scope_type": "workspace"}},
                        {"terms": {"scope_esid": list(allowed_ws_set)}},
                    ]
                }
            }
        )
    if org_esids_in_ws:
        grant_scope_filters.append(
            {
                "bool": {
                    "must": [
                        {"term": {"scope_type": "org"}},
                        {"terms": {"scope_esid": list(org_esids_in_ws)}},
                    ]
                }
            }
        )
    if team_esids_in_ws:
        grant_scope_filters.append(
            {
                "bool": {
                    "must": [
                        {"term": {"scope_type": "team"}},
                        {"terms": {"scope_esid": list(team_esids_in_ws)}},
                    ]
                }
            }
        )
    # skip the grant join entirely when there are no scope filters
    # (a non-privileged org-tier operator with no orgs/teams in scope) — an
    # empty `should` with minimum_should_match:1 matches nothing and is wasted.
    if grant_scope_filters:
        try:
            resp = es.search(
                index="de_authz_data_grant",
                body={
                    "query": {"bool": {"should": grant_scope_filters, "minimum_should_match": 1}},
                    "_source": ["username"],
                    "size": 10000,
                },
            )
            for hit in resp.get("hits", {}).get("hits", []):
                uname = (hit.get("_source") or {}).get("username")
                if uname:
                    in_workspace_users.add(uname)
        except Exception as e:
            logging.warning("data-grant join load failed: %s", e)

    # Always include the current operator.
    own_username = session.get("username", "") or ""
    if own_username:
        in_workspace_users.add(own_username)

    return [u for u in users if u.get("username") in in_workspace_users]


def _require_memberships_in_workspace_scope(memberships: list[str]) -> None:
    """abort 403 if any of the provided ``memberships`` resolves
    to a workspace outside the operator's allowed-workspace set.

    Used by ``api_create_user`` and (indirectly via the per-target gate)
    by ``api_update_roles`` to defend against a workspace-B admin
    provisioning a user with kavanah-media memberships. Without this
    guard the per-target gate keeps the workspace-B admin from
    *managing* the user afterwards (#2's user-list filter
    hides them), but the user still exists with cross-workspace
    privileges they could potentially exploit at login time.

    Wire format: each membership is ``workspace_<slug>`` /
    ``org_<title>`` / ``team_<title>`` (optionally with a ``:role``
    suffix). The prefix dispatches the lookup:

    * workspace_<slug>: lookup the workspace doc by slug (earlier
      slug == _id), use _id as workspace_esid.
    * org_<title>: lookup the org doc by organization_title, read
      workspace_esid from the _source.
    * team_<title>: lookup the team doc by team_title, then the team's
      org by organization_esid, read workspace_esid from the org.

    Superuser / asset_admin with allowed_workspaces=None bypass.

    Membership strings that don't resolve (typo, deleted asset) abort
    400 — better than the silent "membership not found" warning the
    existing _provision path emits.
    """
    if not memberships:
        return

    # Superusers bypass — cluster-level operators provision users in
    # any workspace. Matches the contract on
    # ``_require_target_in_workspace_scope`` so the two helpers stay
    # symmetric.
    if "superuser" in session.get("roles", []):
        return

    allowed_ws = RoleFilterService.get_allowed_workspaces()
    if allowed_ws is None:
        return  # privileged operator — no scope filter
    allowed_ws_set = set(allowed_ws)

    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        abort(503, description="Elasticsearch unavailable; cannot validate membership scope.")

    out_of_scope: list[str] = []
    for membership in memberships:
        if not isinstance(membership, str):
            continue
        parsed = _parse_membership_string(membership)
        if parsed is None:
            abort(400, description=f"Unrecognised membership prefix: {membership}")
        prefix, scope_id, _role_esid, _explicit = parsed
        tier = _AUTHZ_TIER_BY_PREFIX[prefix]

        # Resolve the membership to its workspace_esid.
        try:
            if prefix == "workspace_":
                # Slug == _id. Use a direct get.
                ws_doc = es.get(index="de_asset_workspace", id=scope_id)
                target_ws_esid = ws_doc.get("_id")
            elif prefix == "org_":
                # Title-search the org, read workspace_esid from _source.
                resp = es.search(
                    index="de_asset_organization",
                    body={"query": {"match": {"organization_title": scope_id}}, "size": 1},
                )
                hits = resp.get("hits", {}).get("hits", [])
                if not hits:
                    abort(400, description=f"Org not found: {scope_id}")
                target_ws_esid = (hits[0].get("_source") or {}).get("workspace_esid")
            elif prefix == "team_":
                # Title-search the team, walk to org, read workspace_esid.
                team_resp = es.search(
                    index="de_asset_teams",
                    body={"query": {"match": {"team_title": scope_id}}, "size": 1},
                )
                team_hits = team_resp.get("hits", {}).get("hits", [])
                if not team_hits:
                    abort(400, description=f"Team not found: {scope_id}")
                team_src = team_hits[0].get("_source") or {}
                org_esid = team_src.get("organization_esid")
                if not org_esid:
                    # Orphan team — treat as out-of-scope by definition.
                    out_of_scope.append(membership)
                    continue
                try:
                    org_doc = es.get(index="de_asset_organization", id=org_esid)
                    target_ws_esid = (org_doc.get("_source") or {}).get("workspace_esid")
                except Exception:
                    target_ws_esid = None
            else:
                target_ws_esid = None
        except Exception as e:
            err = str(e).lower()
            if "not_found" in err or "404" in err:
                abort(400, description=f"Membership target not found: {membership}")
            logging.warning(
                "membership-scope lookup failed for %s: %s",
                membership,
                e,
            )
            # Fail closed — cross-workspace risk outweighs convenience.
            out_of_scope.append(membership)
            continue

        if not target_ws_esid or target_ws_esid not in allowed_ws_set:
            out_of_scope.append(membership)

    if out_of_scope:
        abort(
            403,
            description=(f"Membership(s) outside your workspace scope: " f"{', '.join(sorted(out_of_scope))}."),
        )


def _resolve_workspaces_for_memberships(es, memberships: list[str]) -> tuple[set[str], list[str]]:
    """resolve a list of membership strings to the set of
    workspace_esids they touch.

    Mirrors the lookup pattern in ``_require_memberships_in_workspace_scope``
    but instead of asserting against an allowed set, returns the union of
    all resolved workspaces plus the list of memberships that couldn't
    be resolved (typo, deleted asset, orphan team without an org).

    Returns ``(workspace_esids, unresolved)``. Callers decide what
    unresolved memberships mean for their use case — the
    single-workspace check at ``_validate_single_workspace_for_non_superuser``
    rejects them as 400s, mirroring the existing route gate behavior.

    Used as the lookup engine for the single-workspace
    constraint on non-superuser targets.
    """
    resolved: set[str] = set()
    unresolved: list[str] = []

    for membership in memberships:
        if not isinstance(membership, str):
            continue
        parsed = _parse_membership_string(membership)
        if parsed is None:
            unresolved.append(membership)
            continue
        prefix, scope_id, _role_esid, _explicit = parsed

        try:
            if prefix == "workspace_":
                # Slug == _id.
                ws_doc = es.get(index="de_asset_workspace", id=scope_id)
                ws_esid = ws_doc.get("_id")
            elif prefix == "org_":
                resp = es.search(
                    index="de_asset_organization",
                    body={"query": {"match": {"organization_title": scope_id}}, "size": 1},
                )
                hits = resp.get("hits", {}).get("hits", [])
                if not hits:
                    unresolved.append(membership)
                    continue
                ws_esid = (hits[0].get("_source") or {}).get("workspace_esid")
            elif prefix == "team_":
                team_resp = es.search(
                    index="de_asset_teams",
                    body={"query": {"match": {"team_title": scope_id}}, "size": 1},
                )
                team_hits = team_resp.get("hits", {}).get("hits", [])
                if not team_hits:
                    unresolved.append(membership)
                    continue
                team_src = team_hits[0].get("_source") or {}
                org_esid = team_src.get("organization_esid")
                if not org_esid:
                    # P62 fix: a team with no parent org (org_esid
                    # empty/[]) is NOT necessarily an orphan — the team doc
                    # carries its own ``workspace_esid`` (a team can belong to
                    # a workspace directly without an org, e.g. "Kavanah Team").
                    # Resolve via that before declaring it unresolved; only a
                    # team with neither an org NOR a workspace_esid is a true
                    # orphan. Previously this path 400'd every org-less team,
                    # blocking Create User for them.
                    direct_ws = team_src.get("workspace_esid")
                    if direct_ws:
                        resolved.add(direct_ws)
                    else:
                        unresolved.append(membership)
                    continue
                # team.organization_esid can be a list (multi-org-per-team,
                # ). The team belongs to multiple workspaces if its
                # parent orgs do; in practice this is rare and the strict
                # rule says all such workspaces count. Walk every parent org.
                if isinstance(org_esid, list):
                    org_esids_to_check = [oid for oid in org_esid if oid]
                else:
                    org_esids_to_check = [org_esid]
                ws_esid = None
                for oid in org_esids_to_check:
                    try:
                        org_doc = es.get(index="de_asset_organization", id=oid)
                        oid_ws = (org_doc.get("_source") or {}).get("workspace_esid")
                        if oid_ws:
                            resolved.add(oid_ws)
                            ws_esid = oid_ws  # keep last for the bottom add
                    except Exception:
                        continue
                # All parent orgs already added above; skip the bottom add
                # (ws_esid is just a marker that we resolved at least one).
                if ws_esid is None:
                    unresolved.append(membership)
                continue
            else:
                unresolved.append(membership)
                continue
        except Exception as e:
            err = str(e).lower()
            if "not_found" in err or "404" in err:
                unresolved.append(membership)
                continue
            logging.warning(
                "workspace resolution failed for %s: %s",
                membership,
                e,
            )
            unresolved.append(membership)
            continue

        if ws_esid:
            resolved.add(ws_esid)
        else:
            unresolved.append(membership)

    return resolved, unresolved


def _validate_single_workspace_for_non_superuser(target_username: str, memberships: list[str]) -> None:
    """abort 400 if the proposed memberships would put a
    non-superuser target user into >1 workspace.

    Non-superusers must belong to at most one workspace. The constraint
    is enforced at the two write paths a superuser can reach
    (api_create_user, api_update_roles) — workspace-scoped admins are
    already blocked from cross-workspace assignments by the
    ``_require_memberships_in_workspace_scope`` gate.

    Target users who are themselves superusers are exempt: a superuser
    provisioning another superuser can intentionally span workspaces.

    Unresolved memberships (typo, deleted asset, orphan team) are
    surfaced as a 400 with the offending strings — same shape as the
     gate so the operator sees a single consistent error format.

    Zero workspaces is allowed (e.g., a user with only org/team roles
    on deleted orgs, or a partially provisioned account). The constraint
    is "at most one", not "exactly one".
    """
    if not memberships:
        return

    # Exempt superuser targets — cluster operators may span workspaces.
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        # Fail-open on ES outage: without ES we can't walk membership →
        # workspace, and the constraint is defense-in-depth on top of
        # the scope gate. A real outage is rare AND the system
        # is already degraded; better to let the write proceed and
        # re-validate on the next write than to add a hard ES dependency
        # to a path that previously didn't need ES. Logged at WARNING so
        # a regression to a permanent fail-open state is visible.
        logging.warning(
            "ES unavailable; skipping single-workspace constraint for %s. "
            "Cross-workspace memberships will not be detected on this write.",
            target_username,
        )
        return
    try:
        target = dict(es.security.get_user(username=target_username)).get(target_username, {})
        if "superuser" in (target.get("roles") or []):
            return
    except Exception:
        # User doesn't exist yet (create flow) or fetch failed — treat
        # as non-superuser and proceed with the constraint check. A
        # genuine superuser-target on create is impossible anyway
        # because the user is being created in this same request.
        pass

    resolved_workspaces, unresolved = _resolve_workspaces_for_memberships(es, memberships)

    if unresolved:
        abort(
            400,
            description=(
                f"Could not resolve membership(s) to a workspace: "
                f"{', '.join(sorted(unresolved))}. Check the org/team titles and retry."
            ),
        )

    if len(resolved_workspaces) > 1:
        ws_list = ", ".join(repr(w) for w in sorted(resolved_workspaces))
        abort(
            400,
            description=(
                f"User {target_username!r} would belong to multiple workspaces: "
                f"{ws_list}. Non-superusers must belong to exactly one workspace. "
                f"Remove the cross-workspace memberships and retry."
            ),
        )


def _require_target_in_workspace_scope(target_username: str) -> None:
    """abort 403 if ``target_username`` is outside the operator's
    workspace scope.

    Runs alongside ``_require_admin()`` on every per-user mutate route
    in ``/api/user-management/*`` (delete-user, suspend-user,
    update-roles, etc.) so a workspace-B admin can't POST a
    credential-reset to a kavanah-media user via curl. The page-level
    user list filter only hid the user from the rendered
    table; the mutate routes need their own per-target gate.

    Contract:
    * **Superuser** → always pass. Cluster operators need to manage
      every user (password resets, suspensions, etc.) regardless of
      workspace. The deny-all-for-no-grants semantics of
      ``RoleFilterService`` is the right shape for data-read
      filtering but the wrong shape for user-management — a superuser
      with no data grants still needs to administer users.
    * Scoped operator with ``target_username`` in their allowed user
      set (computed by ``_filter_users_to_current_workspace_scope``,
      so the two paths can never drift) → pass.
    * Scoped operator with ``target_username`` outside their allowed
      user set → abort 403 with "User '<x>' is outside your workspace
      scope" — explicit naming so operators understand which name is
      out of bounds.
    * Empty ``target_username`` → abort 400. Bad input; the route
      validators usually catch this earlier, but the gate fails-closed
      to defend against a future caller that forgets to validate.

    Reuses ``_filter_users_to_current_workspace_scope`` for the
    scoped-operator path so the two paths can never drift — a future
    fix to the 3-tier traversal flows through both surfaces.
    """
    target_username = (target_username or "").strip()
    if not target_username:
        abort(400, description="target username is required")

    # Superusers bypass the workspace scope check — cluster-level
    # operators need to manage every user regardless of which
    # workspace they belong to.
    if "superuser" in session.get("roles", []):
        return

    allowed = _filter_users_to_current_workspace_scope([{"username": target_username}])
    if not allowed:
        abort(403, description=f"User {target_username!r} is outside your workspace scope.")
    _refuse_privileged_targets([target_username])


_PLATFORM_ROLES = frozenset({"superuser", "asset_admin"})


def _refuse_privileged_targets(usernames: list[str]) -> None:
    """abort 403 if a non-superuser operator targets a platform admin.

    A superuser or asset_admin can hold a grant inside a customer workspace
    , which puts them in that workspace's user-management scope; a
    workspace owner must still not be able to reset, re-email, suspend or
    delete them. Roles come from ES Security via the fail-closed
    ``_resolve_es_roles``: a target whose roles cannot be read is refused,
    never assumed harmless.
    """
    if "superuser" in session.get("roles", []):
        return
    for username in usernames:
        roles = auth_module._resolve_es_roles(username)
        if roles is None:
            abort(403, description=f"Could not verify user {username!r}; try again.")
        if _PLATFORM_ROLES & set(roles):
            abort(403, description=f"User {username!r} is a platform admin; only a superuser can change that account.")


# ES Security user-metadata key recording who suspended an account.
# Shape: ``{"username", "platform_admin": bool, "suspended_at"}``. The
# ``suspended_at`` copy binds the attribution to ONE suspension: an account
# disabled later by any other path (Kibana, a direct ES write) carries a
# different ``metadata.suspended_at``, so a stale attribution never vouches
# for it.
_SUSPENSION_ATTRIBUTION_KEY = "suspended_by"


def _stamp_suspension_actor(usernames: list[str]) -> None:
    """record the operator on each account they just suspended.

    Called only after ``UserManagementService.suspend_user`` reported success,
    so a re-suspend of an already-suspended account never overwrites the
    original suspender. Best-effort: an account left unstamped is treated by
    :func:`_refuse_platform_suspension_reinstate` as platform-suspended
    (refused to non-superusers), so a failure here fails closed.
    """
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        logging.warning("ES unavailable; suspension of %s left unattributed", usernames)
        return
    actor = session.get("username", "")
    platform_admin = bool(_PLATFORM_ROLES & set(session.get("roles", [])))
    for username in usernames:
        user_data, err = UserManagementService._get_user(es, username)
        if err:
            logging.warning("could not read %r to attribute its suspension: %s", username, err)
            continue
        attribution = {
            "username": actor,
            "platform_admin": platform_admin,
            "suspended_at": (user_data.get("metadata") or {}).get("suspended_at"),
        }
        try:
            UserManagementService._put_user(
                es, username, user_data, metadata_updates={_SUSPENSION_ATTRIBUTION_KEY: attribution}
            )
        except Exception as e:  # noqa: BLE001 — the suspension already happened; unattributed fails closed
            logging.warning("could not attribute suspension of %r: %s", username, e)


def _refuse_platform_suspension_reinstate(username: str) -> None:
    """abort 403 if a non-superuser reinstates a platform suspension.

    A workspace owner may reinstate an account only when its metadata shows
    the CURRENT suspension was made by a non-platform operator (see
    :func:`_stamp_suspension_actor`). An account suspended by a superuser or
    asset_admin, disabled outside the app, or suspended before attribution
    existed carries no such proof and is refused — only a superuser can lift
    it. An account that is not suspended passes through so the service
    reports "not suspended" as before.
    """
    if "superuser" in session.get("roles", []):
        return
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        abort(503, description="Elasticsearch unavailable; cannot verify the suspension.")
    user_data, err = UserManagementService._get_user(es, username)
    if err:
        abort(403, description=f"Could not verify user {username!r}; try again.")
    if user_data.get("enabled", True):
        return
    metadata = user_data.get("metadata") or {}
    attribution = metadata.get(_SUSPENSION_ATTRIBUTION_KEY)
    if (
        isinstance(attribution, dict)
        and attribution.get("platform_admin") is False
        and metadata.get("suspended_at")
        and attribution.get("suspended_at") == metadata.get("suspended_at")
    ):
        return
    abort(
        403,
        description=(f"User {username!r} was suspended by a platform admin; only a superuser can reinstate this account."),
    )


def _caller_workspace_scope() -> set[str] | None:
    """the workspaces the caller may act in; ``None`` = unrestricted.

    Superusers are unrestricted. Everyone else gets
    ``RoleFilterService.get_allowed_workspaces()`` as a set. That resolver's
    ``None`` is overloaded ("see-all" vs "roleless user with no scope"), so it
    widens only for an explicit ``asset_admin``; for anyone else it is the
    empty set — fail closed.
    """
    roles = session.get("roles", [])
    if "superuser" in roles:
        return None
    allowed = RoleFilterService.get_allowed_workspaces()
    if allowed is None:
        return None if "asset_admin" in roles else set()
    return set(allowed)


def _resolve_scope_workspace_esid(es, scope_type: str, scope_esid: str) -> str | None:
    """the workspace a role-assignment scope belongs to, or ``None``.

    The same walk ``api_assign_role`` does: a workspace scope is its own
    esid (earlier ``_id`` == slug == workspace_esid); an org reads
    ``workspace_esid`` off its doc; a team reads its own ``workspace_esid``
    and falls back to team -> org for teams that pre-date the
    backfill. Anything unresolvable — unknown tier, missing doc, the
    ``__all__`` sentinel, an ES error — is ``None``, which every caller
    treats as out of scope.
    """
    if not scope_esid:
        return None
    try:
        if scope_type == "workspace":
            return scope_esid
        if scope_type == "org":
            org = es.get(index="de_asset_organization", id=scope_esid)
            return (org.get("_source") or {}).get("workspace_esid") or None
        if scope_type == "team":
            team_src = es.get(index="de_asset_teams", id=scope_esid).get("_source") or {}
            workspace_esid = team_src.get("workspace_esid")
            org_esid = team_src.get("organization_esid")
            if not workspace_esid and org_esid:
                org = es.get(index="de_asset_organization", id=org_esid)
                workspace_esid = (org.get("_source") or {}).get("workspace_esid")
            return workspace_esid or None
    except Exception as e:  # noqa: BLE001 — unresolvable scope is out of scope (fail closed)
        logging.warning("scope-workspace lookup failed for %s %r: %s", scope_type, scope_esid, e)
    return None


def _load_authz_memberships_index() -> dict:
    """Return ``{username: [prefixed_string, ...]}`` from the new authz indices.

    Reads ``de_authz_user_team_role`` and ``de_authz_user_org_role`` once and
    joins them against ``de_asset_teams`` / ``de_asset_organization`` to render
    each membership as the ``team_<title>`` / ``org_<title>`` string the
    rest of the UI keys off. Used by the user-list view so existing users
    whose memberships only live in the new authz model still surface as
    pills in the row cell — retired the legacy ``org_*``/``team_*``
    role strings on the ES Security record, so the row's ``user.roles`` no
    longer carries them on its own.
    """
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return {}

    # Build esid → title maps once.
    team_titles: dict[str, str] = {}
    org_titles: dict[str, str] = {}
    try:
        for hit in (
            es.search(index="de_asset_teams", body={"query": {"match_all": {}}, "size": 1000}).get("hits", {}).get("hits", [])
        ):
            src = hit.get("_source", {}) or {}
            title = src.get("team_title")
            if title:
                team_titles[hit["_id"]] = title
    except Exception as e:
        logging.warning("authz-membership-index: team title load failed: %s", e)
    try:
        for hit in (
            es.search(index="de_asset_organization", body={"query": {"match_all": {}}, "size": 1000})
            .get("hits", {})
            .get("hits", [])
        ):
            src = hit.get("_source", {}) or {}
            title = src.get("organization_title")
            if title:
                org_titles[hit["_id"]] = title
    except Exception as e:
        logging.warning("authz-membership-index: org title load failed: %s", e)

    out: dict[str, list[str]] = {}

    try:
        for hit in (
            es.search(index="de_authz_user_team_role", body={"query": {"match_all": {}}, "size": 5000})
            .get("hits", {})
            .get("hits", [])
        ):
            src = hit.get("_source", {}) or {}
            uname = src.get("username")
            team_esid = src.get("team_esid")
            if not uname or not team_esid or team_esid == "__all__":
                continue
            title = team_titles.get(team_esid)
            if not title:
                continue
            out.setdefault(uname, []).append(f"team_{title}")
    except Exception as e:
        logging.warning("authz-membership-index: team-role load failed: %s", e)

    try:
        for hit in (
            es.search(index="de_authz_user_org_role", body={"query": {"match_all": {}}, "size": 5000})
            .get("hits", {})
            .get("hits", [])
        ):
            src = hit.get("_source", {}) or {}
            uname = src.get("username")
            org_esid = src.get("org_esid")
            if not uname or not org_esid:
                continue
            title = org_titles.get(org_esid)
            if not title:
                continue
            out.setdefault(uname, []).append(f"org_{title}")
    except Exception as e:
        logging.warning("authz-membership-index: org-role load failed: %s", e)

    # third surface: drop redundant team_<X> entries when the
    # same user holds org_<X> on a scope of the same title. See helper
    # docstring for the full rationale.
    for uname in out:
        out[uname] = _collapse_membership_strings(out[uname])

    # Stable ordering for templates and tests.
    for uname in out:
        out[uname] = sorted(set(out[uname]))
    return out


def _collapse_membership_strings(memberships: list[str]) -> list[str]:
    """Drop redundant ``team_<X>`` entries when ``org_<X>`` is also
    present (third surface — the chip tray on the user-list
    row + the Edit Roles modal's Memberships pre-check).

    The wizard's three-tier fan-out writes ROLE_OWNER
    to both org + team tier indices for individual users. The
    ``_load_authz_memberships_index`` join then emits ``org_<title>`` AND
    ``team_<title>`` for the same bare title, which the chip tray
    renders as two visually-identical pills ("test-account x2"). For
    individual wizard users the org and team are auto-created shadows
    of the workspace, not user-meaningful entities — surfacing both is
    noise.

    Rule: if ``org_<X>`` is in the list, drop ``team_<X>`` for the same
    ``<X>``. Sibling entries with differing titles survive (e.g.
    ``team_TeamA`` survives when only ``org_OrgB`` exists). Workspace
    prefixes aren't in this list (the user-list row's data-memberships
    is org/team only by design — workspaces are surfaced through a
    different mechanism).

    Data is unchanged — this only affects the display payload. The
    underlying team membership still exists in
    ``de_authz_user_team_role`` contract, and the Scoped
    roles + Current memberships panels already collapse it via the
    sibling helpers on the role-assignment APIs.
    """
    org_titles = {m[len("org_") :] for m in memberships if m.startswith("org_")}
    return [m for m in memberships if not (m.startswith("team_") and m[len("team_") :] in org_titles)]


@app.route("/user-management")
def user_management():
    """User Management page — gates on mc_user_management.

    API endpoints under ``/api/user-management/*`` keep their existing
    ``_require_admin`` checks per the ticket non-goals: they are admin
    actions, not user-facing MC pages.
    """
    _require_mc("mc_user_management", "User Management")
    users = MFAService.list_all_users_with_mfa()

    # (2026-05-21): superusers see a workspace filter dropdown
    # at the top of /user-management. Default to the superuser's own
    # primary workspace so they don't drown in everyone's accounts on
    # every page load. ``?workspace=<esid>`` query param overrides the
    # default; empty string ("?workspace=") means "All workspaces".
    # Non-superusers don't see the dropdown and are scoped by the
    # gate as before.
    _is_superuser_3 = "superuser" in (session.get("roles") or [])
    _workspace_filter: str | None = None
    if _is_superuser_3:
        if "workspace" in request.args:
            # Param present (possibly empty string for "All workspaces").
            _workspace_filter = (request.args.get("workspace") or "").strip()
        else:
            _username_2 = session.get("username", "") or ""

            # Build a workspace title map once — used both to sort the
            # grant-inferred candidates and to sort the primary-fallback
            # set, mirroring the Builder's _default_workspace_esid which
            # picks the title-alphabetically-first workspace when no
            # better signal exists.
            _ws_titles: dict[str, str] = {}
            try:
                from lib.es_helpers import get_all_documents as _get_all_3

                _ws_titles = {
                    d.get("_id"): (d.get("_source") or {}).get("workspace_title") or d.get("_id")
                    for d in _get_all_3("de_asset_workspace")
                }
            except Exception as _e:
                logging.warning("workspace title map load failed: %s", _e)

            # follow-up (2026-06-04): for a multi-workspace
            # superuser, "primary" from de_authz_user_workspace_role is
            # arbitrary (workspaces[0] by created_at). A `de_authz_data_grant`
            # row is a stronger signal of intent — it says "this is the
            # workspace this operator was explicitly granted access to"
            # — so prefer the grant-inferred workspace over primary.
            # Resolve workspace-scoped grants directly; resolve org/team-
            # scoped grants via _workspace_esids_from_scope_esids (the
            # same walk get_allowed_workspaces uses).
            _grant_ws: set[str] = set()
            try:
                from asset_management.web.services.data_grant_service import DataGrantService

                _grants = DataGrantService.list_for_user(_username_2)
                _org_esids: set[str] = set()
                _team_esids: set[str] = set()
                for _g in _grants:
                    _stype = _g.get("scope_type")
                    _sesid = _g.get("scope_esid")
                    if not _stype or not _sesid:
                        continue
                    if _stype == "workspace":
                        _grant_ws.add(_sesid)
                    elif _stype == "org":
                        _org_esids.add(_sesid)
                    elif _stype == "team":
                        _team_esids.add(_sesid)
                if _org_esids or _team_esids:
                    from asset_management.web.services.role_filter_service import (
                        _workspace_esids_from_scope_esids as _resolve_2,
                    )

                    _grant_ws |= _resolve_2(_org_esids, _team_esids)
            except Exception as _e:
                logging.warning("data-grant workspace resolution failed: %s", _e)

            if _grant_ws:
                _sorted_grants = sorted(
                    _grant_ws,
                    key=lambda e: (_ws_titles.get(e) or e or "").lower(),
                )
                _workspace_filter = _sorted_grants[0]

            # No usable grant → fall back to the user's primary workspace
            # (the legacy behavior).
            if not _workspace_filter:
                try:
                    from asset_management.web.services.workspace_membership_service import (
                        WorkspaceMembershipService as _wms,
                    )

                    _workspace_filter = _wms.get_primary_workspace(_username_2)
                except Exception as _e:
                    logging.warning("primary workspace lookup failed: %s", _e)
                    _workspace_filter = None

            # No grant AND no primary → first resolvable workspace by
            # title (mirrors Builder's _default_workspace_esid final
            # fallback). Only when the user truly has no resolvable
            # scope do we drop to "" ("All workspaces").
            if not _workspace_filter:
                try:
                    _allowed = RoleFilterService.get_allowed_workspaces()
                except Exception as _e:
                    logging.warning("get_allowed_workspaces failed in User Management default: %s", _e)
                    _allowed = []
                if _allowed:
                    _sorted = sorted(
                        (w for w in _allowed if w in _ws_titles),
                        key=lambda e: (_ws_titles.get(e) or "").lower(),
                    )
                    if _sorted:
                        _workspace_filter = _sorted[0]
        # Normalise None to "" so the rest of the route + template
        # branches uniformly. The override_workspace_esid contract:
        # None = "no override", "" = "All workspaces", any other str
        # = "this workspace only".
        if _workspace_filter is None:
            _workspace_filter = ""

    # #2: workspace-scope the User Management user list. Pre-fix,
    # a workspace-B admin saw users from kavanah-media (and any other
    # workspace) in the list. Post-fix, a non-superuser admin sees only
    # users whose workspace memberships intersect with their own
    # allowed-workspace set. Superusers + asset_admins (where
    # RoleFilterService.get_allowed_workspaces() returns None — no
    # filter applies) see everyone.
    #
    # pass the superuser workspace filter through. Empty
    # string = "All workspaces"; non-empty = narrow to that workspace.
    users = _filter_users_to_current_workspace_scope(
        users,
        override_workspace_esid=_workspace_filter,
    )

    # Fetch orgs, teams, and existing ES roles for the explorer/sync UI
    from lib.es_helpers import get_all_documents as _get_all

    # follow-up: keep each org's _id (== its org_esid) alongside the
    # _source fields so the org-tier dropdown narrowing below can match on the
    # operator's own org_esids. _source carries no _id; the template only reads
    # organization_title, so adding the key is safe.
    orgs = [{**d.get("_source", {}), "_id": d.get("_id")} for d in _get_all("de_asset_organization")]
    teams = [d.get("_source", {}) for d in _get_all("de_asset_teams")]
    # workspace tier for the Create User membership picker.
    # Sorted by title for the picker; slug is the picker value.
    workspaces = sorted(
        (d.get("_source", {}) for d in _get_all("de_asset_workspace")),
        key=lambda w: (w.get("workspace_title") or w.get("workspace_slug") or "").lower(),
    )

    # follow-up 2026-05-19: filter the Create User + Edit Roles
    # modal dropdowns to the operator's workspace scope. The route-level
    # _require_memberships_in_workspace_scope helper rejects POSTs that
    # try to grant cross-workspace memberships, but the UI was still
    # offering them as options — surprising the operator with a 403
    # after they pick a name they shouldn't have seen. The dropdowns
    # are populated from the template context vars below (workspaces,
    # orgs, teams), so the fix narrows the three lists here before they
    # ship to the template.
    #
    # follow-up (2026-05-21): superusers can now narrow the
    # picker too — the workspace filter dropdown at the top of the
    # page selects a single workspace, and when that's set (non-empty)
    # the modal pickers narrow to match. Empty string = "All
    # workspaces" = pickers stay unfiltered (superuser default before
    # the dropdown was added).
    #
    # The whole block is wrapped in try/except so a failure in the
    # scope resolution doesn't 500 the page. On any error we fall
    # back to empty dropdowns for non-superusers — the route-level
    # gates still defend, but the operator sees a "no options"
    # picker rather than a broken page. Logged at WARNING with the
    # operator's username so we can spot a regression.
    _should_filter_pickers = False
    _allowed_ws_for_picker: list[str] | None = None
    if not _is_superuser_3:
        _should_filter_pickers = True
        try:
            _allowed_ws_for_picker = RoleFilterService.get_allowed_workspaces()
        except Exception as _e:
            logging.warning(
                "follow-up: get_allowed_workspaces failed for %s: %s",
                session.get("username", "unknown"),
                _e,
            )
            _allowed_ws_for_picker = []  # fail-closed: empty allowed set
    elif _workspace_filter:
        # Superuser with a workspace selected — narrow pickers to that
        # one workspace.
        _should_filter_pickers = True
        _allowed_ws_for_picker = [_workspace_filter]

    # follow-up: an ORG-tier operator (org owner/admin — not a
    # workspace admin/owner, not superuser/asset_admin) must see only THEIR
    # OWN org(s) in the org dropdown, not every org in the workspace, AND must
    # not see the Create User button (org owners can't create users for now —
    # workspace owners + superusers still can). Privilege is resolved once here
    # and reused below + passed to the template. FAIL CLOSED: on error treat as
    # NON-privileged (deny the workspace-wide org list + hide Create User) — a
    # transient backend error must not re-open the cross-org dropdown leak.
    try:
        _operator_privileged = RoleFilterService.current_user_is_privileged()
    except Exception as _e:  # noqa: BLE001 — boundary
        logging.warning("privilege check failed for user-mgmt page (%s); failing closed", _e)
        _operator_privileged = False
    # Create User is workspace-owner/superuser only — hidden for org-tier ops.
    can_create_user = bool(_operator_privileged)

    # resolve the viewer's active workspace TITLE for the hidden
    # ``data-docs-workspace`` anchor the docs-screenshot gate asserts against
    # (pattern; /dashboard does the same on #dashboard-toolbar). This
    # page shows no workspace name to a non-superuser — the workspace filter
    # bar is superuser-only — so without this a demo-account capture has no
    # way to prove its context and the gate would refuse every shot. Never
    # rendered as visible copy. Fail-soft: on any error the attribute is
    # omitted, the page is unaffected, and the capture gate fail-closes.
    _docs_workspace = ""
    try:
        from asset_management.web.blueprints.dashboard_builder.routes import (
            _selectable_workspaces_for_user,
            _default_workspace_esid,
        )

        _dw = _selectable_workspaces_for_user()
        _dw_default = _default_workspace_esid(_dw)
        _docs_workspace = next(
            (w.get("title", "") for w in _dw if w.get("esid") == _dw_default),
            (_dw[0].get("title", "") if _dw else ""),
        )
    except Exception as _e:  # noqa: BLE001 — never block the render
        logging.warning("workspace anchor load failed: %s", _e)
    # Resolve the org-tier operator's OWN org_esids for the dropdown narrowing.
    # Only needed for a non-privileged operator; privileged keeps the
    # workspace-narrowed list (the behavior).
    _own_org_esids: set[str] | None = None
    if not _operator_privileged:
        try:
            from asset_management.web.services.role_filter_service import resolve_user_scope as _scope_2

            _s = _scope_2(session.get("username", ""), session.get("roles", []) or [], RoleFilterService.load_config())
            _own_org_esids = set(_s.org_esids or [])
        except Exception as _e:  # noqa: BLE001 — boundary
            logging.warning("own-org resolution failed for user-mgmt page (%s); failing closed", _e)
            _own_org_esids = set()  # fail closed: deny cross-org

    if _should_filter_pickers:

        if _allowed_ws_for_picker is not None:
            _allowed_set = set(_allowed_ws_for_picker)

            def _esid_values(raw):
                """Coerce a doc's ESID-or-list field to a frozenset.

                Some asset docs carry ``organization_esid`` (and a few
                similar fields) as a list — a team that belongs to
                multiple orgs is the multi-org-per-team
                shape. A plain ``in _allowed_set`` check fails with
                ``TypeError: unhashable type: 'list'`` against those
                docs. Normalising to a set lets us use overlap
                semantics: a team is in-scope if ANY of its parent
                ESIDs is in the allowed set.
                """
                if raw is None:
                    return frozenset()
                if isinstance(raw, list):
                    return frozenset(v for v in raw if v)
                return frozenset([raw])

            try:
                # Workspaces — the _id IS the slug. We
                # don't have access to the _id here (only _source) so
                # we match on workspace_slug — same identity by
                # construction.
                workspaces = [w for w in workspaces if (w.get("workspace_slug") or "") in _allowed_set]
                # Orgs — carry workspace_esid directly.
                # An org with NO workspace_esid is legacy pre-F5 and
                # treated as out-of-scope (deny by default). The
                # _esid_values helper handles both scalar and list
                # shapes; intersection-non-empty means "this org
                # belongs to at least one allowed workspace".
                orgs = [o for o in orgs if _esid_values(o.get("workspace_esid")) & _allowed_set]
                # follow-up: a non-privileged ORG-tier operator's
                # workspace resolves to the WHOLE workspace above, so the
                # workspace filter alone leaves every org in it (the dropdown
                # leak). Further narrow to the operator's OWN org_esids. An org
                # doc's _id IS its org_esid (kept on the dict above). Empty
                # own-org set → no orgs (deny; the operator can't pick a foreign
                # org). This mirrors the user-list narrowing in
                # _filter_users_to_current_workspace_scope.
                if _own_org_esids is not None:
                    orgs = [o for o in orgs if o.get("_id") in _own_org_esids]
                # Teams — follow-up (2026-05-29): filter on the
                # team's OWN ``workspace_esid``, not via an org-join.
                #
                # The previous code joined teams→orgs by reading
                # ``organization_esid`` out of the team ``_source`` and
                # intersecting with the in-scope org ESIDs. That join was
                # the live-firing leak: on staging the foreign "Kavanah
                # Team" (workspace kavanah-media) carries a STALE
                # ``organization_esid`` in its ``_source`` that happens to
                # equal fb_app_review's own org ESID, so the Python-side
                # ``_source`` join kept it — even though a real ``term``
                # query on that field returns no match (``_source`` vs the
                # indexed/queryable value diverge: the mapping-drift class
                # ). Reading ``_source`` trusts the displayed
                # value and is fooled.
                #
                # The team's own ``workspace_esid`` (backfill, same
                # field the boundary checks use) is the
                # authoritative tenant key and does not require the org
                # round-trip. A team with no ``workspace_esid`` is legacy /
                # un-backfilled → dropped (fail-closed), matching
                # ``filter_hits_by_workspace`` and the orgs filter above.
                teams = [t for t in teams if _esid_values(t.get("workspace_esid")) & _allowed_set]
            except Exception as _e3:
                logging.warning(
                    "follow-up: dropdown filter failed for %s: %s",
                    session.get("username", "unknown"),
                    _e3,
                )
                # Fail-closed: empty dropdowns. The route-level gates
                # still defend against any cross-workspace POST.
                workspaces, orgs, teams = [], [], []
    existing_roles = RoleManagementService.get_all_role_names()
    system_roles = sorted(r["role_name"] for r in RolePermissionsService.get_roles_for_listing() if r["role_type"] == "system")
    # Don't show system roles in the org/team list — they belong only in the System group
    existing_roles = {r for r in existing_roles if r not in set(system_roles)}

    # Synthesize a per-user ``memberships`` list from the new authz indices.
    # The template renders membership chips and the Edit Roles dialog
    # pre-selects from this list — the legacy ES Security ``user.roles``
    # no longer carries ``org_*``/``team_*``.
    membership_index = _load_authz_memberships_index()
    for u in users:
        u["memberships"] = membership_index.get(u["username"], [])

    is_superuser = "superuser" in session.get("roles", [])
    # ``superuser`` and ``asset_admin`` are dead UI in the role
    # pickers for EVERY viewer — superuser is grantable only from Kibana
    # and asset_admin was a permanently disabled checkbox — so both are
    # stripped server-side unconditionally; the checkboxes never reach the
    # rendered DOM. Role BADGES for existing holders are unaffected (they
    # render from user.roles, not these lists).
    system_roles = [r for r in system_roles if r not in _PICKER_HIDDEN_SYSTEM_ROLES]
    existing_roles = {r for r in existing_roles if r not in _PICKER_HIDDEN_SYSTEM_ROLES}
    # ``data_explorer`` (a custom ES role surfaced under "Other") stays
    # visible to superusers — the picker is the only way to assign it —
    # but remains hidden from everyone else so the role never reaches the
    # page for non-superusers.
    if not is_superuser:
        system_roles = [r for r in system_roles if r != "data_explorer"]
        existing_roles = {r for r in existing_roles if r != "data_explorer"}

    # build the full workspace list for the superuser-only
    # filter dropdown. Superusers can switch to ANY workspace per the
    # account-recovery requirement — the dropdown is independent of
    # data-tier visibility narrowing (which is intentionally
    # strict). Non-superusers see no dropdown; the template gates on
    # is_superuser.
    _workspace_filter_options: list[dict] = []
    if _is_superuser_3:
        try:
            from lib.es_helpers import get_all_documents as _get_all_2

            _workspace_filter_options = sorted(
                (
                    {
                        "esid": d.get("_id"),
                        "slug": (d.get("_source") or {}).get("workspace_slug") or d.get("_id"),
                        "title": (d.get("_source") or {}).get("workspace_title")
                        or (d.get("_source") or {}).get("workspace_slug")
                        or d.get("_id"),
                    }
                    for d in _get_all_2("de_asset_workspace")
                ),
                key=lambda w: (w.get("title") or "").lower(),
            )
        except Exception as _e_2:
            logging.warning("workspace filter options load failed: %s", _e_2)
            _workspace_filter_options = []

    # drive the Delete Workspace button's initial state from the
    # same membership-count helper the delete endpoint re-checks. Title
    # is rendered into the confirm() prompt so the operator sees the
    # workspace name, not the opaque esid. None on either => button
    # disabled.
    _workspace_filter_title: str | None = None
    _workspace_filter_membership_count: int | None = None
    if _is_superuser_3 and _workspace_filter:
        for _w in _workspace_filter_options:
            if _w.get("esid") == _workspace_filter:
                _workspace_filter_title = _w.get("title")
                break
        try:
            from lib.es_helpers import get_elasticsearch_client as _get_es_8
            from asset_management.web.services.permissions_index_service import (
                count_workspace_memberships as _count_2,
            )

            _es_7 = _get_es_8()
            if _es_7 is not None:
                _count_val = _count_2(_es_7, _workspace_filter)
                if _count_val >= 0:
                    _workspace_filter_membership_count = _count_val
        except Exception as _e_3:
            logging.warning("membership count for filter button failed: %s", _e_3)

    # Login Activity's "active" pill anchors on the most recent
    # login event, not on per-request activity (no heartbeat is audited).
    # Expose the idle window so the label reads "logged in last Nh/m"
    # instead of the misleading "active now".
    session_idle_minutes = max(1, config.SESSION_LIFETIME // 60)

    # drive the sidebar "Reset password" button from the SAME
    # verdict the reset-password endpoint enforces, so the button never
    # appears when the POST would 403. The endpoint gates on
    # _require_permission(WORKSPACE_DELETE) — cluster admins / superusers
    # bypass; otherwise the operator must hold WORKSPACE_DELETE anywhere
    # (owner-only). The MFA-bypass / set-password controls stay
    # IS_SUPERUSER-gated in the template; this flag is reset-password only.
    # FAIL CLOSED: any resolver error hides the button (the endpoint still
    # defends server-side regardless).
    can_reset_password = is_superuser
    if not can_reset_password:
        try:
            from asset_management.web.services.permission_resolver import PermissionResolver
            from asset_management.web.services.permissions_enum import Permission

            _operator = session.get("username", "")
            _is_cluster_admin = "asset_admin" in session.get("roles", [])
            can_reset_password = _is_cluster_admin or PermissionResolver.has_permission(
                _operator,
                Permission.WORKSPACE_DELETE,
                is_super_admin=_is_cluster_admin,
            )
        except Exception as _e:  # noqa: BLE001 — UI gate is best-effort; endpoint enforces
            logging.warning(
                "reset-password capability resolve failed for %s; hiding button: %s",
                session.get("username", "unknown"),
                _e,
            )
            can_reset_password = False

    return render_template(
        "user_management.html",
        users=users,
        orgs=orgs,
        teams=teams,
        workspaces=workspaces,
        existing_roles=existing_roles,
        system_roles=system_roles,
        is_superuser=is_superuser,
        # single-org installations don't need the org/team tier UI
        # in the Edit Roles modal; there is no agency-style boundary to
        # express. ``orgs`` is already narrowed to the operator's allowed
        # workspaces above, so its length IS the operator-visible org count.
        # The three-tier data model is untouched — this only drives the UI.
        is_multi_org_workspace=len(orgs) > 1,
        session_idle_minutes=session_idle_minutes,
        # superuser-only workspace filter dropdown state.
        # ``workspace_filter_esid`` is "" for "All workspaces" or a
        # specific workspace_esid otherwise. None for non-superusers.
        workspace_filter_esid=_workspace_filter,
        workspace_filter_options=_workspace_filter_options,
        # drive Delete Workspace button initial state.
        workspace_filter_title=_workspace_filter_title,
        workspace_filter_membership_count=_workspace_filter_membership_count,
        # gate the Registrations tab on REGISTRATION_ENABLED.
        # When the flag is off (prod default today) the tab + its
        # approve/reject modals are not rendered. The underlying
        # RegistrationService + /api/admin/registrations endpoints stay
        # live regardless — the flag controls UI visibility only.
        registration_enabled=config.REGISTRATION_ENABLED,
        # follow-up: Create User is workspace-owner/superuser only.
        # Org-tier operators (org owner/admin) can't create users for now, so
        # the button is hidden for them (the create-user API still has its own
        # _require_admin gate independently). True for privileged operators.
        can_create_user=can_create_user,
        # hidden docs-capture workspace anchor (see the template).
        docs_workspace=_docs_workspace,
        # gate the sidebar "Reset password" button (workspace
        # owners + cluster admins; the endpoint re-checks server-side).
        can_reset_password=can_reset_password,
        # canonical role → friendly-name map, serialized into JS
        # so the pill trays / detail chips label roles the same way the
        # Jinja ``role_display`` filter does.
        role_display_names=ROLE_DISPLAY_NAMES,
    )


# api_sync_roles, api_create_role, api_role_sync_status removed.
# These endpoints created and listed legacy org_*/team_* ES Security roles;
# retired the legacy role system in favor of the new de_authz_*
# model where scope membership lives in de_authz_user_team_role /
# de_authz_user_org_role keyed by username, not by ES Security role names.


@app.route("/api/user-management/enable-mfa", methods=["POST"])
def api_enable_mfa():
    """Admin: enable MFA for a user. The user picks their method on next login."""
    _require_admin()
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"success": False, "error": "Username required"}), 400
    _require_target_in_workspace_scope(username)
    result = MFAService.admin_enable_mfa(username, enabled_by=session.get("username", "unknown"))
    return jsonify(result)


@app.route("/api/user-management/disable-mfa", methods=["POST"])
def api_disable_mfa():
    """Superuser only: disable (reset) MFA for a user.

     Tier 3: an admin who has their OWN second factor configured must
    re-confirm it (``mfa_code`` in the body) before resetting another user's —
    so a hijacked admin session can't strip a victim's MFA without also holding
    the admin's authenticator. An admin with no confirmed factor of their own
    can't re-confirm, so the existing superuser gate stands alone for them (we
    must not deadlock a no-MFA superuser out of the reset capability).
    """
    _require_superuser()
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"success": False, "error": "Username required"}), 400

    actor = session.get("username", "")
    if MFAService.has_any_factor(actor):
        mfa_code = (data.get("mfa_code") or "").strip()
        if not mfa_code:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "mfa_required",
                        "message": "Confirm your own two-factor code to reset another user's MFA.",
                    }
                ),
                400,
            )
        ok = MFAService.verify_active_factor(
            actor,
            mfa_code,
            yubico_client_id=config.YUBICO_CLIENT_ID,
            yubico_secret_key=config.YUBICO_SECRET_KEY,
            allow_insecure_dev_mode=config.MFA_ALLOW_INSECURE_DEV,
        )
        if not ok:
            logging.warning(
                f"AUDIT: admin={actor} action=admin_disable_mfa target={username} success=False reason=self_mfa_reconfirm_failed"
            )
            AuditLogService.log_event(
                actor,
                "admin_disable_mfa",
                False,
                target_user=username,
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
                failure_reason="self_mfa_reconfirm_failed",
            )
            return (
                jsonify({"success": False, "error": "invalid_mfa_code", "message": "Your two-factor code was incorrect."}),
                403,
            )

    result = MFAService.admin_disable_mfa(username)
    if result.get("success"):
        AuditLogService.log_event(
            actor,
            "admin_disable_mfa",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )
    return jsonify(result)


@app.route("/api/user-management/set-mfa-bypass", methods=["POST"])
def api_set_mfa_bypass():
    """Superuser only: break-glass MFA bypass for a locked-out user.

    POST {"username": "alice", "enabled": true, "reason": "lost yubikey"} turns
    the bypass on; `enabled: false` turns it off.
    """
    _require_superuser()
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    enabled = data.get("enabled")
    reason = (data.get("reason") or "").strip()
    if not username:
        return jsonify({"success": False, "error": "Username required"}), 400
    if not isinstance(enabled, bool):
        return jsonify({"success": False, "error": "enabled must be a boolean"}), 400
    if enabled and not reason:
        return jsonify({"success": False, "error": "Reason required when enabling bypass"}), 400
    actor = session.get("username", "unknown")
    result = MFAService.set_mfa_bypass(
        username=username,
        enabled=enabled,
        set_by=actor,
        reason=reason,
    )
    if result.get("success"):
        AuditLogService.log_event(
            actor,
            "mfa_bypass_enable" if enabled else "mfa_bypass_disable",
            True,
            target_user=username,
            detail=reason if enabled else "",
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )
    return jsonify(result)


_AUTHZ_TIER_BY_PREFIX = {
    "workspace_": {
        "applies_to": "workspace",
        "scope_index": "de_asset_workspace",
        # workspace_slug is mapped as ``type: keyword`` at the top
        # level (permissions_index_service.WORKSPACE_MAPPING), so the
        # term query targets the field directly — there is no
        # ``.keyword`` sub-field. Matches how F6's WorkspaceSlugService
        # probes the same field.
        "scope_field": "workspace_slug",
        "scope_query": "term",
        "scope_esid_field": "workspace_esid",
        "membership_index": "de_authz_workspace_membership",
        "assignment_index": "de_authz_user_workspace_role",
        # emit workspace_viewer (new canonical). workspace_member is
        # still a seeded, accepted legacy alias for existing docs / older
        # clients, but new bare-form (workspace_<slug>, no explicit role)
        # submissions now write the renamed role.
        "default_role": "workspace_viewer",
    },
    "org_": {
        "applies_to": "org",
        "scope_index": "de_asset_organization",
        "scope_field": "organization_title",
        "scope_query": "match",
        "scope_esid_field": "org_esid",
        "membership_index": "de_authz_org_membership",
        "assignment_index": "de_authz_user_org_role",
        # was "viewer" but viewer applies_to "team", not
        # "org". The legacy bare-form submission (org_<title>, no
        # explicit role) was hitting _validate_role_for_tier and 400ing
        # the user-create POST. New seeded "org_viewer" role is the
        # equivalent of team-tier viewer at the org tier.
        "default_role": "org_viewer",
    },
    "team_": {
        "applies_to": "team",
        "scope_index": "de_asset_teams",
        "scope_field": "team_title",
        "scope_query": "match",
        "scope_esid_field": "team_esid",
        "membership_index": "de_authz_team_membership",
        "assignment_index": "de_authz_user_team_role",
        "default_role": "viewer",
    },
}


def _parse_membership_string(membership: str) -> tuple[str, str, str, bool] | None:
    """Split ``<prefix>_<scope_id>[:<role_esid>]`` into
    (prefix, scope_id, role_esid, explicit). Returns None when the
    prefix is unrecognised.

    ``role_esid`` is the tier's default when no suffix is present, so
     Phase 14 callers (membership strings without a role) keep
    working unchanged. ``explicit`` is True iff the caller supplied
    the suffix — callers that did not opt in to the role-aware shape
    skip tier-validation at the route boundary, preserving back-compat
    with the legacy ``viewer``-on-org-tier default that predates
     role tiering.
    """
    if not isinstance(membership, str):
        return None
    base, sep, role_esid = membership.partition(":")
    for prefix, tier in _AUTHZ_TIER_BY_PREFIX.items():
        if base.startswith(prefix):
            scope_id = base[len(prefix) :]
            return (
                prefix,
                scope_id,
                role_esid if sep else tier["default_role"],
                bool(sep),
            )
    return None


def _validate_role_for_tier(role_esid: str, applies_to: str) -> bool:
    """Check that ``role_esid`` is a seeded role for ``applies_to``.
    Lazy-import the seeder so this module loads when authz is unavailable.
    """
    try:
        from asset_management.web.services.permissions_seeder_service import (
            DEFAULT_ROLES,
        )
    except Exception:
        # If the seeder can't load, accept whatever the caller passed —
        # tests assert on validation behaviour directly with the seeder
        # available, and prod always has it.
        return True
    role_def = DEFAULT_ROLES.get(role_esid)
    if not role_def:
        return False
    return role_def.get("applies_to") == applies_to


def _provision_new_authz_assignments(username: str, roles: list) -> None:
    """Phase 14 / dual-write a user's role memberships
    to the new authz model on user creation.

    Membership wire format: ``<prefix>_<scope_id>[:<role_esid>]`` where
    prefix is ``workspace_`` / ``org_`` / ``team_``. ``scope_id`` is the
    workspace_slug (workspace) or the asset title (org/team). The
    optional ``:role_esid`` suffix selects the role; absent suffix
    falls back to the tier's default (``workspace_viewer`` for
    workspace — renamed from workspace_member; ``viewer`` for
    org/team — back-compat with earlier callers).

    Doc IDs are deterministic (``<username>:<scope_esid>`` for
    membership, ``<username>:<scope_esid>:<role_esid>`` for assignment)
    so re-runs are no-ops via ``op_type=create`` 409s.

    Best-effort. Logs and continues on any per-row failure; does not
    raise. Mismatched scope/role combinations are dropped with a
    warning — the route-level validator in ``api_create_user`` is the
    enforcement point for client-visible 400s.
    """
    if not roles:
        return
    from datetime import datetime, timezone
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        logging.warning("ES unavailable; skipping new-authz dual-write for %s", username)
        return

    for role_name in roles:
        parsed = _parse_membership_string(role_name)
        if parsed is None:
            # System role (asset_admin, custom roles) — no scope assignment.
            continue
        prefix, scope_id, role_esid, explicit = parsed
        tier = _AUTHZ_TIER_BY_PREFIX[prefix]

        # Only enforce tier-validation when the caller explicitly
        # opted in to the role-aware shape. The earlier default
        # (``viewer`` on org/team tiers) predates the applies_to
        # tagging and is grandfathered.
        if explicit and not _validate_role_for_tier(role_esid, tier["applies_to"]):
            logging.warning(
                "role %r is not valid for tier %r on %s; skipping %s",
                role_esid,
                tier["applies_to"],
                username,
                role_name,
            )
            continue

        # Resolve scope id -> ESID. Workspace uses a term query against
        # the keyword field (slug is a stable identifier earlier);
        # org/team keep the match-on-title path for back-compat.
        scope_query = (
            {"term": {tier["scope_field"]: scope_id}}
            if tier["scope_query"] == "term"
            else {"match": {tier["scope_field"]: scope_id}}
        )
        try:
            hit = es.search(
                index=tier["scope_index"],
                body={"query": scope_query, "size": 1},
            )
            hits = hit.get("hits", {}).get("hits", [])
            if not hits:
                logging.warning(
                    "scope %r (%s) not found in %s for %s",
                    scope_id,
                    role_name,
                    tier["scope_index"],
                    username,
                )
                continue
            scope_esid = hits[0]["_id"]
        except Exception as e:
            logging.warning("scope lookup failed for %s on %s: %s", username, role_name, e)
            continue

        assignment_doc = {
            "username": username,
            tier["scope_esid_field"]: scope_esid,
            "role_esid": role_esid,
            # role doc carries is_primary (the retired membership
            # doc's old signal). Admin/create-user grants are non-primary.
            "is_primary": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        # (P3b): membership docs are retired — only the role doc is
        # written. Readers resolve "belongs to" from the role index.
        try:
            es.index(
                index=tier["assignment_index"],
                id=f"{username}:{scope_esid}:{role_esid}",
                document=assignment_doc,
                op_type="create",
            )
        except Exception as e:
            if "version_conflict" not in str(e).lower():
                logging.warning("assignment write failed for %s/%s: %s", username, role_name, e)


def _sync_new_authz_memberships(*, username: str, desired: list) -> None:
    """Reconcile a user's authz memberships against the ``desired`` list
    (``org_<title>`` / ``team_<title>`` strings).

    Reads the user's current memberships from
    ``de_authz_user_team_role`` / ``de_authz_user_org_role`` directly,
    diffs against ``desired``, then routes adds through
    ``_provision_new_authz_assignments`` and deletes the matching
    ``<username>:<scope_esid>:viewer`` doc on each remove.

     (P3b): membership docs are retired, so there is no parallel
    membership doc to keep/delete — the role doc is the only record.

    Replaces the old ``_sync_new_authz_assignments`` helper that diffed
    two ES Security role lists. After v3, ES Security no longer
    carries ``org_*``/``team_*`` strings, so the diff has to come from
    the authz indices directly. Best-effort throughout.
    """
    if not isinstance(desired, list):
        return

    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        logging.warning("authz-membership-sync: ES unavailable for %s", username)
        return

    team_titles: dict[str, str] = {}
    org_titles: dict[str, str] = {}
    try:
        for hit in (
            es.search(index="de_asset_teams", body={"query": {"match_all": {}}, "size": 1000}).get("hits", {}).get("hits", [])
        ):
            src = hit.get("_source", {}) or {}
            t = src.get("team_title")
            if t:
                team_titles[hit["_id"]] = t
    except Exception as e:
        logging.warning("authz-membership-sync: team title load failed for %s: %s", username, e)
    try:
        for hit in (
            es.search(index="de_asset_organization", body={"query": {"match_all": {}}, "size": 1000})
            .get("hits", {})
            .get("hits", [])
        ):
            src = hit.get("_source", {}) or {}
            t = src.get("organization_title")
            if t:
                org_titles[hit["_id"]] = t
    except Exception as e:
        logging.warning("authz-membership-sync: org title load failed for %s: %s", username, e)

    current: set[str] = set()
    current_team_esids: dict[str, str] = {}
    current_org_esids: dict[str, str] = {}
    try:
        for hit in (
            es.search(index="de_authz_user_team_role", body={"query": {"term": {"username": username}}, "size": 200})
            .get("hits", {})
            .get("hits", [])
        ):
            src = hit.get("_source", {}) or {}
            team_esid = src.get("team_esid")
            if not team_esid or team_esid == "__all__":
                continue
            title = team_titles.get(team_esid)
            if not title:
                continue
            key = f"team_{title}"
            current.add(key)
            current_team_esids[key] = team_esid
    except Exception as e:
        logging.warning("authz-membership-sync: team-role load failed for %s: %s", username, e)
    try:
        for hit in (
            es.search(index="de_authz_user_org_role", body={"query": {"term": {"username": username}}, "size": 200})
            .get("hits", {})
            .get("hits", [])
        ):
            src = hit.get("_source", {}) or {}
            org_esid = src.get("org_esid")
            if not org_esid:
                continue
            title = org_titles.get(org_esid)
            if not title:
                continue
            key = f"org_{title}"
            current.add(key)
            current_org_esids[key] = org_esid
    except Exception as e:
        logging.warning("authz-membership-sync: org-role load failed for %s: %s", username, e)

    desired_set = {r for r in desired if isinstance(r, str) and (r.startswith("org_") or r.startswith("team_"))}
    added = sorted(desired_set - current)
    removed = sorted(current - desired_set)

    if added:
        _provision_new_authz_assignments(username, added)

    if not removed:
        return

    for membership in removed:
        if membership.startswith("team_"):
            assignment_index = "de_authz_user_team_role"
            scope_esid = current_team_esids.get(membership)
        elif membership.startswith("org_"):
            assignment_index = "de_authz_user_org_role"
            scope_esid = current_org_esids.get(membership)
        else:
            continue
        if not scope_esid:
            continue
        try:
            es.delete(index=assignment_index, id=f"{username}:{scope_esid}:viewer")
        except Exception as e:
            if "not_found" not in str(e).lower() and "404" not in str(e):
                logging.warning("authz-membership-sync: assignment delete failed for %s/%s: %s", username, membership, e)


@app.route("/api/user-management/create-user", methods=["POST"])
def api_create_user():
    """Admin: create a new Elasticsearch user with a temporary password and send a welcome email.

    Wire format: callers pass ``system_roles`` and
    ``memberships`` separately. ``system_roles`` writes to ES Security; the
    ``memberships`` list (``org_<title>`` / ``team_<title>`` strings) writes
    only to the new authz indices via ``_provision_new_authz_assignments``.
    The two stores never carry duplicate state.
    """
    # owner-only capability gate (replaces the any-scope
    # _require_admin). The per-workspace boundary is enforced by the
    # membership/scope checks below.
    _require_permission(_USER_MGMT_GATE_PERMISSION)
    data = request.get_json() or {}

    username = data.get("username", "").strip()
    full_name = data.get("full_name", "").strip()
    email = data.get("email", "").strip()
    system_roles = data.get("system_roles", [])
    memberships = data.get("memberships", [])
    skip_email = bool(data.get("skip_email", False))

    # skip_email mode: superuser-only, creates user without email and returns temp password
    if skip_email:
        caller_roles = session.get("roles", [])
        if "superuser" not in caller_roles:
            return jsonify({"success": False, "error": "Only superusers can create users without email"}), 403

    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
    if not skip_email and not email:
        return jsonify({"success": False, "error": "Email address is required"}), 400
    if not isinstance(system_roles, list):
        return jsonify({"success": False, "error": "system_roles must be a list"}), 400
    if not isinstance(memberships, list) or not memberships:
        return jsonify({"success": False, "error": "Select at least one org or team membership."}), 400

    # validate membership strings carrying an explicit :role
    # suffix. Mismatched scope/role combinations (e.g. org_admin on a
    # workspace row) are rejected here so the client sees a 400 rather
    # than a silent skip in _provision_new_authz_assignments. Legacy
    # callers passing bare ``org_<title>`` / ``team_<title>`` strings
    # without a role suffix are untouched — the dispatcher uses the
    # tier default and skips validation (back-compat with
    # Phase 14).
    caller_username = session.get("username", "")
    for membership in memberships:
        parsed = _parse_membership_string(membership)
        if parsed is None:
            return (
                jsonify({"success": False, "error": f"Unrecognised membership prefix: {membership}"}),
                400,
            )
        prefix, _scope_id, role_esid, explicit = parsed
        tier = _AUTHZ_TIER_BY_PREFIX[prefix]
        if explicit and not _validate_role_for_tier(role_esid, tier["applies_to"]):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": (
                            f"Role {role_esid!r} is not valid for {tier['applies_to']} scope. "
                            f"Use system_roles[] to grant cluster roles."
                        ),
                    }
                ),
                400,
            )
        # superusers cannot grant themselves scoped roles
        # through this surface — the cluster-level superuser permission
        # is the escape hatch for self-management; scoped roles must be
        # granted to other users. Goal is data separation. Only applies
        # to explicit role grants (legacy bare-membership writes default
        # to viewer and aren't escalation surface).
        if explicit and username == caller_username:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": (
                            "Cannot grant a scoped role to yourself. "
                            "Use system_roles to grant cluster roles to your own account."
                        ),
                    }
                ),
                400,
            )

    # non-agency workspace users must be assigned to at least
    # one org or team at creation. A workspace-only membership lands a
    # user in a workspace they can navigate but with no orgs in scope —
    # which means RoleFilterService.get_allowed_organizations() returns
    # empty for them, and the team-create form has nothing to default
    # into the Organization picker. The downstream symptom is the
    # "Organization is required" failure on /asset/teams/add.
    #
    # Agency workspaces are exempt because they explicitly model the
    # "user belongs to a workspace and acquires orgs over time"
    # workflow — a fresh agency employee with no orgs is a valid state.
    # Solo / non-agency workspaces don't have that lifecycle: the user
    # is supposed to manage exactly the org(s) they were created with.
    #
    # Runs AFTER the role-validation loop so malformed inputs
    # (mismatched role/tier, self-grant) surface the precise
    # error rather than this business-rule one.
    has_scope_below_workspace = any(
        isinstance(m, str) and (m.startswith("org_") or m.startswith("team_")) for m in memberships
    )
    if not has_scope_below_workspace:
        # Every membership is a workspace_* row. Resolve each to its
        # workspace doc and read is_agency. Strictest workspace wins
        # — if ANY referenced workspace is non-agency, require an org.
        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService as _wms_cu,
        )

        all_agency = True
        for _m in memberships:
            if not isinstance(_m, str) or not _m.startswith("workspace_"):
                continue
            _parsed = _parse_membership_string(_m)
            if _parsed is None:
                continue
            _ws_slug = _parsed[1]
            # Slug == _id earlier, same convention
            # _require_memberships_in_workspace_scope uses.
            if not _wms_cu.is_agency_for_workspace(_ws_slug):
                all_agency = False
                break
        if not all_agency:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": (
                            "Non-agency workspace users must be assigned to at " "least one organization or team at creation."
                        ),
                    }
                ),
                400,
            )

    # non-superusers must belong to at most one workspace.
    # Block the create when the requested memberships span multiple
    # workspaces (the inferred-workspace leg in
    # ``RoleFilterService.get_allowed_workspaces`` walks org/team roles
    # up to their parent workspace, so a cross-workspace org grant
    # silently puts the user in two workspaces — root cause of the
    # 2026-05-20 user_management LEAK).
    #
    # Runs AFTER the role-validation loop agency
    # check so structural input errors surface their precise messages
    # first. Target-superuser exemption is handled inside the helper.
    _validate_single_workspace_for_non_superuser(username, memberships)

    # Defensive: drop any org_/team_/workspace_ strings that snuck into
    # system_roles (e.g. an older client). Memberships go to the authz
    # indices only.
    es_security_roles = [
        r
        for r in system_roles
        if isinstance(r, str) and not r.startswith("org_") and not r.startswith("team_") and not r.startswith("workspace_")
    ]

    # Block non-superusers from granting superuser-only cluster roles
    # (asset_admin / data_explorer / superuser). The role pickers hide these,
    # but this endpoint accepts arbitrary system_roles[] — enforce the
    # boundary server side too.
    _superuser_only_err = _superuser_only_role_grant_error(es_security_roles)
    if _superuser_only_err is not None:
        return jsonify({"success": False, "error": _superuser_only_err}), 403

    # gate on the requested memberships before the ES Security
    # user is provisioned. The new user inherits the operator's
    # workspace scope; if any requested membership is outside that
    # scope, refuse before any side effects land.
    _require_memberships_in_workspace_scope(memberships)

    result = UserProvisioningService.create_user(username, full_name, email, es_security_roles)
    if result["success"]:
        # Write scope memberships only to the new authz model. The ES Security
        # record above carries system roles only v3.
        try:
            _provision_new_authz_assignments(username, memberships)
        except Exception as _new_authz_err:
            logging.warning(
                "new-authz assignment write failed for %s: %s",
                username,
                _new_authz_err,
            )

    if not result["success"]:
        # surface duplicate-username collisions as 409 with a
        # ``code`` and ``suggested_username`` the modal can offer to the admin.
        if result.get("code") == "username_taken":
            return (
                jsonify(
                    {
                        "success": False,
                        "code": "username_taken",
                        "error": result["error"],
                        "suggested_username": result.get("suggested_username", ""),
                    }
                ),
                409,
            )
        # duplicate email surfaces the same 409 family as a duplicate
        # username. Admin posture: name the colliding account so the operator
        # can find it (this product is not security-sensitive — same call the
        # signup path made).
        if result.get("code") == "email_taken":
            return (
                jsonify(
                    {
                        "success": False,
                        "code": "email_taken",
                        "error": result["error"],
                        "existing_username": result.get("existing_username", ""),
                    }
                ),
                409,
            )
        return jsonify({"success": False, "error": result["error"]}), 500

    # Every account is 2FA-enabled by default. Write a Pending MFA
    # record so the Users list renders "Pending" until the user picks a method
    # on first login. A failure here is logged but does not fail the whole
    # creation — the user was created and ENFORCE_MFA will still force enrollment
    # on first login even with no MFA record.
    mfa_default = MFAService.admin_enable_mfa(username, enabled_by=session.get("username", "system"))
    if not mfa_default.get("success"):
        logging.warning(
            "failed to set default MFA-enabled for new user %s: %s",
            username,
            mfa_default.get("error"),
        )

    # When skip_email is set, return the temp password directly instead of emailing
    if skip_email:
        logging.info(
            f"AUDIT: admin={session.get('username')} action=create_user target={username} "
            f"skip_email=true ip={request.remote_addr}"
        )
        AuditLogService.log_event(
            session.get("username", "unknown"),
            "create_user",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"system_roles={es_security_roles}, memberships={memberships}, skip_email=true",
        )
        return jsonify(
            {
                "success": True,
                "email_sent": False,
                "skip_email": True,
                "temp_password": result["temp_password"],
            }
        )

    email_result = UserProvisioningService.send_welcome_email(email, username, result["temp_password"], full_name=full_name)

    logging.info(
        f"AUDIT: admin={session.get('username')} action=create_user target={username} "
        f"email_sent={email_result['success']} ip={request.remote_addr}"
    )
    AuditLogService.log_event(
        session.get("username", "unknown"),
        "create_user",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"system_roles={es_security_roles}, memberships={memberships}, email_sent={email_result['success']}",
    )

    return jsonify(
        {
            "success": True,
            "email_sent": email_result["success"],
            "email_error": email_result.get("error"),
        }
    )


@app.route("/api/user-management/reset-password", methods=["POST"])
def api_reset_password():
    """Reset a user's password to a new temp and email it.

    POST {"username": "alice", "skip_email": false} — default path emails the
    temp password. With skip_email=true the temp password is returned in the
    response for the operator to hand off manually.

    this is no longer superuser-only. A workspace owner may reset
    the password of a user inside their own workspace scope — the same
    owner-only-capability + per-target-scope pair the destructive
    ``/api/user-management/*`` routes already enforce:

    * ``_require_permission(WORKSPACE_DELETE)`` is the capability gate
      (owner-only; cluster admins / superusers short-circuit through).
    * ``_require_target_in_workspace_scope`` confines the operator to their
      own workspace boundary so a workspace-A owner cannot reset a
      workspace-B user's password (server-enforced, not just UI-hidden).

    2FA bypass / disable-MFA stays superuser-only and is unaffected
    (``api_set_mfa_bypass`` / ``api_disable_mfa`` keep ``_require_superuser``).
    """
    _require_permission(_USER_MGMT_GATE_PERMISSION)
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    skip_email = bool(data.get("skip_email", False))
    if not username:
        return jsonify({"success": False, "error": "Username required"}), 400
    # / SEC-SSRF-PWRESET-001: reject path-traversal-shaped or otherwise
    # malformed usernames before they reach es.security.get_user(). Uniform
    # error shape avoids enumeration via timing or backtrace differences.
    import re as _re

    if not _re.match(r"^[A-Za-z0-9._-]+$", username) or len(username) > 64:
        return jsonify({"success": False, "error": "Invalid username"}), 400
    if username == session.get("username"):
        return jsonify({"success": False, "error": "Use Change Password to reset your own password"}), 400
    # scope gate — a workspace owner may only reset a password for
    # a user inside their own workspace. Superusers bypass (see helper).
    # Runs after the username-shape validation so the malformed-input
    # rejection above keeps its uniform error shape.
    _require_target_in_workspace_scope(username)

    result = UserProvisioningService.reset_password(username)
    if not result["success"]:
        return jsonify({"success": False, "error": result["error"]}), 500

    actor = session.get("username", "unknown")
    temp_password = result["temp_password"]
    email = result.get("email", "")
    full_name = result.get("full_name", "")

    if skip_email or not email:
        AuditLogService.log_event(
            actor,
            "reset_password",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail="skip_email=true" if skip_email else "no_email_on_file",
        )
        return jsonify(
            {
                "success": True,
                "email_sent": False,
                "skip_email": True,
                "temp_password": temp_password,
            }
        )

    email_result = UserProvisioningService.send_password_reset_email(email, username, temp_password, full_name=full_name)
    AuditLogService.log_event(
        actor,
        "reset_password",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"email_sent={email_result['success']}",
    )
    return jsonify(
        {
            "success": True,
            "email_sent": email_result["success"],
            "email_error": email_result.get("error"),
        }
    )


# ============================================
# TESTING ROUTES (admin only)
# ============================================


# cascade-delete authz docs when a user is deleted. The helper
# lives in permissions_index_service alongside the index name constants
# so api_delete_user (legacy single-delete) and UserManagementService.bulk_action
# (the UI path) share the same fan-out and can't drift out of sync.
from asset_management.web.services.permissions_index_service import (
    cascade_purge_authz_for_user as _cascade_purge_authz_for_user,
    # empty-workspace delete from User Management.
    cascade_purge_empty_workspace as _cascade_purge_empty_workspace,
    count_workspace_memberships as _count_workspace_memberships,
)


@app.route("/api/user-management/delete-user", methods=["POST"])
def api_delete_user():
    """Admin: permanently delete an Elasticsearch user."""
    # owner-only capability gate (replaces _require_admin);
    # _require_target_in_workspace_scope below enforces the per-workspace boundary.
    _require_permission(_USER_MGMT_GATE_PERMISSION)
    data = request.get_json() or {}
    username = data.get("username", "").strip()

    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400

    # Prevent self-deletion
    if username == session.get("username"):
        return jsonify({"success": False, "error": "You cannot delete your own account"}), 400

    # Prevent deletion of the built-in elastic superuser
    if username == "elastic":
        return jsonify({"success": False, "error": "The built-in 'elastic' user cannot be deleted"}), 400

    _require_target_in_workspace_scope(username)

    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 500

    # Prevent deletion of any user with the superuser role
    try:
        target = dict(es.security.get_user(username=username)).get(username, {})
        if "superuser" in (target.get("roles") or []):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Superuser accounts cannot be deleted from the UI — use Elasticsearch directly",
                    }
                ),
                403,
            )
    except Exception:
        pass  # If we can't fetch the user, let the delete attempt surface its own error

    # 2026-06-05 — cancel any Stripe subscription BEFORE deleting the
    # user (the cancel reads the user's stamped stripe_customer_id, which is
    # gone after the delete). Best-effort: a Stripe failure is logged + audited
    # as an orphaned sub but does NOT block the deletion.
    try:
        from asset_management.web.services.subscription_state_service import (
            SubscriptionStateService,
        )

        _cancel_res = SubscriptionStateService.cancel_stripe_for_user(username)
        if _cancel_res.get("mode") == "error":
            AuditLogService.log_event(
                session.get("username", "unknown"),
                "billing_cancel_on_delete_failed",
                False,
                target_user=username,
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
                detail=f"ORPHANED sub; customer={_cancel_res.get('customer_id')} err={_cancel_res.get('detail')}",
            )
    except Exception as _e:  # noqa: BLE001 — never block the delete
        logging.warning("cancel-on-delete wrapper failed for %r: %s", username, _e)

    try:
        get_es_admin_client(es).security.delete_user(username=username)
        # cascade-purge the 7 authz indices after the ES Security
        # delete succeeds. Best-effort — a failure here is logged but
        # doesn't roll back the ES Security delete (already irreversible
        # from this endpoint).
        cascade_counts = _cascade_purge_authz_for_user(es, username)
        cascade_summary = ",".join(f"{i}={n}" for i, n in cascade_counts.items())
        # follow-up — flush the account-state cache so the
        # deleted user's next request falls through to a fresh ES
        # check and triggers session.clear in verify_auth. :
        # this used to do its own ``from .auth import`` here, which
        # raises ImportError when gunicorn runs this file as the
        # top-level ``app`` module (prod and staging) — the except
        # swallowed it, so the flush never ran. The service helper
        # resolves the import root the app actually runs under.
        UserManagementService._flush_account_state_cache(username)
        logging.info(
            f"AUDIT: admin={session.get('username')} action=delete_user target={username} "
            f"ip={request.remote_addr} cascade={cascade_summary}"
        )
        AuditLogService.log_event(
            session.get("username", "unknown"),
            "delete_user",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"authz_cascade={cascade_summary}",
        )
        return jsonify({"success": True, "authz_cascade": cascade_counts})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to delete user",
            log_detail=f"api_delete_user: failed to delete '{username}'",
            exc=e,
        )


@app.route("/api/user-management/audit-log")
def api_audit_log():
    """Superuser: query the audit/login activity log.

    gated on superuser (was _require_admin). The Login
    Activity surface exposes cross-workspace login events; in the
    multi-tenant model only cluster operators (superuser) need it.
    Workspace admins managing their own tenant's users get the
    workspace-scoped users tab — the Login Activity tab is now hidden
    from the UI for them too.
    """
    _require_superuser()
    username = request.args.get("username", "").strip()
    action = request.args.get("action", "").strip()
    success_param = request.args.get("success", "").strip()
    size = min(int(request.args.get("size", "100")), 500)
    from_offset = max(int(request.args.get("from", "0")), 0)

    exclude_scanners = request.args.get("exclude_scanners", "").strip() == "true"
    active_only = request.args.get("active_only", "").strip() == "true"

    success: bool | None = None
    if success_param == "true":
        success = True
    elif success_param == "false":
        success = False

    username_in: list[str] | None = None
    if active_only:
        active = AuditLogService.active_usernames()
        username_in = list(active)

    result = AuditLogService.query_events(
        username=username,
        action=action,
        success=success,
        exclude_scanners=exclude_scanners,
        username_in=username_in,
        size=size,
        from_offset=from_offset,
    )

    return jsonify(result)


@app.route("/api/user-management/login-trend")
def api_login_trend():
    """Daily unique-user login counts for the last N days (default 30).

    superuser-only. See ``api_audit_log`` for rationale.
    """
    _require_superuser()
    try:
        days = max(1, min(int(request.args.get("days", "30")), 365))
    except ValueError:
        days = 30
    points = AuditLogService.daily_unique_logins(days=days)
    return jsonify({"points": points})


@app.route("/api/user-management/login-users")
def api_login_users():
    """Per-user login summary for a given UTC date (YYYY-MM-DD).

    superuser-only. See ``api_audit_log`` for rationale.
    """
    _require_superuser()
    date = request.args.get("date", "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    users = AuditLogService.logins_for_day(date=date)
    return jsonify({"date": date, "users": users, "count": len(users)})


@app.route("/api/user-management/active-users")
def api_active_users():
    """Usernames who are active right now.

    the authoritative signal is the per-request activity heartbeat
    (``de_user_presence`` via ``PresenceService.present_usernames``) — a real
    "acted within the window" timestamp, not the old login-age proxy. The
    window defaults to ``SESSION_LIFETIME`` (the idle window); the optional
    ``minutes`` query-param narrows / widens the ES search horizon (e.g. 5 for
    "clicked in the last 5 minutes").

    superuser-only. See ``api_audit_log`` for rationale.
    """
    _require_superuser()
    minutes_param = request.args.get("minutes", "").strip()
    minutes: int | None = None
    if minutes_param:
        try:
            minutes = max(1, min(int(minutes_param), 1440))
        except ValueError:
            minutes = None
    usernames = PresenceService.present_usernames(minutes=minutes)
    return jsonify({"usernames": sorted(usernames), "count": len(usernames)})


# ============================================
# USER MANAGEMENT — SUSPEND / REINSTATE / ROLES / DETAIL / BULK
# ============================================


@app.route("/api/user-management/suspend-user", methods=["POST"])
def api_suspend_user():
    """Admin: suspend a user (disable login)."""
    _require_permission(_USER_MGMT_GATE_PERMISSION)  # owner-only, like delete-user
    data = request.get_json() or {}
    username = data.get("username", "").strip()

    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
    if username == session.get("username"):
        return jsonify({"success": False, "error": "You cannot suspend your own account"}), 400
    if username == "elastic":
        return jsonify({"success": False, "error": "The built-in 'elastic' user cannot be suspended"}), 400

    _require_target_in_workspace_scope(username)

    result = UserManagementService.suspend_user(username)
    if result["success"]:
        _stamp_suspension_actor([username])
        AuditLogService.log_event(
            session.get("username", "unknown"),
            "suspend_user",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )
        return jsonify({"success": True})
    return jsonify(result), 400


@app.route("/api/user-management/reinstate-user", methods=["POST"])
def api_reinstate_user():
    """Admin: reinstate a suspended user."""
    _require_permission(_USER_MGMT_GATE_PERMISSION)  # owner-only, like delete-user
    data = request.get_json() or {}
    username = data.get("username", "").strip()

    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400

    _require_target_in_workspace_scope(username)
    _refuse_platform_suspension_reinstate(username)

    result = UserManagementService.reinstate_user(username)
    if result["success"]:
        AuditLogService.log_event(
            session.get("username", "unknown"),
            "reinstate_user",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )
        return jsonify({"success": True})
    return jsonify(result), 400


@app.route("/api/user-management/authz-memberships/<username>", methods=["GET"])
def api_get_authz_memberships(username: str):
    """Phase 15: read-only view of a user's new-authz memberships.

    Returns the (scope_type, scope_title, role_name) tuples currently
    in ``de_authz_user_team_role`` / ``de_authz_user_org_role`` for
    the given username, joined to ``de_asset_teams`` /
    ``de_asset_organization`` for human-readable titles and
    ``de_authz_role`` for the role name.

    Used by the Edit Roles modal as an at-a-glance read of what's
    in the new model — useful when an admin is troubleshooting why
    a user can/can't reach a scope and wants to compare the legacy
    role string to the new-model assignments.
    """
    _require_admin()
    if not username or not username.strip():
        return jsonify({"success": False, "error": "username required"}), 400
    _require_target_in_workspace_scope(username)

    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "ES unavailable"}), 503

    # ``workspace_assignments`` joins the existing team /
    # org payload so the legacy "Current memberships" panel and any
    # other read-only consumer can render the workspace tier. The
    # Scoped roles section uses the richer
    # ``/api/user-management/role-assignments/<username>`` endpoint
    # below.
    out = {
        "username": username,
        "team_assignments": [],
        "org_assignments": [],
        "workspace_assignments": [],
    }

    # follow-up: friendly labels for the system-admin sentinel
    # so admins viewing the memberships panel don't see the raw
    # `__all__` / `system_admin` strings.
    _SENTINEL_TEAM_LABEL = "All teams (system admin)"
    _FRIENDLY_ROLE_NAMES = {"system_admin": "System Admin"}

    # Helper: resolve role_esid -> role_name
    role_name_cache: dict[str, str] = {}

    def _role_name_for(role_esid: str) -> str:
        if role_esid in role_name_cache:
            return role_name_cache[role_esid]
        if role_esid in _FRIENDLY_ROLE_NAMES:
            name = _FRIENDLY_ROLE_NAMES[role_esid]
        else:
            try:
                doc = es.get(index="de_authz_role", id=role_esid)
                name = doc.get("_source", {}).get("role_name") or role_esid
            except Exception:
                name = role_esid
        role_name_cache[role_esid] = name
        return name

    # Team assignments
    try:
        team_hits = es.search(
            index="de_authz_user_team_role",
            body={"query": {"term": {"username": username}}, "size": 200},
        )
        for hit in team_hits.get("hits", {}).get("hits", []):
            src = hit.get("_source", {}) or {}
            team_esid = src.get("team_esid")
            role_esid = src.get("role_esid")
            if not team_esid or not role_esid:
                continue
            if team_esid == "__all__":
                team_title = _SENTINEL_TEAM_LABEL
            else:
                try:
                    team_doc = es.get(index="de_asset_teams", id=team_esid)
                    team_title = team_doc.get("_source", {}).get("team_title") or team_esid
                except Exception:
                    team_title = team_esid
            out["team_assignments"].append(
                {
                    "team_esid": team_esid,
                    "team_title": team_title,
                    "role_esid": role_esid,
                    "role_name": _role_name_for(role_esid),
                }
            )
    except Exception as e:
        logging.warning("Phase 15: team-assignment query failed for %s: %s", username, e)

    # Org assignments
    try:
        org_hits = es.search(
            index="de_authz_user_org_role",
            body={"query": {"term": {"username": username}}, "size": 200},
        )
        for hit in org_hits.get("hits", {}).get("hits", []):
            src = hit.get("_source", {}) or {}
            org_esid = src.get("org_esid")
            role_esid = src.get("role_esid")
            if not org_esid or not role_esid:
                continue
            try:
                org_doc = es.get(index="de_asset_organization", id=org_esid)
                org_title = org_doc.get("_source", {}).get("organization_title") or org_esid
            except Exception:
                org_title = org_esid
            out["org_assignments"].append(
                {
                    "org_esid": org_esid,
                    "org_title": org_title,
                    "role_esid": role_esid,
                    "role_name": _role_name_for(role_esid),
                }
            )
    except Exception as e:
        logging.warning("Phase 15: org-assignment query failed for %s: %s", username, e)

    # workspace assignments. Symmetric to the team/org loops
    # above. Best-effort — missing index (cluster pre-F8) returns an
    # empty array rather than erroring.
    try:
        workspace_hits = es.search(
            index="de_authz_user_workspace_role",
            body={"query": {"term": {"username": username}}, "size": 200},
        )
        for hit in workspace_hits.get("hits", {}).get("hits", []):
            src = hit.get("_source", {}) or {}
            workspace_esid = src.get("workspace_esid")
            role_esid = src.get("role_esid")
            if not workspace_esid or not role_esid:
                continue
            try:
                workspace_doc = es.get(index="de_asset_workspace", id=workspace_esid)
                workspace_title = workspace_doc.get("_source", {}).get("workspace_title") or workspace_esid
            except Exception:
                workspace_title = workspace_esid
            out["workspace_assignments"].append(
                {
                    "workspace_esid": workspace_esid,
                    "workspace_title": workspace_title,
                    "role_esid": role_esid,
                    "role_name": _role_name_for(role_esid),
                }
            )
    except Exception as e:
        err_text = str(e).lower()
        if "index_not_found" not in err_text and "404" not in err_text:
            logging.warning("workspace-assignment query failed for %s: %s", username, e)

    # the target is in the caller's scope, but the three queries
    # above are keyed on username alone, so a user who also belongs to
    # another tenant brought that tenant's titles and roles along. Keep only
    # the rows whose scope resolves into the caller's workspaces. Filtered
    # BEFORE the collapse so a hidden org row never hides a visible
    # team row.
    allowed_ws = _caller_workspace_scope()
    if allowed_ws is not None:
        out["team_assignments"] = [
            t for t in out["team_assignments"] if _resolve_scope_workspace_esid(es, "team", t["team_esid"]) in allowed_ws
        ]
        out["org_assignments"] = [
            o for o in out["org_assignments"] if _resolve_scope_workspace_esid(es, "org", o["org_esid"]) in allowed_ws
        ]
        out["workspace_assignments"] = [w for w in out["workspace_assignments"] if w["workspace_esid"] in allowed_ws]

    # same redundant-team-row collapse as
    # /api/user-management/role-assignments. The wizard's three-tier
    # fan-out writes ROLE_OWNER to both org + team
    # tier indices on identically-titled scopes for individual users;
    # the Current memberships panel concatenates team + org lists
    # without dedup, producing two "test-account (owner)" rows. Drop
    # any team_assignments entry whose (team_title, role_esid) matches
    # an org_assignments entry on the same user. Shapes here differ
    # from role-assignments (dict keys are team_title / org_title vs.
    # scope_title) so this can't share the helper above; the rule is
    # identical.
    org_keys = {(o["org_title"], o["role_esid"]) for o in out["org_assignments"]}
    out["team_assignments"] = [t for t in out["team_assignments"] if (t["team_title"], t["role_esid"]) not in org_keys]

    return jsonify({"success": True, **out})


# ─────────────────────────────────────────────────────────────────────────
# per-user role assignment CRUD — Scoped roles section of the
# Edit Roles modal.
# ─────────────────────────────────────────────────────────────────────────


def _collapse_redundant_team_row(assignments: list) -> list:
    """Drop the redundant team-tier row written by the wizard's three-tier
    fan-out.

    OnboardingService.provision_individual / provision_org_owner /
    provision_agency / add_client_to_workspace write ROLE_OWNER to all
    three tier indices so RoleFilterService doesn't have to fall back to
    org-tier inference. For the Edit Roles modal this surfaces three
    rows (workspace + org + team) where two of them share a title and
    role, reading as duplicate data.

    Collapse rule — strict, narrow, reversible:

    * For each (scope_title, role_esid) pair held BOTH at org tier AND
      at team tier, drop the team-tier row from the display payload.
    * Workspace-tier rows are never dropped.
    * Team rows without a matching org twin survive (e.g. a user
      explicitly granted ``viewer`` on a single team has no org row to
      match against).
    * Org rows are never dropped — the org tier is the wizard's
      "canonical" tier for ROLE_OWNER (catalog says applies_to=org).

    Data is unchanged — this is a display-payload filter only. The
    mutation routes still call ``list_for_user`` directly and see the
    full assignment set.
    """
    org_keys = {(a.scope_title, a.role_esid) for a in assignments if a.scope_type == "org"}
    return [a for a in assignments if not (a.scope_type == "team" and (a.scope_title, a.role_esid) in org_keys)]


@app.route("/api/user-management/role-assignments/<username>", methods=["GET"])
def api_list_role_assignments(username: str):
    """Return ``username``'s System 2 role assignments grouped by tier.

    Shape: ``{"success": True, "username", "assignments": [{scope_type,
    scope_esid, scope_title, role_esid, role_name}, ...]}``. Ordered
    workspace → org → team so the modal renders the hierarchy
    top-down.

    the display payload filters out team-tier rows that are
    duplicates of an org-tier row (same title + same role). The wizard
    writes the team row deliberately for resolver fast-path lookup; the
    Edit Roles modal doesn't need to see it.
    """
    _require_admin()
    if not username or not username.strip():
        return jsonify({"success": False, "error": "username required"}), 400
    _require_target_in_workspace_scope(username)

    assignments = UserRoleAssignmentService.list_for_user(username)
    # list_for_user spans every tenant the target belongs to; keep
    # only the scopes that resolve into the caller's workspaces (superuser
    # unfiltered). Filtered BEFORE the collapse, which compares rows.
    allowed_ws = _caller_workspace_scope()
    if allowed_ws is not None:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "ES unavailable"}), 503
        assignments = [a for a in assignments if _resolve_scope_workspace_esid(es, a.scope_type, a.scope_esid) in allowed_ws]
    assignments = _collapse_redundant_team_row(assignments)
    return jsonify(
        {
            "success": True,
            "username": username,
            "assignments": [
                {
                    "scope_type": a.scope_type,
                    "scope_esid": a.scope_esid,
                    "scope_title": a.scope_title,
                    "role_esid": a.role_esid,
                    "role_name": a.role_name,
                }
                for a in assignments
            ],
        }
    )


@app.route("/api/user-management/role-assignments", methods=["POST"])
def api_assign_role():
    """assign or replace a System 2 role for an existing user.

    Body: ``{username, scope_type, scope_esid, role_esid}``. The
    scope/role pairing is validated against
    ``permissions_seeder_service.DEFAULT_ROLES`` (same source of truth
    as Create User modal). Idempotent — re-submitting the
    same triple is a 200 no-op.

    Granting ``superuser`` through this surface is not possible —
    ``superuser`` is not a member of ``DEFAULT_ROLES`` and would fail
    the validator. The cluster-level superuser permission path remains
    the existing ``system_roles[]`` field on ``update-roles``.
    """
    # owner-only capability gate (replaces _require_admin). The
    # per-scope (workspace/org/team) boundary is enforced below.
    _require_permission(_USER_MGMT_GATE_PERMISSION, scope_type="workspace")
    check_csrf_for_state_change()
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    scope_type = (data.get("scope_type") or "").strip()
    scope_esid = (data.get("scope_esid") or "").strip()
    role_esid = (data.get("role_esid") or "").strip()

    if not username or not scope_type or not scope_esid or not role_esid:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "username, scope_type, scope_esid, role_esid are required",
                }
            ),
            400,
        )

    _require_target_in_workspace_scope(username)

    # also gate on the scope (workspace/org/team) the role is
    # being assigned to — a workspace-B admin must not be able to
    # assign a user (even one in their scope) to a kavanah-media
    # team. The scope_esid is the doc _id; resolve it to the parent
    # workspace and intersect. Superuser bypass matches the helper
    # contract above — cluster operators assign roles anywhere.
    _allowed_ws = RoleFilterService.get_allowed_workspaces()
    if "superuser" not in session.get("roles", []) and _allowed_ws is not None:
        _allowed_set = set(_allowed_ws)
        _scope_ws: str | None = None
        try:
            from lib.es_helpers import get_elasticsearch_client as _ges

            _es = _ges()
            if _es is not None:
                if scope_type == "workspace":
                    _scope_ws = scope_esid  # earlier _id == workspace_slug == workspace_esid
                elif scope_type == "org":
                    _org = _es.get(index="de_asset_organization", id=scope_esid)
                    _scope_ws = (_org.get("_source") or {}).get("workspace_esid")
                elif scope_type == "team":
                    _team = _es.get(index="de_asset_teams", id=scope_esid)
                    _scope_ws = (_team.get("_source") or {}).get("workspace_esid")
                    if not _scope_ws:
                        # Walk team -> org -> workspace as a fallback for
                        # teams that pre-date the backfill.
                        _org_esid = (_team.get("_source") or {}).get("organization_esid")
                        if _org_esid:
                            _org = _es.get(index="de_asset_organization", id=_org_esid)
                            _scope_ws = (_org.get("_source") or {}).get("workspace_esid")
        except Exception as _e:
            logging.warning("scope-workspace lookup failed for role-assignment POST: %s", _e)
            _scope_ws = None
        if not _scope_ws or _scope_ws not in _allowed_set:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": (f"Scope {scope_type} {scope_esid!r} is outside your workspace scope."),
                    }
                ),
                403,
            )

    if not _validate_role_for_tier(role_esid, scope_type):
        return (
            jsonify(
                {
                    "success": False,
                    "error": (
                        f"Role {role_esid!r} is not valid for {scope_type} scope. "
                        f"Use system_roles[] to grant cluster roles."
                    ),
                }
            ),
            400,
        )

    # Self-assignment guard mirrors create-user check: the
    # cluster-level superuser route is the only way to escalate your
    # own account; scoped roles must be granted to other users.
    if username == session.get("username", ""):
        return (
            jsonify(
                {
                    "success": False,
                    "error": (
                        "Cannot grant a scoped role to yourself. "
                        "Use system_roles to grant cluster roles to your own account."
                    ),
                }
            ),
            400,
        )

    # follow-up: one-workspace-per-user constraint on the
    # interactive Edit Roles surface. The system layer supports
    # multi-workspace users (is_multi_workspace_user_supported on the
    # workspace doc, the agency provisioning path in
    # OnboardingService.provision_agency, the primary-vs-secondary
    # membership distinction), but the interactive role-assignment
    # surface is restricted to one workspace per user. Re-assigning the
    # same workspace with a different role is allowed (the
    # UserRoleAssignmentService handles that as a role swap); adding a
    # SECOND workspace is rejected. Operators who need to move a user
    # to a different workspace must revoke the existing assignment
    # first. The non-interactive paths (agency provisioning) bypass
    # this gate by calling the service directly.
    if scope_type == "workspace":
        existing_workspace_assignments = [
            a
            for a in UserRoleAssignmentService.list_for_user(username)
            if a.scope_type == "workspace" and a.scope_esid != scope_esid
        ]
        if existing_workspace_assignments:
            existing = existing_workspace_assignments[0]
            return (
                jsonify(
                    {
                        "success": False,
                        "error": (
                            f"{username} is already assigned to workspace "
                            f"{existing.scope_title!r} ({existing.role_esid}). "
                            "Revoke that assignment before adding a different "
                            "workspace. Users can hold at most one workspace "
                            "assignment through this surface."
                        ),
                    }
                ),
                409,
            )

    # item 8 — a superuser (platform admin) bypasses the per-org
    # 5-user cap; an org admin does not.
    _actor_is_superuser = "superuser" in session.get("roles", [])
    result = UserRoleAssignmentService.assign(
        username=username,
        scope_type=scope_type,
        scope_esid=scope_esid,
        role_esid=role_esid,
        bypass_cap=_actor_is_superuser,
    )

    if not result.get("success"):
        status = int(result.get("status", 400))
        return jsonify({"success": False, "error": result.get("error", "Unknown error")}), status

    AuditLogService.log_event(
        session.get("username", "unknown"),
        "assign_scoped_role",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"scope_type={scope_type}, scope_esid={scope_esid}, role_esid={role_esid}",
    )
    return jsonify({"success": True, "scope_type": scope_type, "scope_esid": scope_esid, "role_esid": role_esid})


@app.route(
    "/api/user-management/role-assignments/<username>/<scope_type>/<scope_esid>/<role_esid>",
    methods=["DELETE"],
)
def api_revoke_role_assignment(username: str, scope_type: str, scope_esid: str, role_esid: str):
    """revoke one role assignment for an existing user.

    Idempotent — deleting an already-absent assignment returns 200
    with ``already_absent: True`` so the modal can refresh without
    surfacing a confusing 404 to the operator.

    owner-only like ``api_assign_role`` (it was ``_require_admin``),
    and the path's ``scope_esid`` must resolve into the caller's workspaces —
    binding only the target user let an owner strip that user's role in a
    workspace the owner does not belong to.
    """
    _require_permission(_USER_MGMT_GATE_PERMISSION, scope_type="workspace")
    check_csrf_for_state_change()
    _require_target_in_workspace_scope(username)
    allowed_ws = _caller_workspace_scope()
    if allowed_ws is not None:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "ES unavailable"}), 503
        if _resolve_scope_workspace_esid(es, scope_type, scope_esid) not in allowed_ws:
            return (
                jsonify({"success": False, "error": f"Scope {scope_type} {scope_esid!r} is outside your workspace scope."}),
                403,
            )
    result = UserRoleAssignmentService.revoke(
        username=username,
        scope_type=scope_type,
        scope_esid=scope_esid,
        role_esid=role_esid,
    )
    if not result.get("success"):
        status = int(result.get("status", 400))
        return jsonify({"success": False, "error": result.get("error", "Unknown error")}), status

    AuditLogService.log_event(
        session.get("username", "unknown"),
        "revoke_scoped_role",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"scope_type={scope_type}, scope_esid={scope_esid}, role_esid={role_esid}",
    )
    return jsonify({"success": True, "already_absent": bool(result.get("already_absent"))})


@app.route("/api/user-management/workspaces", methods=["GET"])
def api_list_workspaces():
    """list workspaces for the Create User membership picker.

    Returns ``[{esid, slug, title}]`` from ``de_asset_workspace``.
    added ``esid`` (the ES doc ``_id``) so the Edit Roles picker
    can send the doc id as ``scope_esid`` — the role-assignment service's
    ``es.get(asset_index, id=scope_esid)`` validator expects ``_id``, not
    the slug. ``slug`` is retained for the Create User modal which still
    encodes memberships as ``workspace_<slug>`` strings (resolved server-
    side via ``_provision_new_authz_assignments``). For workspaces whose
    ``_id`` matches the slug — the documented natural-key convention —
    the two fields are identical; the API surfaces both so consumers
    don't have to know which call site needs which.
    """
    _require_admin()
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "ES unavailable"}), 503

    # narrow to the operator's workspace scope. Non-superusers
    # only see workspaces they're a member of (mirrors the page-render
    # filter from the follow-up). Without this, the Edit Roles
    # → Scoped roles → Add (workspace tier) picker shows every workspace
    # in the cluster, and a workspace-scoped admin could attempt to
    # assign a workspace_admin role in a workspace they don't belong to
    # (the route-level _require_memberships_in_workspace_scope and
    # single-workspace gate stop the WRITE, but the UI shouldn't offer
    # the option in the first place).
    #
    # ``get_allowed_workspaces()`` returns:
    #   * ``None`` — superuser / asset_admin with no scope filter (pass through unfiltered)
    #   * ``[]`` — non-privileged user with no scope (deny all)
    #   * ``[<esid>, ...]`` — scoped operator (filter to intersection)
    try:
        _allowed_ws = RoleFilterService.get_allowed_workspaces()
    except Exception as _e:
        logging.warning("get_allowed_workspaces failed; failing closed: %s", _e)
        _allowed_ws = []  # fail-closed for non-superusers
    is_superuser = "superuser" in session.get("roles", [])
    if is_superuser:
        _allowed_set: set[str] | None = None  # no filter
    else:
        _allowed_set = set(_allowed_ws) if _allowed_ws is not None else set()

    workspaces: list[dict] = []
    try:
        hits = es.search(
            index="de_asset_workspace",
            body={"query": {"match_all": {}}, "size": 500},
        )
        for hit in hits.get("hits", {}).get("hits", []):
            src = hit.get("_source", {}) or {}
            slug = src.get("workspace_slug")
            if not slug:
                continue
            esid = hit.get("_id")
            # earlier _id == slug for new workspaces, but legacy
            # docs may have drifted. Match by both so neither identity
            # accidentally leaks the other through.
            if _allowed_set is not None and esid not in _allowed_set and slug not in _allowed_set:
                continue
            workspaces.append(
                {
                    "esid": esid,
                    "slug": slug,
                    "title": src.get("workspace_title") or slug,
                }
            )
    except Exception as e:
        logging.warning("workspace list query failed: %s", e)
        return jsonify({"success": False, "error": "workspace lookup failed"}), 500

    workspaces.sort(key=lambda w: w["title"].lower())
    return jsonify({"success": True, "workspaces": workspaces})


@app.route("/api/user-management/orgs", methods=["GET"])
def api_list_orgs():
    """follow-up: list orgs for the Edit Roles scoped-role picker.

    Returns ``[{esid, title}]`` from ``de_asset_organization``. The Edit
    Roles dialog's org/team Assign button hinged on a non-empty
    ``scope_esid``; before this endpoint existed the JS read titles out
    of the Memberships checklist and wired them as ESIDs, which left the
    real ESID empty and the Assign button permanently disabled. This
    endpoint surfaces the real ``_id`` so the picker can submit it.

    Mirrors the scope-narrowing in :func:`api_list_workspaces`: non-
    superusers see only orgs in their workspace via
    ``RoleFilterService.get_allowed_organizations()`` (titles).
    """
    _require_admin()
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "ES unavailable"}), 503

    try:
        _allowed_org_titles = RoleFilterService.get_allowed_organizations()
    except Exception as _e:
        logging.warning("api_list_orgs: get_allowed_organizations failed; failing closed: %s", _e)
        _allowed_org_titles = []  # fail-closed for non-superusers
    is_superuser = "superuser" in session.get("roles", [])
    if is_superuser:
        _allowed_titles_set: set[str] | None = None  # no filter
    else:
        _allowed_titles_set = set(_allowed_org_titles) if _allowed_org_titles is not None else set()

    orgs: list[dict] = []
    try:
        hits = es.search(
            index="de_asset_organization",
            body={"query": {"match_all": {}}, "size": 500},
        )
        for hit in hits.get("hits", {}).get("hits", []):
            src = hit.get("_source", {}) or {}
            title = src.get("organization_title")
            if not title:
                continue
            if _allowed_titles_set is not None and title not in _allowed_titles_set:
                continue
            orgs.append({"esid": hit.get("_id"), "title": title})
    except Exception as e:
        logging.warning("api_list_orgs: query failed: %s", e)
        return jsonify({"success": False, "error": "org lookup failed"}), 500

    orgs.sort(key=lambda o: o["title"].lower())
    return jsonify({"success": True, "orgs": orgs})


@app.route("/api/user-management/teams", methods=["GET"])
def api_list_teams():
    """follow-up: list teams for the Edit Roles scoped-role picker.

    Returns ``[{esid, title}]`` from ``de_asset_teams``. Same rationale
    as :func:`api_list_orgs`. Non-superusers see only teams in their
    workspace scope (union of team-tier and workspace-tier visibility,
    via ``RoleFilterService.get_allowed_teams()`` which already returns
    titles for the privileged + workspace-fallback paths).
    """
    _require_admin()
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "ES unavailable"}), 503

    try:
        _allowed_team_titles = RoleFilterService.get_allowed_teams()
    except Exception as _e:
        logging.warning("api_list_teams: get_allowed_teams failed; failing closed: %s", _e)
        _allowed_team_titles = []
    is_superuser = "superuser" in session.get("roles", [])
    if is_superuser:
        _allowed_titles_set: set[str] | None = None
    else:
        _allowed_titles_set = set(_allowed_team_titles) if _allowed_team_titles is not None else set()

    teams: list[dict] = []
    try:
        hits = es.search(
            index="de_asset_teams",
            body={"query": {"match_all": {}}, "size": 1000},
        )
        hit_list = hits.get("hits", {}).get("hits", [])
        # tenant-boundary by workspace_esid. The title filter
        # below (``title not in _allowed_titles_set``) leaks across
        # workspaces because team titles collide; narrow by workspace
        # first (None = superuser/no-filter, [] = deny-all, fail-closed on
        # missing field). Superusers resolve get_allowed_workspaces()
        # → None and are unaffected, matching the _allowed_titles_set=None
        # superuser branch above.
        hit_list = RoleFilterService.filter_hits_by_workspace(hit_list)
        for hit in hit_list:
            src = hit.get("_source", {}) or {}
            title = src.get("team_title")
            if not title:
                continue
            if _allowed_titles_set is not None and title not in _allowed_titles_set:
                continue
            teams.append({"esid": hit.get("_id"), "title": title})
    except Exception as e:
        logging.warning("api_list_teams: query failed: %s", e)
        return jsonify({"success": False, "error": "team lookup failed"}), 500

    teams.sort(key=lambda t: t["title"].lower())
    return jsonify({"success": True, "teams": teams})


# empty-workspace delete from User Management. Superuser-only,
# enabled only when the selected workspace has zero rows in
# ``de_authz_workspace_membership``. Purges the workspace doc + its
# child orgs/teams + all matching authz rows. Data indices
# (``{team_esid}__*``) are deliberately NOT purged — that belongs to a
# separate "delete all user data" surface (filed as out-of-scope follow-
# up on the ticket).
@app.route("/api/user-management/workspaces/<workspace_esid>/empty-check", methods=["GET"])
def api_workspace_empty_check(workspace_esid: str):
    """Return ``{empty, membership_count}`` for the workspace.

    Drives the Delete Workspace button's enable state. The same count is
    re-checked server-side at delete time so a stale UI cannot bypass
    the gate.
    """
    _require_superuser()
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "ES unavailable"}), 503

    count = _count_workspace_memberships(es, workspace_esid)
    if count < 0:
        return jsonify({"success": False, "error": "Membership count unavailable"}), 500
    return jsonify({"success": True, "empty": count == 0, "membership_count": count})


@app.route("/api/user-management/workspaces/<workspace_esid>/delete", methods=["POST"])
def api_delete_workspace(workspace_esid: str):
    """Purge an empty workspace + its orgs/teams + authz rows.

    Rejects with 409 if the workspace has any membership rows — the
    button's enable state already enforces this on the client, but the
    server is authoritative.
    """
    _require_superuser()
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "ES unavailable"}), 503

    # Resolve the title once for audit logging BEFORE the asset doc is
    # purged. Title is best-effort; a missing workspace doc is a 404.
    try:
        ws_doc = es.get(index="de_asset_workspace", id=workspace_esid)
        workspace_title = (ws_doc.get("_source") or {}).get("workspace_title") or workspace_esid
    except Exception as e:
        err = str(e).lower()
        if "not_found" in err or "404" in err:
            return jsonify({"success": False, "error": "Workspace not found"}), 404
        logging.warning("workspace title lookup failed for %s: %s", workspace_esid, e)
        workspace_title = workspace_esid

    membership_count = _count_workspace_memberships(es, workspace_esid)
    if membership_count < 0:
        return jsonify({"success": False, "error": "Membership count unavailable"}), 500
    if membership_count > 0:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"Workspace has {membership_count} user(s) — remove users before deleting",
                    "membership_count": membership_count,
                }
            ),
            409,
        )

    counts = _cascade_purge_empty_workspace(es, workspace_esid)
    cascade_summary = ",".join(f"{i}={n}" for i, n in counts.items())
    logging.info(
        "AUDIT: admin=%s action=delete_workspace target=%s title=%r ip=%s cascade=%s",
        session.get("username"),
        workspace_esid,
        workspace_title,
        request.remote_addr,
        cascade_summary,
    )
    AuditLogService.log_event(
        session.get("username", "unknown"),
        "delete_workspace",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"workspace_esid={workspace_esid} title={workspace_title!r} cascade={cascade_summary}",
    )
    return jsonify({"success": True, "workspace_esid": workspace_esid, "cascade": counts})


@app.route("/api/user-management/update-roles", methods=["POST"])
def api_update_roles():
    """Admin: update a user's roles and memberships.

    Wire format: ``system_roles`` (free-form ES Security role names — system
    roles + custom non-prefixed roles) and ``memberships`` (a list of
    ``org_<title>`` / ``team_<title>`` strings) are passed separately. The
    legacy combined ``roles`` payload is no longer accepted; ES Security and
    the new authz indices are written independently so the two stores never
    carry duplicate state.
    """
    # owner-only capability gate (replaces _require_admin);
    # target/membership scope checks below enforce the per-workspace boundary.
    _require_permission(_USER_MGMT_GATE_PERMISSION)
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    system_roles = data.get("system_roles", [])
    # ``memberships`` is now OPTIONAL. The Edit Roles dialog
    # manages scoped roles live via the role-assignments endpoints and no
    # longer sends a membership list; an ABSENT key means "leave scoped
    # roles untouched — only update system_roles". A PRESENT list keeps the
    # legacy reconcile semantics for any caller still sending
    # it. ``None`` is the sentinel for "key absent".
    memberships = data.get("memberships", None)
    reconcile_memberships = memberships is not None

    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
    if not isinstance(system_roles, list):
        return jsonify({"success": False, "error": "system_roles must be a list"}), 400
    # Only validate/gate the membership list when the caller actually sent
    # one. An empty list is still rejected (a present-but-empty list is a
    # client bug, not the new "untouched" contract — that is key-absence).
    if reconcile_memberships and (not isinstance(memberships, list) or not memberships):
        return (
            jsonify({"success": False, "error": "Select at least one org or team membership."}),
            400,
        )

    _require_target_in_workspace_scope(username)
    if reconcile_memberships:
        _require_memberships_in_workspace_scope(memberships)
        # non-superusers must belong to at most one workspace.
        # The desired ``memberships`` list IS the final state after the
        # update (``_sync_new_authz_memberships`` diffs against current
        # authz and writes the difference), so we validate the same list
        # the writer will apply. Target-superuser exemption is handled
        # inside the helper.
        _validate_single_workspace_for_non_superuser(username, memberships)

    # Defensive: keep any org_/team_ strings out of ES Security regardless of
    # what the client sent.
    es_security_roles = [
        r for r in system_roles if isinstance(r, str) and not r.startswith("org_") and not r.startswith("team_")
    ]

    # Superuser-only cluster roles (asset_admin / data_explorer / superuser):
    # the role pickers hide these from non-superusers, but update_user_roles
    # REPLACES the user's role array wholesale. So for a non-superuser caller
    # we must both (a) refuse to ADD a superuser-only role the target doesn't
    # already hold, and (b) NOT silently strip one the caller couldn't see —
    # otherwise editing a target who holds asset_admin would wipe it. Preserve
    # the target's existing superuser-only roles and reject any genuinely new
    # ones. The add-refusal still short-circuits for superusers.
    #
    # ``superuser`` and ``asset_admin`` are now hidden from the
    # pickers for ALL viewers, so every dialog payload omits them regardless
    # of caller privilege. Preserve a target's held picker-hidden roles for
    # superuser callers too — otherwise a superuser merely editing an
    # asset_admin holder's other roles would silently strip asset_admin and
    # lock that account out of login (config.REQUIRED_ROLE, auth.py). For
    # superusers only those two are re-added (data_explorer is still visible
    # to them, so its omission is a deliberate revoke).
    _caller_is_superuser = "superuser" in session.get("roles", [])
    _preservable_roles = _PICKER_HIDDEN_SYSTEM_ROLES if _caller_is_superuser else _SUPERUSER_ONLY_SYSTEM_ROLES
    _target_detail = UserManagementService.get_user_detail(username)
    _target_existing_roles = set(_target_detail.get("roles", []) or []) if _target_detail.get("success") else set()
    if not _caller_is_superuser:
        _newly_requested = [
            r for r in es_security_roles if r in _SUPERUSER_ONLY_SYSTEM_ROLES and r not in _target_existing_roles
        ]
        if _newly_requested:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": _superuser_only_role_grant_error(_newly_requested),
                    }
                ),
                403,
            )
    # Re-add any preservable roles the target already held but that the
    # (picker-hidden) payload omitted, so an edit preserves rather than
    # strips them.
    _preserved = [r for r in _target_existing_roles if r in _preservable_roles and r not in es_security_roles]
    if _preserved:
        es_security_roles = es_security_roles + _preserved

    result = UserManagementService.update_user_roles(username, es_security_roles)
    if result["success"]:
        # only reconcile scoped roles when the caller sent a
        # membership list. The Edit Roles dialog manages them live now, so
        # it omits the key and this reconcile is skipped — preserving the
        # user's existing scoped roles. Legacy list-sending callers keep the
        # v3 diff-against-current-authz semantics.
        if reconcile_memberships:
            try:
                _sync_new_authz_memberships(username=username, desired=memberships)
            except Exception as _new_authz_err:
                logging.warning(
                    "new-authz membership sync failed for %s: %s",
                    username,
                    _new_authz_err,
                )
        AuditLogService.log_event(
            session.get("username", "unknown"),
            "update_roles",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"system_roles={es_security_roles}, memberships={memberships}"
                if reconcile_memberships
                else f"system_roles={es_security_roles} (scoped roles untouched)"
            ),
        )
        return jsonify(
            {
                "success": True,
                "system_roles": es_security_roles,
                "memberships": memberships if reconcile_memberships else None,
            }
        )
    return jsonify(result), 400


@app.route("/api/user-management/update-email", methods=["POST"])
def api_update_email():
    """Admin: update a user's email address."""
    _require_permission(_USER_MGMT_GATE_PERMISSION)  # owner-only, like delete-user
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()

    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400

    _require_target_in_workspace_scope(username)

    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 500

    try:
        response = es.security.get_user(username=username)
        user_data = dict(response).get(username, {})
        if not user_data:
            return jsonify({"success": False, "error": f"User '{username}' not found"}), 404

        # block reassigning this user's email to one already owned by
        # a DIFFERENT user. exclude_username skips the target's own record so a
        # no-op save (or a case-only change) isn't a false collision. Admin
        # posture: name the colliding account.
        if email:
            owner = UserProvisioningService.find_user_by_email(email, exclude_username=username)
            if owner is not None:
                return (
                    jsonify(
                        {
                            "success": False,
                            "code": "email_taken",
                            "error": f"Email '{email}' is already in use by user '{owner.get('username', '')}'.",
                            "existing_username": owner.get("username", ""),
                        }
                    ),
                    409,
                )

        get_es_admin_client(es).security.put_user(
            username=username,
            roles=user_data.get("roles", []),
            full_name=user_data.get("full_name", ""),
            email=email,
            # ES writes enabled=true when it is omitted.
            enabled=user_data.get("enabled", True),
            metadata=user_data.get("metadata", {}),
        )
        logging.info(
            f"AUDIT: admin={session.get('username')} action=update_email " f"target={username} ip={request.remote_addr}"
        )
        AuditLogService.log_event(
            session.get("username", "unknown"),
            "update_email",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"email={email}",
        )
        return jsonify({"success": True})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to update email",
            log_detail=f"api_update_email: failed for '{username}'",
            exc=e,
        )


@app.route("/api/user-management/admin-set-password", methods=["POST"])
def api_admin_set_password():
    """Admin: set a user's password to an explicit value, no email sent.

    For service / demo accounts where emailing a temporary password defeats the
    purpose. Requires asset_admin or superuser. Audit reason is required and
    is recorded in the AUDIT log alongside the admin's username.
    """
    _require_permission(_USER_MGMT_GATE_PERMISSION)  # owner-only, like delete-user
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    new_password = data.get("new_password") or ""
    reason = (data.get("reason") or "").strip()

    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
    if not new_password:
        return jsonify({"success": False, "error": "new_password is required"}), 400
    if not reason:
        return jsonify({"success": False, "error": "Audit reason is required"}), 400

    _require_target_in_workspace_scope(username)

    set_by = session.get("username") or "unknown"
    result = UserProvisioningService.admin_set_password(
        username=username,
        new_password=new_password,
        set_by=set_by,
        reason=reason,
    )

    if not result.get("success"):
        error = result.get("error", "")
        # Distinguish complexity / validation failures (400) from server errors (500)
        if "User '" in error and "not found" in error:
            status = 404
        elif "built-in user" in error or "Password must" in error or "is required" in error:
            status = 400
        else:
            status = 500
        return jsonify(result), status

    AuditLogService.log_event(
        set_by,
        "admin_set_password",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"reason={reason}",
    )
    return jsonify({"success": True})


@app.route("/api/user-management/user-detail/<username>")
def api_user_detail(username):
    """Admin: get full user detail including login history."""
    _require_admin()
    _require_target_in_workspace_scope(username)

    result = UserManagementService.get_user_detail(username)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/user-management/bulk-action", methods=["POST"])
def api_bulk_action():
    """Admin only: perform bulk suspend/reinstate/delete on multiple users."""
    _require_permission(_USER_MGMT_GATE_PERMISSION)  # owner-only, like delete-user
    data = request.get_json() or {}
    action = data.get("action", "").strip()
    usernames = data.get("usernames", [])

    if action not in ("suspend", "reinstate", "delete"):
        return jsonify({"success": False, "error": "Invalid action. Must be: suspend, reinstate, or delete"}), 400
    if not isinstance(usernames, list) or not usernames:
        return jsonify({"success": False, "error": "At least one username is required"}), 400

    # all-or-nothing workspace-scope gate. Reject the whole
    # request if ANY target is outside the operator's scope, rather
    # than partial-apply + report. Partial-apply would leak which
    # cross-workspace usernames exist (via the errors[] payload) and
    # also surprise the operator with an inconsistent half-applied
    # state. Superuser bypasses — cluster operators must be able to
    # bulk-operate on any user regardless of workspace.
    if "superuser" in session.get("roles", []):
        out_of_scope: list[str] = []
    else:
        in_scope = {u["username"] for u in _filter_users_to_current_workspace_scope([{"username": n} for n in usernames])}
        out_of_scope = [n for n in usernames if n not in in_scope]
    if out_of_scope:
        return (
            jsonify(
                {
                    "success": False,
                    "error": (
                        f"{len(out_of_scope)} username(s) are outside your workspace scope: "
                        f"{', '.join(sorted(out_of_scope))}. Remove them and retry."
                    ),
                }
            ),
            403,
        )

    _refuse_privileged_targets(usernames)
    if action == "reinstate":
        # all-or-nothing, like the scope gate above — the same
        # platform-suspension refusal as the single-user reinstate route.
        for uname in usernames:
            _refuse_platform_suspension_reinstate(uname)
    admin_username = session.get("username", "")
    result = UserManagementService.bulk_action(action, usernames, admin_username=admin_username)

    # Log individual audit events for each successful operation
    audit_action = {"suspend": "suspend_user", "reinstate": "reinstate_user", "delete": "delete_user"}[action]
    failed_usernames = {e["username"] for e in result.get("errors", [])}
    if action == "suspend":
        _stamp_suspension_actor([u for u in usernames if u not in failed_usernames])
    for uname in usernames:
        if uname not in failed_usernames:
            AuditLogService.log_event(
                admin_username,
                audit_action,
                True,
                target_user=uname,
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
                detail="bulk=true",
            )

    return jsonify(result)


# ── Role permissions endpoints ──────────────────────────────────────────────


@app.route("/api/user-management/roles")
def api_list_roles():
    """List all roles with metadata (user count, deletable, locked)."""
    _require_admin()
    roles = RolePermissionsService.get_roles_for_listing()

    is_superuser = "superuser" in session.get("roles", [])

    # Hide system roles from non-superusers
    if not is_superuser:
        roles = [r for r in roles if r["role_type"] != "system"]

    return jsonify({"roles": roles})


@app.route("/api/user-management/role-permissions/<role_name>")
def api_get_role_permissions(role_name):
    """Get the permission settings for a role.

    for seeded System 2 role esids (workspace/org/team-tier
    admin bundles), return the read-only ``permissions`` shape from
    ``de_authz_role`` + ``de_authz_role_permission`` instead of the
    System 1 ``mc_*`` shape. The frontend distinguishes the two by the
    presence of the ``permissions`` array.

    a non-superuser reads only the roles ``api_list_roles`` shows
    them — System 2 tier roles and non-system (org_/team_) System 1 roles —
    not the platform role matrix.
    """
    _require_admin()
    if (
        "superuser" not in session.get("roles", [])
        and not RolePermissionsService.is_system2_role(role_name)
        and RolePermissionsService._classify_role(role_name) == "system"
    ):
        abort(403, description="Only superusers can view system role permissions.")
    if RolePermissionsService.is_system2_role(role_name):
        system2 = RolePermissionsService.get_system2_permissions(role_name)
        if system2 is not None:
            return jsonify(system2)
        # Fall through if the doc is missing on this cluster — preserves
        # the legacy behavior for orphan / pre-seed environments.
    perms = RolePermissionsService.get_permissions(role_name)
    return jsonify(perms)


@app.route("/api/user-management/roles", methods=["POST"])
def api_create_role_with_permissions():
    """Create a new custom role with permissions (superuser only)."""
    _require_superuser()
    check_csrf_for_state_change()
    data = request.get_json() or {}
    role_name = data.get("role_name", "")
    role_type = data.get("role_type", "")
    settings = data.get("settings", {}) or {}
    result = RolePermissionsService.create_role(
        role_name=role_name,
        role_type=role_type,
        settings=settings,
        created_by=session.get("username", "unknown"),
    )
    if result.get("success"):
        AuditLogService.log_event(
            session.get("username", "unknown"),
            "create_role",
            True,
            target_user=result["role_name"],
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"Created role '{result['role_name']}' (type={result['role_type']})",
        )
    return jsonify(result)


@app.route("/api/user-management/role-permissions/<role_name>", methods=["POST"])
def api_update_role_permissions(role_name):
    """Update the permission settings for a role.

    seeded System 2 roles are code-defined and read-only —
    refuse the update before it can shadow the System 2 catalog by
    writing a System 1 ``de_role_permissions`` doc with the same id.

    superuser only, like create-role — roles are cluster-wide, so a
    workspace owner editing one changed it for every tenant.
    """
    _require_superuser()
    check_csrf_for_state_change()
    if RolePermissionsService.is_system2_role(role_name):
        return jsonify({"success": False, "error": "Built-in role permissions cannot be modified"}), 403
    data = request.get_json() or {}
    result = RolePermissionsService.update_permissions(role_name, data, updated_by=session.get("username", "unknown"))
    if result.get("success"):
        AuditLogService.log_event(
            session.get("username", "unknown"),
            "update_role_permissions",
            True,
            target_user=role_name,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"Updated permissions for role '{role_name}'",
        )
    return jsonify(result)


@app.route("/api/user-management/delete-role/<role_name>", methods=["DELETE"])
def api_delete_role(role_name):
    """Delete an org/team role (with safety checks).

    superuser only, like create-role — roles are cluster-wide.
    """
    _require_superuser()
    check_csrf_for_state_change()
    result = RolePermissionsService.delete_role(role_name)
    if result.get("success"):
        AuditLogService.log_event(
            session.get("username", "unknown"),
            "delete_role",
            True,
            target_user=role_name,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"Deleted role '{role_name}'",
        )
    return jsonify(result)


@app.route("/testing")
def testing_page():
    """Regression test harness for dashboards — gates on mc_testing.

    The ``/api/testing/*`` API endpoints keep their ``_require_admin``
    checks per the ticket non-goals: they execute privileged actions
    (pytest, locust, security tests), not user-facing pages.
    """
    _require_mc("mc_testing", "Testing")
    return render_template("testing.html")


@app.route("/api/testing/dashboards")
def api_testing_dashboards():
    """Return all testable dashboards (Kibana + ECharts) with their check URLs."""
    _require_cluster_admin()
    from lib.es_helpers import get_all_documents as _get_all

    results = []
    from asset_management.web.config import DEFAULT_KIBANA_URL

    kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)

    # ── Kibana dashboards from de_asset_dashboards ───────────────────────────
    for doc in _get_all("de_asset_dashboards"):
        src = doc.get("_source", {})
        if src.get("visibility_mode") == "hide":
            continue
        dash_id = src.get("dashboard_id", "").strip()
        if not dash_id:
            continue
        space = src.get("space_id", "default")
        iframe_url = (
            f"{kibana_url}/s/{space}/app/dashboards#/view/{dash_id}"
            "?embed=true"
            "&_g=(refreshInterval:(pause:!t),time:(from:now-30d%2Fd,to:now))"
            "&_a=(viewMode:view)"
            "&hide-filter-bar=true"
        )
        results.append(
            {
                "id": dash_id,
                "title": src.get("dashboard_title", dash_id),
                "type": "kibana",
                "check_url": url_for("api_testing_check_dashboard", dtype="kibana", did=dash_id),
                "iframe_url": iframe_url,
            }
        )

    # ── ECharts dashboards from de_asset_echarts_dashboards ──────────────────
    for doc in _get_all("de_asset_echarts_dashboards"):
        src = doc.get("_source", {})
        if src.get("visibility_mode") == "hide":
            continue
        dash_id = src.get("dashboard_id", "").strip()
        if not dash_id:
            continue
        viewer_path = url_for("dashboard_builder.view_dashboard", dashboard_id=dash_id)
        if USE_PREFIX and not viewer_path.startswith("/assets"):
            viewer_path = "/assets" + viewer_path
        results.append(
            {
                "id": dash_id,
                "title": src.get("title", dash_id),
                "type": "echarts",
                "check_url": url_for("api_testing_check_dashboard", dtype="echarts", did=dash_id),
                "iframe_url": viewer_path + "?test_mode=1",
            }
        )

    return jsonify(results)


@app.route("/api/testing/run-pytest", methods=["POST"])
def api_testing_run_pytest():
    """Run the pytest suite and write output to /app/data/logs/. Admin only."""
    _require_cluster_admin()
    import subprocess
    from datetime import datetime as _dt

    log_dir = "/app/data/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "pytest_{}.log".format(_dt.now().strftime("%Y%m%d")))
    try:
        env = os.environ.copy()
        # /app/bin/python is a runtime symlink to /app/python created by
        # start_web_apps.sh so tests/conftest.py (which resolves bin/python
        # relative to its own location) can import asset_management.
        env["PYTHONPATH"] = "/app/bin/python:/app/bin/python/asset_management/web"
        result = subprocess.run(
            ["python3", "-m", "pytest", "/app/tests/", "-v", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        output = result.stdout + result.stderr
        with open(log_path, "w") as f:
            f.write(output)
        passed = result.returncode == 0
        return jsonify({"success": True, "passed": passed, "filename": os.path.basename(log_path)})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "pytest timed out after 180s"}), 500
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to run pytest",
            log_detail="api_testing_run_pytest failed",
            exc=e,
        )


@app.route("/api/testing/pytest-log")
def api_testing_pytest_log():
    """Return the latest pytest log file from /app/data/logs/."""
    _require_cluster_admin()
    import glob as _glob

    log_dir = "/app/data/logs"
    try:
        files = sorted(_glob.glob(os.path.join(log_dir, "pytest_*.log")), reverse=True)
        if not files:
            return jsonify({"exists": False, "filename": None, "content": ""})
        latest = files[0]
        filename = os.path.basename(latest)
        with open(latest, "r", errors="replace") as f:
            content = f.read()
        return jsonify({"exists": True, "filename": filename, "content": content})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to read pytest log",
            log_detail="api_testing_pytest_log failed",
            exc=e,
            body={"exists": False, "content": ""},
        )


@app.route("/api/testing/check-dashboard")
def api_testing_check_dashboard():
    """
    Server-side health check for a single dashboard.
    For Kibana: verifies the ES document exists and has a valid dashboard_id.
    For ECharts: fetches the echarts config and checks it has charts.
    """
    _require_cluster_admin()
    from lib.es_helpers import get_elasticsearch_client

    dtype = request.args.get("dtype", "")
    did = request.args.get("did", "").strip()

    if not dtype or not did:
        return jsonify({"ok": False, "error": "dtype and did are required"}), 400

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"ok": False, "error": "Cannot connect to Elasticsearch"}), 503

    try:
        if dtype == "kibana":
            # Check de_asset_dashboards for a doc with this dashboard_id.
            # Use match_phrase (not term) because dashboard_id may be mapped as text,
            # which causes term queries to fail on tokenized UUIDs.
            result = es.search(
                index="de_asset_dashboards",
                body={"query": {"match_phrase": {"dashboard_id": did}}},
                size=1,
            )
            hits = result.get("hits", {}).get("hits", [])
            if not hits:
                return jsonify({"ok": False, "error": "Dashboard not found in de_asset_dashboards"})
            src = hits[0].get("_source", {})
            if not src.get("dashboard_id"):
                return jsonify({"ok": False, "error": "dashboard_id field is empty"})
            return jsonify({"ok": True, "title": src.get("dashboard_title", did)})

        elif dtype == "echarts":
            # Fetch from de_asset_echarts_dashboards
            result = es.search(
                index="de_asset_echarts_dashboards",
                body={"query": {"match_phrase": {"dashboard_id": did}}},
                size=1,
            )
            hits = result.get("hits", {}).get("hits", [])
            if not hits:
                return jsonify(
                    {
                        "ok": False,
                        "error": "Dashboard not found in de_asset_echarts_dashboards",
                    }
                )
            src = hits[0].get("_source", {})
            charts = src.get("echarts_config", {}).get("charts", [])
            if not isinstance(charts, list):
                return jsonify({"ok": False, "error": "echarts_config.charts is not a list"})
            return jsonify({"ok": True, "title": src.get("title", did), "chart_count": len(charts)})

        else:
            return jsonify({"ok": False, "error": f"Unknown dtype: {dtype}"}), 400

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to check dashboard",
            log_detail="api_testing_check_dashboard failed",
            exc=e,
            body={"ok": False},
        )


# ============================================
# LOAD TESTING (Locust)
# ============================================

# Locust state shared across gunicorn workers via filesystem
_locust_log_path = "/app/data/logs/locust_latest.log"
_locust_csv_prefix = "/app/data/logs/locust_results"
_locust_pid_path = "/app/data/logs/locust.pid"
# a resource sampler subprocess runs alongside every locust run and
# a run-metadata JSON records the requested profile for the report/UI.
_locust_resources_path = "/app/data/logs/locust_resources.csv"
_locust_sampler_pid_path = "/app/data/logs/locust_sampler.pid"
_locust_sampler_log_path = "/app/data/logs/locust_sampler.log"
_locust_run_meta_path = "/app/data/logs/locust_run_meta.json"


def _pidfile_running_pid(pid_path: str, cmdline_marker: str) -> int | None:
    """Return the PID from ``pid_path`` if that process is still running AND
    its cmdline contains ``cmdline_marker``, else None.

    a bare ``os.kill(pid, 0)`` only proves *some* process owns that
    PID — after the original subprocess exits, the OS will eventually reuse
    its PID for an unrelated process and the running flag never clears.
    Verify identity by reading ``/proc/<pid>/cmdline``. Stale PID files are
    removed so subsequent callers don't keep paying the same lookup.
    """
    if not os.path.isfile(pid_path):
        return None

    def _drop_stale_pid_file() -> None:
        try:
            os.remove(pid_path)
        except OSError:
            pass

    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as cmdf:
            cmdline = cmdf.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except (ValueError, FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        _drop_stale_pid_file()
        return None

    if cmdline_marker not in cmdline:
        _drop_stale_pid_file()
        return None
    return pid


def _locust_running_pid() -> int | None:
    """Return the Locust PID if it is still running, else None."""
    return _pidfile_running_pid(_locust_pid_path, "locust")


def _sampler_running_pid() -> int | None:
    """Return the resource-sampler PID if still running, else None."""
    return _pidfile_running_pid(_locust_sampler_pid_path, "resource_sampler")


def _load_probe_identity(identity_file: str) -> tuple[str, str] | None:
    """(username, password) of the seeded probe row (role == "probe") from the
    identity CSV, or None. Failure-soft — a missing file just means the
    seeder hasn't run."""
    import csv as _csv

    try:
        with open(identity_file, newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                if (row.get("role") or "").strip() == "probe":
                    username = (row.get("username") or "").strip()
                    password = (row.get("password") or "").strip()
                    if username and password:
                        return (username, password)
    except OSError:
        pass
    return None


@app.route("/api/testing/run-locust", methods=["POST"])
def api_testing_run_locust():
    """Start a headless Locust run. Admin only."""
    _require_cluster_admin()
    import subprocess

    # Don't start a second run
    if _locust_running_pid() is not None:
        return jsonify({"success": False, "error": "A load test is already running"}), 409

    data = request.get_json(silent=True) or {}
    users = min(int(data.get("users", 10)), 100)
    spawn_rate = min(int(data.get("spawn_rate", 2)), 20)
    # duration flows into Locust's --run-time arg. Reject anything
    # that isn't a Locust-style duration (e.g. "60s", "5m", "1h") so a
    # malformed value can't reach the subprocess argv (CodeQL py/command-
    # line-injection). cmd is a list (no shell), so this is defense-in-depth.
    import re

    duration_raw = str(data.get("duration", "60s"))
    if not re.fullmatch(r"\d{1,5}[smh]", duration_raw):
        return (
            jsonify({"success": False, "error": "duration must look like '60s', '5m', or '1h'"}),
            400,
        )
    duration = duration_raw

    # profile ("classic" | "steps") and identity mode ("single" |
    # "pool"). Steps mode swaps --users/--spawn-rate/--run-time for a
    # LoadTestShape driven by env vars; like duration, the step values flow
    # into the subprocess env, so validate them here (step_profile raises
    # ValueError with an operator-readable message).
    profile = str(data.get("profile", "classic"))
    if profile not in ("classic", "steps"):
        return jsonify({"success": False, "error": "profile must be 'classic' or 'steps'"}), 400
    identity_mode = str(data.get("identity_mode", "single"))
    if identity_mode not in ("single", "pool"):
        return jsonify({"success": False, "error": "identity_mode must be 'single' or 'pool'"}), 400
    # Unscoped pool identities are opt-in per run: they can't see any
    # dashboards (403 on the list gate), so by default they'd silently dilute
    # the capacity numbers (28% of the pool at 50 users).
    include_unscoped = bool(data.get("include_unscoped"))

    step_users: list[int] = []
    step_seconds = 0
    total_seconds: int | None = None
    if profile == "steps":
        from load_tests import step_profile

        try:
            step_users = step_profile.parse_step_users(str(data.get("step_users", step_profile.DEFAULT_STEP_USERS)))
            step_seconds = step_profile.parse_step_seconds(data.get("step_seconds", step_profile.DEFAULT_STEP_SECONDS))
        except InvalidRequestError as e:
            # step_profile raises this with its own wording of the expected input.
            return jsonify({"success": False, "error": e.public_message}), 400
        total_seconds = step_profile.total_run_seconds(step_users, step_seconds)

    # locustfile lives under bin/python/load_tests/ so it's available in the
    # container via the bin/python → /app/python mount on all servers.
    _app_dir = os.path.dirname(os.path.abspath(__file__))
    locustfile = os.path.normpath(os.path.join(_app_dir, "..", "..", "load_tests", "locustfile.py"))
    if not os.path.isfile(locustfile):
        return (
            jsonify({"success": False, "error": f"Locustfile not found: {locustfile}"}),
            404,
        )

    # default to THIS deployment (ProxyFix supplies the public scheme +
    # host); LOAD_TEST_TARGET_URL overrides for a remote target. Trusts the Host
    # header only because nginx enforces exact server_name matching (return 444
    # otherwise) and :8084 is bound to 127.0.0.1 — a direct hit needs the network.
    target_url = os.environ.get("LOAD_TEST_TARGET_URL") or request.host_url.rstrip("/")

    os.makedirs(os.path.dirname(_locust_log_path), exist_ok=True)

    # Clean up old CSV files
    for suffix in [
        "_stats.csv",
        "_stats_history.csv",
        "_failures.csv",
        "_exceptions.csv",
    ]:
        path = _locust_csv_prefix + suffix
        if os.path.exists(path):
            os.remove(path)
    # previous run's sampler CSV + run metadata
    for path in (_locust_resources_path, _locust_run_meta_path):
        if os.path.exists(path):
            os.remove(path)

    env = os.environ.copy()
    # identity + profile config travels via env (validated above).
    env["LOAD_TEST_IDENTITY_MODE"] = identity_mode
    if include_unscoped:
        env["LOAD_TEST_INCLUDE_UNSCOPED"] = "1"
    else:
        env.pop("LOAD_TEST_INCLUDE_UNSCOPED", None)
    env.setdefault("LOAD_TEST_IDENTITY_FILE", "/app/data/load_tests/identities.csv")
    # Probe identity. Under ENFORCE_MFA the historical default (elastic +
    # ES01_PASSWORD) can never web-login (MFAService rule 1 hard-blocks
    # elastic), so prefer the seeded loadtest-probe row from the identity CSV
    # (scoped viewer + bypass_mfa). An explicit LOAD_TEST_USERNAME env var on
    # the container still wins; the ES01_PASSWORD fallback stays as last
    # resort for pre-seeder installs without MFA enforcement.
    if not env.get("LOAD_TEST_USERNAME"):
        probe = _load_probe_identity(env["LOAD_TEST_IDENTITY_FILE"])
        if probe is not None:
            env["LOAD_TEST_USERNAME"], env["LOAD_TEST_PASSWORD"] = probe
    if not env.get("LOAD_TEST_PASSWORD"):
        env["LOAD_TEST_PASSWORD"] = env.get("ES01_PASSWORD", "")
    cmd = [
        "python3",
        "-m",
        "locust",
        "--headless",
        "--host",
        target_url,
        # --csv-full-history adds per-request-name rows to _stats_history.csv
        # each tick — that's what lets the degradation report separate the
        # probe user's experience from the aggregate.
        "--csv-full-history",
        "--csv",
        _locust_csv_prefix,
        "-f",
        locustfile,
    ]
    if profile == "steps":
        # A LoadTestShape drives user count and end-of-run; locust ignores
        # --users/--spawn-rate/--run-time when a shape is present (verified,
        # locust 2.44), so omit them rather than emit misleading warnings.
        env["LOAD_TEST_PROFILE"] = "steps"
        env["LOAD_TEST_STEP_USERS"] = ",".join(str(u) for u in step_users)
        env["LOAD_TEST_STEP_SECONDS"] = str(step_seconds)
    else:
        env.pop("LOAD_TEST_PROFILE", None)
        cmd += [
            "--users",
            str(users),
            "--spawn-rate",
            str(spawn_rate),
            "--run-time",
            duration,
        ]

    log_file = open(_locust_log_path, "w")
    # Endpoint is admin-gated via _require_admin(); cmd is a list (not a shell
    # string) so argv entries can't be reinterpreted as shell metacharacters;
    # env is derived from os.environ plus one server-local secret, never from
    # the request body.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, env=env)

    # Write PID to a file so all gunicorn workers can track the process
    with open(_locust_pid_path, "w") as f:
        f.write(str(proc.pid))

    # record run metadata (display-only — the report derives every
    # computed value from the CSVs) and start the resource sampler. Both are
    # failure-soft: a run without metadata or resource series is degraded,
    # not broken.
    import json as _json
    import signal
    import time as _time

    try:
        with open(_locust_run_meta_path, "w") as f:
            _json.dump(
                {
                    "profile": profile,
                    "identity_mode": identity_mode,
                    "include_unscoped": include_unscoped,
                    "users": users,
                    "spawn_rate": spawn_rate,
                    "duration": duration,
                    "step_users": step_users or None,
                    "step_seconds": step_seconds or None,
                    "total_seconds": total_seconds,
                    "target_url": target_url,
                    "started_at": _time.time(),
                },
                f,
            )
    except OSError as e:
        app.logger.warning("run-locust: could not write run metadata: %s", e)

    stale_sampler = _sampler_running_pid()
    if stale_sampler is not None:
        try:
            os.kill(stale_sampler, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.remove(_locust_sampler_pid_path)
        except OSError:
            pass
    try:
        sampler_script = os.path.normpath(os.path.join(_app_dir, "..", "..", "load_tests", "resource_sampler.py"))
        sampler_cmd = [
            "python3",
            sampler_script,
            "--out",
            _locust_resources_path,
            "--interval",
            "5",
            # Self-exits within one interval of the locust process dying, so
            # it cannot outlive the run even if stop-locust is never called.
            "--watch-pid",
            str(proc.pid),
        ]
        sampler_log = open(_locust_sampler_log_path, "w")
        # Same posture as the locust Popen above: admin-gated, list argv, env
        # from os.environ — nothing request-derived reaches the command.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        sampler_proc = subprocess.Popen(sampler_cmd, stdout=sampler_log, stderr=subprocess.STDOUT, env=env)
        with open(_locust_sampler_pid_path, "w") as f:
            f.write(str(sampler_proc.pid))
    except OSError as e:
        # boundary: the load test is still valuable without resource series
        app.logger.warning("run-locust: resource sampler failed to start: %s", e)

    return jsonify(
        {
            "success": True,
            "pid": proc.pid,
            "users": users,
            "duration": duration,
            "spawn_rate": spawn_rate,
            "target_url": target_url,
            "profile": profile,
            "identity_mode": identity_mode,
            "include_unscoped": include_unscoped,
            # Steps mode: server-computed wall-clock so the UI countdown can't
            # drift from the shape math. None in classic mode (UI keeps using
            # the requested duration).
            "total_seconds": total_seconds,
            "step_users": step_users or None,
        }
    )


@app.route("/api/testing/locust-status")
def api_testing_locust_status():
    """Check if a Locust run is still active. Admin only."""
    _require_cluster_admin()

    if not os.path.isfile(_locust_pid_path):
        return jsonify({"running": False, "ever_ran": False})

    pid = _locust_running_pid()
    running = pid is not None
    # If not running, try to read exit status from log (Locust prints it)
    exit_code = 0 if not running else None
    return jsonify(
        {
            "running": running,
            "ever_ran": True,
            "exit_code": exit_code,
        }
    )


@app.route("/api/testing/stop-locust", methods=["POST"])
def api_testing_stop_locust():
    """Stop a running Locust process. Admin only.

    the previous version sent SIGINT and returned, leaving the PID
    file in place. Combined with PID reuse in ``_locust_running_pid``, this
    is what wedged the prod load tester. Now we wait for the process to exit
    (escalating to SIGTERM if it ignores SIGINT) and always remove the PID
    file before returning.
    """
    _require_cluster_admin()
    import signal
    import time

    pid = _locust_running_pid()
    if pid is None:
        return jsonify({"success": False, "error": "No load test is running"}), 404

    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass

    # Give Locust up to 5s to drain and exit cleanly.
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    try:
        os.remove(_locust_pid_path)
    except OSError:
        pass

    # stop the resource sampler too. It would self-exit within one
    # interval anyway (--watch-pid); this just makes stop immediate.
    sampler_pid = _sampler_running_pid()
    if sampler_pid is not None:
        try:
            os.kill(sampler_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        os.remove(_locust_sampler_pid_path)
    except OSError:
        pass

    return jsonify({"success": True})


@app.route("/api/testing/locust-results")
def api_testing_locust_results():
    """Return Locust CSV results and log output. Admin only."""
    _require_cluster_admin()

    log_content = ""
    if os.path.isfile(_locust_log_path):
        with open(_locust_log_path, "r", errors="replace") as f:
            log_content = f.read()

    stats_csv = ""
    stats_path = _locust_csv_prefix + "_stats.csv"
    if os.path.isfile(stats_path):
        with open(stats_path, "r", errors="replace") as f:
            stats_csv = f.read()

    failures_csv = ""
    failures_path = _locust_csv_prefix + "_failures.csv"
    if os.path.isfile(failures_path):
        with open(failures_path, "r", errors="replace") as f:
            failures_csv = f.read()

    # the per-tick time series (written with --csv-full-history)
    # and the resource sampler's CSV.
    stats_history_csv = ""
    stats_history_path = _locust_csv_prefix + "_stats_history.csv"
    if os.path.isfile(stats_history_path):
        with open(stats_history_path, "r", errors="replace") as f:
            stats_history_csv = f.read()

    resources_csv = ""
    if os.path.isfile(_locust_resources_path):
        with open(_locust_resources_path, "r", errors="replace") as f:
            resources_csv = f.read()

    return jsonify(
        {
            "log": log_content,
            "stats_csv": stats_csv,
            "failures_csv": failures_csv,
            "stats_history_csv": stats_history_csv,
            "resources_csv": resources_csv,
        }
    )


@app.route("/api/testing/load-report")
def api_testing_load_report():
    """Degradation-vs-baseline report for the latest load-test run.

    Server-side so the math is unit-testable; the Load Testing tab only
    draws. Admin only.
    """
    _require_cluster_admin()
    import json as _json

    stats_history_path = _locust_csv_prefix + "_stats_history.csv"
    if not os.path.isfile(stats_history_path):
        return jsonify({"available": False})
    with open(stats_history_path, "r", errors="replace") as f:
        stats_history_csv = f.read()

    resources_csv = ""
    if os.path.isfile(_locust_resources_path):
        with open(_locust_resources_path, "r", errors="replace") as f:
            resources_csv = f.read()

    meta = None
    if os.path.isfile(_locust_run_meta_path):
        try:
            with open(_locust_run_meta_path) as f:
                meta = _json.load(f)
        except (OSError, ValueError):
            meta = None  # display-only; the report works without it

    from asset_management.web.services.load_report_service import build_report

    report = build_report(stats_history_csv, resources_csv, meta)
    # Phase 3: overlay the most recent import-load run's per-source
    # timing markers so import starts/ends line up with the charts.
    from asset_management.web.services.import_load_service import latest_run_markers

    report["imports"] = latest_run_markers()
    report["available"] = True
    return jsonify(report)


# ============================================
# IMPORT-LOAD MODE — concurrent-import capacity testing
# ============================================


@app.route("/api/testing/import-load-candidates")
def api_testing_import_load_candidates():
    """(team, source) pairs with already-imported data that a capacity test
    can safely re-import, each with its effective lock group. Admin only."""
    _require_cluster_admin()
    from asset_management.web.services.import_load_service import list_candidates
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "Cannot connect to Elasticsearch"}), 503
    return jsonify({"success": True, "candidates": list_candidates(es)})


@app.route("/api/testing/import-load-worst-case", methods=["POST"])
def api_testing_import_load_worst_case():
    """greedy worst-case pick over the panel's loaded candidate
    list: up to N heaviest rows with pairwise non-conflicting lock groups.
    The client posts its candidates back so the returned indices match the
    rows on screen; the pick only checks boxes — Run stays a separate,
    reviewable click. Admin only."""
    _require_cluster_admin()
    from asset_management.web.services.import_load_service import select_worst_case

    data = request.get_json(silent=True) or {}
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not all(isinstance(c, dict) for c in candidates):
        return jsonify({"success": False, "error": "candidates must be a list of objects"}), 400
    try:
        n = int(data.get("n"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "n must be an integer"}), 400
    return jsonify({"success": True, "indices": select_worst_case(candidates, n)})


@app.route("/api/testing/run-import-load", methods=["POST"])
def api_testing_run_import_load():
    """Kick off up to MAX_CONCURRENT_IMPORTS concurrent re-imports (pairwise
    non-conflicting lock groups, no force_refresh, staging only). Admin only."""
    _require_cluster_admin()
    from asset_management.web.services.import_load_service import ImportLoadError, run_import_load
    from lib.es_helpers import get_elasticsearch_client

    data = request.get_json(silent=True) or {}
    try:
        result = run_import_load(data.get("imports"), request.host, get_elasticsearch_client())
    except ImportLoadError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    result["success"] = True
    return jsonify(result)


@app.route("/api/testing/import-load-status")
def api_testing_import_load_status():
    """Status + per-source timing markers for an import-load run. Admin only."""
    _require_cluster_admin()
    from asset_management.web.services.import_load_service import import_load_status

    import_id = (request.args.get("import_id") or "").strip()
    if not import_id:
        return jsonify({"success": False, "error": "import_id is required"}), 400
    status = import_load_status(import_id)
    status["success"] = True
    return jsonify(status)


# ── Security dashboard ───────────────────────────────────────────────────────


# / Story 4: lazy-loaded catalog of release-plan markers across
# tests/flask/ and tests/e2e/. Populated on first request to
# /api/testing/release-plans (or run-release-plan) and cached for the
# process lifetime. SIGHUP gunicorn to refresh after adding markers.
from asset_management.web.services.release_plan_service import ReleasePlanCatalog as _ReleasePlanCatalog


def _release_plan_test_roots() -> list[str]:
    """Candidate roots for the marker scanner. Container runs `app.py` at
    `/app/python/asset_management/web/`, so `<__file__>/../../../../tests`
    points at the bind-mounted `/app/tests`. On a dev machine
    the same relative climb lands at `<repo>/tests`."""
    here = os.path.dirname(os.path.abspath(__file__))
    relative_root = os.path.normpath(os.path.join(here, "..", "..", "..", "..", "tests"))
    return [relative_root, "/app/tests"]


_RELEASE_PLAN_CATALOG = _ReleasePlanCatalog(roots=_release_plan_test_roots())


def _discover_known_release_plans() -> tuple[str, ...]:
    """Discover the set of valid release-plan names by globbing for
    ``the release-plan tests`` directories. prod cutover
    surfaced that the previous hardcoded tuple (/ /)
    drifted 25 releases out of date and broke the Testing page's
    \"Run security regression\" UI button for every recent plan.

    Resolves at module-import time (the set is stable per Flask
    process; new plans require a SIGHUP to register, same constraint
    as the marker catalog above). Re-globs both bind-mount candidates
    so dev + container both work.
    """
    import glob as _glob

    found: set[str] = set()
    for root in _release_plan_test_roots():
        plans_dir = os.path.join(root, "release_plans")
        if not os.path.isdir(plans_dir):
            continue
        for entry in _glob.glob(os.path.join(plans_dir, "PR-*")):
            if os.path.isdir(entry):
                found.add(os.path.basename(entry))
    return tuple(sorted(found))


_KNOWN_RELEASE_PLANS: tuple[str, ...] = _discover_known_release_plans()


@app.route("/api/testing/run-security-tests", methods=["POST"])
def api_testing_run_security_tests():
    """Run security regression tests and return parsed results. Admin only.

    Query parameters:
        release_plan: optional release-plan name (e.g. "plan-25"). When set,
            only tests marked with `@pytest.mark.release_plan(<name>)` run,
            and the response includes per-`test_id` results from the JSON
            report emitted by `tests/conftest.py`.
    """
    _require_cluster_admin()
    import subprocess
    from datetime import datetime as _dt

    release_plan = (request.args.get("release_plan") or "").strip() or None
    if release_plan and release_plan not in _KNOWN_RELEASE_PLANS:
        return jsonify({"success": False, "error": "Unknown release_plan: {}".format(release_plan)}), 400

    log_dir = "/app/data/logs"
    os.makedirs(log_dir, exist_ok=True)
    timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, "security_{}.log".format(_dt.now().strftime("%Y%m%d")))
    report_path = os.path.join(log_dir, "release_plan_results_{}.json".format(timestamp))
    try:
        env = os.environ.copy()
        # /app/bin/python is a runtime symlink to /app/python (start_web_apps.sh)
        env["PYTHONPATH"] = "/app/bin/python:/app/bin/python/asset_management/web"
        if release_plan:
            env["RELEASE_PLAN_REPORT"] = report_path
            env["RELEASE_PLAN_NAME"] = release_plan
        cmd = [
            "python3",
            "-m",
            "pytest",
            "/app/tests/flask/test_security.py",
            "-v",
            "--tb=short",
            "-c",
            "/app/pytest.ini",
            "--rootdir",
            "/app",
        ]
        # Filtering by release_plan happens in tests/conftest.py via the
        # RELEASE_PLAN_NAME env var. pytest's `-m` expression language can't
        # match marker arguments, only marker names.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        output = result.stdout + result.stderr
        with open(log_path, "w") as f:
            f.write(output)

        # Parse pytest -v output into structured results.
        # Pytest 9 prints lines like:
        #   "tests/flask/test_security.py::TestClass::test_name PASSED [  0%]"
        # so we strip a trailing "[ NN%]" before splitting status off the end.
        # Statuses we capture: PASSED, FAILED, ERROR, SKIPPED, XFAIL, XPASS.
        import re as _re

        _PROGRESS_RE = _re.compile(r"\s*\[\s*\d+%\]\s*$")
        _STATUS_TOKENS = (" PASSED", " FAILED", " ERROR", " SKIPPED", " XFAIL", " XPASS")
        tests = []
        for line in result.stdout.splitlines():
            if not any(token in line for token in _STATUS_TOKENS):
                continue
            stripped = _PROGRESS_RE.sub("", line)
            parts = stripped.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            name = parts[0].strip()
            # Shorten: remove file path, keep class::method
            if "::" in name:
                name = name.split("::", 1)[1]
            status = parts[1].strip()
            tests.append({"name": name, "status": status})

        passed = sum(1 for t in tests if t["status"] == "PASSED")
        skipped = sum(1 for t in tests if t["status"] == "SKIPPED")
        failed = sum(1 for t in tests if t["status"] not in ("PASSED", "SKIPPED"))

        release_plan_results = None
        if release_plan and os.path.exists(report_path):
            try:
                with open(report_path) as f:
                    import json as _json

                    release_plan_results = _json.load(f)
            except (OSError, ValueError):
                release_plan_results = None

        return jsonify(
            {
                "success": True,
                "passed": result.returncode == 0,
                "total": len(tests),
                "pass_count": passed,
                "fail_count": failed,
                "skip_count": skipped,
                "tests": tests,
                "filename": os.path.basename(log_path),
                "release_plan": release_plan,
                "release_plan_results": release_plan_results,
            }
        )
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Security tests timed out after 180s"}), 500
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to run security tests",
            log_detail="api_testing_run_security_tests failed",
            exc=e,
        )


@app.route("/api/testing/security-test-log")
def api_testing_security_test_log():
    """Return the latest security test log. Admin only."""
    _require_cluster_admin()
    import glob as _glob

    log_dir = "/app/data/logs"
    try:
        files = sorted(_glob.glob(os.path.join(log_dir, "security_*.log")), reverse=True)
        if not files:
            return jsonify({"exists": False, "filename": None, "content": ""})
        latest = files[0]
        with open(latest, "r", errors="replace") as f:
            content = f.read()
        return jsonify({"exists": True, "filename": os.path.basename(latest), "content": content})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to read security test log",
            log_detail="api_testing_security_test_log failed",
            exc=e,
            body={"exists": False, "content": ""},
        )


@app.route("/api/testing/release-plans", methods=["GET"])
def api_testing_release_plans():
    """Enumerate release plans discovered from `release_plan(...)` markers
    across the test tree. Cached for the process lifetime.

    Returns:
        {
          "plans": [
            {"name": "plan-25", "test_ids": ["SEC-CSP-001",...]}
          ],
          "scanned_at": "2026-04-28T20:00:00Z" | null
        }
    """
    _require_cluster_admin()
    plans = _RELEASE_PLAN_CATALOG.get()
    scanned = _RELEASE_PLAN_CATALOG.scanned_at()
    return jsonify(
        {
            "plans": [{"name": name, "test_ids": ids} for name, ids in plans.items()],
            "scanned_at": scanned.isoformat() if scanned else None,
        }
    )


def _load_latest_playwright_results(plan_name: str) -> dict | None:
    """Read the most recent Playwright release-plan JSON for `plan_name` from
    /app/data/logs/e2e/. Returns the parsed payload or None if absent.

    The TS reporter writes one file per run; we pick the newest
    that names this plan or has no plan filter set."""
    import glob as _glob
    import json as _json

    e2e_dir = "/app/data/logs/e2e"
    if not os.path.isdir(e2e_dir):
        return None
    candidates = sorted(_glob.glob(os.path.join(e2e_dir, "*.json")), reverse=True)
    for path in candidates:
        try:
            with open(path) as f:
                payload = _json.load(f)
        except (OSError, ValueError):
            continue
        plan = payload.get("release_plan")
        # Accept either a plan-specific report or a "no filter" report
        # whose results carry test_ids that belong to this plan.
        if plan == plan_name or plan is None:
            return payload
    return None


@app.route("/api/testing/run-release-plan", methods=["POST"])
def api_testing_run_release_plan():
    """Run a release plan end-to-end. Admin only.

    Body: {"plan": "plan-25"}

    Pytest is executed live with the plan filter; Playwright results are
    merged from the most recent JSON written by the Playwright reporter
     under /app/data/logs/e2e/. The container has no Node runtime,
    so live Playwright execution is intentionally NOT done here — that runs
    in CI on PRs, or on a dev machine.

    Returns:
        {
          "plan": "plan-25",
          "results": [
            {"test_id": "SEC-CSP-001", "outcome": "passed",
             "source": "pytest" | "playwright", "duration": 0.12,
             "log_path": "...", "longrepr": null | "..."}
          ],
          "playwright_run_at": "2026-04-28T...Z" | null,
          "playwright_stale": true | false  (true if older than 24h)
        }
    """
    _require_cluster_admin()
    import subprocess
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    data = request.get_json(silent=True) or {}
    plan = (data.get("plan") or "").strip()
    if not plan:
        return jsonify({"success": False, "error": "plan is required"}), 400

    catalog = _RELEASE_PLAN_CATALOG.get()
    if plan not in catalog:
        return jsonify({"success": False, "error": "Unknown plan: {}".format(plan)}), 400

    log_dir = "/app/data/logs"
    os.makedirs(log_dir, exist_ok=True)
    timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    # phase 4: plan is whitelist-validated against catalog above;
    # safe_path_join makes the containment under log_dir visible to CodeQL.
    from lib.paths import safe_path_join

    pytest_report = safe_path_join(log_dir, "release_plan_{}_pytest_{}.json".format(plan, timestamp))
    pytest_log = safe_path_join(log_dir, "release_plan_{}_pytest_{}.log".format(plan, timestamp))

    # ── Pytest ─────────────────────────────────────────────────────────────
    pytest_results: dict = {}
    pytest_returncode = -1
    try:
        env = os.environ.copy()
        # /app/bin/python is a runtime symlink to /app/python (start_web_apps.sh)
        env["PYTHONPATH"] = "/app/bin/python:/app/bin/python/asset_management/web"
        env["RELEASE_PLAN_REPORT"] = pytest_report
        env["RELEASE_PLAN_NAME"] = plan
        result = subprocess.run(
            [
                "python3",
                "-m",
                "pytest",
                "/app/tests",
                "-q",
                "--tb=short",
                "-c",
                "/app/pytest.ini",
                "--rootdir",
                "/app",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        pytest_returncode = result.returncode
        with open(pytest_log, "w") as f:
            f.write(result.stdout + result.stderr)
        if os.path.exists(pytest_report):
            import json as _json

            with open(pytest_report) as f:
                pytest_results = _json.load(f).get("results", {}) or {}
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "pytest timed out after 300s"}), 500
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to run pytest for release plan",
            log_detail="api_testing_run_release_plan pytest failed",
            exc=e,
        )

    # ── Playwright (read-only, from most-recent JSON) ──────────────────────
    playwright_payload = _load_latest_playwright_results(plan)
    playwright_results: dict = {}
    playwright_run_at = None
    playwright_stale = False
    if playwright_payload:
        playwright_results = playwright_payload.get("results", {}) or {}
        generated = playwright_payload.get("generated_at")
        if generated:
            playwright_run_at = generated
            try:
                run_dt = _dt.fromisoformat(generated.replace("Z", "+00:00"))
                playwright_stale = (_dt.now(run_dt.tzinfo) - run_dt) > _td(hours=24)
            except ValueError:
                playwright_stale = True

    # ── Merge ──────────────────────────────────────────────────────────────
    merged = []
    seen_ids = set()
    for test_id, entry in sorted(pytest_results.items()):
        merged.append({"test_id": test_id, **entry})
        seen_ids.add(test_id)
    for test_id, entry in sorted(playwright_results.items()):
        if test_id in seen_ids:
            continue  # pytest takes precedence on collision (shouldn't happen)
        merged.append({"test_id": test_id, **entry})
        seen_ids.add(test_id)

    return jsonify(
        {
            "success": True,
            "plan": plan,
            "results": merged,
            "pytest_returncode": pytest_returncode,
            "pytest_log": os.path.basename(pytest_log),
            "playwright_run_at": playwright_run_at,
            "playwright_stale": playwright_stale,
        }
    )


@app.route("/api/testing/run-pip-audit", methods=["POST"])
def api_testing_run_pip_audit():
    """Run pip-audit and return structured vulnerability list. Admin only."""
    _require_cluster_admin()
    import subprocess

    try:
        result = subprocess.run(
            [
                "python3",
                "-m",
                "pip_audit",
                "--desc",
                "--format",
                "json",
                # CVE-2026-3219: pip's tar/ZIP concatenation handling. No fix
                # version published yet; mirrors the same `--ignore-vuln` line
                # in `.github/workflows/pytest.yml`. Drop both when pip ships
                # a patched version.
                "--ignore-vuln",
                "CVE-2026-3219",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        vulns = []
        if result.stdout.strip():
            import json as _json

            try:
                raw = _json.loads(result.stdout)
                # pip-audit JSON: {"dependencies": [{"name":..., "version":..., "vulns": [...]}]}
                for dep in raw.get("dependencies", []):
                    for v in dep.get("vulns", []):
                        vulns.append(
                            {
                                "package": dep.get("name", ""),
                                "version": dep.get("version", ""),
                                "vuln_id": v.get("id", ""),
                                "fix_versions": v.get("fix_versions", []),
                                "description": v.get("description", ""),
                            }
                        )
            except Exception:
                pass  # Non-JSON output — treat as clean
        return jsonify(
            {
                "success": True,
                "clean": len(vulns) == 0,
                "vulnerabilities": vulns,
                "exit_code": result.returncode,
            }
        )
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "pip-audit timed out after 120s"}), 500
    except FileNotFoundError:
        return jsonify({"success": False, "error": "pip-audit not installed"}), 500
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to run pip-audit",
            log_detail="api_testing_run_pip_audit failed",
            exc=e,
        )


def _ci_scan_error_message(status_code, headers=None):
    """turn a GitHub API failure into an actionable message.

    All five CI-status rows once read a bare "HTTP 401" for weeks after the
    PAT behind GITHUB_TOKEN was deleted — strictly worse than the
    never-configured case, which has always had a real message. Name the
    remedy for the codes an operator can act on; anything else stays a plain
    pass-through.
    """
    if status_code == 401:
        return "HTTP 401 — GITHUB_TOKEN is invalid or was deleted; generate a new token and update the environment"
    if status_code == 403:
        if (headers or {}).get("x-ratelimit-remaining") == "0":
            return "HTTP 403 — GitHub API rate limit exhausted; retries succeed after the window resets"
        return "HTTP 403 — GITHUB_TOKEN lacks the required scopes (actions:read on the repo); regenerate it"
    if status_code == 404:
        return "HTTP 404 — workflow file not found in the repo; was it renamed or removed?"
    return f"HTTP {status_code}"


@app.route("/api/testing/ci-scan-status")
def api_testing_ci_scan_status():
    """Return latest GitHub Actions run status for security workflows. Admin only."""
    _require_cluster_admin()
    import requests as http_requests

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return jsonify({"success": False, "error": "GITHUB_TOKEN not configured"})

    repo = "Kavanah/digital-engagement-dashboard"
    workflows = [
        {"file": "e2e-and-zap.yml", "name": "E2E + OWASP ZAP"},
        {"file": "trivy.yml", "name": "Trivy (Container)"},
        {"file": "semgrep.yml", "name": "Semgrep (SAST)"},
        {"file": "gitleaks.yml", "name": "Gitleaks (Secrets)"},
        {"file": "pytest.yml", "name": "Pytest"},
    ]

    results = []
    for wf in workflows:
        try:
            url = f"https://api.github.com/repos/{repo}/actions/workflows/{wf['file']}/runs?per_page=1"
            resp = http_requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                results.append(
                    {
                        "name": wf["name"],
                        "status": "unknown",
                        "error": _ci_scan_error_message(resp.status_code, resp.headers),
                    }
                )
                continue
            runs = resp.json().get("workflow_runs", [])
            if not runs:
                results.append({"name": wf["name"], "status": "no_runs"})
                continue
            run = runs[0]
            results.append(
                {
                    "name": wf["name"],
                    "status": run.get("status", ""),
                    "conclusion": run.get("conclusion", ""),
                    "created_at": run.get("created_at", ""),
                    "html_url": run.get("html_url", ""),
                    "run_number": run.get("run_number", 0),
                }
            )
        except Exception:  # noqa: BLE001 — one workflow failing must not hide the others
            logging.exception("api_testing_ci_scan_status: GitHub API call failed for %s", wf["file"])
            results.append({"name": wf["name"], "status": "error", "error": "Could not reach the GitHub API"})

    return jsonify({"success": True, "workflows": results})


# ============================================
# MFA CHALLENGE ROUTES (login step 2)
# ============================================


@app.route("/mfa/challenge", methods=["GET"])
def mfa_challenge_page():
    """Show MFA challenge form."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))
    method = session.get("mfa_pending_method")
    csrf_token = auth_module.generate_csrf_token()
    error_message = request.args.get("error")
    return render_template(
        "mfa_challenge.html",
        method=method,
        csrf_token=csrf_token,
        error_message=error_message,
    )


def _complete_mfa_login(method: str):
    """
    Promote an mfa_pending session to an authenticated session and redirect
    to the user's next_url. Shared by the MFA challenge handler and the
    first-time enrollment handlers — a valid TOTP/YubiKey code proves
    possession whether it came from /mfa/challenge or /mfa/setup/*.
    """
    username = session.get("mfa_pending_username")
    kibana_sid_cookie = session.get("mfa_pending_kibana_sid")
    roles = session.get("mfa_pending_roles", [])
    full_name = session.get("mfa_pending_full_name", username)
    email = session.get("mfa_pending_email", "")
    next_url = session.get("mfa_pending_next", "/")
    login_type = session.get("mfa_pending_login_type", "form_login")
    # when login was via a recovery code, carry the remaining-count
    # nudge across the session.clear() so the post-login UI can prompt the user
    # to regenerate codes / re-secure their account.
    recovery_used_remaining = session.get("mfa_recovery_used_remaining")

    user_metadata = UserProvisioningService.get_user_metadata(username)
    if user_metadata and user_metadata.get("force_password_change"):
        session.clear()
        session["fpc_pending"] = True
        session["fpc_username"] = username
        session["fpc_roles"] = roles
        session["fpc_full_name"] = full_name
        session["fpc_email"] = email
        session["fpc_timestamp"] = time.time()
        logging.info(
            f"AUDIT: user={username} action=mfa_complete force_password_change=required method={method} ip={request.remote_addr}"
        )
        return redirect(url_for("change_password_page"))

    session.clear()
    session["authenticated"] = True
    session["username"] = username
    session["roles"] = roles
    session["full_name"] = full_name
    session["email"] = email
    session["auth_timestamp"] = time.time()
    session["last_activity"] = time.time()
    if recovery_used_remaining is not None:
        session["mfa_recovery_used_remaining"] = recovery_used_remaining
    session["auth_method"] = login_type + "_mfa"
    session.permanent = True

    # write a "login" audit event when the session reaches the
    # authenticated state via MFA. Without this the Login Activity graph
    # (which counts action=login, success=true, is_scanner!=true) shows
    # nothing for any user with MFA enabled.
    AuditLogService.log_event(
        username,
        "login",
        True,
        auth_method=login_type + "_mfa",
        mfa_method=method,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    MFAService.update_last_used(username)

    # run the login gate here too. It used to live only in
    # the non-MFA tail of the form-login POST, which this path never reaches:
    # the handler returns to the MFA challenge well before it. Since the wizard
    # makes MFA mandatory at logical step 3, that meant the gate never ran for
    # the wizard users it exists to catch, and session["wizard_pending"] was
    # never set for them so verify_auth's wizard confinement never engaged.
    #
    # Deliberately BEFORE the kibana cookie is attached: a gated user is being
    # sent to the wizard, not to a dashboard, and the form-login path likewise
    # creates its kibana session only after the gate comes back clear.
    from asset_management.web.login_gate_redirect import gate_redirect_after_auth

    if gate_response := gate_redirect_after_auth(
        username,
        roles,
        auth_method=login_type + "_mfa",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    ):
        return gate_response

    # kibana_sid_cookie was created at password-auth time so the
    # plaintext password never needs to be stored in the session.

    # keep the allow-list aligned with the unified /login handler so
    # users who MFA from /dashboards/* land back there instead of being sent
    # to /assets/dashboards/* (which 404s).
    allowed_without_prefix = [
        "/dashboard",
        "/dashboards/",
        "/api/dashboards/",
        "/profile",
        "/signup",
        "/verify-email",
    ]
    needs_prefix = (
        USE_PREFIX and not next_url.startswith("/assets") and not any(next_url.startswith(p) for p in allowed_without_prefix)
    )
    if needs_prefix:
        next_url = "/assets" + next_url

    response = make_response(redirect(next_url))
    if kibana_sid_cookie:
        response.set_cookie(
            "sid",
            kibana_sid_cookie,
            secure=True,
            httponly=True,
            samesite="Lax",
            path="/kibana",
        )
    return response


# ============================================
# MFA RECOVERY CODES — display once
# ============================================


def _show_recovery_codes_then(method: str, return_to: str):
    """At first MFA enrollment, generate recovery codes (only if the user has
    none yet), stash the one-time plaintext in the session, and render the
    display screen. The user's "Continue" POST then runs the deferred completion.

    ``return_to`` is the completion context the display's continue handler
    replays: "login" → finish the pending login via _complete_mfa_login;
    "profile" → redirect back to /profile. ``method`` is carried so the login
    completion records the right mfa_method on audit/session.

    Re-enrollment (the user already has codes — e.g. switching TOTP↔YubiKey)
    must NOT clobber their existing unused codes, so in that case we skip the
    display and run the completion directly.
    """
    username = session.get("mfa_pending_username") if return_to == "login" else session.get("username", "")

    if MFAService.count_recovery_codes(username) > 0:
        return _finish_recovery_return(method, return_to)

    result = MFAService.generate_recovery_codes(username)
    if not result.get("success"):
        # Generation failed (transient ES blip). Don't block the user from
        # finishing enrollment over a non-critical convenience feature — they
        # can regenerate from Account Settings later.
        logging.error(f"MFAService: could not generate recovery codes for {username}: {result.get('error')}")
        return _finish_recovery_return(method, return_to)

    AuditLogService.log_event(
        username,
        "recovery_codes_generated",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    session["_recovery_codes_plaintext"] = result["plaintext"]
    session["_recovery_codes_method"] = method
    session["_recovery_codes_return_to"] = return_to
    return _mfa_redirect("mfa_recovery_codes_page")


def _finish_recovery_return(method: str, return_to: str):
    """Run the deferred post-enrollment completion for the given context."""
    if return_to == "profile":
        return _profile_redirect(success="mfa")
    if return_to == "wizard":
        # follow-up: onboarding wizard step-3 enrollment. The user is
        # already authenticated mid-wizard, so just advance to step 4 (the
        # wizard's own redirect target after MFA setup).
        return redirect(url_for("analytics.onboarding_step4_page"))
    return _complete_mfa_login(method)


@app.route("/mfa/recovery-codes", methods=["GET"])
def mfa_recovery_codes_page():
    """Render the one-time recovery-code display. Reads the staged plaintext
    from the session; if none is staged this was reached out of band, so bounce
    the user home."""
    codes = session.get("_recovery_codes_plaintext")
    if not codes:
        # No staged codes — either reached directly or already continued past.
        # For an authenticated user this is almost always the browser Back
        # button landing on the one-time page after Continue cleared the
        # plaintext. Render a 200 "already shown" state (with a link to
        # profile) rather than a 302 redirect: a 302 sitting in the back-stack
        # re-fires on every Back press and traps the user in a loop.
        if session.get("authenticated"):
            return render_template(
                "mfa_recovery_codes.html",
                recovery_codes=[],
                codes_consumed=True,
                profile_url=url_for("profile_page"),
                csrf_token="",
            )
        return redirect(url_for("analytics.login_page"))
    csrf_token = auth_module.generate_csrf_token()
    return render_template(
        "mfa_recovery_codes.html",
        recovery_codes=codes,
        codes_consumed=False,
        csrf_token=csrf_token,
    )


@app.route("/mfa/recovery-codes/continue", methods=["POST"])
def mfa_recovery_codes_continue():
    """User has acknowledged saving their codes. Clear the one-time plaintext
    from the session and run the deferred enrollment completion."""
    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return _mfa_redirect("mfa_recovery_codes_page")

    method = session.pop("_recovery_codes_method", "totp")
    return_to = session.pop("_recovery_codes_return_to", "login")
    session.pop("_recovery_codes_plaintext", None)
    return _finish_recovery_return(method, return_to)


@app.route("/mfa/challenge", methods=["POST"])
def mfa_challenge_post():
    """Handle MFA challenge submission."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return _mfa_redirect("mfa_challenge_page", error="Invalid request. Please try again.")

    username = session.get("mfa_pending_username")
    method = session.get("mfa_pending_method")
    code = request.form.get("code", "").strip()

    mfa_ok = False
    if method == "totp":
        mfa_ok = MFAService.validate_totp_code(username, code)
    elif method == "yubikey":
        record = MFAService.get_mfa_record(username)
        if record:
            mfa_ok = MFAService.validate_yubikey_otp(
                otp=code,
                expected_public_id=record.get("yubikey_public_id", ""),
                yubico_client_id=config.YUBICO_CLIENT_ID,
                yubico_secret_key=config.YUBICO_SECRET_KEY,
                allow_insecure_dev_mode=config.MFA_ALLOW_INSECURE_DEV,
            )

    if not mfa_ok:
        logging.warning(f"AUDIT: user={username} action=mfa_challenge success=False method={method} ip={request.remote_addr}")
        AuditLogService.log_event(
            username,
            "mfa_challenge",
            False,
            mfa_method=method,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            failure_reason="invalid_mfa_code",
        )
        auth_module.record_failed_attempt(username, surface="mfa")
        csrf_token = auth_module.generate_csrf_token()
        return render_template(
            "mfa_challenge.html",
            method=method,
            csrf_token=csrf_token,
            error_message="Invalid code. Please try again.",
        )

    logging.info(f"AUDIT: user={username} action=mfa_challenge success=True method={method} ip={request.remote_addr}")
    AuditLogService.log_event(
        username,
        "mfa_challenge",
        True,
        mfa_method=method,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    return _complete_mfa_login(method)


# ============================================
# MFA RECOVERY-CODE CHALLENGE
# ============================================


@app.route("/mfa/challenge/recovery", methods=["GET"])
def mfa_challenge_recovery_page():
    """Show the recovery-code entry form (reached via the link on the standard
    MFA challenge screen). Requires the same mfa_pending session as the primary
    challenge."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))
    csrf_token = auth_module.generate_csrf_token()
    return render_template(
        "mfa_challenge.html",
        method="recovery",
        csrf_token=csrf_token,
        error_message=request.args.get("error"),
    )


@app.route("/mfa/challenge/recovery", methods=["POST"])
def mfa_challenge_recovery_post():
    """Validate a single-use recovery code and, on success, complete login.

    Mirrors mfa_challenge_post's audit/lockout handling. On success the code is
    already consumed by consume_recovery_code; we surface the remaining count to
    the user via a session flag so the post-login banner can nudge regeneration.
    """
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return _mfa_redirect("mfa_challenge_recovery_page", error="Invalid request. Please try again.")

    username = session.get("mfa_pending_username")
    method = session.get("mfa_pending_method")
    code = request.form.get("code", "").strip()

    if not MFAService.consume_recovery_code(username, code):
        logging.warning(f"AUDIT: user={username} action=mfa_challenge_recovery success=False ip={request.remote_addr}")
        AuditLogService.log_event(
            username,
            "mfa_challenge",
            False,
            mfa_method="recovery_code",
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            failure_reason="invalid_recovery_code",
        )
        auth_module.record_failed_attempt(username, surface="mfa")
        csrf_token = auth_module.generate_csrf_token()
        return render_template(
            "mfa_challenge.html",
            method="recovery",
            csrf_token=csrf_token,
            error_message="Invalid recovery code. Please try again.",
        )

    remaining = MFAService.count_recovery_codes(username)
    logging.info(
        f"AUDIT: user={username} action=mfa_challenge_recovery success=True remaining={remaining} ip={request.remote_addr}"
    )
    AuditLogService.log_event(
        username,
        "recovery_code_used",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"remaining={remaining}",
    )
    # Carry the remaining count into the authenticated session so the user can
    # be nudged to regenerate (and re-secure their account) after login. The
    # original method is preserved so audit/session record the user's real
    # second factor, not "recovery".
    session["mfa_recovery_used_remaining"] = remaining
    return _complete_mfa_login(method or "totp")


# ============================================
# MFA EMAIL RECOVERY
# ============================================


@app.route("/mfa/challenge/email", methods=["GET"])
def mfa_challenge_email_page():
    """Show the 'send a recovery link to my email' page (reached from the MFA
    challenge screen). Requires the mfa_pending session, like the primary
    challenge. Surfaces whether a verified recovery email exists so the template
    can either offer the send or explain why it can't."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))
    username = session.get("mfa_pending_username")
    info = MFAService.get_recovery_email(username)
    csrf_token = auth_module.generate_csrf_token()
    return render_template(
        "mfa_challenge_email.html",
        has_verified_email=bool(info["email"]) and info["verified"],
        masked_email=_mask_email(info["email"]),
        sent=False,
        csrf_token=csrf_token,
        error_message=request.args.get("error"),
    )


@app.route("/mfa/challenge/email", methods=["POST"])
def mfa_challenge_email_post():
    """Send a recovery-login link to the user's verified recovery email.

    Gated on: mfa_pending session, a VERIFIED recovery email, and the 3/hour
    rate limit. Always renders the same 'if an account exists, a link was sent'
    confirmation regardless of outcome so the form can't be used to probe which
    accounts have a recovery email configured.
    """
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return _mfa_redirect("mfa_challenge_email_page", error="Invalid request. Please try again.")

    username = session.get("mfa_pending_username")
    info = MFAService.get_recovery_email(username)

    def _confirmation():
        # Neutral confirmation — identical whether or not a link was actually
        # sent (no recovery-email-existence oracle).
        token = auth_module.generate_csrf_token()
        return render_template(
            "mfa_challenge_email.html",
            has_verified_email=True,
            masked_email=_mask_email(info["email"]),
            sent=True,
            csrf_token=token,
            error_message=None,
        )

    if not (info["email"] and info["verified"]):
        logging.info(f"AUDIT: user={username} action=recovery_email_sent skipped=no_verified_email")
        return _confirmation()

    if not MFAService.recovery_email_rate_ok(username):
        logging.warning(f"AUDIT: user={username} action=recovery_email_rate_limited ip={request.remote_addr}")
        AuditLogService.log_event(
            username,
            "recovery_email_rate_limited",
            False,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )
        return _confirmation()

    issued = MfaRecoveryTokenService.generate_recovery_login_token(username, config.SECRET_KEY)
    MFAService.stage_recovery_login_jti(username, issued["jti"])
    MFAService.record_recovery_email_send(username)

    link = _external_mfa_url("mfa_recovery_email_consume", token=issued["token"])
    MfaRecoveryEmailService.send("recovery_login", info["email"], {"link": link})

    logging.info(f"AUDIT: user={username} action=recovery_email_sent ip={request.remote_addr}")
    AuditLogService.log_event(
        username,
        "recovery_email_sent",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    return _confirmation()


@app.route("/mfa/recovery/email/<token>", methods=["GET"])
def mfa_recovery_email_consume(token):
    """Consume a recovery-login link: verify the token + its single-use jti,
    disable MFA (force re-enroll), and establish an authenticated session.

    Reached from an email link, so there is NO mfa_pending session — the token
    (signature + 15-min exp + matching stored jti) is the sole proof. On success
    the user lands authenticated with a 'set up MFA again' banner.
    """
    result = MfaRecoveryTokenService.verify_recovery_login_token(token, config.SECRET_KEY)
    if not result["valid"]:
        return render_template("mfa_recovery_email_result.html", ok=False, message=result["error"])

    username = result["username"]
    # Single-use: consume_recovery_login matches + clears the stored jti AND
    # disables MFA in one write. A second click finds no jti and fails.
    if not MFAService.consume_recovery_login(username, result["jti"]):
        return render_template(
            "mfa_recovery_email_result.html",
            ok=False,
            message="This recovery link has already been used or is no longer valid.",
        )

    AuditLogService.log_event(
        username,
        "mfa_recovery_login",
        True,
        target_user=username,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    established = _establish_session_after_email_recovery(username)
    if not established:
        return render_template(
            "mfa_recovery_email_result.html",
            ok=False,
            message="We couldn't sign you in. Please try logging in directly.",
        )
    return established


def _establish_session_after_email_recovery(username: str):
    """Build an authenticated session for a user who proved control of their
    verified recovery email. MFA was just disabled, so this is the sole gate —
    equivalent to a fresh password+MFA login. Returns a redirect response, or
    None if the user can't be loaded.
    """
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return None
    try:
        record = dict(es.security.get_user(username=username)).get(username)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logging.error(f"MFA email recovery: get_user({username!r}) failed: {e}")
        return None
    if not record:
        return None

    session.clear()
    session["authenticated"] = True
    session["username"] = username
    session["roles"] = record.get("roles", [])
    session["full_name"] = record.get("full_name", "") or username
    session["email"] = record.get("email", "") or ""
    session["auth_timestamp"] = time.time()
    session["last_activity"] = time.time()
    session["auth_method"] = "mfa_email_recovery"
    # Surface a "you must set up MFA again" banner on the next profile view.
    session["mfa_recovery_email_used"] = True
    session.permanent = True

    AuditLogService.log_event(
        username,
        "login",
        True,
        auth_method="mfa_email_recovery",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    logging.info(f"AUDIT: user={username} action=login auth_method=mfa_email_recovery ip={request.remote_addr}")

    # Land on the profile security tile so the re-enroll banner is right there.
    target = "/profile"
    if USE_PREFIX:
        target = "/assets/profile"
    return redirect(target)


def _mask_email(email: str) -> str:
    """Mask a recovery email for display (a***@e***.com style) so the challenge
    page hints which address without disclosing it fully."""
    email = (email or "").strip()
    if "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    masked_local = (local[0] + "***") if local else "***"
    dot = domain.rfind(".")
    if dot > 0:
        masked_domain = domain[0] + "***" + domain[dot:]
    else:
        masked_domain = (domain[0] + "***") if domain else "***"
    return f"{masked_local}@{masked_domain}"


def _external_mfa_url(endpoint: str, **values) -> str:
    """Absolute URL for an emailed MFA link. Uses the configured app base URL
    when available (the email client has no Flask context), falling back to
    Flask's url_for with _external."""
    try:
        # nosemgrep: flask-url-for-external-true - emailed MFA link; host fixed by nginx
        return url_for(endpoint, _external=True, **values)
    except Exception:
        return url_for(endpoint, **values)


# ============================================
# MFA SETUP ROUTES (first-time enrollment)
# ============================================


@app.route("/mfa/setup", methods=["GET"])
def mfa_setup_page():
    """MFA setup landing — route to method picker, TOTP, or YubiKey enrollment."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))
    method = session.get("mfa_pending_method")
    if method == "totp":
        return _mfa_redirect("mfa_setup_totp_generate")
    elif method == "yubikey":
        csrf_token = auth_module.generate_csrf_token()
        error_message = request.args.get("error")
        return render_template("mfa_setup_yubikey.html", csrf_token=csrf_token, error_message=error_message)
    # No method chosen yet — user picks TOTP or YubiKey
    return _mfa_redirect("mfa_setup_choose")


@app.route("/mfa/setup/choose", methods=["GET"])
def mfa_setup_choose():
    """Ask the user to pick TOTP or YubiKey before starting enrollment."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))
    csrf_token = auth_module.generate_csrf_token()
    error_message = request.args.get("error")
    return render_template("mfa_setup_choose.html", csrf_token=csrf_token, error_message=error_message)


@app.route("/mfa/setup/choose", methods=["POST"])
def mfa_setup_choose_post():
    """Persist the user's MFA method choice, then route to the matching setup page."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return _mfa_redirect("mfa_setup_choose", error="Invalid request. Please try again.")

    method = request.form.get("method", "").strip()
    if method not in ("totp", "yubikey"):
        return _mfa_redirect("mfa_setup_choose", error="Please choose a method.")

    username = session.get("mfa_pending_username")
    result = MFAService.set_mfa_method(username, method)
    if not result.get("success"):
        return _mfa_redirect("mfa_setup_choose", error="Could not save your choice. Please try again.")

    session["mfa_pending_method"] = method
    return _mfa_redirect("mfa_setup_page")


@app.route("/mfa/setup/totp", methods=["GET"])
def mfa_setup_totp_generate():
    """Show the TOTP QR code, reusing any pending secret.

    A refresh/back-navigation must NOT mint a new secret — that would
    invalidate the QR the user already scanned and strand their
    authenticator entry."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))
    username = session.get("mfa_pending_username")
    result = MFAService.get_or_create_totp_setup(username)
    csrf_token = auth_module.generate_csrf_token()
    error_message = request.args.get("error")
    return render_template(
        "mfa_setup_totp.html",
        qr_code_b64=result["qr_code_b64"],
        totp_secret=result["totp_secret"],
        csrf_token=csrf_token,
        error_message=error_message,
    )


@app.route("/mfa/setup/totp/confirm", methods=["POST"])
def mfa_setup_totp_confirm():
    """Confirm TOTP enrollment by verifying the first code."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return _mfa_redirect("mfa_setup_totp_generate", error="Invalid request. Please try again.")

    username = session.get("mfa_pending_username")
    code = request.form.get("code", "").strip()

    ok = MFAService.verify_and_confirm_totp(username, code)
    if not ok:
        # setup failures used to be invisible — no audit event, and
        # every retry regenerated the secret (killing the QR the user had
        # already scanned). Log the failure and re-render the SAME secret.
        AuditLogService.log_event(
            username,
            "mfa_setup",
            False,
            mfa_method="totp",
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            failure_reason="invalid_mfa_code",
        )
        result = MFAService.get_or_create_totp_setup(username)
        csrf_token = auth_module.generate_csrf_token()
        return render_template(
            "mfa_setup_totp.html",
            qr_code_b64=result["qr_code_b64"],
            totp_secret=result["totp_secret"],
            csrf_token=csrf_token,
            error_message="Invalid code. Enter the current code from your authenticator app, or re-scan the QR code.",
        )

    logging.info(f"AUDIT: user={username} action=totp_setup_complete ip={request.remote_addr}")
    AuditLogService.log_event(
        username,
        "mfa_setup",
        True,
        mfa_method="totp",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    # The confirmation code just proved TOTP possession — skip the challenge page.
    # show one-time recovery codes before completing login.
    return _show_recovery_codes_then("totp", return_to="login")


@app.route("/mfa/setup/yubikey", methods=["POST"])
def mfa_setup_yubikey_register():
    """Register a YubiKey by validating its OTP and storing the public ID."""
    if not session.get("mfa_pending"):
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return _mfa_redirect("mfa_setup_page", error="Invalid request. Please try again.")

    username = session.get("mfa_pending_username")
    otp = request.form.get("otp", "").strip()

    result = MFAService.register_yubikey(
        username=username,
        otp=otp,
        yubico_client_id=config.YUBICO_CLIENT_ID,
        yubico_secret_key=config.YUBICO_SECRET_KEY,
        allow_insecure_dev_mode=config.MFA_ALLOW_INSECURE_DEV,
    )
    if not result["success"]:
        csrf_token = auth_module.generate_csrf_token()
        return render_template(
            "mfa_setup_yubikey.html",
            csrf_token=csrf_token,
            error_message=result["error"],
        )

    logging.info(f"AUDIT: user={username} action=yubikey_setup_complete ip={request.remote_addr}")
    # Successful registration validated the OTP against Yubico — skip the
    # challenge page. show one-time recovery codes first.
    return _show_recovery_codes_then("yubikey", return_to="login")


@app.route("/api/backup/create", methods=["POST"])
def api_backup_create():
    """Create a backup of all de_asset* indices."""
    try:
        user = get_current_user()
        if not user or not any(r in (user.get("roles") or []) for r in ("superuser", "asset_admin")):
            return jsonify({"error": "Forbidden"}), 403
        result = BackupService.create_backup()
        return jsonify({"ok": True, **result})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Backup failed",
            log_detail="api_backup_create failed",
            exc=e,
            body={"ok": False},
        )


@app.route("/api/backup/list", methods=["GET"])
def api_backup_list():
    """List available backup files."""
    try:
        user = get_current_user()
        if not user or not any(r in (user.get("roles") or []) for r in ("superuser", "asset_admin")):
            return jsonify({"error": "Forbidden"}), 403
        backups = BackupService.list_backups()
        return jsonify({"backups": backups})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to list backups",
            log_detail="api_backup_list failed",
            exc=e,
            body={},
        )


@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    """Restore de_asset* indices from a backup file."""
    try:
        user = get_current_user()
        if not user or not any(r in (user.get("roles") or []) for r in ("superuser", "asset_admin")):
            return jsonify({"error": "Forbidden"}), 403
        data = request.get_json(silent=True) or {}
        filename = data.get("filename", "").strip()
        if not filename:
            return jsonify({"ok": False, "error": "filename is required"}), 400
        result = BackupService.restore_backup(filename)
        return jsonify({"ok": True, **result})
    except (InvalidRequestError, RequestedItemNotFoundError) as e:
        # BackupService raises these with operator-facing text
        # ("Invalid backup filename", "Backup file not found"); any other
        # ValueError now reaches the generic handler instead of the caller.
        return jsonify({"ok": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Restore failed",
            log_detail="api_backup_restore failed",
            exc=e,
            body={"ok": False},
        )


@app.errorhandler(400)
def bad_request(error):
    """Return JSON for API 400s instead of Flask's HTML error page.

    several validation helpers (``_require_memberships_in_workspace_scope``
    , ``_validate_single_workspace_for_non_superuser``) reject
    bad input via ``abort(400, description=...)``. Flask renders that as an HTML
    page, so an API caller's ``response.json()`` throws "Unexpected token '<'"
    and the real, actionable message ("Could not resolve membership(s) to a
    workspace: ...") is lost. For the API surface, surface the description as a
    JSON ``error`` (the shape the user-management JS already reads); non-API
    requests keep the default HTML page.
    """
    if (
        request.path.startswith("/api/")
        or request.path.startswith("/dashboard-builder/api/")
        or request.path.startswith("/restore-backup/api/")
    ):
        # ``error.description`` carries the abort() message; HTTPException
        # defaults it to a generic string when none was supplied.
        desc = getattr(error, "description", None) or "Bad request"
        return jsonify({"success": False, "error": desc}), 400
    return error


@app.errorhandler(403)
def forbidden(error):
    """Handle forbidden access.

    For authenticated page requests, redirect to the first allowed page
    instead of showing a generic error. API requests still get a 403 JSON.
    """
    # API requests keep their 403. The ``/api/`` prefix covers top-level
    # routes; ``/dashboard-builder/api/`` covers the builder blueprint's
    # JSON endpoints, whose per-dashboard authorization needs
    # to surface as a real 403 instead of a friendly redirect.
    # ``/restore-backup/api/`` covers the snapshot + config-apply endpoints
    #.
    if (
        request.path.startswith("/api/")
        or request.path.startswith("/dashboard-builder/api/")
        or request.path.startswith("/restore-backup/api/")
    ):
        return jsonify({"error": "Forbidden"}), 403

    # Authenticated users get redirected to a page they can access
    if "authenticated" in session:
        return _redirect_to_allowed_page()

    return (
        render_template(
            "error.html",
            error_code=403,
            error_title="Access Denied",
            error_message="Your account needs the 'asset_admin' or 'superuser' role to access this application.",
        ),
        403,
    )


@app.errorhandler(429)
def too_many_requests(error):
    """Handle rate limiting."""
    return (
        render_template(
            "error.html",
            error_code=429,
            error_title="Too Many Attempts",
            error_message="Too many failed login attempts. Please try again later.",
        ),
        429,
    )


# ============================================
# REGISTRATION ADMIN ROUTES (authenticated)
# ============================================


@app.route("/api/admin/registrations")
def api_list_registrations():
    """List pending registrations for admin review."""
    _require_cluster_admin()  # pending applicants' PII is platform-only
    status_filter = request.args.get("status", None)
    registrations = RegistrationService.get_registrations(status=status_filter)
    return jsonify({"success": True, "registrations": registrations})


@app.route("/api/admin/registrations/<registration_id>/approve", methods=["POST"])
def api_approve_registration(registration_id):
    """Approve a pending registration and create the ES user."""
    _require_cluster_admin()

    data = request.get_json(silent=True) or {}
    roles = data.get("roles", [])
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        return jsonify({"success": False, "error": "roles must be a list of role names"}), 400
    # the roles come from the request body and become ES Security
    # roles verbatim; only a superuser may hand out a platform role.
    if {"superuser", "asset_admin"} & set(roles) and "superuser" not in session.get("roles", []):
        return jsonify({"success": False, "error": "Only a superuser can grant platform roles."}), 403

    current_user = get_current_user()
    admin_username = current_user.get("username", "unknown")

    result = RegistrationService.approve_registration(registration_id, admin_username, roles)

    if result["success"]:
        AuditLogService.log_event(
            admin_username,
            "registration_approved",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            target_user=registration_id,
            detail=f"roles={roles}",
        )

    return jsonify(result)


@app.route("/api/admin/registrations/<registration_id>/reject", methods=["POST"])
def api_reject_registration(registration_id):
    """Reject a pending registration."""
    _require_cluster_admin()

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    # Cap reason length — stored in ES, included in audit log detail.
    if len(reason) > 500:
        return jsonify({"success": False, "error": "Reason must be 500 characters or fewer."}), 400

    current_user = get_current_user()
    admin_username = current_user.get("username", "unknown")

    result = RegistrationService.reject_registration(registration_id, admin_username, reason)

    if result["success"]:
        AuditLogService.log_event(
            admin_username,
            "registration_rejected",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            target_user=registration_id,
            detail=f"reason={reason}",
        )

    return jsonify(result)


# ============================================
# FEEDBACK / SYSTEM SETTINGS API
# ============================================


@app.route("/api/feedback/check")
def api_feedback_check():
    """Return whether the feedback system is configured and available."""
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"available": False})

    email = FeedbackService.get_feedback_email(es)
    return jsonify({"available": bool(email)})


_ALLOWED_FEEDBACK_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".pdf"}
_MAX_FEEDBACK_BYTES = 5 * 1024 * 1024  # 5 MB


@app.route("/api/feedback/submit", methods=["POST"])
def api_feedback_submit():
    """Accept a user feedback submission, create, and send email."""
    from lib.es_helpers import get_elasticsearch_client

    # Support both FormData and JSON submissions
    if request.content_type and "multipart/form-data" in request.content_type:
        data = request.form.to_dict()
    else:
        data = request.get_json(silent=True) or {}

    # Validate required fields + length caps. The HTML form caps each field
    # via maxlength, but a scripted POST can bypass that — enforce here too.
    subject = (data.get("subject") or "").strip()
    description = (data.get("description") or "").strip()
    steps = (data.get("steps") or "").strip()
    if not subject or not description:
        return jsonify({"success": False, "error": "Subject and description are required"}), 400
    if len(subject) > 200:
        return jsonify({"success": False, "error": "Subject must be 200 characters or fewer."}), 400
    if len(description) > 5000:
        return jsonify({"success": False, "error": "Description must be 5000 characters or fewer."}), 400
    if len(steps) > 2000:
        return jsonify({"success": False, "error": "Steps must be 2000 characters or fewer."}), 400

    fb_type = data.get("type", "bug")
    if fb_type not in ("bug", "feature"):
        fb_type = "bug"

    priority = data.get("priority", "medium")
    if priority not in ("low", "medium", "high"):
        priority = "medium"

    # Handle file upload
    attachment_data = None
    attachment_filename = None
    file = request.files.get("attachment")
    if file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in _ALLOWED_FEEDBACK_EXTENSIONS:
            return jsonify({"success": False, "error": "Attachment must be a PNG, JPG, GIF, or PDF file."}), 400
        attachment_data = file.read()
        if len(attachment_data) > _MAX_FEEDBACK_BYTES:
            return jsonify({"success": False, "error": "Attachment exceeds the 5 MB size limit."}), 400
        attachment_filename = file.filename

    current_user = get_current_user()
    feedback_data = {
        "type": fb_type,
        "subject": subject,
        "description": description,
        "steps": steps,
        "priority": priority,
        "user_name": current_user.get("full_name") or current_user.get("username", "Unknown"),
        "user_email": current_user.get("email", ""),
    }
    if attachment_data:
        feedback_data["attachment_data"] = attachment_data
        feedback_data["attachment_filename"] = attachment_filename

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "Database unavailable"}), 503

    # Create if configured
    jira_result = None
    if config.JIRA_ENABLED:
        jira_result = FeedbackService.create_jira_issue(feedback_data, config)
        if jira_result.get("success"):
            feedback_data["jira_issue_url"] = jira_result["issue_url"]
        else:
            logging.warning("Feedback: creation failed: %s", jira_result.get("error"))

    result = FeedbackService.send_feedback_email(es, feedback_data)
    status_code = 200 if result.get("success") else 500

    if jira_result and jira_result.get("success"):
        result["jira_issue_key"] = jira_result["issue_key"]
        result["jira_issue_url"] = jira_result["issue_url"]

    return jsonify(result), status_code


@app.route("/api/admin/settings", methods=["GET"])
def api_get_settings():
    """Return system settings (superuser only)."""
    _require_superuser()

    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"feedback_email": ""})

    email = FeedbackService.get_feedback_email(es) or ""
    return jsonify({"feedback_email": email})


@app.route("/api/admin/settings", methods=["POST"])
def api_save_settings():
    """Save system settings (superuser only)."""
    _require_superuser()

    from lib.es_helpers import get_elasticsearch_client

    data = request.get_json(silent=True) or {}
    feedback_email = (data.get("feedback_email") or "").strip()

    current_user = get_current_user()
    username = current_user.get("username", "unknown")

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "Database unavailable"}), 503

    result = FeedbackService.save_feedback_email(es, feedback_email, username)
    return jsonify(result)


# ============================================
# Grants management (superuser-only)
# ============================================
#
# Per-user data-grant CRUD on top of the model. UI surface for
# what bin/python/tools/bootstrap_data_grants.py does in bulk: pick a
# user, see their grants, add/remove individual grants. granted_by is
# auto-filled from the logged-in superuser's session so audit-log
# attribution is accurate (NOT "system_migration" — that's reserved
# for the bootstrap actor).


@app.route("/grants/", methods=["GET"])
def grants_page():
    """Render the grants management page (superuser only).

    Lists every ES Security user with a non-trivial role and lets a
    superuser select one to view/edit their de_authz_data_grant docs.
    The page is server-rendered HTML with a small JS client that hits
    the /api/admin/grants endpoints below. Keeping the JS out of a
    framework — this is admin-only, used by a handful of staff, the
    extra build complexity of a SPA is not worth it for this surface.
    """
    _require_superuser()
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()

    # surface only superuser usernames. Data grants exist as
    # a cluster-operator audit-tracked mechanism; non-superusers gain
    # workspace access via memberships, not grants. earlier the
    # picker dangled every ES Security user as an option, half of
    # which would 400 on submit. ES built-in service users are still
    # excluded (they don't log into DEAM at all). Filter mirrors
    # bin/python/tools/bootstrap_data_grants.py:80-88.
    usernames: list[str] = []
    if es is not None:
        try:
            users = es.security.get_user()
            _builtin = {
                "elastic",
                "kibana_system",
                "logstash_system",
                "beats_system",
                "apm_system",
                "remote_monitoring_user",
            }
            usernames = sorted(
                username
                for username, info in users.items()
                if username not in _builtin and "superuser" in (info.get("roles", []) or [])
            )
        except Exception as e:
            logging.error("grants_page: user list failed: %s", e)
    orgs: list[dict] = []
    teams: list[dict] = []
    # workspaces are the third grant scope. Resolved at query
    # time in _grant_scoped_titles via the workspace→orgs→teams join, so
    # a single grant covers every team currently in the workspace.
    workspaces: list[dict] = []
    if es is not None:
        try:
            org_resp = es.search(
                index="de_asset_organization",
                body={"query": {"match_all": {}}, "_source": ["organization_title"], "size": 1000},
            )
            orgs = [
                {"esid": hit["_id"], "title": hit["_source"].get("organization_title", "<no-title>")}
                for hit in org_resp.get("hits", {}).get("hits", [])
            ]
            orgs.sort(key=lambda o: o["title"].lower())
        except Exception as e:
            logging.error("grants_page: org list failed: %s", e)
        try:
            team_resp = es.search(
                index="de_asset_teams",
                body={"query": {"match_all": {}}, "_source": ["team_title"], "size": 5000},
            )
            teams = [
                {"esid": hit["_id"], "title": hit["_source"].get("team_title", "<no-title>")}
                for hit in team_resp.get("hits", {}).get("hits", [])
            ]
            teams.sort(key=lambda t: t["title"].lower())
        except Exception as e:
            logging.error("grants_page: team list failed: %s", e)
        try:
            ws_resp = es.search(
                index="de_asset_workspace",
                body={"query": {"match_all": {}}, "_source": ["workspace_title"], "size": 1000},
            )
            workspaces = [
                {"esid": hit["_id"], "title": hit["_source"].get("workspace_title", "<no-title>")}
                for hit in ws_resp.get("hits", {}).get("hits", [])
            ]
            workspaces.sort(key=lambda w: w["title"].lower())
        except Exception as e:
            # index_not_found on clusters that haven't bootstrapped the
            # workspace index yet (earlier environments) — degrade to
            # an empty list so the picker still renders for org/team.
            err_text = str(e).lower()
            if "index_not_found" not in err_text and "404" not in err_text:
                logging.error("grants_page: workspace list failed: %s", e)

    # Optional ?username= deep-link from the User Management edit modal.
    # Only honored if the value is in the rendered usernames list so a
    # crafted query string can't widen the picker's scope.
    requested_user = (request.args.get("username") or "").strip()
    selected_username = requested_user if requested_user in usernames else ""

    return render_template(
        "grants.html",
        usernames=usernames,
        orgs=orgs,
        teams=teams,
        workspaces=workspaces,
        selected_username=selected_username,
    )


@app.route("/api/admin/grants", methods=["GET"])
def api_list_grants():
    """List de_authz_data_grant docs for a single user.

    Query: ``?username=<u>``. Returns ``{grants: [...]}`` with each
    grant's username, scope_type, scope_esid, granted_by, granted_at,
    expires_at, reason. Empty list if the user has no grants.
    Decorates each grant with the resolved scope title so the UI can
    show "Acme Corp" instead of the raw ESID.
    """
    _require_superuser()
    try:
        from web.services.data_grant_service import DataGrantService
    except ImportError:
        from .services.data_grant_service import DataGrantService

    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"error": "username query param required"}), 400

    grants = DataGrantService.list_for_user(username)

    # Decorate with scope title for UI display.
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is not None and grants:
        org_esids = [grant["scope_esid"] for grant in grants if grant.get("scope_type") == "org"]
        team_esids = [grant["scope_esid"] for grant in grants if grant.get("scope_type") == "team"]
        # workspace-scoped grants — decorate with workspace_title
        # so the Edit Roles modal renders a meaningful label.
        workspace_esids = [grant["scope_esid"] for grant in grants if grant.get("scope_type") == "workspace"]
        title_by_esid: dict[str, str] = {}
        if org_esids:
            try:
                resp = es.search(
                    index="de_asset_organization",
                    body={
                        "query": {"ids": {"values": org_esids}},
                        "_source": ["organization_title"],
                        "size": len(org_esids),
                    },
                )
                for hit in resp.get("hits", {}).get("hits", []):
                    title_by_esid[hit["_id"]] = hit["_source"].get("organization_title", "<no-title>")
            except Exception as e:
                logging.warning("api_list_grants: org title lookup failed: %s", e)
        if team_esids:
            try:
                resp = es.search(
                    index="de_asset_teams",
                    body={
                        "query": {"ids": {"values": team_esids}},
                        "_source": ["team_title"],
                        "size": len(team_esids),
                    },
                )
                for hit in resp.get("hits", {}).get("hits", []):
                    title_by_esid[hit["_id"]] = hit["_source"].get("team_title", "<no-title>")
            except Exception as e:
                logging.warning("api_list_grants: team title lookup failed: %s", e)
        if workspace_esids:
            try:
                resp = es.search(
                    index="de_asset_workspace",
                    body={
                        "query": {"ids": {"values": workspace_esids}},
                        "_source": ["workspace_title"],
                        "size": len(workspace_esids),
                    },
                )
                for hit in resp.get("hits", {}).get("hits", []):
                    title_by_esid[hit["_id"]] = hit["_source"].get("workspace_title", "<no-title>")
            except Exception as e:
                logging.warning("api_list_grants: workspace title lookup failed: %s", e)
        for grant in grants:
            grant["scope_title"] = title_by_esid.get(grant.get("scope_esid"), "<missing>")

    return jsonify({"grants": grants})


@app.route("/api/admin/grants", methods=["POST"])
def api_create_grant():
    """Create a de_authz_data_grant doc. granted_by is the logged-in
    superuser; the bootstrap actor 'system_migration' is reserved for
    bin/python/tools/bootstrap_data_grants.py runs."""
    _require_superuser()
    try:
        from web.services.data_grant_service import DataGrantService, VALID_SCOPE_TYPES
    except ImportError:
        from .services.data_grant_service import DataGrantService, VALID_SCOPE_TYPES

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    scope_type = (data.get("scope_type") or "").strip()
    scope_esid = (data.get("scope_esid") or "").strip()
    reason = (data.get("reason") or "").strip()
    expires_at = data.get("expires_at")  # Optional ISO string or None.

    if not username:
        return jsonify({"error": "username required"}), 400
    if scope_type not in VALID_SCOPE_TYPES:
        return jsonify({"error": f"scope_type must be one of {list(VALID_SCOPE_TYPES)}"}), 400
    if not scope_esid:
        return jsonify({"error": "scope_esid required"}), 400
    if not reason:
        return jsonify({"error": "reason required (audit trail)"}), 400

    # data grants exist only as a cross-workspace audit-tracked
    # mechanism for cluster operators. Reject non-superuser targets at
    # the route boundary so the operator gets a clear 400 (matching
    # error wording the UI surfaces verbatim). Service-level check at
    # DataGrantService.create is defense-in-depth.
    from lib.es_helpers import get_elasticsearch_client as _get_es_5

    _es_6 = _get_es_5()
    if _es_6 is not None:
        try:
            _target_2 = dict(_es_6.security.get_user(username=username)).get(username, {})
        except Exception as _e:
            # User-not-found surfaces here too; treat the same as "not
            # a superuser" so the operator sees a clear rejection
            # rather than ES error noise.
            logging.warning("target lookup failed for %s: %s", username, _e)
            _target_2 = {}
        if "superuser" not in (_target_2.get("roles") or []):
            return (
                jsonify(
                    {
                        "error": (
                            f"Data grants can only target superusers. {username!r} is not "
                            f"a superuser; use the Edit Roles flow to grant workspace/org/"
                            f"team memberships instead."
                        ),
                    }
                ),
                400,
            )

    current_user = get_current_user()
    granted_by = current_user.get("username", "unknown_superuser")

    created = DataGrantService.create(
        username=username,
        scope_type=scope_type,
        scope_esid=scope_esid,
        granted_by=granted_by,
        reason=reason,
        expires_at=expires_at,
    )
    return jsonify({"created": created, "username": username, "scope_type": scope_type, "scope_esid": scope_esid})


@app.route("/api/admin/grants/<username>/<scope_type>/<scope_esid>", methods=["DELETE"])
def api_revoke_grant(username, scope_type, scope_esid):
    """Revoke a de_authz_data_grant doc."""
    _require_superuser()
    try:
        from web.services.data_grant_service import DataGrantService, VALID_SCOPE_TYPES
    except ImportError:
        from .services.data_grant_service import DataGrantService, VALID_SCOPE_TYPES

    if scope_type not in VALID_SCOPE_TYPES:
        return jsonify({"error": f"scope_type must be one of {list(VALID_SCOPE_TYPES)}"}), 400

    current_user = get_current_user()
    revoked_by = current_user.get("username", "unknown_superuser")

    deleted = DataGrantService.revoke(
        username=username,
        scope_type=scope_type,
        scope_esid=scope_esid,
        revoked_by=revoked_by,
    )
    return jsonify({"deleted": deleted, "username": username, "scope_type": scope_type, "scope_esid": scope_esid})


# ============================================
# Maintenance alert (header banner)
# ============================================


@app.route("/api/maintenance-alert", methods=["GET"])
def api_maintenance_alert_get():
    """Return the current alert payload for the header banner.

    Readable by any authenticated user. The response is always shaped the
    same so the banner JS can render unconditionally; ``published`` is the
    only field the client checks before showing.
    """
    if not session.get("authenticated"):
        return jsonify({"published": False, "message": "", "version": 0}), 401

    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    alert = MaintenanceAlertService.get_alert(es)
    return jsonify(
        {
            "published": alert["published"],
            "message": alert["message"],
            "version": alert["version"],
        }
    )


@app.route("/api/maintenance-alert/publish", methods=["POST"])
def api_maintenance_alert_publish():
    """Publish or update the alert message (superuser only)."""
    _require_superuser()

    from lib.es_helpers import get_elasticsearch_client

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "Message text is required."}), 400
    if len(message) > 500:
        return jsonify({"success": False, "error": "Message must be 500 characters or fewer."}), 400

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "Database unavailable"}), 503

    username = (get_current_user() or {}).get("username", "unknown")
    result = MaintenanceAlertService.publish(es, message, username)
    status = 200 if result.get("success") else 500
    return jsonify(result), status


@app.route("/api/maintenance-alert/hide", methods=["POST"])
def api_maintenance_alert_hide():
    """Hide the alert for all users (superuser only). Keeps the message."""
    _require_superuser()

    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "Database unavailable"}), 503

    username = (get_current_user() or {}).get("username", "unknown")
    result = MaintenanceAlertService.hide(es, username)
    status = 200 if result.get("success") else 500
    return jsonify(result), status


# ============================================
# MAIN ENTRY POINT
# ============================================

if __name__ == "__main__":
    app.run(host=config.APP_HOST, port=config.APP_PORT, debug=False)
