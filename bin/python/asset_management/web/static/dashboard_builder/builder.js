/**
 * builder.js — Main Dashboard Builder controller
 * Manages DASHBOARD_STATE, data view selection, and dashboard CRUD.
 *
 * DASHBOARD_BASE is injected by the Jinja template so API calls
 * include the correct /assets/ nginx prefix automatically.
 */

// ══════════════════════════════════════════════════════════════
// State
// ══════════════════════════════════════════════════════════════

const DASHBOARD_STATE = {
  id: null,
  title: '',
  menu: 'Reports',
  // teams-only visibility. Every dashboard is ``show_teams``;
  // visibility hinges on the ticked teams. ``allowed_org_esids`` is always
  // empty (org tier dropped from the editor). A new dashboard seeds its
  // teams from the server-rendered defaults (the creator's own teams).
  visibility_mode: 'show_teams',
  allowed_org_esids: [],
  allowed_team_esids: [],
  global_date_range: null,
  // dashboard-level filter controls (top-of-dashboard value
  // pickers). Distinct from per-chart filters under each chart's
  // ``filters`` key; managed by dashboard_filters.js.
  filters: [],
  // author-curated viewer date-basis choices: [{field, label}],
  // first = default. When non-empty, the viewer shows a "Date basis" selector
  // that re-scopes the global time range on the chosen field (Echo-focused).
  time_field_choices: [],
  charts: {}   // keyed by chart_id
};

// follow-up: the explorer SELECTS canvas documents (title/menu/
// visibility editing through the ordinary properties panel) instead of
// navigating away; content editing goes through the Canvas Editor
// explicitly (row ✎ button / right-click / properties-panel button).
// True while the currently selected doc is a canvas page.
let _activeCanvasDoc = false;

// The Canvas Editor is open to every builder user; the
// "+ Canvas" topbar button's presence stays the client signal for the
// explorer/properties entry points (auth.py's mc_dashboard_builder prefix
// rule is the real gate server-side).
function _canOpenCanvasEditor() {
  return !!document.getElementById('btn-canvas');
}

function openCanvasEditorForCurrent() {
  if (!DASHBOARD_STATE.id) return;
  window.location.href = DASHBOARD_BASE + 'canvas?id=' + encodeURIComponent(DASHBOARD_STATE.id);
}

// follow-up: "+ Canvas page" opens the Create New Dashboard dialog
// first, so the user names the page and picks its menu up front (prefilled
// from the open dashboard's menu when one is open — no dashboard needs to be
// open any more). Both carry into the canvas editor as ?title= and ?menu=;
// the editor's save sends them, replacing the old fall-back to the
// workspace's first menu. adds the built-in theme picker to the
// same dialog; the choice rides along as ?theme= and becomes the new
// page's canvas_config.page.theme.
async function openCanvasCreate() {
  const suggested = await _nextDashboardName('New Canvas Page');
  const defaultMenu = (DASHBOARD_STATE.menu || '').trim() || undefined;
  const chosen = await _promptDashboardName(suggested, defaultMenu, { withTheme: true });
  if (!chosen) return; // cancelled
  window.location.href = DASHBOARD_BASE + 'canvas?menu=' + encodeURIComponent(chosen.menu)
    + '&title=' + encodeURIComponent(chosen.name)
    + '&theme=' + encodeURIComponent(chosen.theme || 'classic');
}

// built-in theme library for the picker (fetched once per page
// load from /api/canvas-themes, the /api/canvas-icons pattern).
let _canvasThemes = null;
async function _fetchCanvasThemes() {
  if (_canvasThemes) return _canvasThemes;
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/canvas-themes');
    const data = await res.json();
    if (data && data.success && Array.isArray(data.themes) && data.themes.length) {
      _canvasThemes = data.themes;
    }
  } catch (e) { /* fall through to the classic-only fallback below */ }
  // Fallback: the picker still works (and the editor treats any unknown
  // key as classic anyway), it just can't preview the library.
  if (!_canvasThemes) _canvasThemes = [{ key: 'classic', name: 'Classic', swatch: {} }];
  return _canvasThemes;
}

// Render one swatch card per theme into the Create dialog's theme row.
// Selection is tracked on the container via data-theme.
function _renderThemePicker(row, themes) {
  row.innerHTML = '';
  if (!row.dataset.theme) row.dataset.theme = 'classic';
  themes.forEach(t => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'db-theme-card' + (row.dataset.theme === t.key ? ' active' : '');
    const sw = t.swatch || {};
    const strip = document.createElement('span');
    strip.className = 'db-theme-swatch';
    const parts = [sw.paper, sw.accent, sw.header_bg].concat((sw.series || []).slice(0, 3));
    parts.filter(Boolean).forEach(color => {
      const seg = document.createElement('span');
      seg.style.background = color;
      strip.appendChild(seg);
    });
    const name = document.createElement('span');
    name.className = 'db-theme-card-name';
    name.textContent = t.name || t.key;
    card.appendChild(strip);
    card.appendChild(name);
    card.addEventListener('click', () => {
      row.dataset.theme = t.key;
      row.querySelectorAll('.db-theme-card').forEach(el => el.classList.remove('active'));
      card.classList.add('active');
    });
    row.appendChild(card);
  });
}

// server-rendered visibility defaults for new dashboards.
// ``index.html`` injects window.BUILDER_VISIBILITY_DEFAULTS:
//   { show_all_allowed, default_mode, default_org_esids, default_team_esids }
// Visibility is now teams-only — ``default_mode`` is always ``show_teams`` and
// ``default_org_esids`` is always empty (the dropdown + org list are gone).
// ``default_team_esids`` pre-selects the creator's own teams. This helper
// returns a safe, normalized copy so callers can ``.slice()`` without a null
// check; it forces ``show_teams`` + empty orgs regardless of the injection so
// a stale/missing payload can never reintroduce show_all.
function _builderVisibilityDefaults() {
  const d = window.BUILDER_VISIBILITY_DEFAULTS || {};
  return {
    show_all_allowed: false,
    default_mode: 'show_teams',
    default_org_esids: [],
    default_team_esids: Array.isArray(d.default_team_esids) ? d.default_team_esids : [],
  };
}

// Track the currently selected chart id
let SELECTED_CHART_ID = null;

// Dirty state — true when there are unsaved changes to the current dashboard
let _isDirty = false;

// Default field configurations for the preset map statistics.
// key must match the value sent to /api/map-data as ?statistic=<key>.
const MAP_STAT_DEFAULTS = [
  // DT stats target the team-keyed `*_disciple_tools` pattern (the
  // retired cross-tenant `disciple-tools` alias is gone). the pull
  // importer fetches meta.baptism_generation and
  // meta.quick_button_contact_established (mapped long, omitted when 0/unset),
  // so multiplier_contacts and spiritual_conversations aggregate again.
  { key: 'app_installs',           label: 'App installs',           data_view: '*fb_insights',        field: 'insight_actions_mobile_app_install', aggregation: 'sum'   },
  { key: 'baptisms',               label: 'Baptisms',               data_view: '*_disciple_tools',    field: 'meta.baptism_date',                        aggregation: 'count' },
  { key: 'dt_contacts',            label: 'Disciple Tool Contacts', data_view: '*_disciple_tools',    field: '',                                         aggregation: 'count' },
  { key: 'messages',               label: 'Messages received',      data_view: '*echo_conversations', field: 'incomingMessagesCount',                    aggregation: 'sum'   },
  { key: 'multiplier_contacts',    label: 'Multiplier contacts',    data_view: '*_disciple_tools',    field: 'meta.baptism_generation',                  aggregation: 'count' },
  { key: 'page_visits',            label: 'Page visits',            data_view: '*fb_insights',        field: 'insight_actions_page_engagement',           aggregation: 'sum'   },
  { key: 'reach',                  label: 'Reach',                  data_view: '*fb_insights',        field: 'insight_reach',                            aggregation: 'sum'   },
  { key: 'salvation',              label: 'Salvation decisions',    data_view: '*_disciple_tools',    field: 'meta.faith_status',                        aggregation: 'count' },
  { key: 'spiritual_conversations',label: 'Spiritual conversations',data_view: '*_disciple_tools',    field: 'meta.quick_button_contact_established',    aggregation: 'sum'   },
  { key: 'video_views',            label: 'Video views',            data_view: '*fb_insights',        field: 'insight_actions_video_view_total',aggregation: 'sum'   },
];

function _markDirty() {
  _isDirty = true;
  _updateSaveBtn();
}

function _markClean() {
  _isDirty = false;
  _updateSaveBtn();
}

function _updateSaveBtn() {
  // Enabled only when a dashboard has been created/loaded (has an id)
  const hasDash = !!DASHBOARD_STATE.id;
  const saveBtn = document.getElementById('btn-save');
  if (saveBtn) saveBtn.disabled = !hasDash;
  const dupBtn = document.getElementById('btn-duplicate');
  if (dupBtn) dupBtn.disabled = !hasDash;
  // "Save as template" only meaningful once a dashboard exists.
  // Button only present in the DOM for workspace admins (gated by the
  // {% if is_workspace_admin %} block in index.html).
  const tplBtn = document.getElementById('btn-save-as-template');
  if (tplBtn) tplBtn.disabled = !hasDash;
}

// ══════════════════════════════════════════════════════════════
// Initialisation
// ══════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  // builder.js also loads on the Canvas Editor page (which embeds
  // the table config panel via populateTablePanel). Only the full builder
  // page has the explorer/left panel; everywhere else this init must not run.
  if (!document.getElementById('db-panel-left')) return;
  _ensureGtDataViews();  // pre-warm data view cache
  loadDashboardList();
  updateCanvasEmptyState();
  _initBuilderTimeRange();
  _initBuilderOrgTeamFilters();
  _initLeftPanelCollapse();
  _initShowHiddenToggle();
  _initStdDateFormatOptions();  // populate line/bar Data Format dropdown

  // When a metric chart re-renders, its breakdown bucket keys may have
  // changed (new breakdown field, new data). If that chart's config panel
  // is open, refresh the per-tile label editor so it lists the current
  // buckets. charts.js dispatches 'chart-rendered' after every render;
  // we only act when it's the selected chart to avoid needless work.
  document.addEventListener('chart-rendered', (e) => {
    const renderedId = e.detail && e.detail.chartId;
    if (renderedId && renderedId === SELECTED_CHART_ID
        && typeof _renderMetricBreakdownLabelsEditor === 'function') {
      _renderMetricBreakdownLabelsEditor(renderedId);
    }
  });
});

// populate the bar/line "Data Format" dropdown (#cfg-std-date-format,
// inside #standard-field-section) from the same DATE_FORMAT_OPTIONS list the
// table date columns use, so the two are always in sync. Runs once at init; the
// dropdown's visibility (date X only) is toggled by onStandardXFieldChange /
// populateConfigPanel. DATE_FORMAT_OPTIONS[0] is the blank "— select format —"
// option, which for a chart means "no custom format → raw bucket key".
function _initStdDateFormatOptions() {
  const sel = document.getElementById('cfg-std-date-format');
  if (!sel || typeof DATE_FORMAT_OPTIONS === 'undefined') return;
  sel.innerHTML = DATE_FORMAT_OPTIONS
    .map(o => `<option value="${escHtml(o.value)}">${escHtml(o.label)}</option>`)
    .join('');
}

// ── Left sidebar collapse / expand ──────────────────────────────────────────
// Whole panel collapses to a 32px rail; only the toggle chevron remains
// visible. State persists in localStorage so the choice survives reloads.
// Expanded by default — first-time users need to see the dashboard explorer
// and chart-creation entry points at least once.

const _LEFT_PANEL_LS_KEY = 'db-left-panel-collapsed';

function _initLeftPanelCollapse() {
  const panel = document.getElementById('db-panel-left');
  if (!panel) return;
  let collapsed = false;
  try {
    collapsed = localStorage.getItem(_LEFT_PANEL_LS_KEY) === '1';
  } catch (_) {
    // localStorage can throw in private browsing modes; fall back to default.
  }
  _applyLeftPanelCollapsed(panel, collapsed);
}

function _applyLeftPanelCollapsed(panel, collapsed) {
  panel.classList.toggle('is-collapsed', collapsed);
  const toggle = document.getElementById('db-panel-left-toggle');
  if (toggle) {
    toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    toggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
    toggle.setAttribute('title', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
  }
}

function toggleLeftPanel() {
  const panel = document.getElementById('db-panel-left');
  if (!panel) return;
  const collapsed = !panel.classList.contains('is-collapsed');
  _applyLeftPanelCollapsed(panel, collapsed);
  try {
    localStorage.setItem(_LEFT_PANEL_LS_KEY, collapsed ? '1' : '0');
  } catch (_) {
    // private browsing — silently drop persistence; the toggle still works
    // for the current session.
  }
}

// ── Unsaved-changes guard on tab close / refresh / navigation away ──────────
// Fires only when there are pending edits. In-page navigation between
// dashboards is already guarded by _confirmUnsaved in newDashboard,
// selectTemplate, and _explorerLoadDashboard — this handler covers the
// leave-the-page leak (close tab, refresh, browser back, ⌘W).
//
// Modern browsers ignore the returned string and show their own generic
// dialog ("Changes you made may not be saved"). Setting event.returnValue
// is what actually triggers the prompt; the return-string covers older
// Safari fallthroughs. Both are required for cross-browser coverage.
window.addEventListener('beforeunload', e => {
  if (!_isDirty) return;
  e.preventDefault();
  e.returnValue = '';
  return '';
});

// ── Builder time range selector ───────────────────────────────────────────────
// Sets window.DASHBOARD_TIME_RANGE so charts.js picks it up in _withExtraFilters.

function _applyBuilderTimeRange(from, to) {
  window.DASHBOARD_TIME_RANGE = from ? { from, to: to || 'now' } : {};
  // Re-render every chart in the current dashboard
  Object.keys(DASHBOARD_STATE.charts).forEach(id => {
    if (typeof queryAndRender === 'function') queryAndRender(id);
  });
  _reloadCanvasPreview(); // a selected canvas page has no charts
}

// === Time-range URL persistence ===========================================
// Encode the active range as URL query params so the builder is
// bookmark- and share-able, and so "Open in new tab" / Duplicate Tab
// preserve the window. Two shapes:
//
//     ?range=now-30d/d         (any preset)
//     ?range=custom&from=YYYY-MM-DD&to=YYYY-MM-DD
//
// Default ("Last 90 days" / now-90d/d) leaves the URL clean — no params.
// Mirrors the equivalent helpers on the dashboards page.

// the cal:* calendar presets join the allowlist — keep
// this set in sync with TIME_PRESETS in time_preset_service.py.
const _BUILDER_KNOWN_PRESETS = ['', 'now-7d/d', 'now-30d/d', 'now-90d/d', 'now-6M/d', 'now-1y/d',
  'cal:this_month', 'cal:last_month', 'cal:this_quarter', 'cal:last_quarter'];
const _BUILDER_ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

// convert a cal:* sentinel to an explicit {from, to} ISO
// window; the server date-math can't express calendar anchoring. Mirrors
// _calendarPresetRange in dashboards.html (same UTC reference rules —
// SERVER_TODAY when the page provides it, else client-local today).
function _builderCalendarPresetRange(sentinel) {
  let ref;
  if (window.SERVER_TODAY && _BUILDER_ISO_DATE.test(window.SERVER_TODAY)) {
    const p = window.SERVER_TODAY.split('-');
    ref = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
  } else {
    const d = new Date();
    ref = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  }
  const iso = (x) => x.getUTCFullYear() + '-'
    + String(x.getUTCMonth() + 1).padStart(2, '0') + '-'
    + String(x.getUTCDate()).padStart(2, '0');
  const y = ref.getUTCFullYear();
  const m = ref.getUTCMonth();
  if (sentinel === 'cal:this_month') {
    return { from: iso(new Date(Date.UTC(y, m, 1))), to: iso(ref) };
  }
  if (sentinel === 'cal:last_month') {
    return { from: iso(new Date(Date.UTC(y, m - 1, 1))), to: iso(new Date(Date.UTC(y, m, 0))) };
  }
  const qStartMonth = Math.floor(m / 3) * 3;
  if (sentinel === 'cal:this_quarter') {
    return { from: iso(new Date(Date.UTC(y, qStartMonth, 1))), to: iso(ref) };
  }
  if (sentinel === 'cal:last_quarter') {
    return { from: iso(new Date(Date.UTC(y, qStartMonth - 3, 1))), to: iso(new Date(Date.UTC(y, qStartMonth, 0))) };
  }
  return null;
}

function _writeBuilderTimeRangeToUrl() {
  const url = new URL(window.location.href);
  const sel = document.getElementById('builder-time-preset');
  if (!sel) return;
  const preset = sel.value;
  if (preset === 'custom') {
    const f = (document.getElementById('builder-time-from') || {}).value || '';
    const t = (document.getElementById('builder-time-to') || {}).value || '';
    if (f && t && _BUILDER_ISO_DATE.test(f) && _BUILDER_ISO_DATE.test(t) && f <= t) {
      url.searchParams.set('range', 'custom');
      url.searchParams.set('from', f);
      url.searchParams.set('to', t);
    } else {
      url.searchParams.delete('range');
      url.searchParams.delete('from');
      url.searchParams.delete('to');
    }
  } else if (preset && preset !== 'now-90d/d') {
    url.searchParams.set('range', preset);
    url.searchParams.delete('start');
    url.searchParams.delete('from');
    url.searchParams.delete('to');
  } else {
    url.searchParams.delete('range');
    url.searchParams.delete('start');
    url.searchParams.delete('from');
    url.searchParams.delete('to');
  }
  history.replaceState(null, '', url);
}

// Read URL params and return what the initial DASHBOARD_TIME_RANGE +
// dropdown / date-input values should be. Returns null when the URL has
// no recognised range param, so callers fall back to defaults.
function _readBuilderTimeRangeFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const range = params.get('range');
  if (!range) return null;
  if (range === 'custom') {
    const f = params.get('from') || '';
    const t = params.get('to') || '';
    if (!_BUILDER_ISO_DATE.test(f) || !_BUILDER_ISO_DATE.test(t) || f > t) return null;
    return { kind: 'custom', from: f, to: t };
  }
  if (!_BUILDER_KNOWN_PRESETS.includes(range)) return null;
  return { kind: 'preset', preset: range };
}

function _initBuilderTimeRange() {
  // Default: Last 90 days (matches selected option)
  window.DASHBOARD_TIME_RANGE = { from: 'now-90d/d', to: 'now' };

  const preset  = document.getElementById('builder-time-preset');
  const custom  = document.getElementById('builder-custom-date-range');
  const fromEl  = document.getElementById('builder-time-from');
  const toEl    = document.getElementById('builder-time-to');
  const applyBtn = document.getElementById('builder-time-apply');

  // Apply URL-persisted range BEFORE any chart query fires so charts
  // render straight to the requested window without flickering
  // through the default.
  const fromUrl = _readBuilderTimeRangeFromUrl();
  if (fromUrl) {
    if (fromUrl.kind === 'custom') {
      preset.value = 'custom';
      if (fromEl) fromEl.value = fromUrl.from;
      if (toEl)   toEl.value   = fromUrl.to;
      window.DASHBOARD_TIME_RANGE = { from: fromUrl.from, to: fromUrl.to };
    } else {
      preset.value = fromUrl.preset;
      // calendar sentinels resolve to an explicit ISO window.
      const cal = _builderCalendarPresetRange(fromUrl.preset);
      window.DASHBOARD_TIME_RANGE = cal
        ? { from: cal.from, to: cal.to }
        : { from: fromUrl.preset, to: fromUrl.preset ? 'now' : '' };
    }
  }

  // Toggle the `is-custom-open` class on the filter row so CSS can
  // hide the redundant "Time range:" label and divider when custom is
  // active — frees the ~330 px the from/to/Apply inputs need so the
  // filter row stays compact instead of wrapping.
  const rightCluster = preset.closest('.db-topbar-filters');

  preset.addEventListener('change', () => {
    const val = preset.value;
    if (val === 'custom') {
      custom.style.display = 'flex';
      if (rightCluster) rightCluster.classList.add('is-custom-open');
      // Don't write URL yet — wait for Apply with valid dates so the
      // params we put on the URL always describe a complete range.
    } else {
      custom.style.display = 'none';
      if (rightCluster) rightCluster.classList.remove('is-custom-open');
      // calendar sentinels resolve to an explicit ISO window.
      const cal = _builderCalendarPresetRange(val);
      if (cal) _applyBuilderTimeRange(cal.from, cal.to);
      else _applyBuilderTimeRange(val, val ? 'now' : '');
      _writeBuilderTimeRangeToUrl();
    }
  });

  // Custom-range guards (mirrors on the dashboards page):
  // - end date can't be before start date
  // - neither date can be in the future (capped to server-rendered UTC today,
  //   falling back to client local today)
  // - Apply stays disabled while the range is empty or invalid
  // When one side is picked the other side's min/max is clamped so the
  // browser greys out invalid days in the picker.
  function _todayValue() {
    if (window.SERVER_TODAY) return window.SERVER_TODAY;
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  // Hard cap both inputs to today so the picker never offers future days.
  const today = _todayValue();
  if (fromEl) fromEl.max = today;
  if (toEl)   toEl.max   = today;

  function _validate() {
    const f = fromEl.value;
    const t = toEl.value;
    const today = _todayValue();
    // NOTE: do NOT write min/max onto the inputs here. This handler fires on
    // every `input` event — i.e. on each digit as the user types the year —
    // and re-clamping the *other* input's min/max mid-keystroke makes the
    // browser reject a partially-typed year (e.g. "2" parsed as year 0002 >
    // max), which shows up as "the year field only accepts one digit" and
    // blocks entering old dates. Validity is checked by comparing the values
    // below and toggling the button; the static future-date cap set once at
    // init is enough.
    const invalid = (f && f > today) || (t && t > today) || (f && t && f > t);
    const blocked = invalid || !f || !t;
    // Inline opacity/cursor used to live here, but .db-btn-primary:disabled
    // and the generic .db-btn:disabled rule in builder.css now handle the
    // washed-out look + not-allowed cursor consistently.
    if (applyBtn) applyBtn.disabled = blocked;
    return !invalid;
  }

  if (fromEl) {
    fromEl.addEventListener('input', _validate);
    fromEl.addEventListener('change', _validate);
  }
  if (toEl) {
    toEl.addEventListener('input', _validate);
    toEl.addEventListener('change', _validate);
  }
  // Run once so the button starts in the disabled state until both sides
  // have valid values.
  _validate();

  // Sync the date-input visibility AND re-validate, three times, to
  // cover every form-restoration timing:
  //
  //   1. Immediately (catches the URL-driven path where the
  //      _readBuilderTimeRangeFromUrl block above set
  //      preset.value = 'custom').
  //   2. On `pageshow` (catches Duplicate Tab + bfcache restoration
  //      — Chrome restores form values *after* DOMContentLoaded, so
  //      a first-pass sync would see the default value and miss the
  //      restored "custom" selection).
  //   3. On `load` (defensive fallback for older browsers).
  //
  // Re-running _validate on each event matters because the initial
  // _validate() above ran when fromEl.value / toEl.value were still
  // empty (browser hasn't restored yet) — leaving the Apply button
  // stuck disabled even after Chrome later filled in the dates.
  // Idempotent: each call just normalises visibility, the
  // is-custom-open class, and the Apply button's disabled state
  // against current input values.
  function _syncBuilderCustomRangeVisibility() {
    const isCustom = (preset.value === 'custom');
    custom.style.display = isCustom ? 'flex' : 'none';
    if (rightCluster) rightCluster.classList.toggle('is-custom-open', isCustom);
    if (isCustom) _validate();
  }
  _syncBuilderCustomRangeVisibility();
  window.addEventListener('pageshow', _syncBuilderCustomRangeVisibility);
  window.addEventListener('load', _syncBuilderCustomRangeVisibility);

  applyBtn.addEventListener('click', () => {
    const f = fromEl.value;
    const t = toEl.value;
    if (!f || !t) return;
    if (!_validate()) return;
    if (f > t) return;  // defence in depth — validator should have caught this
    // Send plain yyyy-MM-dd values (no time component). Many indices map
    // their date field as `yyyy-MM-dd` strict and reject ISO datetime
    // strings, which is why the dashboards page also stripped the time
    // component.
    _applyBuilderTimeRange(f, t);
    _writeBuilderTimeRangeToUrl();
  });
}

// ── Builder org/team filters ─────────────────────────────────────────────────
// Sets window.DASHBOARD_EXTRA_FILTERS so charts.js applies them in queries.

function _applyBuilderOrgTeamFilters() {
  const orgSelect  = document.getElementById('builder-org-filter');
  const teamSelect = document.getElementById('builder-team-filter');
  const orgVal  = orgSelect  ? orgSelect.value  : '';
  const teamVal = teamSelect ? teamSelect.value : '';

  const filters = [];
  // Some data views (e.g. Disciple Tools) don't have an organization_title
  // field, so filtering by org directly returns 0 hits for those views.
  // Instead, resolve the org to all available teams and filter by team_title.
  if (teamVal) {
    filters.push({ field: 'team_title', op: 'match_phrase', value: teamVal });
  } else if (orgVal && teamSelect) {
    // Org selected with "All Teams" — add all teams from the dropdown as an OR filter
    const availableTeams = Array.from(teamSelect.options)
      .filter(opt => opt.value !== '')
      .map(opt => opt.value);
    if (availableTeams.length > 0) {
      filters.push({ field: 'team_title', op: 'in', value: availableTeams });
    } else {
      // Fallback: no teams loaded yet, use org filter directly
      filters.push({ field: 'organization_title', op: 'match_phrase', value: orgVal });
    }
  }

  window.DASHBOARD_EXTRA_FILTERS = filters.length ? filters : undefined;

  // Re-render every chart in the current dashboard
  Object.keys(DASHBOARD_STATE.charts).forEach(id => {
    if (typeof queryAndRender === 'function') queryAndRender(id);
  });
  _reloadCanvasPreview(); // a selected canvas page has no charts
}

async function _loadBuilderFilterOptions(orgFilter) {
  const orgSelect  = document.getElementById('builder-org-filter');
  const teamSelect = document.getElementById('builder-team-filter');
  if (!orgSelect && !teamSelect) return;

  const prevOrg  = orgSelect  ? orgSelect.value  : '';
  const prevTeam = teamSelect ? teamSelect.value : '';

  try {
    // DASHBOARD_BASE is e.g. "/assets/dashboard-builder/" — go up one level to reach app root
    const appBase = DASHBOARD_BASE.replace(/dashboard-builder\/?$/, '');
    let url = appBase + 'api/dashboards/filter-options';
    // scope the org/team options to the selected workspace so a
    // multi-workspace user sees only that workspace's orgs/teams.
    const params = [];
    if (orgFilter) params.push('org=' + encodeURIComponent(orgFilter));
    const wsEsid = _currentWorkspaceEsid();
    if (wsEsid) params.push('workspace_esid=' + encodeURIComponent(wsEsid));
    if (params.length) url += '?' + params.join('&');

    const resp = await fetch(url);
    const data = await resp.json();
    if (!data.success) return;

    if (orgSelect) {
      orgSelect.innerHTML = '<option value="">All Organizations</option>';
      (data.organizations || []).forEach(org => {
        const opt = document.createElement('option');
        opt.value = org.title;
        opt.textContent = org.title;
        orgSelect.appendChild(opt);
      });
      if (prevOrg && orgSelect.querySelector('option[value="' + CSS.escape(prevOrg) + '"]')) {
        orgSelect.value = prevOrg;
      }
    }

    if (teamSelect) {
      teamSelect.innerHTML = '<option value="">All Teams</option>';
      (data.teams || []).forEach(team => {
        const opt = document.createElement('option');
        opt.value = team.title;
        opt.textContent = team.title;
        teamSelect.appendChild(opt);
      });
      if (prevTeam && teamSelect.querySelector('option[value="' + CSS.escape(prevTeam) + '"]')) {
        teamSelect.value = prevTeam;
      }
    }
  } catch (err) {
    console.error('Error loading builder filter options:', err);
  }
}

// the workspace the user is currently acting in. Drives the org/team
// dropdown scoping AND the new-dashboard create-stamp so a multi-workspace
// user (superuser) can't accidentally write into the wrong workspace.
// Reads the topbar selector when present (multi-workspace), else the
// read-only single-workspace label, else the server-rendered default.
function _currentWorkspaceEsid() {
  const sel = document.getElementById('builder-workspace-select');
  if (sel && sel.value) return sel.value;
  const label = document.getElementById('builder-workspace-label');
  if (label && label.dataset.esid) return label.dataset.esid;
  return window.BUILDER_DEFAULT_WORKSPACE || '';
}

function _initBuilderOrgTeamFilters() {
  const orgSelect  = document.getElementById('builder-org-filter');
  const teamSelect = document.getElementById('builder-team-filter');
  const wsSelect   = document.getElementById('builder-workspace-select');

  // switching workspace re-scopes the org/team dropdowns to that
  // workspace and clears the current org/team selection (the prior org may
  // not exist in the new workspace).
  if (wsSelect) {
    wsSelect.addEventListener('change', async () => {
      if (orgSelect) orgSelect.value = '';
      if (teamSelect) teamSelect.value = '';
      // invalidate the per-workspace org/team-options cache so the
      // properties picker refetches for the newly-selected workspace.
      _orgTeamOptionsCache = {};
      await _loadBuilderFilterOptions();
      _applyBuilderOrgTeamFilters();
      // Re-scope the explorer list to the newly-selected workspace.
      if (typeof loadDashboardList === 'function') await loadDashboardList();
    });
  }

  if (orgSelect) {
    orgSelect.addEventListener('change', async () => {
      // Refetch teams scoped to selected org
      await _loadBuilderFilterOptions(orgSelect.value || undefined);
      _applyBuilderOrgTeamFilters();
    });
  }

  if (teamSelect) {
    teamSelect.addEventListener('change', () => {
      _applyBuilderOrgTeamFilters();
    });
  }

  // Initial load of filter options
  _loadBuilderFilterOptions();
}

// ══════════════════════════════════════════════════════════════
// Data view helpers (shared by standard charts, table, map, GT)
// ══════════════════════════════════════════════════════════════

function _populateDataViewSelect(selectEl) {
  if (!selectEl) return;
  const current = selectEl.value;
  selectEl.innerHTML = '<option value="">— Select data view —</option>';
  (_gtDataViews || []).forEach(dv => {
    const opt = document.createElement('option');
    opt.value = dv.id;
    opt.textContent = dv.label;
    selectEl.appendChild(opt);
  });
  if (current) selectEl.value = current;
}

// ══════════════════════════════════════════════════════════════
// Canvas management
// ══════════════════════════════════════════════════════════════

function addChartToCanvas(chartType, existingConfig) {
  if (!DASHBOARD_STATE.id && !existingConfig) {
    showToast('Create a new dashboard first', true);
    return;
  }
  const chartId = existingConfig ? existingConfig.chart_id : 'c' + Date.now();

  let defaultConfig;
  if (chartType === 'grouped_table') {
    defaultConfig = {
      chart_id: chartId,
      title: chartTypeLabel(chartType),
      position: { col: 0, row: 0, w: 6, h: 4 },
      chart_type: 'grouped_table',
      width: null,
      height: null,
      table_title: '',
      title_bg_color: '#2c6da0',
      group_header_bg_color: '#5a9cc5',
      col_header_bg_color: '#2c6da0',
      header_text_color: '#ffffff',
      group_text_color: '#ffffff',
      group_font_size: '12',
      group_bold: true,
      col_text_color: '#ffffff',
      col_font_size: '12',
      col_bold: true,
      groups: [],
    };
  } else if (chartType === 'leaflet_map') {
    defaultConfig = {
      chart_id: chartId,
      title: chartTypeLabel(chartType),
      position: { col: 0, row: 0, w: 6, h: 4 },
      chart_type: 'leaflet_map',
      map_statistic: 'reach',
      map_statistics: MAP_STAT_DEFAULTS.map(s => ({...s})),
      width: null,
      height: null,
      hide_title: true,
    };
  } else if (chartType === 'profile') {
    // Profile card — fixed-field Instagram account display. Data view is
    // auto-locked to the IG insights pattern; there are no x/y bindings to
    // configure, so the right panel only exposes title/properties.
    defaultConfig = {
      chart_id: chartId,
      title: chartTypeLabel(chartType),
      position: { col: 0, row: 0, w: 4, h: 4 },
      chart_type: 'profile',
      data_view: '*_ig_insights',
      width: null,
      height: null,
      hide_title: true,
      filters: [],
    };
  } else if (chartType === 'posts_table') {
    // Recent FB Page Posts — raw-hits table (one row per post). Data view
    // auto-locked to the fb_page_posts pattern; default columns mirror the
    // backend _POSTS_TABLE_DEFAULT_COLUMNS so the panel renders immediately.
    defaultConfig = {
      chart_id: chartId,
      title: chartTypeLabel(chartType),
      position: { col: 0, row: 0, w: 12, h: 5 },
      chart_type: 'posts_table',
      data_view: '*_fb_page_posts',
      num_values: 25,
      table_columns: [
        // Date ships pre-formatted (MMM DD, YYYY) so it renders "May 29,
        // 2026" not the raw ISO timestamp; editable in the column editor.
        { field: 'post_created_time', label: 'Date', aggregation: 'date', date_format: 'MMM DD, YYYY' },
        { field: 'post_type', label: 'Type' },
        { field: 'post_message', label: 'Post' },
        { field: 'post_reactions_total', label: 'Reactions' },
        { field: 'post_comments', label: 'Comments' },
        { field: 'post_shares', label: 'Shares' },
        // views / viewers replace impressions + engaged users,
        // which Meta retired; mirrors _POSTS_TABLE_DEFAULT_COLUMNS.
        { field: 'post_media_view', label: 'Views' },
        { field: 'post_total_media_view_unique', label: 'Viewers' },
      ],
      width: null,
      height: null,
      hide_title: true,
      table_font_size: 12,
      filters: [],
    };
  } else if (chartType === 'canva') {
    defaultConfig = {
      chart_id: chartId,
      title: 'Summary Report',
      position: { col: 0, row: 0, w: 12, h: 8 },
      chart_type: 'canva',
      canva_template: 'summary_report',
      width: '100%',
      height: null,
    };
  } else if (chartType === 'report') {
    defaultConfig = {
      chart_id: chartId,
      title: 'Impact Report',
      position: { col: 0, row: 0, w: 12, h: 10 },
      chart_type: 'report',
      org_name: '',
      width: '100%',
      height: null,
    };
  } else if (chartType === 'combined_report') {
    defaultConfig = {
      chart_id: chartId,
      title: 'Combined Impact Report',
      position: { col: 0, row: 0, w: 12, h: 10 },
      chart_type: 'combined_report',
      org_name: '',
      width: '100%',
      height: null,
    };
  } else if (chartType === 'google_report') {
    defaultConfig = {
      chart_id: chartId,
      title: 'Google Ads Impact Report',
      position: { col: 0, row: 0, w: 12, h: 10 },
      chart_type: 'google_report',
      org_name: '',
      width: '100%',
      height: null,
    };
  } else {
    defaultConfig = {
      chart_id: chartId,
      title: chartTypeLabel(chartType),
      position: { col: 0, row: 0, w: 6, h: 4 },
      data_view: '',
      chart_type: chartType,
      x_field: null,
      x_field_type: null,
      y_fields: [],
      aggregation: 'sum',
      breakdown: null,
      max_columns: 1,
      max_rows: 1,
      other_bucket: false,
      date_format: '',
      y_min: '',
      y_max: '',
      width: null,
      height: null,
      hide_title: true,
      // table-specific display defaults (no-op for non-table types).
      // Existing tables with no table_font_size are treated as 12px (the
      // legacy 14px from .gt-table CSS is overridden by the renderer
      // fallback) — matches "change all existing tables to 12" so the
      // dashboard set adopts a consistent denser look without a migration.
      table_font_size: 12,
      cell_gradient_enabled: false,
      cell_gradient_color: '#006d2c',
      filters: [],
      display_toggles: {},
    };
  }

  const config = existingConfig || defaultConfig;

  DASHBOARD_STATE.charts[chartId] = config;
  renderChartSlot(chartId, config);
  updateCanvasEmptyState();
  _markDirty();

  if (typeof initSortable === 'function') initSortable();

  return chartId;
}

// clamp a saved slot height to the visible canvas so a runaway value
// can't strand a slot off-screen. A bad resize used to ratchet the saved height
// up (each Save re-captured the ballooned style.height): once it exceeded the
// viewport, the bottom-right resize handle sat below the fold and the slot could
// no longer be dragged shorter — looking like "can't resize" until the doc was
// hand-patched. Capping on load lets such a value self-heal to one screenful.
// Returns the original string unchanged when it's within bounds (or unparseable).
function _clampSavedSlotHeight(heightStr) {
  const px = parseFloat(heightStr);
  if (!isFinite(px)) return heightStr;
  const canvas = document.getElementById('db-canvas');
  // Leave headroom so the handle stays comfortably on-screen, not flush at the edge.
  const maxPx = canvas ? canvas.clientHeight - 32 : 0;
  if (maxPx > 0 && px > maxPx) return maxPx + 'px';
  return heightStr;
}

function renderChartSlot(chartId, config) {
  const grid = document.getElementById('chart-grid');
  let slot = document.getElementById(`slot-${chartId}`);

  if (!slot) {
    slot = document.createElement('div');
    slot.className = `chart-slot chart-type-${config.chart_type}`;
    slot.id = `slot-${chartId}`;
    slot.dataset.chartId = chartId;
    slot.addEventListener('click', () => selectChart(chartId));
    grid.appendChild(slot);
    // Restore saved dimensions (set only on creation; resize events update style directly)
    if (config.width)  slot.style.width  = config.width;
    if (config.height) slot.style.height = _clampSavedSlotHeight(config.height);
  }

  if (config.chart_type === 'canva') {
    // Canva sizes to content like grouped_table
    slot.style.height = '';
    slot.style.minHeight = '';
    slot.innerHTML = `
      <button class="gt-remove-btn" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      <div class="chart-render-area" id="chart-${escHtml(chartId)}" style="padding:0;"></div>
      <div class="chart-resize-handle" title="Drag to resize"></div>
    `;
    _initResizeHandle(slot);
    if (typeof renderCanvaChart === 'function') renderCanvaChart(chartId);
    return;
  }

  if (config.chart_type === 'report' || config.chart_type === 'google_report' || config.chart_type === 'combined_report') {
    slot.style.height = '';
    slot.style.minHeight = '';
    slot.innerHTML = `
      <button class="gt-remove-btn" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      <div class="chart-render-area" id="chart-${escHtml(chartId)}" style="padding:0;"></div>
      <div class="chart-resize-handle" title="Drag to resize"></div>
    `;
    _initResizeHandle(slot);
    if (typeof renderReportChart === 'function') renderReportChart(chartId);
    return;
  }

  if (config.chart_type === 'leaflet_map') {
    slot.innerHTML = `
      <button class="gt-remove-btn" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      <div class="chart-slot-header">
        <span class="chart-title">${escHtml(config.title)}</span>
        <button class="btn-remove" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      </div>
      <div class="chart-render-area" id="chart-${escHtml(chartId)}" style="min-height:400px;"></div>
      <div class="chart-resize-handle" title="Drag to resize"></div>
    `;
    _initResizeHandle(slot);
    applyHideTitleToSlot(chartId);
    if (typeof requeryLeafletMap === 'function') requeryLeafletMap(chartId);
    return;
  }

  if (config.chart_type === 'profile') {
    slot.innerHTML = `
      <button class="gt-remove-btn" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      <div class="chart-slot-header">
        <span class="chart-title">${escHtml(config.title)}</span>
        <button class="btn-remove" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      </div>
      <div class="chart-render-area" id="chart-${escHtml(chartId)}"></div>
      <div class="chart-resize-handle" title="Drag to resize"></div>
    `;
    _initResizeHandle(slot);
    applyHideTitleToSlot(chartId);
    if (typeof queryAndRender === 'function') queryAndRender(chartId);
    return;
  }

  if (config.chart_type === 'grouped_table') {
    // Grouped table sizes to content — clear any saved/inline height
    slot.style.height = '';
    slot.style.minHeight = '';
    // Grouped table: no slot header — the table title row serves as the heading
    slot.innerHTML = `
      <button class="gt-remove-btn" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      <div class="chart-render-area" id="chart-${escHtml(chartId)}" style="padding:0;"></div>
      <div class="chart-resize-handle" title="Drag to resize"></div>
    `;
    _initResizeHandle(slot);
    if (config.groups && config.groups.length > 0) {
      if (typeof queryAndRender === 'function') queryAndRender(chartId);
    } else {
      const area = document.getElementById(`chart-${chartId}`);
      if (area) area.innerHTML = '<p style="padding:24px;text-align:center;color:#aaa;font-size:13px;">Click to configure — add groups &amp; columns in the right panel.</p>';
    }
    return;
  }

  if (config.chart_type === 'posts_table') {
    // Posts table — same slot chrome as a regular table, but it's a
    // raw-hits panel (no x_field). Renders as soon as it has a data view
    // + columns (the default config ships both, so it renders on add).
    slot.innerHTML = `
      <button class="gt-remove-btn" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      <div class="chart-slot-header">
        <span class="chart-title">${escHtml(config.title)}</span>
        <button class="btn-remove" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      </div>
      <div class="chart-render-area" id="chart-${escHtml(chartId)}"></div>
      <div class="chart-slot-footer">
        <button class="db-btn db-btn-sm" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); selectChart(this.dataset.chartId, 'properties')">Properties</button>
      </div>
      <div class="chart-resize-handle" title="Drag to resize"></div>
    `;
    _initResizeHandle(slot);
    applyHideTitleToSlot(chartId);
    const hasPostsData = config.data_view && config.table_columns && config.table_columns.length > 0;
    if (hasPostsData && typeof queryAndRender === 'function') queryAndRender(chartId);
    return;
  }

  if (config.chart_type === 'table') {
    // Table: no canvas drop zones — fields are configured in the right panel.
    // Footer buttons (Edit/Properties) are the discoverability affordance for
    // the right-panel config — acceptance criteria.
    slot.innerHTML = `
      <button class="gt-remove-btn" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      <div class="chart-slot-header">
        <span class="chart-title">${escHtml(config.title)}</span>
        <button class="btn-remove" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
      </div>
      <div class="chart-render-area" id="chart-${escHtml(chartId)}"></div>
      <div class="chart-legend" id="legend-${escHtml(chartId)}" style="display:none;"></div>
      <div class="chart-slot-footer">
        <button class="db-btn db-btn-sm" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); selectChart(this.dataset.chartId, 'edit')">Edit</button>
        <button class="db-btn db-btn-sm" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); selectChart(this.dataset.chartId, 'properties')">Properties</button>
      </div>
      <div class="chart-resize-handle" title="Drag to resize"></div>
    `;
    _initResizeHandle(slot);
    applyHideTitleToSlot(chartId);
    const hasTableData = config.data_view && config.x_field &&
      ((config.y_fields && config.y_fields.length > 0) || (config.table_columns && config.table_columns.length > 0));
    if (hasTableData) {
      if (typeof queryAndRender === 'function') queryAndRender(chartId);
    }
    return;
  }

  // Normalize legacy single y_field → y_fields array
  if (!config.y_fields) {
    config.y_fields = config.y_field ? [config.y_field] : [];
  }

  // Footer buttons (Edit/Properties) make the right-panel config discoverable
  // acceptance criteria. follow-up: the absolute
  // ``.gt-remove-btn`` is always reachable on hover regardless of the
  // ``hide_title`` setting on the chart-slot-header (which by default
  // hides the inline ``.btn-remove``). Same pattern as canva/report/
  // grouped_table slots above.
  slot.innerHTML = `
    <button class="gt-remove-btn" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
    <div class="chart-slot-header">
      <span class="chart-title">${escHtml(config.title)}</span>
      <button class="btn-remove" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); removeChart(this.dataset.chartId)" title="Remove">✕</button>
    </div>
    <div class="chart-render-area" id="chart-${escHtml(chartId)}"></div>
    <div class="chart-legend" id="legend-${escHtml(chartId)}" style="display:none;"></div>
    <div class="chart-slot-footer">
      <button class="db-btn db-btn-sm" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); selectChart(this.dataset.chartId, 'edit')">Edit</button>
      <button class="db-btn db-btn-sm" data-chart-id="${escHtml(chartId)}" onclick="event.stopPropagation(); selectChart(this.dataset.chartId, 'properties')">Properties</button>
    </div>
    <div class="chart-resize-handle" title="Drag to resize"></div>
  `;

  _initResizeHandle(slot);
  applyHideTitleToSlot(chartId);

  const hasYFields = config.y_fields && config.y_fields.length > 0;
  const hasYAndView = hasYFields && config.data_view;
  const canRender = (config.chart_type === 'trend_line' && config.sources && config.sources.length > 0)
    || (config.chart_type === 'metric' && config.data_view && (hasYFields || config.breakdown))
    || (config.chart_type !== 'metric' && config.x_field && hasYAndView);
  if (canRender) {
    if (typeof queryAndRender === 'function') queryAndRender(chartId);
  }
}

// Always-visible drag handle for reordering chart slots. The original
// SortableJS handle was the .chart-slot-header (title bar), which is
// hidden whenever hide_title is set (the default) — leaving title-hidden
// charts with no way to grab. This adds a small, always-present handle to
// every slot regardless of title visibility. Idempotent: guarded so
// re-rendering a slot doesn't stack duplicate handles. Called from
// _initResizeHandle, the one seam every chart-type branch hits.
function _addDragHandle(slotEl) {
  if (!slotEl || slotEl.querySelector('.chart-drag-handle')) return;
  const handle = document.createElement('div');
  handle.className = 'chart-drag-handle';
  handle.title = 'Drag to reorder';
  // Six-dot grip rendered via CSS; aria-hidden — reordering is a
  // pointer affordance, not a keyboard control here.
  handle.setAttribute('aria-hidden', 'true');
  // Don't let a click on the handle select/deselect the chart.
  handle.addEventListener('click', e => e.stopPropagation());
  slotEl.appendChild(handle);
}

function _initResizeHandle(slotEl) {
  _addDragHandle(slotEl);
  const handle = slotEl.querySelector('.chart-resize-handle');
  if (!handle) return;

  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    e.stopPropagation();

    const startX = e.clientX;
    const startY = e.clientY;
    const rect   = slotEl.getBoundingClientRect();
    const startW = rect.width;
    const startH = rect.height;

    // per-type minimum size, matching the CSS floors. Metric
    // scorecards (label + number only) shrink smaller than tables/charts;
    // everything else keeps the 260x220 floor. Keep these in sync with the
    // .chart-slot / .chart-slot.chart-type-metric rules in builder.css.
    const chartId = slotEl.dataset.chartId;
    const cfg = (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE.charts[chartId]) || {};
    const isMetric = cfg.chart_type === 'metric';
    const minW = isMetric ? 140 : 260;
    const minH = isMetric ? 90 : 220;

    // suppress text selection while dragging. Without this, dragging
    // the handle up over a scrollable table render-area (overflow:auto) lets the
    // pointer grab the table content as a selection/scroll instead of resizing.
    const prevUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = 'none';

    const onMove = ev => {
      const newW = Math.max(minW, startW + (ev.clientX - startX));
      const newH = Math.max(minH, startH + (ev.clientY - startY));
      slotEl.style.width  = newW + 'px';
      slotEl.style.height = newH + 'px';
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.userSelect = prevUserSelect;
      // Tell ECharts to repaint at the new size (chartId resolved above).
      if (typeof CHART_INSTANCES !== 'undefined' && CHART_INSTANCES[chartId]) {
        CHART_INSTANCES[chartId].resize();
      }
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function removeChart(chartId) {
  delete DASHBOARD_STATE.charts[chartId];
  const slot = document.getElementById(`slot-${chartId}`);
  if (slot) slot.remove();
  if (SELECTED_CHART_ID === chartId) {
    SELECTED_CHART_ID = null;
    document.getElementById('config-panel').style.display = 'none';
    document.getElementById('grouped-table-config').style.display = 'none';
    document.getElementById('leaflet-map-config').style.display = 'none';
    document.querySelector('.db-panel-right-placeholder').style.display = '';
  }
  updateCanvasEmptyState();
  _markDirty();
}

function selectChart(chartId, mode) {
  document.querySelectorAll('.chart-slot.selected').forEach(s => s.classList.remove('selected'));
  const slot = document.getElementById(`slot-${chartId}`);
  if (slot) slot.classList.add('selected');

  SELECTED_CHART_ID = chartId;
  populateConfigPanel(chartId);

  // Apply requested mode (edit | properties). Default to edit on first
  // open of this chart; otherwise preserve whichever mode the user last
  // saw for the panel as a whole. The setConfigPanelMode helper is a
  // no-op when the standard config panel isn't visible (grouped_table,
  // leaflet, canva, report — those have their own panels).
  setConfigPanelMode(mode || 'edit');
}

/**
 * Switch #config-panel between "edit" (data-binding fields) and
 * "properties" (display-only fields like title, type, scale, date format).
 *
 * Mode is held as a class on the panel:
 *   .db-config-panel--mode-edit       → hides [data-mode="properties"]
 *   .db-config-panel--mode-properties → hides [data-mode="edit"]
 *
 * Population-driven hides (inline style.display = 'none' set when, e.g.,
 * the X axis isn't a date) still win — see builder.css for the !important
 * mode rules.
 */
function setConfigPanelMode(mode) {
  const panel = document.getElementById('config-panel');
  if (!panel) return;
  const m = (mode === 'properties') ? 'properties' : 'edit';
  panel.classList.toggle('db-config-panel--mode-edit', m === 'edit');
  panel.classList.toggle('db-config-panel--mode-properties', m === 'properties');
  panel.querySelectorAll('.db-mode-tab').forEach(btn => {
    btn.classList.toggle('is-active', btn.dataset.mode === m);
  });
  // First time the panel becomes visible, install the section-collapse
  // wrappers. Idempotent — guarded by a one-shot flag.
  initCollapsibleSections(panel);
  // Hide any section whose body is empty (every child has inline
  // display:none, OR the body has no children). Catches the
  // "Aggregation" / "Display" headers that show on table charts even
  // though their content is hidden.
  syncCollapsibleVisibility(panel);
}

// Hide section wrappers whose body has no visible children. Visibility
// here is just "doesn't have inline style.display=none AND isn't
// data-mode-hidden by the current panel mode". We don't need to walk
// computed style — inline display:none is what populateConfigPanel
// uses, and data-mode hides via CSS that targets the wrapper itself,
// so a mode-hidden section is already off-screen regardless of body
// content. The synchronisation is one-shot per setConfigPanelMode call,
// which fires every time the panel is opened or the mode is switched.
function syncCollapsibleVisibility(panel) {
  if (!panel) return;
  const sections = panel.querySelectorAll('.db-section');
  sections.forEach(section => {
    const body = section.querySelector(':scope > .db-section-body');
    if (!body) return;
    const children = Array.from(body.children);
    const anyVisible = children.length > 0 && children.some(child => {
      // A child counts as visible if it has no inline display:none
      // AND it isn't data-mode-hidden by the panel's current mode.
      // Anything else (form group, sub-section wrapper) is fair game.
      if (child.style && child.style.display === 'none') return false;
      const childMode = child.dataset && child.dataset.mode;
      if (childMode === 'edit' && panel.classList.contains('db-config-panel--mode-properties')) return false;
      if (childMode === 'properties' && panel.classList.contains('db-config-panel--mode-edit')) return false;
      return true;
    });
    section.style.display = anyVisible ? '' : 'none';
  });
}

// Phase 17 T4 — make right-panel config sections collapsible.
// Walks .db-section-label elements inside the panel and wraps each one
// + the run of sibling elements up to the next label in a
// <div class="db-section">. The label becomes a clickable button with
// a chevron; clicking toggles .is-collapsed on the wrapper. Idempotent
// — re-running on an already-processed panel is a no-op.
//
// Visibility note: populateConfigPanel mutates inline style.display on
// individual form-groups based on chart shape (e.g. cfg-aggregation-group
// is hidden for table charts; cfg-date-format-group is hidden when the
// X axis isn't a date). After a wrap, the section header would still
// show even though its body is empty — that's the "orphan label that
// does nothing" the user reported. Fix: after every populateConfigPanel
// pass, syncCollapsibleVisibility() hides any .db-section whose body
// has no visible children.
function initCollapsibleSections(panel) {
  if (!panel || panel.dataset.sectionsInit === '1') return;
  panel.dataset.sectionsInit = '1';

  // Collect (label, [siblings until next label]) pairs from each
  // direct or shallow-nested .db-section-label that isn't already a
  // collapse toggle. Walk in document order; group siblings by their
  // parent so nested sections (e.g. inside #standard-field-section)
  // get their own wrappers.
  const labels = Array.from(panel.querySelectorAll('.db-section-label'))
    .filter(el => !el.classList.contains('db-section-label--collapsible'));

  labels.forEach(labelEl => {
    const parent = labelEl.parentNode;
    if (!parent) return;

    // Capture the run of siblings between this label and the next
    // section boundary. A boundary is either:
    //   - a sibling that IS a .db-section-label, or
    //   - a sibling that CONTAINS a .db-section-label (so a nested
    //     section like #standard-field-section starts its own
    //     section and shouldn't get absorbed into ours).
    // Build the wrapper before we mutate so the moves are cheap.
    const body = document.createElement('div');
    body.className = 'db-section-body';
    let next = labelEl.nextSibling;
    while (next) {
      const after = next.nextSibling;
      if (next.nodeType === 1) {
        if (next.classList && next.classList.contains('db-section-label')) break;
        if (next.querySelector && next.querySelector('.db-section-label')) break;
      }
      body.appendChild(next);
      next = after;
    }

    // Replace the plain label with a clickable button + chevron, and
    // wrap label+body in a .db-section container. Preserve the
    // label's original text and any data-mode attribute (Phase 16).
    const sectionWrap = document.createElement('div');
    sectionWrap.className = 'db-section';
    if (labelEl.dataset.mode) sectionWrap.dataset.mode = labelEl.dataset.mode;
    // follow-up — labels marked data-default-collapsed start in
    // the collapsed state. Sticky thereafter (the wrap happens once per
    // panel via the dataset.sectionsInit guard, so the user's first
    // un-collapse persists for the rest of the session).
    if (labelEl.dataset.defaultCollapsed === 'true') {
      sectionWrap.classList.add('is-collapsed');
    }

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'db-section-label db-section-label--collapsible';
    if (labelEl.dataset.mode) toggle.dataset.mode = labelEl.dataset.mode;
    // ▲ when expanded ("click to collapse up"), ▼ when collapsed
    // ("click to expand down"). The arrow is the *action*, not the
    // current state — matches the convention used by GitHub /
    // Finder disclosure widgets. The is-collapsed class on the
    // wrapper switches the glyph via CSS (::before content swap).
    toggle.innerHTML = `<span class="db-section-collapse-chevron" aria-hidden="true"></span> <span>${escHtml(labelEl.textContent)}</span>`;
    toggle.addEventListener('click', () => {
      sectionWrap.classList.toggle('is-collapsed');
      toggle.setAttribute('aria-expanded', sectionWrap.classList.contains('is-collapsed') ? 'false' : 'true');
    });
    // Initial aria-expanded reflects the wrapper's collapsed state.
    toggle.setAttribute('aria-expanded', sectionWrap.classList.contains('is-collapsed') ? 'false' : 'true');

    parent.insertBefore(sectionWrap, labelEl);
    sectionWrap.appendChild(toggle);
    sectionWrap.appendChild(body);
    parent.removeChild(labelEl);
  });
}

function reconfigureChart(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  config.x_field = null;
  config.x_field_type = null;
  config.y_fields = [];
  config.breakdown = null;
  renderChartSlot(chartId, config);
  selectChart(chartId);
}

function updateCanvasEmptyState() {
  const empty = document.getElementById('canvas-empty-state');
  // A selected canvas page has no charts but shows its read-only preview
  // iframe — the "add your first chart" empty state would sit on top of it.
  const hasContent = Object.keys(DASHBOARD_STATE.charts).length > 0 || _activeCanvasDoc;
  empty.style.display = hasContent ? 'none' : '';
}

// ══════════════════════════════════════════════════════════════
// Standard chart (bar/line/pie/metric) — right panel field helpers
// ══════════════════════════════════════════════════════════════

// shared <option> builder so every field-picker render site
// carries the ratio metadata. Without this, only the table-column
// editor's pickers would lock; the bar/line/metric/pie x/y pickers
// would render bare options with no data-is-ratio attribute, and the
// _applyRatioLock helper would not fire on selection.
function _fieldOptHtml(f) {
  const type = escHtml(f.type || '');
  const isRatio = f.is_ratio ? '1' : '';
  const num = escHtml(f.ratio_numerator || '');
  const denom = escHtml(f.ratio_denominator || '');
  return `<option value="${escHtml(f.name)}" data-field-type="${type}" data-is-ratio="${isRatio}" data-ratio-num="${num}" data-ratio-denom="${denom}">${escHtml(f.name)} (${type})</option>`;
}

// toggle the bar/line date controls (Interval + Data Format) and
// hydrate their values from config. Date-only: shown when x_field_type==='date'
// (Interval additionally requires a bar/line chart). Extracted so it can run
// both synchronously in populateConfigPanel (stored x_field_type) AND again
// after the async field list loads and self-heals a missing/stale type.
function _applyStdDateControlsVisibility(config) {
  const dateGroup = document.getElementById('std-date-format-group');
  const dateSelect = document.getElementById('cfg-std-date-format');
  if (dateGroup && dateSelect) {
    const isDate = config.x_field_type === 'date';
    dateGroup.style.display = isDate ? '' : 'none';
    dateSelect.value = config.date_format || '';
  }
  const intervalGroup = document.getElementById('std-interval-group');
  const intervalSelect = document.getElementById('cfg-std-interval');
  if (intervalGroup && intervalSelect) {
    const showInterval = config.x_field_type === 'date' && ['bar', 'line'].includes(config.chart_type);
    intervalGroup.style.display = showInterval ? '' : 'none';
    intervalSelect.value = config.calendar_interval || 'day';
  }
}

function _populateStandardFieldSelects(fields, config) {
  const xSel = document.getElementById('cfg-std-x-field');
  const yAddSel = document.getElementById('cfg-std-y-field-add');
  const bkSel = document.getElementById('cfg-std-breakdown');

  const fieldOpts = '<option value="">— Select field —</option>' +
    fields.map(_fieldOptHtml).join('');
  const bkOpts = '<option value="">— None —</option>' +
    fields.map(f => `<option value="${escHtml(f.name)}">${escHtml(f.name)} (${escHtml(f.type)})</option>`).join('');

  if (xSel) { xSel.innerHTML = fieldOpts; if (config.x_field) xSel.value = config.x_field; _makeSearchableSelect(xSel); }
  if (yAddSel) { yAddSel.innerHTML = fieldOpts; _makeSearchableSelect(yAddSel); }
  if (bkSel) { bkSel.innerHTML = bkOpts; if (config.breakdown) bkSel.value = config.breakdown; _makeSearchableSelect(bkSel); }

  // self-heal x_field_type. Charts created outside the Builder
  // (imported "from a kibana chart", migrated, or hand-seeded) often never
  // stamped x_field_type, so the date-only Interval / Data Format controls
  // stayed hidden even on a date X. If _shouldHealXFieldTypeToDate says the
  // stored x_field resolves to a date-typed field, correct the type (and
  // persist on next save) so the controls reveal.
  if (_shouldHealXFieldTypeToDate(config, fields)) {
    config.x_field_type = 'date';
    _markDirty();
  }
  _applyStdDateControlsVisibility(config);

  _renderStandardYFieldsList(config);
}

// decide whether to upgrade config.x_field_type to 'date'. True only
// when the chart has an x_field, its stored type is not already 'date', and the
// field resolves to a date-typed field in the loaded field list. Never
// downgrades and never overrides an explicit 'date'. Pure (no DOM) so it is
// unit-testable.
function _shouldHealXFieldTypeToDate(config, fields) {
  if (!config || !config.x_field || config.x_field_type === 'date') return false;
  if (!Array.isArray(fields)) return false;
  const xf = fields.find(f => f && f.name === config.x_field);
  return !!(xf && xf.type === 'date');
}

function _renderStandardYFieldsList(config) {
  const container = document.getElementById('cfg-std-y-fields-list');
  if (!container) return;
  const yFields = config.y_fields || [];
  if (yFields.length === 0) {
    container.innerHTML = '<p class="db-hint" style="margin:0 0 4px;font-size:11px;color:#888;">No Y fields selected.</p>';
    return;
  }
  const labels = config.y_field_labels || {};
  // the field name reaches the handlers via data-y-field, never as
  // an inline-string arg — escHtml does not escape ' and the browser decodes
  // entities before compiling the handler, so a quoted name would run as JS.
  container.innerHTML = yFields.map(f => {
    const fEsc = escHtml(f);
    const labelVal = escHtml(labels[f] || '');
    return `
    <div class="std-y-row">
      <div class="std-y-row-head">
        <span class="std-y-row-field" title="${fEsc}">${fEsc}</span>
        <button class="btn-rm-std-y" data-y-field="${fEsc}" onclick="removeStandardYField(this.dataset.yField)" title="Remove">✕</button>
      </div>
      <input type="text" class="std-y-row-label" placeholder="Label (defaults to field name)"
             data-y-field="${fEsc}" value="${labelVal}"
             oninput="onStandardYFieldLabelChange(this.dataset.yField, this.value)">
    </div>`;
  }).join('');
}

// Per-Y-field display label override. Stored on config.y_field_labels
// as { field_name: human_label } — the same dict the back-end's
// echarts_transformer already reads for bar/line series names and
// legend entries. Empty input clears the override so the field falls
// back to its raw name (matches table_columns.label semantics).
function onStandardYFieldLabelChange(fieldName, value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  if (!config.y_field_labels) config.y_field_labels = {};
  const trimmed = (value || '').trim();
  if (trimmed) {
    config.y_field_labels[fieldName] = trimmed;
  } else {
    delete config.y_field_labels[fieldName];
  }
  _markDirty();
  requeryChart();
}

// Per-tile label override editor for metric breakdown scorecards. Lists
// the bucket keys from the chart's last query response (stashed by
// renderMetric in __METRIC_BREAKDOWN_KEYS) and lets the user type a
// custom title for each — useful when the raw bucket value is unhelpful
// (e.g. a bare number like "1.0"). Overrides live on
// config.breakdown_labels = { rawKey: customLabel }, mirroring the
// y_field_labels pattern. The group is hidden unless the chart is a
// metric with a breakdown that has produced at least one bucket.
function _renderMetricBreakdownLabelsEditor(chartId) {
  const group     = document.getElementById('std-metric-tile-labels-group');
  const container = document.getElementById('cfg-std-metric-tile-labels');
  if (!group || !container) return;

  const config = DASHBOARD_STATE.charts[chartId];
  const keys = (typeof __METRIC_BREAKDOWN_KEYS !== 'undefined' && __METRIC_BREAKDOWN_KEYS[chartId]) || [];

  if (!config || config.chart_type !== 'metric' || !config.breakdown || keys.length === 0) {
    group.style.display = 'none';
    container.innerHTML = '';
    return;
  }

  group.style.display = '';
  const labels = config.breakdown_labels || {};
  // Only the first max_rows buckets actually render as tiles (renderMetric
  // slices data.breakdowns.slice(0, maxRows)), so the title editor lists
  // exactly those — a title for a hidden bucket would never be visible.
  // Mirror the renderer's fallback so the two stay in lock-step.
  const maxRows = config.max_rows || 10;
  const visibleKeys = keys.slice(0, maxRows);
  container.innerHTML = visibleKeys.map(rawKey => {
    const keyEsc = escHtml(rawKey);
    const rawDisplay = escHtml(String(rawKey).replace(/_/g, ' '));
    const labelVal = escHtml(labels[rawKey] || '');
    // The bucket key flows back through onMetricBreakdownLabelChange via a
    // data attribute rather than an inline-string arg so keys containing
    // quotes/specials can't break the handler call.
    return `
    <div class="std-y-row">
      <div class="std-y-row-head">
        <span class="std-y-row-field" title="${keyEsc}">${rawDisplay}</span>
      </div>
      <input type="text" class="std-y-row-label" placeholder="Title (defaults to value)"
             data-bucket-key="${keyEsc}" value="${labelVal}"
             oninput="onMetricBreakdownLabelChange(this.dataset.bucketKey, this.value)">
    </div>`;
  }).join('');
}

// Stores/clears a per-tile label override. Empty input removes the
// override so the tile falls back to its raw bucket value. Re-renders
// the chart so the new title shows immediately, but does NOT re-run the
// breakdown editor (the bucket list is unchanged — only labels moved).
function onMetricBreakdownLabelChange(bucketKey, value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  if (!config.breakdown_labels) config.breakdown_labels = {};
  const trimmed = (value || '').trim();
  if (trimmed) {
    config.breakdown_labels[bucketKey] = trimmed;
  } else {
    delete config.breakdown_labels[bucketKey];
  }
  _markDirty();
  requeryChart();
}

function onStandardDataViewChange(dataViewId) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.data_view = dataViewId;
  // Clear field selections when data view changes
  config.x_field = null;
  config.x_field_type = null;
  config.y_fields = [];
  config.breakdown = null;
  // the time-range field belongs to the old data view; clear it and
  // repopulate the dropdown (date fields of the new view) so it can't point at
  // a field the new index doesn't have.
  config.time_range_field = null;
  _markDirty();
  _loadTableFields(dataViewId).then(fields => {
    _populateStandardFieldSelects(fields, config);
  });
  _populateTimeRangeFieldSelect(
    document.getElementById('cfg-std-time-range-field'),
    dataViewId,
    config.time_range_field,
  );
}

function onStandardXFieldChange(value, fieldType) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.x_field = value || null;
  // Resolve field type from the select option's data attribute if not passed
  if (!fieldType && value) {
    const xSel = document.getElementById('cfg-std-x-field');
    const opt = xSel && xSel.querySelector(`option[value="${CSS.escape(value)}"]`);
    fieldType = opt ? opt.dataset.fieldType : null;
  }
  config.x_field_type = fieldType || null;
  _markDirty();
  // Show/hide the bar/line Data Format dropdown (#std-date-format-group, inside
  // #standard-field-section). was previously toggling the table's
  // #cfg-date-format-group, which lives in the hidden #table-field-section for
  // bar/line, so it never actually appeared. Also resync the collapsible-section
  // visibility so the "Display" section header hides/reveals along with its only
  // visible child.
  const dateGroup = document.getElementById('std-date-format-group');
  if (dateGroup) dateGroup.style.display = (fieldType === 'date') ? '' : 'none';
  // date-bucket Interval (day/week/month) is meaningful only for a
  // bar/line chart over a date X axis. Mirror the date-format toggle.
  const intervalGroup = document.getElementById('std-interval-group');
  if (intervalGroup) {
    const showInterval = (fieldType === 'date') && ['bar', 'line'].includes(config.chart_type);
    intervalGroup.style.display = showInterval ? '' : 'none';
  }
  syncCollapsibleVisibility(document.getElementById('config-panel'));
  requeryChart();
}

function addStandardYField() {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  const addSel = document.getElementById('cfg-std-y-field-add');
  const fieldName = addSel && addSel.value;
  if (!fieldName) return;
  if (!config.y_fields) config.y_fields = [];
  if (!config.y_fields.includes(fieldName)) {
    config.y_fields.push(fieldName);
    // Store field type so the query builder can fall back for text/keyword fields
    const opt = addSel.options[addSel.selectedIndex];
    if (opt && opt.dataset.fieldType) {
      if (!config.y_field_types) config.y_field_types = {};
      config.y_field_types[fieldName] = opt.dataset.fieldType;
    }
    _markDirty();
    _renderStandardYFieldsList(config);
    requeryChart();
  }
  if (addSel) addSel.value = '';
}

function removeStandardYField(fieldName) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.y_fields = (config.y_fields || []).filter(f => f !== fieldName);
  if (config.y_field_types) delete config.y_field_types[fieldName];
  if (config.y_field_labels) delete config.y_field_labels[fieldName];
  _markDirty();
  _renderStandardYFieldsList(config);
  requeryChart();
}

function onStandardBreakdownChange(value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.breakdown = value || null;
  _markDirty();
  requeryChart();
}

// Explicit clear button next to the Breakdown picker — picking
// "— None —" from inside the searchable-select dropdown isn't
// discoverable, so the ✕ button gives users a one-click escape.
// Resets both the underlying <select> value AND the search-filter
// input that _makeSearchableSelect wraps it with, so the picker
// reflects the cleared state immediately.
function clearStandardBreakdown() {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.breakdown = null;
  const bkSel = document.getElementById('cfg-std-breakdown');
  if (bkSel) {
    bkSel.value = '';
    if (bkSel._searchInput) bkSel._searchInput.value = '';
  }
  _markDirty();
  requeryChart();
}

function onMetricLayoutChange(key, rawValue) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  if (key === 'other_bucket') {
    config[key] = rawValue === true || rawValue === 'true';
  } else {
    let val = parseInt(rawValue, 10);
    if (isNaN(val) || val < 1) return;
    // Max Values to Display is capped at 10 (matches the input's max=10);
    // clamp defensively so a manual/over-max entry can't request more tiles
    // than the renderer (and the tile-title editor) will show.
    if (key === 'max_rows' && val > 10) {
      val = 10;
      const maxRowsEl = document.getElementById('cfg-std-max-rows');
      if (maxRowsEl) maxRowsEl.value = val;
    }
    config[key] = val;
  }
  _markDirty();
  // Changing Max Values changes how many tiles render, so re-sync the
  // tile-title editor immediately (the requery below also refreshes it via
  // chart-rendered, but that's async — this keeps the list in step now).
  if (key === 'max_rows') _renderMetricBreakdownLabelsEditor(SELECTED_CHART_ID);
  requeryChart();
}

// ══════════════════════════════════════════════════════════════
// Right panel — config
// ══════════════════════════════════════════════════════════════

// the table branch of populateConfigPanel, extracted so the Canvas
// Editor can hydrate the SAME table config panel for a canvas table object
// (canvas.html includes the same Jinja partials; SELECTED_CHART_ID points at
// the canvas object id). Every getElementById is null-guarded because the
// canvas page only carries the table-specific elements — builder-only panel
// chrome (grouped-table / leaflet / standard sections, Type dropdown, Title)
// is simply absent there and each hide/set becomes a no-op.
function populateTablePanel(chartId, config) {
  const _gtCfg = document.getElementById('grouped-table-config');
  if (_gtCfg) _gtCfg.style.display = 'none';
  const _lmCfg = document.getElementById('leaflet-map-config');
  if (_lmCfg) _lmCfg.style.display = 'none';

  const _cfgPanel = document.getElementById('config-panel');
  if (_cfgPanel) _cfgPanel.style.display = '';
  const _tblSecT = document.getElementById('table-field-section');
  if (_tblSecT) _tblSecT.style.display = '';
  const _stdSec2 = document.getElementById('standard-field-section');
  if (_stdSec2) _stdSec2.style.display = 'none';
  const _aggGrpT = document.getElementById('cfg-aggregation-group');
  if (_aggGrpT) _aggGrpT.style.display = 'none';
  const _typeGrpT = document.getElementById('cfg-type-group');
  if (_typeGrpT) _typeGrpT.style.display = '';
  // re-show the Row sub-section (posts_table hides it).
  const _rowSubT = document.getElementById('table-row-subsection');
  if (_rowSubT) _rowSubT.style.display = '';

  const _titleT = document.getElementById('cfg-title');
  if (_titleT) _titleT.value = config.title || '';
  const _typeT = document.getElementById('cfg-type');
  if (_typeT) _typeT.value = 'table';

  // hydrate hide-title checkbox (table branch returns early so
  // it doesn't reach the standard hydration at the bottom).
  const _hideTitleTbl = document.getElementById('cfg-hide-title');
  if (_hideTitleTbl) _hideTitleTbl.checked = config.hide_title !== false;

  // reveal + hydrate the Properties-tab "Include totals row"
  // controls. The narrow `'table'` check here (not _isTableLikeConfig)
  // deliberately excludes posts_table. Default unchecked; the Sum/Avg
  // selector is shown only when the box is checked.
  _toggleTotalsRowControls(true);
  const _inclTotals = document.getElementById('cfg-include-totals-row');
  if (_inclTotals) _inclTotals.checked = config.include_totals_row === true;
  const _totalsAgg = document.getElementById('cfg-totals-aggregation');
  if (_totalsAgg) _totalsAgg.value = config.totals_aggregation || 'sum';
  const _totalsAggGrp = document.getElementById('cfg-totals-aggregation-group');
  if (_totalsAggGrp) _totalsAggGrp.style.display = config.include_totals_row === true ? '' : 'none';

  // table-only display props (font size, cell shading toggle +
  // color). Backwards-compat: earlier tables had no table_font_size
  // and shipped with .gt-table's inherited 14px — populate the dropdown
  // with 14 so existing tables look unchanged until the user picks a
  // different value. cell_gradient_enabled falls back to the legacy
  // cell_gradient flag when undefined so existing shaded tables stay
  // shaded after the toggle ships.
  const _fontGroup = document.getElementById('cfg-table-font-size-group');
  const _gradGroup = document.getElementById('cfg-cell-gradient-group');
  const _gradColorGroup = document.getElementById('cfg-cell-gradient-color-group');
  if (_fontGroup) _fontGroup.style.display = '';
  if (_gradGroup) _gradGroup.style.display = '';
  if (_gradColorGroup) _gradColorGroup.style.display = '';

  const _fontEl = document.getElementById('cfg-table-font-size');
  if (_fontEl) {
    // Legacy tables (no table_font_size field) hydrate at 12 — matches
    // the renderer's fallback in charts.js. Set the dropdown to 12 so
    // the picker reflects what the user actually sees on screen.
    const _effectiveFontSize = config.table_font_size != null
      ? String(config.table_font_size)
      : '12';
    _fontEl.value = _effectiveFontSize;
  }
  const _gradEnabledEl = document.getElementById('cfg-cell-gradient-enabled');
  if (_gradEnabledEl) {
    // cell_gradient_enabled is the new explicit flag; cell_gradient is
    // the legacy boolean. Prefer the new flag, fall back to legacy.
    _gradEnabledEl.checked = config.cell_gradient_enabled != null
      ? !!config.cell_gradient_enabled
      : !!config.cell_gradient;
  }
  const _gradColorEl = document.getElementById('cfg-cell-gradient-color');
  if (_gradColorEl) _gradColorEl.value = config.cell_gradient_color || '#006d2c';

  // Migrate y_fields/y_labels to table_columns if not yet done
  _initTableColumns(config);

  _populateTableDataViewSelect();
  const _dvSelT = document.getElementById('cfg-table-data-view');
  if (_dvSelT) _dvSelT.value = config.data_view || '';
  const _xLabelT = document.getElementById('cfg-table-x-label');
  if (_xLabelT) _xLabelT.value = config.x_label || '';
  const _xLabel2T = document.getElementById('cfg-table-x-label-2');
  if (_xLabel2T) _xLabel2T.value = config.x_label_2 || '';
  const _numValsT = document.getElementById('cfg-table-num-values');
  if (_numValsT) _numValsT.value = config.num_values || 10;

  if (config.data_view) {
    _loadTableFields(config.data_view).then(fields => {
      _populateTableXFieldSelect(fields, config.x_field);
      _populateTableXField2Select(fields, config.x_field_2);
      renderTableColumnsEditor(chartId, fields);
    });
  } else {
    _populateTableXFieldSelect([], config.x_field);
    _populateTableXField2Select([], config.x_field_2);
    renderTableColumnsEditor(chartId, []);
  }

  // chart-level time-range date field dropdown (all columns
  // inherit it). Populated with the table data view's date-typed fields.
  _populateTimeRangeFieldSelect(
    document.getElementById('cfg-table-time-range-field'),
    config.data_view,
    config.time_range_field,
  );

  // hydrate Date Format dropdown (lives on the Edit tab
  // under Row Labels Field as of 2026-05-25). The generic-chart
  // branch below ALSO hydrates it for bar/line charts; the table
  // branch returns early so we hydrate here too. Toggle visibility
  // off x_field_type so the dropdown only appears once the user
  // picked a date-typed Row field.
  const _dateGroup = document.getElementById('cfg-date-format-group');
  const _dateSelect = document.getElementById('cfg-date-format');
  if (_dateGroup && _dateSelect) {
    _dateGroup.style.display = (config.x_field_type === 'date') ? '' : 'none';
    _dateSelect.value = config.date_format || '';
  }

  if (typeof renderFilterRows === 'function') renderFilterRows(chartId);
}

function populateConfigPanel(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;

  document.querySelector('.db-panel-right-placeholder').style.display = 'none';
  document.getElementById('dashboard-props-panel').style.display = 'none';

  document.getElementById('canva-config').style.display = 'none';
  document.getElementById('report-config').style.display = 'none';

  // totals-row controls are `table`-only. Hide them up front so
  // every non-table path leaves them off; the table branch re-shows them.
  _toggleTotalsRowControls(false);

  if (config.chart_type === 'report' || config.chart_type === 'google_report' || config.chart_type === 'combined_report') {
    document.getElementById('config-panel').style.display = 'none';
    document.getElementById('grouped-table-config').style.display = 'none';
    document.getElementById('leaflet-map-config').style.display = 'none';
    document.getElementById('canva-config').style.display = 'none';
    document.getElementById('report-config').style.display = '';
    document.getElementById('cfg-rpt-org-name').value = config.org_name || '';
    document.getElementById('cfg-rpt-show-geo-list').checked = config.show_geo_list !== false;
    _renderReportMetricsEditor(chartId);
    return;
  }

  if (config.chart_type === 'canva') {
    document.getElementById('config-panel').style.display = 'none';
    document.getElementById('grouped-table-config').style.display = 'none';
    document.getElementById('leaflet-map-config').style.display = 'none';
    document.getElementById('canva-config').style.display = '';
    _renderCanvaMetricsEditor(chartId);
    return;
  }

  if (config.chart_type === 'grouped_table') {
    document.getElementById('config-panel').style.display = 'none';
    document.getElementById('grouped-table-config').style.display = '';
    document.getElementById('leaflet-map-config').style.display = 'none';


    document.getElementById('cfg-gt-table-title').value = config.table_title || '';
    document.getElementById('cfg-gt-title-color').value = config.title_bg_color || '#2c6da0';
    document.getElementById('cfg-gt-group-color').value = config.group_header_bg_color || '#5a9cc5';
    document.getElementById('cfg-gt-col-color').value = config.col_header_bg_color || '#2c6da0';
    document.getElementById('cfg-gt-text-color').value = config.header_text_color || '#ffffff';
    document.getElementById('cfg-gt-group-text-color').value = config.group_text_color || '#ffffff';
    document.getElementById('cfg-gt-group-font-size').value = config.group_font_size || '12';
    document.getElementById('cfg-gt-group-bold').checked = config.group_bold !== false;
    document.getElementById('cfg-gt-col-text-color').value = config.col_text_color || '#ffffff';
    document.getElementById('cfg-gt-col-font-size').value = config.col_font_size || '12';
    document.getElementById('cfg-gt-col-bold').checked = config.col_bold !== false;

    renderGroupsEditor(chartId);
    return;
  }

  if (config.chart_type === 'leaflet_map') {
    document.getElementById('config-panel').style.display = 'none';
    document.getElementById('grouped-table-config').style.display = 'none';

    document.getElementById('leaflet-map-config').style.display = '';

    renderMapStatDropdown(chartId);
    renderMapFieldEditor(chartId);
    return;
  }

  // ── Profile ──────────────────────────────────────────────
  // Fixed-field IG account card: data view is locked and there are no
  // x/y/aggregation bindings. Show the standard panel for the title +
  // hide-title only; hide every data-binding section.
  if (config.chart_type === 'profile') {
    document.getElementById('grouped-table-config').style.display = 'none';
    document.getElementById('leaflet-map-config').style.display = 'none';
    document.getElementById('config-panel').style.display = '';

    const _tblSec = document.getElementById('table-field-section');
    if (_tblSec) _tblSec.style.display = 'none';
    const _stdSecP = document.getElementById('standard-field-section');
    if (_stdSecP) _stdSecP.style.display = 'none';
    const _aggGrpP = document.getElementById('cfg-aggregation-group');
    if (_aggGrpP) _aggGrpP.style.display = 'none';
    // Hide the Type dropdown: a Profile card can't be reclassified to
    // bar/table/etc (it has no x/y bindings and a locked IG data view).
    const _typeGrpP = document.getElementById('cfg-type-group');
    if (_typeGrpP) _typeGrpP.style.display = 'none';

    document.getElementById('cfg-title').value = config.title || '';
    const _hideTitleP = document.getElementById('cfg-hide-title');
    if (_hideTitleP) _hideTitleP.checked = config.hide_title !== false;

    if (typeof renderFilterRows === 'function') renderFilterRows(chartId);
    return;
  }

  // ── Posts table (FB Page Posts) ──────────────────────────
  // a raw-hits table with a locked data view. It reuses the
  // regular Table's Data View / Max Rows / Columns editor so the user can
  // add/remove/reorder columns from the *_fb_page_posts fields — but it
  // has NO x_field grouping (posts are one row per document), so the Row /
  // Sub-Row sub-section is hidden.
  if (config.chart_type === 'posts_table') {
    document.getElementById('grouped-table-config').style.display = 'none';
    document.getElementById('leaflet-map-config').style.display = 'none';
    document.getElementById('config-panel').style.display = '';

    // Show the table field section (Data View / Max Rows / Columns editor);
    // hide the standard bar/line section, aggregation group, and the Type
    // dropdown (a posts_table can't be reclassified).
    const _tblSecQ = document.getElementById('table-field-section');
    if (_tblSecQ) _tblSecQ.style.display = '';
    const _stdSecQ = document.getElementById('standard-field-section');
    if (_stdSecQ) _stdSecQ.style.display = 'none';
    const _aggGrpQ = document.getElementById('cfg-aggregation-group');
    if (_aggGrpQ) _aggGrpQ.style.display = 'none';
    const _typeGrpQ = document.getElementById('cfg-type-group');
    if (_typeGrpQ) _typeGrpQ.style.display = 'none';
    // Hide the Row / Sub-Row grouping sub-section — posts_table is raw hits,
    // no x_field. Also hide the table-only cell-gradient/font groups that
    // don't apply (keep it focused on columns).
    const _rowSub = document.getElementById('table-row-subsection');
    if (_rowSub) _rowSub.style.display = 'none';

    document.getElementById('cfg-title').value = config.title || '';
    document.getElementById('cfg-type').value = 'table';  // inert (Type group hidden)
    const _hideTitleQ = document.getElementById('cfg-hide-title');
    if (_hideTitleQ) _hideTitleQ.checked = config.hide_title !== false;

    // Migrate/seed table_columns (keeps the default 8 columns).
    _initTableColumns(config);

    _populateTableDataViewSelect();
    document.getElementById('cfg-table-data-view').value = config.data_view || '*_fb_page_posts';
    document.getElementById('cfg-table-num-values').value = config.num_values || 25;

    // Load the data view's fields and render the column editor — the user
    // can now add/remove/reorder columns like a regular table.
    if (config.data_view) {
      _loadTableFields(config.data_view).then(fields => {
        renderTableColumnsEditor(chartId, fields);
      });
    } else {
      renderTableColumnsEditor(chartId, []);
    }

    if (typeof renderFilterRows === 'function') renderFilterRows(chartId);
    return;
  }

  // ── Table ────────────────────────────────────────────────
  if (config.chart_type === 'table') {
    populateTablePanel(chartId, config);
    return;
  }

  // ── Generic: bar / line / pie / metric ───────────────────
  document.getElementById('grouped-table-config').style.display = 'none';
  document.getElementById('leaflet-map-config').style.display = 'none';
  document.getElementById('config-panel').style.display = '';

  // Hide table section, show standard field section
  const _tableSec = document.getElementById('table-field-section');
  if (_tableSec) _tableSec.style.display = 'none';
  const _stdSec = document.getElementById('standard-field-section');
  if (_stdSec) _stdSec.style.display = '';
  const _aggGrp = document.getElementById('cfg-aggregation-group');
  if (_aggGrp) _aggGrp.style.display = '';
  const _typeGrpG = document.getElementById('cfg-type-group');
  if (_typeGrpG) _typeGrpG.style.display = '';

  document.getElementById('cfg-title').value = config.title || '';
  document.getElementById('cfg-type').value = config.chart_type || 'bar';
  document.getElementById('cfg-aggregation').value = config.aggregation || 'sum';

  // Populate data view dropdown and field selects
  const dvSel = document.getElementById('cfg-std-data-view');
  _populateDataViewSelect(dvSel);
  if (dvSel) dvSel.value = config.data_view || '';

  if (config.data_view) {
    _loadTableFields(config.data_view).then(fields => {
      _populateStandardFieldSelects(fields, config);
    });
  } else {
    _populateStandardFieldSelects([], config);
  }

  // Metric charts: hide X/Y, rename breakdown → "Metric"
  const isMetric = config.chart_type === 'metric';
  const xGroup  = document.getElementById('std-x-field-group');
  const yGroup  = document.getElementById('std-y-fields-group');
  const bkGroup = document.getElementById('std-breakdown-group');
  const bkLabel = document.getElementById('std-breakdown-label');
  if (xGroup)  xGroup.style.display  = isMetric ? 'none' : '';
  if (yGroup)  yGroup.style.display  = isMetric ? 'none' : '';
  if (bkGroup) bkGroup.style.display = '';
  if (bkLabel) bkLabel.innerHTML = isMetric
    ? 'Metric'
    : 'Breakdown <span style="color:#888;font-size:11px;">(optional)</span>';

  const metricLayoutGroup = document.getElementById('std-metric-layout-group');
  if (metricLayoutGroup) {
    metricLayoutGroup.style.display = isMetric ? '' : 'none';
    if (isMetric) {
      const maxColsEl    = document.getElementById('cfg-std-max-columns');
      const maxRowsEl    = document.getElementById('cfg-std-max-rows');
      const otherBktEl   = document.getElementById('cfg-std-other-bucket');
      if (maxColsEl)  maxColsEl.value   = config.max_columns  != null ? config.max_columns  : 1;
      if (maxRowsEl)  maxRowsEl.value   = config.max_rows     != null ? config.max_rows     : 1;
      if (otherBktEl) otherBktEl.checked = config.other_bucket ? true : false;
      _renderMetricBreakdownLabelsEditor(chartId);
    }
  }

  // Date controls (Interval + Data Format) — date X only. these
  // live in #standard-field-section (the table's #cfg-date-format is in the
  // hidden #table-field-section, so it can't be shared). This synchronous pass
  // uses the stored x_field_type; _populateStandardFieldSelects re-runs it after
  // the async field list loads, having self-healed a missing/stale type.
  _applyStdDateControlsVisibility(config);

  // Time-range date field: which date field the global time-range
  // dropdown filters this chart on (default kibana_date; e.g. closed_at for
  // Echo). Populated async with the data view's date-typed fields.
  _populateTimeRangeFieldSelect(
    document.getElementById('cfg-std-time-range-field'),
    config.data_view,
    config.time_range_field,
  );

  // Y Axis Scale — only relevant for bar/line charts
  const scaleGroup = document.getElementById('cfg-scale-group');
  if (scaleGroup) {
    const showScale = ['bar', 'line'].includes(config.chart_type);
    scaleGroup.style.display = showScale ? '' : 'none';
    document.getElementById('cfg-y-min').value = config.y_min || '';
    document.getElementById('cfg-y-max').value = config.y_max || '';
  }

  // Hide-title checkbox: undefined (legacy charts saved before this field
  // existed) is treated as true so existing dashboards adopt the new default.
  const hideTitleEl = document.getElementById('cfg-hide-title');
  if (hideTitleEl) hideTitleEl.checked = config.hide_title !== false;

  // hide table-only groups for non-table chart types. The table
  // branch above shows them; this branch handles bar/line/pie/metric where
  // these props don't apply.
  ['cfg-table-font-size-group', 'cfg-cell-gradient-group', 'cfg-cell-gradient-color-group'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });

  if (typeof renderFilterRows === 'function') renderFilterRows(chartId);
}

// Toggle the .chart-slot-header visibility for a slot based on the chart's
// hide_title flag. Treat undefined as true so legacy dashboards get the
// new default — matches the populateConfigPanel hydration rule above.
function applyHideTitleToSlot(chartId) {
  if (!chartId) return;
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  const header = document.querySelector(`#slot-${chartId} .chart-slot-header`);
  if (!header) return;
  const hide = config.hide_title !== false;
  header.style.display = hide ? 'none' : '';
}

function updateChartConfig(key, value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config[key] = value;
  _markDirty();

  if (key === 'title') {
    const titleEl = document.querySelector(`#slot-${SELECTED_CHART_ID} .chart-title`);
    if (titleEl) titleEl.textContent = value;
  }
  if (key === 'chart_type') {
    populateConfigPanel(SELECTED_CHART_ID);
  }
}

function requeryChart() {
  if (!SELECTED_CHART_ID) return;
  if (typeof queryAndRender === 'function') queryAndRender(SELECTED_CHART_ID);
}

// ── Table config helpers ──────────────────────────────────

// show/hide the Properties-tab totals-row controls. `show` only
// reveals the "Include totals row" checkbox group; the Sum/Avg selector group
// is gated on the checkbox being checked and is toggled separately (here it is
// always hidden when the whole feature is hidden).
function _toggleTotalsRowControls(show) {
  const grp = document.getElementById('cfg-include-totals-group');
  if (grp) grp.style.display = show ? '' : 'none';
  if (!show) {
    const aggGrp = document.getElementById('cfg-totals-aggregation-group');
    if (aggGrp) aggGrp.style.display = 'none';
  }
}

// "Include totals row" change handler. Writes the boolean config
// field, reveals/hides the Sum/Avg selector, and re-renders the table so the
// totals row appears/disappears live (reuses the cached data, like the format
// edits in updateTableColumnProp — no requery needed).
function onIncludeTotalsRowChange(checked) {
  updateChartConfig('include_totals_row', checked);
  const aggGrp = document.getElementById('cfg-totals-aggregation-group');
  if (aggGrp) aggGrp.style.display = checked ? '' : 'none';
  if (typeof _lastTableData !== 'undefined' && SELECTED_CHART_ID &&
      _lastTableData[SELECTED_CHART_ID]) {
    window.renderTable(SELECTED_CHART_ID, _lastTableData[SELECTED_CHART_ID]);
  }
}

// posts_table reuses the regular Table's column editor + Max Rows,
// so the helpers that mutate table_columns / num_values must accept both
// chart types. (The other column mutators — remove/move/updateProp — already
// operate on SELECTED_CHART_ID generically with no chart_type gate.)
function _isTableLikeConfig(config) {
  return !!config && (config.chart_type === 'table' || config.chart_type === 'posts_table');
}

function updateTableConfig() {
  // Only handles max rows — other fields updated via specific handlers
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!_isTableLikeConfig(config)) return;
  const nv = parseInt(document.getElementById('cfg-table-num-values').value, 10);
  config.num_values = isNaN(nv) ? 10 : nv;
  _markDirty();
}

// Cache for fields loaded per data view
const _tableFieldsCache = {};

// a data view whose team index does not exist yet (first import
// has not run) returns success + fields:[] + no_data_yet:true. Render that as
// a disabled explanatory option instead of an empty picker.
function _appendNoDataYetOption(sel, message) {
  if (!sel) return;
  const o = document.createElement('option');
  o.value = '';
  o.textContent = message || 'No data imported yet for this source';
  o.disabled = true;
  o.selected = true;
  sel.appendChild(o);
}

async function _loadTableFields(dataViewId) {
  if (!dataViewId) return [];
  if (_tableFieldsCache[dataViewId]) return _tableFieldsCache[dataViewId];
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/data-views/' + encodeURIComponent(dataViewId) + '/fields');
    const data = await res.json();
    if (data.success) {
      _tableFieldsCache[dataViewId] = data.fields || [];
      return data.fields || [];
    }
  } catch (e) {
    console.error('Failed to load table fields:', e);
  }
  return [];
}

function _initTableColumns(config) {
  if (!config.table_columns) {
    const yFields = config.y_fields || [];
    const yLabels = config.y_labels || [];
    const globalAgg = config.aggregation || 'sum';
    config.table_columns = yFields.map((field, i) => ({
      field,
      label: yLabels[i] || field,
      aggregation: globalAgg,
      format: '0',
      // new columns are included in the totals row by default;
      // a user can opt a column out per-column on the Columns page. Only
      // matters when config.include_totals_row is on.
      include_in_totals: true,
    }));
  }
}

function _populateTableDataViewSelect() {
  _populateDataViewSelect(document.getElementById('cfg-table-data-view'));
}

function _populateTableXFieldSelect(fields, currentValue) {
  const sel = document.getElementById('cfg-table-x-field');
  if (!sel) return;
  sel.innerHTML = '<option value="">— Select field —</option>';
  fields.forEach(f => {
    const o = document.createElement('option');
    o.value = f.name;
    o.textContent = f.name + (f.type ? ` (${f.type})` : '');
    // stamp data-field-type so onTableXFieldChange can
    // propagate the field type into config.x_field_type and the
    // back-end can fire date_histogram branch. Matches the
    // standard-chart x-field selector's data-field-type pattern.
    if (f.type) o.dataset.fieldType = f.type;
    sel.appendChild(o);
  });
  if (currentValue) {
    sel.value = currentValue;
    // If saved value wasn't found in the field list, add it so it's visible
    if (sel.value !== currentValue) {
      const o = document.createElement('option');
      o.value = currentValue;
      o.textContent = currentValue + ' (saved)';
      sel.appendChild(o);
      sel.value = currentValue;
    }
  }
  _makeSearchableSelect(sel);
}

function onTableDataViewChange(dataViewId) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.data_view = dataViewId;
  // the time-range field belonged to the old data view; clear and
  // repopulate from the new one's date fields.
  config.time_range_field = null;
  _markDirty();
  _loadTableFields(dataViewId).then(fields => {
    _populateTableXFieldSelect(fields, config.x_field);
    _populateTableXField2Select(fields, config.x_field_2);
    renderTableColumnsEditor(SELECTED_CHART_ID, fields);
  });
  _populateTimeRangeFieldSelect(
    document.getElementById('cfg-table-time-range-field'),
    dataViewId,
    config.time_range_field,
  );
}

function onTableXLabelChange(value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.x_label = value || null;
  _markDirty();
}

function onTableXFieldChange(value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.x_field = value || null;
  // resolve field type from the select option's
  // data-field-type so the back-end can fire date_histogram
  // branch. Pre-fix the standard-chart handler (setStandardXField)
  // saved x_field_type but the table-chart handler did not, so even
  // when the user picked a date field the table query fell through to
  // the terms-agg path and rendered raw epoch_millis on staging.
  let fieldType = null;
  if (value) {
    const sel = document.getElementById('cfg-table-x-field');
    const opt = sel && sel.querySelector(`option[value="${CSS.escape(value)}"]`);
    fieldType = opt ? opt.dataset.fieldType : null;
  }
  config.x_field_type = fieldType || null;
  _markDirty();
  // Show/hide the Date Format dropdown (dropdown moved onto
  // the Edit tab under Row Labels Field; was originally on Properties).
  const dateGroup = document.getElementById('cfg-date-format-group');
  if (dateGroup) dateGroup.style.display = (fieldType === 'date') ? '' : 'none';
  requeryChart();
}

function onTableXLabel2Change(value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.x_label_2 = value || null;
  _markDirty();
}

function onTableXField2Change(value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config.x_field_2 = value || null;
  _markDirty();
}

function _populateTableXField2Select(fields, currentValue) {
  const sel = document.getElementById('cfg-table-x-field-2');
  if (!sel) return;
  sel.innerHTML = '<option value="">— None —</option>';
  fields.forEach(f => {
    const o = document.createElement('option');
    o.value = f.name;
    o.textContent = f.name + (f.type ? ` (${f.type})` : '');
    sel.appendChild(o);
  });
  if (currentValue) {
    sel.value = currentValue;
    // If saved value wasn't found in the field list, add it so it's visible
    if (sel.value !== currentValue) {
      const o = document.createElement('option');
      o.value = currentValue;
      o.textContent = currentValue + ' (saved)';
      sel.appendChild(o);
      sel.value = currentValue;
    }
  }
  _makeSearchableSelect(sel);
}

const DATE_FORMAT_OPTIONS = [
  { value: '',              label: '— select format —' },
  { value: 'YYYY-MM-DD',   label: 'YYYY-MM-DD  (2024-01-15)' },
  { value: 'DD/MM/YYYY',   label: 'DD/MM/YYYY  (15/01/2024)' },
  { value: 'MM/DD/YYYY',   label: 'MM/DD/YYYY  (01/15/2024)' },
  { value: 'MMM DD, YYYY', label: 'MMM DD, YYYY  (Jan 15, 2024)' },
  { value: 'MMMM DD, YYYY',label: 'MMMM DD, YYYY  (January 15, 2024)' },
  { value: 'MMM YYYY',     label: 'MMM YYYY  (Jan 2024)' },
  { value: 'MMMM YYYY',    label: 'MMMM YYYY  (January 2024)' },
  { value: 'YYYY',         label: 'YYYY  (year only)' },
  { value: 'MM/YYYY',      label: 'MM/YYYY  (01/2024)' },
  { value: 'DD',           label: 'DD  (day number)' },
  { value: 'MMM',          label: 'MMM  (month name)' },
];

function _toggleTableColDateFormat(colIdx, aggregation) {
  // earlier T4: only one.tc-col-card is rendered at a time (the
  // edit pane for the currently-selected column from the picker
  // dropdown). The colIdx is informational here — if the toggle is
  // firing for a column that isn't the visible edit-pane column it's
  // a no-op, which is fine because the change applies on next select.
  if (colIdx !== _tableColEditIdx[SELECTED_CHART_ID]) return;
  const card = document.querySelector('.tc-col-edit-pane');
  if (!card) return;
  const isDate = aggregation === 'date';
  const dateRow = card.querySelector('.tc-date-format-row');
  const decimalsRow = card.querySelector('.tc-decimals-row');
  if (dateRow) dateRow.style.display = isDate ? '' : 'none';
  if (decimalsRow) decimalsRow.style.display = isDate ? 'none' : '';
}

// lock the aggregation dropdown when the column's field is a
// registered ratio (sum-of-num / sum-of-denom). The backend ignores the
// stored aggregation for these fields anyway; the UI lock surfaces the
// contract so chart authors can't accidentally pick `avg` and get the
// plausible-looking wrong number.
//
// when locked, render a synthetic `<option value="ratio">ratio</option>`
// and select it so the dropdown reflects the actual backend behavior instead of
// the stored aggregation (which was typically "avg" on earlier saved charts).
// The synthetic option is removed on unlock; the stored `config.aggregation` is
// never mutated (setting select.value programmatically does not fire onchange,
// and the dropdown is disabled while the lock is active).
function _applyRatioLock(card, fieldOpt) {
  if (!card) return;
  const aggSel = card.querySelector('.tc-col-agg-select');
  const hint   = card.querySelector('.tc-ratio-hint');
  const isRatio = !!(fieldOpt && fieldOpt.dataset && fieldOpt.dataset.isRatio === '1');
  if (isRatio) {
    const num   = fieldOpt.dataset.ratioNum   || 'num';
    const denom = fieldOpt.dataset.ratioDenom || 'denom';
    if (aggSel) {
      if (!aggSel.querySelector('option[value="ratio"][data-synthetic="1"]')) {
        const opt = document.createElement('option');
        opt.value = 'ratio';
        opt.textContent = 'ratio';
        opt.dataset.synthetic_2 = '1';
        aggSel.appendChild(opt);
      }
      aggSel.value = 'ratio';
      aggSel.disabled = true;
    }
    if (hint) {
      hint.textContent = `Computed as: sum(${num}) / sum(${denom})`;
      hint.style.display = '';
    }
  } else {
    if (aggSel) {
      const synth = aggSel.querySelector('option[value="ratio"][data-synthetic="1"]');
      if (synth) synth.remove();
      aggSel.disabled = false;
    }
    if (hint) {
      hint.textContent = '';
      hint.style.display = 'none';
    }
  }
}

// Per-chart "currently editing column" pointer — transient UI state,
// not persisted in the chart config. Resets to 0 on chart load via
// renderTableColumnsEditor. Phase 17 T4 — Columns dropdown.
const _tableColEditIdx = {};

function renderTableColumnsEditor(chartId, fields) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  const container = document.getElementById('table-columns-editor');
  if (!container) return;

  const cols = config.table_columns || [];

  // Pin the current edit-target index so swap re-renders preserve
  // selection. Clamp into range when columns are added/removed.
  let editIdx = _tableColEditIdx[chartId];
  if (editIdx === undefined || editIdx < 0 || editIdx >= cols.length) {
    editIdx = cols.length > 0 ? 0 : -1;
    _tableColEditIdx[chartId] = editIdx;
  }

  // Picker dropdown sits on its own row; the action buttons (move
  // up / down, add, remove) live on a row below so they don't get
  // squeezed off-screen on a narrow right panel.
  const dropdownOpts = cols.map((col, i) => {
    const label = col.label || col.field || `(unnamed column ${i + 1})`;
    return `<option value="${i}"${i === editIdx ? ' selected' : ''}>${escHtml(label)}</option>`;
  }).join('');
  const pickerHtml = `
    <div class="tc-col-picker-row">
      <select id="tc-col-picker" onchange="selectTableColumn(parseInt(this.value, 10))">
        ${cols.length === 0 ? '<option value="-1">— No columns —</option>' : dropdownOpts}
      </select>
    </div>
    <div class="tc-col-picker-actions">
      <button class="db-btn db-btn-sm" onclick="moveTableColumn(${editIdx}, -1)" ${editIdx <= 0 ? 'disabled' : ''} title="Move up">↑</button>
      <button class="db-btn db-btn-sm" onclick="moveTableColumn(${editIdx}, 1)" ${editIdx < 0 || editIdx >= cols.length - 1 ? 'disabled' : ''} title="Move down">↓</button>
      <button class="db-btn db-btn-sm" onclick="addTableColumn()" title="Add column">+</button>
      <button class="db-btn db-btn-sm" onclick="removeTableColumn(${editIdx})" ${editIdx < 0 ? 'disabled' : ''} title="Remove column">−</button>
    </div>
  `;

  // Edit pane for the currently-selected column. Empty when no columns.
  if (editIdx < 0) {
    container.innerHTML = pickerHtml + '<p class="db-hint" style="margin:8px 0 0;color:#888;font-size:12px;">No columns yet — click + above to add one.</p>';
    return;
  }

  const col = cols[editIdx];
  const isDate = col.aggregation === 'date';
  const isCalc = col.aggregation === 'calculation';
  const isEquals = col.aggregation === 'equals';
  const aggLabels = { cardinality: 'unique', calculation: 'calculation' };
  // Phase 18 — "calculation" pseudo-aggregation. The column has
  // no single backing field; instead it stores a formula like
  // ``conversions / reach`` and ``formula_fields: ["conversions", "reach"]``.
  // The backend issues a sum sub-agg per referenced field and computes
  // the formula value at row-build time.
  // 'equals' counts docs where the field equals the column's
  // Equals Value (e.g. an Echo outcomes table: one column per outcome string).
  // 'exists' counts docs where the field has any value. Without it
  // in this list, a stored exists column displayed as "sum" and any touch of
  // the dropdown silently overwrote the aggregation.
  const aggOptions = ['sum', 'avg', 'min', 'max', 'count', 'cardinality', 'equals', 'exists', 'text', 'date', 'calculation']
    .map(a => `<option value="${a}">${aggLabels[a] || a}</option>`).join('');
  // _fieldOptHtml emits data-is-ratio / data-ratio-num /
  // data-ratio-denom for registered fields, so the editor can lock the
  // aggregation dropdown and show a "Computed as: …" helper.
  const fieldOptHtml = '<option value="">— Select field —</option>' +
    fields.map(_fieldOptHtml).join('');
  const dateFormatOptHtml = DATE_FORMAT_OPTIONS
    .map(o => `<option value="${escHtml(o.value)}">${escHtml(o.label)}</option>`).join('');
  const dvOptHtml = '<option value="">— chart default —</option>' +
    (_gtDataViews || []).map(dv => `<option value="${escHtml(dv.id)}">${escHtml(dv.label)}</option>`).join('');

  const i = editIdx;
  // Calculation columns hide Data View / Field (no single backing
  // field) and reveal Formula + parser-error. Field-bound columns
  // hide Formula. Both keep Aggregation + Format visible.
  const formulaErrId = `cfg-tc-formula-err-${i}`;
  const formulaInputId = `cfg-tc-formula-input-${i}`;
  const formulaPickerId = `cfg-tc-formula-picker-${i}`;
  const formulaPickerDvId = `cfg-tc-formula-picker-dv-${i}`;
  // Field Picker uses the same option list as the regular Field
  // dropdown above. The first option is a sentinel so the change
  // event only fires on a real pick. Phase 18 follow-up.
  const formulaPickerOptHtml = '<option value="">— Pick field to insert —</option>' +
    fields.map(f => `<option value="${escHtml(f.name)}">${escHtml(f.name)} (${escHtml(f.type || '')})</option>`).join('');
  container.innerHTML = pickerHtml + `
    <div class="tc-col-card tc-col-edit-pane">
      <div class="tc-col-card-body">
        <div class="db-form-group">
          <label>Label</label>
          <input type="text" value="${escHtml(col.label || '')}" oninput="updateTableColumnProp(${i}, 'label', this.value); _refreshTableColPickerLabel(${i})">
        </div>
        <div class="db-form-group tc-data-view-row" style="${isCalc ? 'display:none' : ''}">
          <label>Data View</label>
          <select class="tc-col-dv-select" onchange="onTableColumnDataViewChange(${i}, this.value)">${dvOptHtml}</select>
        </div>
        <div class="db-form-group tc-field-row" style="${isCalc ? 'display:none' : ''}">
          <label>Field</label>
          <select class="tc-col-field-select" onchange="updateTableColumnProp(${i}, 'field', this.value)">${fieldOptHtml}</select>
        </div>
        <div class="db-form-group">
          <label>Aggregation</label>
          <select class="tc-col-agg-select" onchange="updateTableColumnProp(${i}, 'aggregation', this.value)">${aggOptions}</select>
          <p class="db-hint tc-ratio-hint" style="display:none;margin:4px 0 0;color:#888;font-size:11px;"></p>
        </div>
        <div class="db-form-group tc-equals-row" style="${isEquals ? '' : 'display:none'}">
          <label>Equals Value</label>
          <!-- styled combobox: a free-text input with a themed
               suggestion panel (.db-filter-popover, the same value-picker
               styling the dashboard filters use) of the field's distinct
               values, scoped to the topbar team/org. Typing any value is
               ALWAYS allowed; the panel is suggestions only (the field may
               have more distinct values than the agg cap). The wrapper is the
               positioning anchor for the absolutely-positioned panel. -->
          <div class="tc-equals-combo" style="position:relative;">
            <input type="text" class="tc-col-equals-input" autocomplete="off"
                   placeholder="Value to match" value="${escHtml(col.equals_value || '')}"
                   oninput="_onEqualsInput(${i}, this)"
                   onfocus="_openEqualsSuggest(${i}, this)"
                   onchange="updateTableColumnProp(${i}, 'equals_value', this.value); requeryChart()">
            <div class="db-filter-popover tc-equals-suggest" style="display:none;"></div>
          </div>
          <p class="db-hint" style="margin:4px 0 0;color:#888;font-size:11px;">Counts rows where the field equals this value exactly. Pick a suggested value or type any value (e.g. an outcome title).</p>
        </div>
        <div class="db-form-group tc-formula-picker-dv-row" style="${isCalc ? '' : 'display:none'}">
          <label>Field Picker Data View</label>
          <!-- the calc Field Picker can source names from any
               data view, so a formula can mix fields across views (e.g.
               FB reach ÷ Echo conversations). This select only switches
               which view the picker below lists; the durable field→view
               binding is recorded per inserted field in
               col.formula_field_views by insertFieldIntoFormula. -->
          <select id="${formulaPickerDvId}" class="tc-col-formula-picker-dv" onchange="onCalcPickerDvChange(${i}, this.value)">${dvOptHtml}</select>
        </div>
        <div class="db-form-group tc-formula-picker-row" style="${isCalc ? '' : 'display:none'}">
          <label>Field Picker</label>
          <select id="${formulaPickerId}" class="tc-col-formula-picker" onchange="insertFieldIntoFormula(${i}, this.value); this.value='';">${formulaPickerOptHtml}</select>
        </div>
        <div class="db-form-group tc-formula-row" style="${isCalc ? '' : 'display:none'}">
          <label title="e.g. conversions / reach &nbsp;or&nbsp; (a + b) / 2">Formula</label>
          <input type="text" id="${formulaInputId}" class="tc-col-formula" placeholder="conversions / reach" value="${escHtml(col.formula || '')}"
                 oninput="updateTableColumnFormula(${i}, this.value, false)"
                 onblur="_commitTableColumnFormula(${i}, this.value)"
                 onkeydown="if(event.key==='Enter'){event.preventDefault();_commitTableColumnFormula(${i}, this.value);this.blur();}">
          <div id="${formulaErrId}" class="tc-formula-err" style="display:none;"></div>
          <p class="db-hint" style="margin:4px 0 0;color:#888;font-size:11px;">Use bare field names with + − × ÷ and parentheses. Use the Field Picker above to insert names.</p>
        </div>
        <div class="db-form-group tc-date-format-row" style="${isDate ? '' : 'display:none'}">
          <label>Date Format</label>
          <select class="tc-col-date-fmt-select" onchange="updateTableColumnProp(${i}, 'date_format', this.value)">${dateFormatOptHtml}</select>
        </div>
        <div class="db-form-group tc-decimals-row" style="${isDate ? 'display:none' : ''}">
          <label title="e.g. 0.00, $0.00, 1.00%, 0,000.0, 0.00\\%">Format</label>
          <input type="text" maxlength="20" placeholder="0.00"
                 value="${escHtml(col.format !== undefined ? col.format : (col.decimals !== undefined ? (col.decimals === 0 ? '0' : '0.' + '0'.repeat(col.decimals)) : ''))}"
                 oninput="updateTableColumnProp(${i}, 'format', this.value)">
          <p class="db-hint" style="margin:4px 0 0;color:#888;font-size:11px;">
            <code>%</code> multiplies by 100 (e.g. 0.05 → 5%). Use <code>\\%</code> if your value is already a percentage.
          </p>
        </div>
        <!-- per-column heatmap opt-out. Default true; columns
             where higher isn't always "better" (e.g. CTR, Cost) can flip
             this off so the column renders without shading even when the
             table's cell_gradient_enabled flag is on. -->
        <div class="db-form-group db-form-group--checkbox tc-shade-row" style="${isDate ? 'display:none' : ''}">
          <label>
            <input type="checkbox" ${col.shade !== false ? 'checked' : ''}
                   onchange="updateTableColumnProp(${i}, 'shade', this.checked); requeryChart()">
            Apply heatmap shading to this column
          </label>
        </div>
        <!-- per-column totals opt-out. Default true; a column the
             user doesn't want summed (e.g. a rate/ratio, or an ID-like numeric)
             can flip this off. Only takes effect when the table's
             include_totals_row flag is on; shown unconditionally otherwise. -->
        <div class="db-form-group db-form-group--checkbox tc-totals-row">
          <label>
            <input type="checkbox" ${col.include_in_totals !== false ? 'checked' : ''}
                   onchange="updateTableColumnProp(${i}, 'include_in_totals', this.checked)">
            Include in Totals
          </label>
        </div>
      </div>
    </div>
  `;

  // Sync: set dynamic select values for the selected column's pane.
  const card = container.querySelector('.tc-col-edit-pane');
  if (card) {
    const dvSel      = card.querySelector('.tc-col-dv-select');
    const fieldSel   = card.querySelector('.tc-col-field-select');
    const aggSel     = card.querySelector('.tc-col-agg-select');
    const dateFmtSel = card.querySelector('.tc-col-date-fmt-select');
    if (dvSel && col.data_view)    dvSel.value      = col.data_view;
    if (aggSel && col.aggregation) aggSel.value     = col.aggregation;
    if (dateFmtSel && col.date_format) dateFmtSel.value = col.date_format;
    if (fieldSel && col.field && !col.data_view) fieldSel.value = col.field;
    if (fieldSel) _makeSearchableSelect(fieldSel);

    // apply the ratio lock on initial render for columns
    // whose currently-selected field is a registered ratio. Without
    // this, the lock only fires on user-driven field changes; an
    // already-configured ratio column would render with an enabled
    // dropdown.
    if (fieldSel) {
      const currentOpt = Array.from(fieldSel.options).find(o => o.value === fieldSel.value);
      _applyRatioLock(card, currentOpt);
    }

    // Phase 18 follow-up — wrap the calc-column Field Picker
    // with a filter input. Same _makeSearchableSelect pattern as the
    // regular Field dropdown above. The picker stays at its sentinel
    // value ("") because the inline onchange resets it to "" after
    // each insertion.
    const formulaPicker = card.querySelector('.tc-col-formula-picker');
    if (formulaPicker) _makeSearchableSelect(formulaPicker);

    // populate the equals-value combobox suggestions for an
    // equals column whose field is already chosen.
    if (col.aggregation === 'equals' && col.field) {
      _populateEqualsValueOptions(card, col.data_view, col.field);
    }

    // Async: reload field options when this column has its own data view.
    if (col.data_view) {
      _loadTableFields(col.data_view).then(colFields => {
        if (!fieldSel) return;
        // _fieldOptHtml carries the ratio attributes; values
        // are escaped via escHtml in the helper, matching the pattern
        // used elsewhere in this file.
        const optsHtml = '<option value="">— Select field —</option>' + colFields.map(_fieldOptHtml).join('');
        fieldSel.innerHTML = optsHtml;
        if (col.field) fieldSel.value = col.field;
        if (fieldSel._searchInput) { fieldSel._searchInput.remove(); fieldSel._searchInput = null; }
        _makeSearchableSelect(fieldSel);
      });
    }
  }
}

// Switch which column is being edited. Re-renders the editor pane;
// the dropdown itself preserves its value across the re-render.
function selectTableColumn(idx) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  const cols = config.table_columns || [];
  if (idx < 0 || idx >= cols.length) return;
  _tableColEditIdx[SELECTED_CHART_ID] = idx;
  const dvVal = (document.getElementById('cfg-table-data-view') || {}).value || '';
  _loadTableFields(dvVal).then(fields => renderTableColumnsEditor(SELECTED_CHART_ID, fields));
}

// Phase 18 — bare-name formula parser for calculation columns.
// Recursive-descent parser over the tokens emitted by the small
// tokenizer below. Browser-side only (no child_process / shell exec
// involved despite the regex methods named ``exec``).
//
// Grammar (left-associative):
//
//   expr   = term (('+'|'-') term)*
//   term   = factor (('*'|'/') factor)*
//   factor = '(' expr ')'  |  number  |  identifier  |  ('-'|'+') factor
//
//   identifier = [A-Za-z_][A-Za-z0-9_.]*
//   number     = digits ('.' digits)?
//
// Returns { ok: true, fields: [<unique field names>] } on success, or
// { ok: false, error: "<message>" } on the first parse failure. The
// parser doesn't evaluate — it only validates and extracts the
// referenced field names for storage in col.formula_fields. The
// backend Python parser is the authoritative evaluator (T3).
function _parseFormulaJS(formula) {
  if (typeof formula !== 'string') return { ok: false, error: 'Formula must be a string.' };
  const src = formula.trim();
  if (!src) return { ok: false, error: 'Formula is empty.' };

  // Tokenizer — sticky regexes over the source string.
  const tokens = [];
  const idRe = /[A-Za-z_][A-Za-z0-9_.]*/y;
  const numRe = /\d+(?:\.\d+)?/y;
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === ' ' || c === '\t') { i++; continue; }
    if ('+-*/()'.indexOf(c) !== -1) { tokens.push({ kind: c, pos: i }); i++; continue; }
    idRe.lastIndex = i;
    const idM = idRe.exec(src);
    if (idM && idM.index === i) { tokens.push({ kind: 'id', value: idM[0], pos: i }); i = idRe.lastIndex; continue; }
    numRe.lastIndex = i;
    const numM = numRe.exec(src);
    if (numM && numM.index === i) { tokens.push({ kind: 'num', value: numM[0], pos: i }); i = numRe.lastIndex; continue; }
    return { ok: false, error: `Unexpected character "${c}" at position ${i + 1}.` };
  }
  if (tokens.length === 0) return { ok: false, error: 'Formula is empty.' };

  // Recursive-descent parser. ``pos`` is shared across helpers via
  // closure; ``fields`` collects unique identifier names.
  let pos = 0;
  const fields = [];
  const seen = new Set();
  const peek = () => tokens[pos];
  const eat = () => tokens[pos++];

  function parseExpr() {
    let r = parseTerm();
    if (!r.ok) return r;
    while (peek() && (peek().kind === '+' || peek().kind === '-')) {
      eat();
      const right = parseTerm();
      if (!right.ok) return right;
    }
    return { ok: true };
  }

  function parseTerm() {
    let r = parseFactor();
    if (!r.ok) return r;
    while (peek() && (peek().kind === '*' || peek().kind === '/')) {
      eat();
      const right = parseFactor();
      if (!right.ok) return right;
    }
    return { ok: true };
  }

  function parseFactor() {
    const t = peek();
    if (!t) return { ok: false, error: 'Unexpected end of formula.' };
    if (t.kind === '+' || t.kind === '-') {
      eat();
      return parseFactor();
    }
    if (t.kind === '(') {
      eat();
      const inner = parseExpr();
      if (!inner.ok) return inner;
      const close = eat();
      if (!close || close.kind !== ')') return { ok: false, error: `Expected ")" near position ${(close && close.pos + 1) || src.length}.` };
      return { ok: true };
    }
    if (t.kind === 'num') { eat(); return { ok: true }; }
    if (t.kind === 'id')  {
      eat();
      if (!seen.has(t.value)) { seen.add(t.value); fields.push(t.value); }
      return { ok: true };
    }
    return { ok: false, error: `Unexpected "${t.value || t.kind}" at position ${t.pos + 1}.` };
  }

  const r = parseExpr();
  if (!r.ok) return r;
  if (pos < tokens.length) {
    const t = tokens[pos];
    return { ok: false, error: `Unexpected "${t.value || t.kind}" at position ${t.pos + 1}.` };
  }
  return { ok: true, fields };
}

// Update a calculation column's formula. Two callers:
//
// * `oninput` (per keystroke) — passes ``showError=false``. Parses,
//   persists col.formula + col.formula_fields, clears any previously
//   visible error on a successful parse, but does NOT surface a new
//   error mid-typing — "unexpected end of formula" lighting up on
//   every character is annoying when the user is still typing.
//
// * `onkeydown` Enter + `onblur` — pass ``showError=true``. Same
//   parse + persist; this time, if the formula is invalid, the
//   inline red error box appears so the user knows the saved value
//   won't render correctly. Hitting Save / Refresh & Save is the
//   other "I'm done editing" signal that should also surface errors.
//
// Both paths mark the dashboard dirty. Re-querying ES is not done
// here — the bottom "↻ Refresh & Save" button is the live trigger.
function updateTableColumnFormula(colIdx, formula, showError) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config || !config.table_columns) return;
  const col = config.table_columns[colIdx];
  if (!col) return;

  col.formula = formula;
  const errEl = document.getElementById(`cfg-tc-formula-err-${colIdx}`);
  const trimmed = (formula || '').trim();

  if (!trimmed) {
    // Empty formula — clear the field list, always hide error.
    // per-field DV bindings go with it.
    col.formula_fields = [];
    col.formula_field_views = undefined;
    if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
    _markDirty();
    return;
  }

  const r = _parseFormulaJS(trimmed);
  if (!r.ok) {
    col.formula_fields = [];
    if (errEl) {
      if (showError) {
        errEl.textContent = r.error;
        errEl.style.display = '';
      }
      // showError === false: leave the error box as-is. If a previous
      // commit error is showing, it stays until the user fixes the
      // formula or commits again.
    }
    _markDirty();
    return;
  }

  // Successful parse — always clear any visible error so the user
  // sees the formula is now valid, even mid-typing.
  col.formula_fields = r.fields;
  // prune DV bindings for names no longer in the formula,
  // so the sparse map mirrors formula_fields' lifecycle. A name later
  // re-typed by hand (rather than picker-inserted) therefore falls
  // back to the chart-default data view — the picker is the only
  // binding mechanism.
  if (col.formula_field_views) {
    const pruned = {};
    r.fields.forEach(f => {
      if (col.formula_field_views[f]) pruned[f] = col.formula_field_views[f];
    });
    col.formula_field_views = Object.keys(pruned).length ? pruned : undefined;
  }
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  _markDirty();
}

// Commit-style validation: surface errors. Wired to Enter + blur.
function _commitTableColumnFormula(colIdx, value) {
  updateTableColumnFormula(colIdx, value, true);
}

// Phase 18 follow-up — insert a field name into the formula
// input at the current cursor position. The Field Picker dropdown
// next to the formula calls this on `change`; the picker resets to
// its sentinel value ("") after each insertion so the user can pick
// the same field twice in a row if needed (the change event fires
// because the value transitions sentinel → field → sentinel).
//
// Cursor handling: if the input is focused and has a selection, the
// selection is replaced. Otherwise we append at the end. After
// insertion, focus returns to the input with the caret right after
// the inserted text so the next slash / paren the user types lands
// in the right place.
function insertFieldIntoFormula(colIdx, fieldName) {
  if (!fieldName) return;
  const input = document.getElementById(`cfg-tc-formula-input-${colIdx}`);
  if (!input) return;

  // record which data view this field came from. The
  // durable binding lives in col.formula_field_views ({name: dv},
  // sparse — chart-default fields carry no entry), which the backend
  // uses to route each field's sub-agg to its own data view.
  // Re-inserting a name that's already in the formula bound to a
  // DIFFERENT view is blocked: the map holds one view per name, so
  // allowing it would silently re-bind every existing occurrence.
  const config = SELECTED_CHART_ID && DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  const col = config && config.table_columns && config.table_columns[colIdx];
  if (col) {
    const dvSel = document.getElementById(`cfg-tc-formula-picker-dv-${colIdx}`);
    const pickerDv = (dvSel && dvSel.value) || '';
    const views = col.formula_field_views || {};
    const boundDv = views[fieldName] || '';
    const referenced = (col.formula_fields || []).indexOf(fieldName) !== -1;
    if (referenced && boundDv !== pickerDv) {
      const errEl = document.getElementById(`cfg-tc-formula-err-${colIdx}`);
      if (errEl) {
        errEl.textContent = `"${fieldName}" is already in the formula from ` +
          `${boundDv ? 'a different data view' : 'the chart-default data view'} — ` +
          'a field name can only come from one data view per formula.';
        errEl.style.display = '';
      }
      return;
    }
    if (pickerDv) {
      views[fieldName] = pickerDv;
      col.formula_field_views = views;
    } else if (views[fieldName]) {
      delete views[fieldName];
    }
  }
  const before = input.value || '';
  const start = (typeof input.selectionStart === 'number') ? input.selectionStart : before.length;
  const end   = (typeof input.selectionEnd   === 'number') ? input.selectionEnd   : before.length;
  // Pad with a leading space when the preceding char isn't whitespace
  // or an operator/open-paren, so consecutive picks don't collide
  // (e.g. picking "a" then "b" with no space between would yield "ab"
  // instead of "a b" which the parser would treat as the identifier
  // "ab"). Trailing space lets the user type an operator next.
  const prevChar = before.slice(0, start).slice(-1);
  const needLeadSpace = start > 0 && prevChar && !/[\s+\-*/(]/.test(prevChar);
  const insertion = (needLeadSpace ? ' ' : '') + fieldName;
  const next = before.slice(0, start) + insertion + before.slice(end);
  input.value = next;
  // Re-fire the parse + persist pipeline as if the user had typed it.
  updateTableColumnFormula(colIdx, next);
  // Restore focus + place caret right after the inserted text.
  const caret = start + insertion.length;
  try {
    input.focus();
    input.setSelectionRange(caret, caret);
  } catch (_) {
    // Some older browsers throw on setSelectionRange for non-text
    // inputs; ignore — the value was set, the user can click to
    // place the caret.
  }
}

// switch the calc Field Picker's source data view. Reloads
// the picker's option list from the chosen view (chart default when
// blank). Transient UI state: a re-render resets the select to chart
// default; the durable per-field binding is col.formula_field_views,
// written by insertFieldIntoFormula at insertion time.
async function onCalcPickerDvChange(colIdx, dataViewId) {
  const picker = document.getElementById(`cfg-tc-formula-picker-${colIdx}`);
  if (!picker) return;
  const effectiveDv = dataViewId || (document.getElementById('cfg-table-data-view') || {}).value || '';
  const fields = effectiveDv ? await _loadTableFields(effectiveDv) : [];
  picker.innerHTML = '<option value="">— Pick field to insert —</option>' +
    fields.map(f => `<option value="${escHtml(f.name)}">${escHtml(f.name)} (${escHtml(f.type || '')})</option>`).join('');
  // Rebuild the search-filter wrapper for the new option list — same
  // pattern as onTableColumnDataViewChange's field-select refresh.
  if (picker._searchInput) { picker._searchInput.remove(); picker._searchInput = null; }
  _makeSearchableSelect(picker);
}

// Live-update the dropdown's option label as the user types in the
// Label field, so the picker stays in sync without a full re-render.
function _refreshTableColPickerLabel(i) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config || !config.table_columns) return;
  const col = config.table_columns[i];
  if (!col) return;
  const picker = document.getElementById('tc-col-picker');
  if (!picker) return;
  const opt = picker.options[i];
  if (!opt) return;
  opt.textContent = col.label || col.field || `(unnamed column ${i + 1})`;
}

function addTableColumn() {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!_isTableLikeConfig(config)) return;
  if (!config.table_columns) config.table_columns = [];
  config.table_columns.push({ label: '', field: '', aggregation: 'sum', format: '0' });
  // Jump the edit pane to the new column so the user can fill it in.
  _tableColEditIdx[SELECTED_CHART_ID] = config.table_columns.length - 1;
  _markDirty();
  const dvVal = (document.getElementById('cfg-table-data-view') || {}).value || '';
  _loadTableFields(dvVal).then(fields => renderTableColumnsEditor(SELECTED_CHART_ID, fields));
}

function removeTableColumn(colIdx) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config || !config.table_columns) return;
  if (colIdx < 0 || colIdx >= config.table_columns.length) return;
  config.table_columns.splice(colIdx, 1);
  config.y_fields = config.table_columns.map(c => c.field).filter(Boolean);
  // Keep the edit-pane pointer pointing somewhere valid: stay on the
  // same index when possible; clamp down when we removed the last.
  const newLen = config.table_columns.length;
  if (newLen === 0) {
    _tableColEditIdx[SELECTED_CHART_ID] = -1;
  } else if (_tableColEditIdx[SELECTED_CHART_ID] >= newLen) {
    _tableColEditIdx[SELECTED_CHART_ID] = newLen - 1;
  }
  _markDirty();
  const chartId = SELECTED_CHART_ID;
  const dvVal = (document.getElementById('cfg-table-data-view') || {}).value || '';
  _loadTableFields(dvVal).then(fields => {
    renderTableColumnsEditor(chartId, fields);
    if (typeof queryAndRender === 'function') queryAndRender(chartId);
  });
}

function moveTableColumn(colIdx, direction) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config || !config.table_columns) return;
  const newIdx = colIdx + direction;
  if (newIdx < 0 || newIdx >= config.table_columns.length) return;
  const [moved] = config.table_columns.splice(colIdx, 1);
  config.table_columns.splice(newIdx, 0, moved);
  config.y_fields = config.table_columns.map(c => c.field).filter(Boolean);
  // Follow the moved column so it stays in the edit pane.
  _tableColEditIdx[SELECTED_CHART_ID] = newIdx;
  _markDirty();
  const chartId = SELECTED_CHART_ID;
  const dvVal = (document.getElementById('cfg-table-data-view') || {}).value || '';
  _loadTableFields(dvVal).then(fields => {
    renderTableColumnsEditor(chartId, fields);
    if (typeof queryAndRender === 'function') queryAndRender(chartId);
  });
}

function updateTableColumnProp(colIdx, key, value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config || !config.table_columns) return;
  const col = config.table_columns[colIdx];
  if (!col) return;
  col[key] = value;
  // Keep y_fields in sync for the query builder
  config.y_fields = config.table_columns.map(c => c.field).filter(Boolean);
  // Auto-set aggregation when field changes based on field type.
  // Post-T4: the edit pane element is .tc-col-edit-pane (single
  // visible card, always for the currently-selected column).
  if (key === 'field' && value) {
    const card = document.querySelector('.tc-col-edit-pane');
    const fieldSel = card && card.querySelector('.tc-col-field-select');
    const opt = fieldSel && Array.from(fieldSel.options).find(o => o.value === value);
    const fieldType = opt ? (opt.dataset.fieldType || '') : '';
    if (fieldType) {
      const suggestedAgg = _defaultAggForType(fieldType);
      col.aggregation = suggestedAgg;
      const aggSel = card && card.querySelector('.tc-col-agg-select');
      if (aggSel) aggSel.value = suggestedAgg;
      _toggleTableColDateFormat(colIdx, suggestedAgg);
      // the auto-suggested agg is never 'equals'; hide the row.
      const eqRowOnField = card && card.querySelector('.tc-equals-row');
      if (eqRowOnField) eqRowOnField.style.display = (suggestedAgg === 'equals') ? '' : 'none';
    }
    // lock the aggregation dropdown when the picked field is a
    // registered ratio. Backend ignores `aggregation` for these fields,
    // but the UI lock keeps the contract visible to chart authors and
    // prevents the wrong-math-looking-right footgun.
    _applyRatioLock(card, opt);
    // refresh equals-value suggestions for the newly-picked field.
    if (col.aggregation === 'equals') {
      _populateEqualsValueOptions(card, col.data_view, value);
    }
  }
  if (key === 'field' && !value) {
    // Field cleared — undo any prior ratio lock so the dropdown is
    // usable when the user picks a new (non-ratio) field.
    const card = document.querySelector('.tc-col-edit-pane');
    _applyRatioLock(card, null);
  }
  if (key === 'aggregation') {
    _toggleTableColDateFormat(colIdx, value);
    // 'equals' reveals the Equals Value input; any other
    // aggregation hides it (the stored equals_value is kept so switching
    // back doesn't lose the typed value).
    const eqPane = document.querySelector('.tc-col-edit-pane');
    const eqRow = eqPane && eqPane.querySelector('.tc-equals-row');
    if (eqRow) eqRow.style.display = (value === 'equals') ? '' : 'none';
    // load the field's distinct values into the combobox when
    // switching to equals (field already chosen).
    if (value === 'equals' && eqPane && col.field) {
      _populateEqualsValueOptions(eqPane, col.data_view, col.field);
    }
    // Phase 18 — switching to/from "calculation" changes which
    // rows the edit pane shows (Data View + Field hide; Formula
    // appears, and vice versa). Easiest: re-render the editor pane.
    if (value === 'calculation' || (col.formula !== undefined && value !== 'calculation')) {
      // Synthesize a stable __calc_<ts> field so the backend keeps
      // the column in ordered_fields (which filters on col.field).
      // Only set when entering calc mode for the first time; preserve
      // the existing key on re-render so column reorder doesn't
      // change the row identity.
      if (value === 'calculation' && (!col.field || !col.field.startsWith('__calc_'))) {
        col.field = '__calc_' + Date.now();
      }
      // Clear the synthesized __calc_ field when leaving calc mode so
      // the column behaves like a normal field-bound column.
      if (value !== 'calculation' && col.field && col.field.startsWith('__calc_')) {
        col.field = '';
        col.formula = undefined;
        col.formula_fields = undefined;
        col.formula_field_views = undefined;
      }
      const dvVal = (document.getElementById('cfg-table-data-view') || {}).value || '';
      _loadTableFields(dvVal).then(fields => renderTableColumnsEditor(SELECTED_CHART_ID, fields));
    }
  }
  _markDirty();
  // Re-render immediately with cached data when the user-visible
  // formatting changes (format string was "decimals" earlier).
  // include_in_totals only changes the totals row, computed
  // client-side from cached data; re-render in place rather than requery.
  if ((key === 'format' || key === 'include_in_totals') &&
      typeof _lastTableData !== 'undefined' && _lastTableData[SELECTED_CHART_ID]) {
    window.renderTable(SELECTED_CHART_ID, _lastTableData[SELECTED_CHART_ID]);
  }
  // Live picker dropdown label refresh handled by the inline
  // _refreshTableColPickerLabel call on the Label input — see
  // renderTableColumnsEditor.
}

async function onTableColumnDataViewChange(colIdx, dataViewId) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config || !config.table_columns) return;
  const col = config.table_columns[colIdx];
  if (!col) return;
  col.data_view = dataViewId || null;
  col.field = null;
  _markDirty();

  // Post-T4: only the currently-selected column has a visible card
  // (.tc-col-edit-pane). If the data-view change came from a
  // non-selected column (shouldn't happen via UI but guard against
  // programmatic calls), bail out.
  if (colIdx !== _tableColEditIdx[SELECTED_CHART_ID]) return;
  const card = document.querySelector('.tc-col-edit-pane');
  const fieldSel = card && card.querySelector('.tc-col-field-select');
  if (!fieldSel) return;

  const effectiveDv = dataViewId || (document.getElementById('cfg-table-data-view') || {}).value || '';
  const fields = effectiveDv ? await _loadTableFields(effectiveDv) : [];

  // _fieldOptHtml carries the ratio attributes; values are
  // escaped via escHtml inside the helper.
  const optsHtml = '<option value="">— Select field —</option>' + fields.map(_fieldOptHtml).join('');
  fieldSel.innerHTML = optsHtml;
  if (fieldSel._searchInput) { fieldSel._searchInput.remove(); fieldSel._searchInput = null; }
  _makeSearchableSelect(fieldSel);
}

// ══════════════════════════════════════════════════════════════
// Save / Load
// ══════════════════════════════════════════════════════════════

/**
 * Save Changes — update the current dashboard.
 * If no dashboard is active yet, create one first.
 */
async function saveDashboard() {
  // follow-up: a selected canvas page has no charts here — its
  // content saves through the Canvas Editor, its properties through the
  // panel's Save Properties. (The server's cross-type guard would 400 this
  // anyway; fail with a useful message instead.)
  if (_activeCanvasDoc) {
    showToast('This is a canvas page — use Save Properties here, or open it in the Canvas Editor to edit its content.', true);
    return;
  }

  const title = (DASHBOARD_STATE.title || '').trim();

  if (!title) {
    showToast('No dashboard name set — create a new dashboard first.', true);
    return;
  }

  // Capture current slot dimensions so they're persisted in the saved payload
  // Grouped tables size to content — never save a fixed height for them
  Object.keys(DASHBOARD_STATE.charts).forEach(cid => {
    const cfg = DASHBOARD_STATE.charts[cid];
    const slot = document.getElementById(`slot-${cid}`);
    if (slot) {
      if (slot.style.width)  cfg.width  = slot.style.width;
      if (cfg.chart_type === 'grouped_table') {
        cfg.height = null;  // always content-height
      } else if (slot.style.height) {
        cfg.height = slot.style.height;
      }
    }
  });

  const payload = {
    ...DASHBOARD_STATE,
    charts: Object.values(DASHBOARD_STATE.charts),
    // explicit so the wire shape is grep-able and a future
    // refactor that drops the spread doesn't silently lose dashboard
    // filters.
    filters: DASHBOARD_STATE.filters || [],
    // explicit for the same reason: author-curated viewer
    // date-basis choices must survive the save round-trip.
    time_field_choices: DASHBOARD_STATE.time_field_choices || [],
    // the workspace the user is acting in (topbar selector). The
    // server stamps a NEW dashboard with this (validated against the user's
    // resolvable workspaces) instead of guessing — so a superuser can't
    // create a dashboard in the wrong workspace.
    workspace_esid: _currentWorkspaceEsid(),
  };

  try {
    const res = await fetch(DASHBOARD_BASE + 'api/dashboards', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.success) {
      DASHBOARD_STATE.id = data.id;
      _markClean();
      showToast('Dashboard saved.');
      _highlightExplorerItem(data.id);
    } else {
      showToast('Save failed: ' + data.error, true);
    }
  } catch (e) {
    showToast('Save failed: ' + e.message, true);
  }
}

/**
 * New Dashboard — prompts for a name, creates and saves immediately.
 * Shows an unsaved-changes dialog first if the current dashboard is dirty.
 */
async function newDashboard() {
  if (DASHBOARD_STATE.id && _isDirty) {
    const choice = await _confirmUnsaved();
    if (choice === 'cancel') return;
    if (choice === 'save') {
      await saveDashboard();
      if (_isDirty) return;  // save failed — abort
    }
    // 'discard' falls through
  }

  const suggested = await _nextDashboardName('New Dashboard');
  // modal also collects the menu selection. Default to the
  // currently-loaded dashboard's menu so consecutive "New" clicks from
  // the same menu land in the same menu — matches the user's mental
  // model. Falls back to the workspace's first menu when no dashboard
  // is open.
  const defaultMenu = DASHBOARD_STATE.menu || null;
  const result = await _promptDashboardName(suggested, defaultMenu);
  if (result === null) return;  // user cancelled
  const title = result.name;
  const selectedMenu = result.menu || defaultMenu || 'Reports';

  // Reset canvas
  clearCanvas();
  DASHBOARD_STATE.id = null;
  DASHBOARD_STATE.title = title;
  DASHBOARD_STATE.menu = selectedMenu;
  // teams-only visibility. A new dashboard is always ``show_teams``
  // and seeds its allowed teams from the server-rendered defaults — the
  // creator's own teams — so it's immediately visible to them (and only them)
  // until they adjust the Teams checkboxes. No org scope, no ``show_all``.
  const _visDefaults = _builderVisibilityDefaults();
  DASHBOARD_STATE.visibility_mode = _visDefaults.default_mode;
  DASHBOARD_STATE.allowed_org_esids = _visDefaults.default_org_esids.slice();
  DASHBOARD_STATE.allowed_team_esids = _visDefaults.default_team_esids.slice();
  // clear filters when starting a new dashboard.
  DASHBOARD_STATE.filters = [];
  DASHBOARD_STATE.charts = {};
  _markClean();

  // Create on backend immediately (empty dashboard)
  const payload = {
    ...DASHBOARD_STATE,
    charts: [],
    workspace_esid: _currentWorkspaceEsid(),  // stamp the active workspace
  };

  try {
    const res = await fetch(DASHBOARD_BASE + 'api/dashboards', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.success) {
      DASHBOARD_STATE.id = data.id;
      _updateSaveBtn();
      showToast(`Dashboard "${title}" created.`);
      await loadDashboardList();
    } else {
      showToast('Could not create dashboard: ' + data.error, true);
    }
  } catch (e) {
    showToast('Could not create dashboard: ' + e.message, true);
  }

  updateCanvasEmptyState();
  // show the always-visible filter strip with "+ Filters" entry
  // button immediately on a brand-new dashboard so the feature is
  // discoverable before any chart exists.
  if (typeof renderDashboardFilterStrip === 'function') {
    renderDashboardFilterStrip();
  }
}

/**
 * Duplicate Dashboard — clone the currently-loaded dashboard with a [N]
 * suffix on the title. The copy is saved under Reports, hidden by default,
 * and given fresh chart_ids so it's fully independent from the original.
 *
 * If the current dashboard is dirty, prompt to save / discard first so the
 * duplicate matches what's actually persisted (no half-edits leaking into
 * the copy).
 */
async function duplicateDashboard() {
  if (!DASHBOARD_STATE.id) {
    showToast('Open a dashboard first.', true);
    return;
  }

  if (_isDirty) {
    const choice = await _confirmUnsaved();
    if (choice === 'cancel') return;
    if (choice === 'save') {
      await saveDashboard();
      if (_isDirty) return;  // save failed — abort
    }
    // 'discard' falls through; the in-memory state still has dirty edits,
    // but we'll re-fetch from the server below to copy the persisted shape.
  }

  // Re-fetch the persisted dashboard so the duplicate reflects what's
  // actually in ES, not whatever's in memory (which may have been mutated
  // by a 'discard' path).
  let source;
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/dashboards/' + encodeURIComponent(DASHBOARD_STATE.id));
    source = await res.json();
    if (source.error) { showToast('Duplicate failed: ' + source.error, true); return; }
  } catch (e) {
    showToast('Duplicate failed: ' + e.message, true);
    return;
  }

  // This duplicate path always creates an echarts dashboard — leaflet maps
  // have a separate creation path, and canvas documents are
  // duplicated from the canvas editor, not the grid builder.
  if (source.dashboard_type === 'leaflet_map') {
    showToast('Cannot duplicate map dashboards yet.', true);
    return;
  }
  if (source.dashboard_type === 'canvas') {
    showToast('Open canvas pages in the Canvas editor to work with them.', true);
    return;
  }

  const baseName = (source.title || 'Dashboard').replace(/\s*\[\d+\]\s*$/, '');
  const suggested = await _nextDashboardName(baseName);
  // default the modal's menu picker to the source dashboard's
  // menu so the duplicate lands in the same group by default. The user
  // can still pick a different menu in the modal.
  const result = await _promptDashboardName(suggested, source.menu || 'Reports');
  if (result === null) return;  // user cancelled
  const name = result.name;
  const selectedMenu = result.menu || source.menu || 'Reports';

  // Deep-clone charts and assign fresh ids so the original and copy can be
  // edited independently. Date.now() collisions across the loop are
  // avoided by appending a per-chart index.
  const now = Date.now();
  const sourceCharts = Array.isArray(source.charts) ? source.charts : Object.values(source.charts || {});
  const clonedCharts = sourceCharts.map((cfg, i) => {
    const copy = JSON.parse(JSON.stringify(cfg));
    copy.chart_id = 'c' + (now + i);
    return copy;
  });

  // ``menu`` is taken from the modal's picker (the save endpoint
  // honours payload menu at create-time).
  // teams-only visibility. The duplicate is ``show_teams`` and seeds
  // its teams from the current user's defaults (their own teams) — it does NOT
  // inherit the source dashboard's team scope, so the copy is scoped to the
  // creator until they adjust the Teams checkboxes. No org scope, no ``show_all``.
  const _dupVisDefaults = _builderVisibilityDefaults();
  const payload = {
    id: null,
    title: name,
    menu: selectedMenu,
    visibility_mode: _dupVisDefaults.default_mode,
    allowed_org_esids: _dupVisDefaults.default_org_esids.slice(),
    allowed_team_esids: _dupVisDefaults.default_team_esids.slice(),
    global_date_range: source.global_date_range || null,
    charts: clonedCharts,
    workspace_esid: _currentWorkspaceEsid(),  // stamp the active workspace
  };

  try {
    const res = await fetch(DASHBOARD_BASE + 'api/dashboards', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Dashboard "${name}" created (hidden).`);
      await loadDashboardList();
      await loadDashboard(data.id);
    } else {
      showToast('Duplicate failed: ' + data.error, true);
    }
  } catch (e) {
    showToast('Duplicate failed: ' + e.message, true);
  }
}

/**
 * Show an unsaved-changes dialog.
 * Resolves with 'save', 'discard', or 'cancel'.
 */
function _confirmUnsaved() {
  return new Promise(resolve => {
    const modal = document.getElementById('unsaved-modal');
    const nameEl = document.getElementById('unsaved-dash-name');
    if (nameEl) nameEl.textContent = DASHBOARD_STATE.title || 'Untitled Dashboard';
    modal.style.display = 'flex';

    const cleanup = choice => {
      modal.style.display = 'none';
      document.getElementById('unsaved-save').onclick = null;
      document.getElementById('unsaved-discard').onclick = null;
      document.getElementById('unsaved-cancel').onclick = null;
      resolve(choice);
    };

    document.getElementById('unsaved-save').onclick = () => cleanup('save');
    document.getElementById('unsaved-discard').onclick = () => cleanup('discard');
    document.getElementById('unsaved-cancel').onclick = () => cleanup('cancel');
  });
}

// workspace-scoped menu order. Loaded alongside the dashboard
// list and consumed by renderDashboardExplorer + the Create/Properties
// menu pickers. Refreshed whenever loadDashboardList runs.
let _workspaceMenus = [];

async function loadWorkspaceMenus() {
  try {
    // scope the menu list to the selected workspace (topbar) so a
    // multi-workspace superuser sees the same workspace's menus that the
    // reorder/add/delete writes target — otherwise the dragged menu_esids
    // wouldn't match the write workspace and the order would be lost.
    let url = DASHBOARD_BASE + 'api/menus';
    const _ws = _currentWorkspaceEsid();
    if (_ws) url += '?workspace_esid=' + encodeURIComponent(_ws);
    const res = await fetch(url);
    const data = await res.json();
    if (data && data.success && Array.isArray(data.menus)) {
      _workspaceMenus = data.menus;
    }
  } catch (e) {
    console.error('loadWorkspaceMenus error:', e);
  }
}

async function loadDashboardList() {
  try {
    // refresh the workspace menu order before rendering the
    // explorer so the group order is in sync with the latest reorder.
    await loadWorkspaceMenus();
    // scope the explorer list to the selected workspace so a
    // multi-workspace user (superuser) sees only that workspace's dashboards.
    let listUrl = DASHBOARD_BASE + 'api/dashboards';
    const _ws = _currentWorkspaceEsid();
    if (_ws) listUrl += '?workspace_esid=' + encodeURIComponent(_ws);
    const res = await fetch(listUrl);
    const data = await res.json();
    if (!data.success) return;
    // Render the dashboard explorer in the left panel
    renderDashboardExplorer(data.dashboards);
  } catch (e) {
    console.error('loadDashboardList error:', e);
  }
}

// ══════════════════════════════════════════════════════════════
// Dashboard Explorer (left panel)
// ══════════════════════════════════════════════════════════════

// Track which menu groups are expanded in the explorer
const _explorerExpandedGroups = new Set();

function _isWorkspaceAdmin() {
  const explorerEl = document.getElementById('dashboard-explorer');
  return !!(explorerEl && explorerEl.dataset.isWorkspaceAdmin === 'true');
}

// ── Show-hidden-dashboards toggle ────────────────────────────────
// Per-user view preference persisted in localStorage (same idiom as
// the left-panel-collapse state above). Unchecked by default: the
// explorer filters out dashboards with ``visibility_mode == 'hide'``
// until the box is ticked. The last-rendered dashboard list is cached
// so toggling re-renders from memory without re-fetching.
const _SHOW_HIDDEN_LS_KEY = 'db-show-hidden-dashboards';
let _lastRenderedDashboards = [];

// the menu-group reorder Sortable. renderDashboardExplorer runs on
// every dashboard-list load / show-hidden toggle / post-reorder refresh, so
// we MUST destroy the prior instance before creating a new one — otherwise
// stacked Sortables on the same #dashboard-explorer container fight on drop
// and serialize the menu order back to its pre-drag (stored) state, making
// the reorder POST a no-op (200, nothing written). Mirrors drag.js's
// _sortableInstance.destroy() guard for the chart-slot grid.
let _explorerMenuSortable = null;
// one SortableJS instance per menu group's items list (the
// per-dashboard drag-reorder). Tracked so renderDashboardExplorer can
// destroy them all before rebuilding the DOM, same as _explorerMenuSortable.
let _explorerDashboardSortables = [];

function _showHiddenDashboards() {
  try {
    return localStorage.getItem(_SHOW_HIDDEN_LS_KEY) === '1';
  } catch (_) {
    // localStorage can throw in private browsing — default to hiding.
    return false;
  }
}

function _initShowHiddenToggle() {
  const checkbox = document.getElementById('db-show-hidden-checkbox');
  if (!checkbox) return;
  checkbox.checked = _showHiddenDashboards();
  checkbox.addEventListener('change', () => {
    try {
      localStorage.setItem(_SHOW_HIDDEN_LS_KEY, checkbox.checked ? '1' : '0');
    } catch (_) {
      // private browsing — drop persistence; the toggle still works
      // for the current session.
    }
    renderDashboardExplorer(_lastRenderedDashboards);
  });
}

function renderDashboardExplorer(dashboards) {
  const container = document.getElementById('dashboard-explorer');
  if (!container) return;
  // destroy the prior menu-reorder Sortable BEFORE rebuilding the
  // DOM. The instance is bound to the group elements we're about to remove;
  // leaving it (or stacking a new one over it) makes drags revert on drop.
  // Re-created at the end of this function for workspace admins.
  if (_explorerMenuSortable) {
    try { _explorerMenuSortable.destroy(); } catch (_) { /* already gone */ }
    _explorerMenuSortable = null;
  }
  // same teardown for the per-group dashboard-reorder Sortables.
  _explorerDashboardSortables.forEach(s => {
    try { s.destroy(); } catch (_) { /* already gone */ }
  });
  _explorerDashboardSortables = [];
  // Clear via DOM API rather than innerHTML so the security-reminder
  // hook stays quiet (and to avoid any XSS surface if a future
  // refactor ends up concatenating untrusted strings here).
  while (container.firstChild) container.removeChild(container.firstChild);

  // Cache the full (unfiltered) list so the show-hidden toggle can
  // re-render from memory without re-fetching.
  _lastRenderedDashboards = dashboards || [];

  // Filter out hidden dashboards (``visibility_mode == 'hide'``) unless
  // the per-user "Show hidden dashboards" toggle is on. Hidden ones are
  // still marked with a strikethrough label below when shown.
  if (!_showHiddenDashboards()) {
    dashboards = _lastRenderedDashboards.filter(d => d.visibility_mode !== 'hide');
  } else {
    dashboards = _lastRenderedDashboards;
  }

  // Group dashboards by their ``menu`` string field. Empty / missing
  // menus fall into a defensive "Reports" bucket so they remain
  // reachable in the UI.
  const groups = {};
  (dashboards || []).forEach(d => {
    const groupName = (d.menu || '').trim() || 'Reports';
    if (!groups[groupName]) groups[groupName] = [];
    groups[groupName].push(d);
  });

  // render groups in the workspace's configured order
  // (_workspaceMenus, persisted via WorkspaceMenuService). Menus with
  // zero dashboards still render as empty headers so a workspace admin
  // can see the menu they just added even before placing a dashboard
  // under it. Any dashboard whose menu string doesn't match a known
  // workspace menu (defensive — should not happen after rename
  // cascade) gets appended in alphabetical order at the bottom.
  const knownMenuNames = new Set(_workspaceMenus.map(m => m.title));
  const orderedGroupNames = _workspaceMenus.map(m => m.title);
  const unknownGroupNames = Object.keys(groups)
    .filter(g => !knownMenuNames.has(g))
    .sort((a, b) => a.localeCompare(b));
  const renderGroupNames = orderedGroupNames.concat(unknownGroupNames);

  // Sort items within each group by menu_position (nulls last), then title
  Object.values(groups).forEach(items => {
    items.sort((a, b) => {
      const posA = a.menu_position != null ? a.menu_position : Infinity;
      const posB = b.menu_position != null ? b.menu_position : Infinity;
      if (posA !== posB) return posA - posB;
      return a.title.localeCompare(b.title);
    });
  });

  if (renderGroupNames.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'db-explorer-empty';
    empty.textContent = 'No dashboards';
    container.appendChild(empty);
    return;
  }

  // Track which workspace_menu esid each rendered group corresponds
  // to, so the drag-reorder handler can POST ordered_esids without
  // round-tripping through the title.
  const menuEsidByTitle = {};
  _workspaceMenus.forEach(m => { menuEsidByTitle[m.title] = m.esid; });

  renderGroupNames.forEach(groupName => {
    const groupItems = groups[groupName] || [];
    const groupEl = document.createElement('div');
    groupEl.className = 'db-explorer-group';
    // stamp the menu_esid (when known) so SortableJS's
    // drag-reorder handler can read it off the DOM. Empty string when
    // the menu title doesn't match a workspace menu row (defensive \u2014
    // should not happen after rename cascade).
    groupEl.dataset.menuEsid = menuEsidByTitle[groupName] || '';
    groupEl.dataset.menuTitle = groupName;

    const isExpanded = _explorerExpandedGroups.has(groupName);

    // Group header
    const header = document.createElement('div');
    header.className = 'db-explorer-group-header';

    // drag handle for workspace admins. SortableJS uses
    // ``.db-explorer-group-drag-handle`` as its handle so the rest
    // of the header still toggles expand/collapse on click.
    if (_isWorkspaceAdmin()) {
      const dragHandle = document.createElement('span');
      dragHandle.className = 'db-explorer-group-drag-handle';
      dragHandle.title = 'Drag to reorder menu';
      // follow-up: render a 2x3 grid of dots via CSS (six
      // pseudo-element dots) so the drag affordance reads as a
      // recognizable "grip" rather than the prior braille glyphs that
      // were too sparse to read as draggable.
      for (let i = 0; i < 6; i++) {
        const dot = document.createElement('span');
        dot.className = 'db-drag-dot';
        dragHandle.appendChild(dot);
      }
      header.appendChild(dragHandle);
    }

    const arrow = document.createElement('span');
    arrow.className = 'db-explorer-arrow' + (isExpanded ? ' expanded' : '');
    arrow.textContent = '\u25B6';

    const labelSpan = document.createElement('span');
    labelSpan.className = 'db-explorer-group-label';
    labelSpan.textContent = groupName;

    const countSpan = document.createElement('span');
    countSpan.className = 'db-explorer-group-count';
    countSpan.textContent = `(${groupItems.length})`;

    header.appendChild(arrow);
    header.appendChild(labelSpan);
    header.appendChild(countSpan);

    // follow-up: workspace admins can delete an empty menu
    // from the explorer. Renders only for known workspace menus
    // (menu_esid present) and only when the group has zero
    // dashboards — the server endpoint refuses non-empty deletes
    // anyway, but hiding the button when it would 409 keeps the UI
    // honest. Hover-revealed like the dashboard delete button.
    const menuEsid = menuEsidByTitle[groupName];
    if (_isWorkspaceAdmin() && menuEsid && groupItems.length === 0) {
      const delBtn = document.createElement('span');
      delBtn.className = 'db-explorer-menu-delete-btn';
      delBtn.textContent = '×';
      delBtn.title = 'Delete this menu';
      delBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteWorkspaceMenu(menuEsid, groupName);
      });
      header.appendChild(delBtn);
    }

    header.addEventListener('click', () => {
      const itemsContainer = groupEl.querySelector('.db-explorer-items');
      if (_explorerExpandedGroups.has(groupName)) {
        _explorerExpandedGroups.delete(groupName);
        arrow.classList.remove('expanded');
        itemsContainer.classList.add('collapsed');
      } else {
        _explorerExpandedGroups.add(groupName);
        arrow.classList.add('expanded');
        itemsContainer.classList.remove('collapsed');
      }
    });
    groupEl.appendChild(header);

    // Items
    const itemsEl = document.createElement('div');
    itemsEl.className = 'db-explorer-items' + (isExpanded ? '' : ' collapsed');
    groupItems.forEach(d => {
      const item = document.createElement('div');
      item.className = 'db-explorer-item';
      item._dashboardId = d.id;
      item._dashboardMenu = d.menu || 'Reports';
      // stash the id on a data attribute too so the SortableJS
      // onEnd handler can read the new order straight off the DOM (the
      // _dashboardId JS prop is preserved across the drag, but the data
      // attribute keeps the reorder handler symmetric with the menu one).
      item.dataset.dashboardId = d.id;
      if (d.id === DASHBOARD_STATE.id) item.classList.add('active');

      // per-dashboard drag handle for workspace admins, mirroring
      // the menu-group handle. SortableJS uses
      // ``.db-explorer-item-drag-handle`` as its grab target so a normal
      // click on the row still opens the dashboard.
      if (_isWorkspaceAdmin()) {
        const itemHandle = document.createElement('span');
        itemHandle.className = 'db-explorer-item-drag-handle';
        itemHandle.title = 'Drag to reorder dashboard';
        for (let i = 0; i < 6; i++) {
          const dot = document.createElement('span');
          dot.className = 'db-drag-dot';
          itemHandle.appendChild(dot);
        }
        item.appendChild(itemHandle);
      }

      // follow-up: dashboards with ``visibility_mode == 'hide'``
      // are not surfaced on the user-facing /dashboards page, but the
      // Builder still shows them so an editor can un-hide / delete
      // them. Mark them visually with a strikethrough on the label
      // (replaces the prior red \u2715 icon, which read as "deletable"
      // more than "hidden").
      const labelSpan = document.createElement('span');
      labelSpan.className = 'db-explorer-item-label';
      // follow-up: strike through a dashboard that isn't shared with
      // anyone — legacy ``hide``, OR (teams-only) a show_teams dashboard with
      // no allowed teams/orgs, which is private-to-creator and the new
      // equivalent of "hidden from everyone". The server computes ``shared``;
      // fall back to the legacy visibility_mode check when it's absent (older
      // list payloads / cached responses).
      const _unshared = (d.shared === false) || (d.shared === undefined && d.visibility_mode === 'hide');
      // the server names the owning team only when this title repeats
      // in the workspace (one starter-pack copy per team), so same-titled rows
      // can be told apart; a single-team user never receives a label.
      const _titleWithTeam = d.team_label ? d.title + ' · ' + d.team_label : d.title;
      if (_unshared) {
        labelSpan.classList.add('is-hidden');
        item.title = _titleWithTeam + ' — not shared with any team (only you can see it)';
      } else {
        item.title = _titleWithTeam;
      }
      labelSpan.textContent = d.title;
      if (d.team_label) {
        const teamSpan = document.createElement('span');
        teamSpan.className = 'db-explorer-item-team';
        teamSpan.textContent = ' · ' + d.team_label;
        labelSpan.appendChild(teamSpan);
      }
      item.appendChild(labelSpan);

      // follow-up: a click on a canvas row SELECTS it (properties/
      // visibility panel via loadDashboard's canvas branch) like any other
      // dashboard. The Canvas Editor opens only explicitly: the ✎ button or
      // right-click (open to every builder user).
      if (d.dashboard_type === 'canvas' && _canOpenCanvasEditor()) {
        const editBtn = document.createElement('span');
        editBtn.className = 'db-explorer-canvas-edit-btn';
        editBtn.textContent = '✎';
        editBtn.title = 'Open in Canvas Editor';
        editBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          window.location.href = DASHBOARD_BASE + 'canvas?id=' + encodeURIComponent(d.id);
        });
        item.appendChild(editBtn);
        item.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          window.location.href = DASHBOARD_BASE + 'canvas?id=' + encodeURIComponent(d.id);
        });
      }

      // delete button renders for every dashboard regardless
      // of menu. The Reports-only client-side restriction was dropped
      // as part of making menus first-class workspace state; the
      // server delete endpoint still gates on mc_dashboard_builder +
      // visibility_mode.
      //
      // Appended AFTER the canvas ✎ button so on a
      // canvas row the order reads label · ✎ · × — edit is the common
      // action, delete keeps the outermost spot.
      {
        const delBtn = document.createElement('span');
        delBtn.className = 'db-explorer-delete-btn';
        delBtn.textContent = '×';
        delBtn.title = 'Delete dashboard';
        delBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          deleteDashboard(d.id, d.title);
        });
        item.appendChild(delBtn);
      }

      item.addEventListener('click', () => {
        _explorerLoadDashboard(d.id);
      });
      itemsEl.appendChild(item);
    });
    groupEl.appendChild(itemsEl);

    // per-dashboard drag-reorder within this menu group. Only for
    // workspace admins (the server still enforces writes via
    // _require_workspace_admin). Mirrors the menu-group Sortable below; the
    // grab target is the per-item handle so a click on the row still opens
    // the dashboard. The POST persists menu_position 0..n-1 for this menu.
    if (_isWorkspaceAdmin() && typeof Sortable !== 'undefined' && groupItems.length > 1) {
      try {
        const itemSortable = Sortable.create(itemsEl, {
          handle: '.db-explorer-item-drag-handle',
          animation: 150,
          onEnd: () => {
            const orderedIds = Array.from(itemsEl.querySelectorAll('.db-explorer-item'))
              .map(el => el.dataset.dashboardId)
              .filter(id => !!id);
            if (orderedIds.length === 0) return;
            fetch(DASHBOARD_BASE + 'api/dashboards/reorder', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                menu: groupName,
                ordered_dashboard_ids: orderedIds,
                workspace_esid: _currentWorkspaceEsid(),
              }),
            })
              .then(r => r.json())
              .then(d => {
                if (!d || !d.success) {
                  showToast('Could not save dashboard order: ' + ((d && d.error) || 'unknown'), true);
                  loadDashboardList();
                }
                // On success leave the DOM as dropped — it already reflects
                // the saved order; no reload needed (matches the optimistic
                // menu-reorder behavior).
              })
              .catch(err => {
                showToast('Could not save dashboard order: ' + err.message, true);
                loadDashboardList();
              });
          },
        });
        _explorerDashboardSortables.push(itemSortable);
      } catch (e) {
        console.error('SortableJS init failed for dashboard items:', e);
      }
    }

    container.appendChild(groupEl);
  });

  // SortableJS-driven drag-reorder of menu groups. Only
  // initialised for workspace admins (the explorer's data attribute
  // governs disclosure; the server still enforces writes). The
  // ``.db-explorer-group-drag-handle`` we added to each header is the
  // grab target; clicking elsewhere in the header still toggles
  // expand/collapse.
  if (_isWorkspaceAdmin() && typeof Sortable !== 'undefined') {
    try {
      // prior instance already destroyed at the top of
      // renderDashboardExplorer; just create + retain the new one.
      _explorerMenuSortable = Sortable.create(container, {
        handle: '.db-explorer-group-drag-handle',
        animation: 150,
        onEnd: () => {
          // send the on-screen order by TITLE, not esid. A workspace
          // whose menus exist only implicitly (no de_asset_workspace_menu rows
          // → empty dataset.menuEsid) used to collect an empty esid list here
          // and silently bail, so the drag reverted on reload with no error.
          // dataset.menuTitle is always set; the server materializes a row for
          // any title that lacks one (reorder_menus_by_title).
          const orderedTitles = Array.from(container.querySelectorAll('.db-explorer-group'))
            .map(g => g.dataset.menuTitle)
            .filter(t => !!t);
          if (orderedTitles.length === 0) return;
          fetch(DASHBOARD_BASE + 'api/menus/reorder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // stamp the selected workspace so the reorder targets
            // the topbar-selected workspace, not the user's primary one.
            body: JSON.stringify({ ordered_titles: orderedTitles, workspace_esid: _currentWorkspaceEsid() }),
          })
            .then(r => r.json())
            .then(d => {
              if (!d || !d.success) {
                showToast('Could not save menu order: ' + ((d && d.error) || 'unknown'), true);
                loadDashboardList();
              } else {
                loadWorkspaceMenus();
              }
            })
            .catch(err => {
              showToast('Could not save menu order: ' + err.message, true);
              loadDashboardList();
            });
        },
      });
    } catch (e) {
      console.error('SortableJS init failed for menu groups:', e);
    }
  }
}

async function _explorerLoadDashboard(dashboardId) {
  // Guard against unsaved changes
  if (DASHBOARD_STATE.id && _isDirty) {
    const choice = await _confirmUnsaved();
    if (choice === 'cancel') return;
    if (choice === 'save') {
      await saveDashboard();
      if (_isDirty) return;
    }
  }
  loadDashboard(dashboardId);
}

function _highlightExplorerItem(dashboardId) {
  const container = document.getElementById('dashboard-explorer');
  if (!container) return;
  container.querySelectorAll('.db-explorer-item').forEach(item => {
    const isMatch = item._dashboardId === dashboardId;
    item.classList.toggle('active', isMatch);
    // Expand the parent group so the highlighted item is visible
    if (isMatch) {
      const group = item.closest('.db-explorer-group');
      if (group) {
        const itemsContainer = group.querySelector('.db-explorer-items');
        const arrow = group.querySelector('.db-explorer-arrow');
        if (itemsContainer && itemsContainer.classList.contains('collapsed')) {
          itemsContainer.classList.remove('collapsed');
          if (arrow) arrow.classList.add('expanded');
          // Find group name from the label and track expanded state
          const label = group.querySelector('.db-explorer-group-label');
          if (label) _explorerExpandedGroups.add(label.textContent);
        }
      }
    }
  });
}

// ══════════════════════════════════════════════════════════════
// Dashboard Properties Panel (right panel)
// ══════════════════════════════════════════════════════════════

// Cache for org/team options, keyed by the active workspace so
// switching the topbar workspace selector refetches that workspace's teams
// instead of returning the prior workspace's cached list.
let _orgTeamOptionsCache = {};

async function _fetchOrgTeamOptions() {
  // scope the picker to the selected workspace so a multi-workspace
  // user (superuser) only sees that workspace's teams in the properties panel
  // and Select All.
  const wsEsid = _currentWorkspaceEsid();
  const cacheKey = wsEsid || '_all';
  if (_orgTeamOptionsCache[cacheKey]) return _orgTeamOptionsCache[cacheKey];
  try {
    let url = DASHBOARD_BASE + 'api/dashboards/org-team-options';
    if (wsEsid) url += '?workspace_esid=' + encodeURIComponent(wsEsid);
    const res = await fetch(url);
    const data = await res.json();
    if (data.success) {
      _orgTeamOptionsCache[cacheKey] = data;
      return data;
    }
  } catch (e) {
    console.error('Error fetching org/team options:', e);
  }
  return { organizations: [], teams: [] };
}

// visibility is teams-only — the team fields are always shown and
// there is no visibility dropdown to react to. Retained as a no-op so any
// stale caller doesn't throw; the team list renders unconditionally.
function togglePropTeamFields() {
  const fields = document.getElementById('prop-team-fields');
  if (fields) fields.style.display = '';
}

// Select All / Unselect All for the Teams checkbox list. Ticking all
// shares the dashboard with every team the editor can see; unticking all hides
// it from everyone (the teams-only replacement for the old "Hidden" mode).
// Only toggles rows currently visible after a filter is applied is NOT desired
// here — Select All means all teams in the editor's scope, so operate on the
// full list regardless of the filter input.
function propTeamsSelectAll(checked) {
  const list = document.getElementById('prop-teams-list');
  if (!list) return;
  list.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.checked = checked; });
}

async function showDashboardPropertiesPanel() {
  const propsPanel = document.getElementById('dashboard-props-panel');
  if (!propsPanel || !DASHBOARD_STATE.id) return;

  // Hide other right-panel sections
  document.querySelector('.db-panel-right-placeholder').style.display = 'none';
  document.getElementById('config-panel').style.display = 'none';
  const gtPanel = document.getElementById('grouped-table-config');
  if (gtPanel) gtPanel.style.display = 'none';
  const leafletPanel = document.getElementById('leaflet-map-config');
  if (leafletPanel) leafletPanel.style.display = 'none';
  const canvaPanel = document.getElementById('canva-config');
  if (canvaPanel) canvaPanel.style.display = 'none';
  const reportPanel = document.getElementById('report-config');
  if (reportPanel) reportPanel.style.display = 'none';

  // Deselect any selected chart
  document.querySelectorAll('.chart-slot.selected').forEach(s => s.classList.remove('selected'));
  SELECTED_CHART_ID = null;

  // Populate fields
  document.getElementById('prop-title').value = DASHBOARD_STATE.title || '';
  // no visibility dropdown — visibility is the teams checkbox list below.

  // follow-up: for a selected canvas page, offer the jump to the
  // Canvas Editor (open to every builder user).
  const openCanvasBtn = document.getElementById('prop-open-canvas');
  if (openCanvasBtn) {
    openCanvasBtn.style.display = _activeCanvasDoc && _canOpenCanvasEditor() ? '' : 'none';
  }

  // Menu picker — same workspace menus as the Create modal.
  const menuSelect = document.getElementById('prop-menu');
  if (menuSelect) {
    while (menuSelect.firstChild) menuSelect.removeChild(menuSelect.firstChild);
    const menus = (_workspaceMenus && _workspaceMenus.length)
      ? _workspaceMenus.map(m => m.title)
      : [DASHBOARD_STATE.menu || 'Reports'];
    // Include the dashboard's current menu even if it isn't in the
    // workspace list (defensive — dashboards from before the workspace
    // had its menus rebuilt).
    const current = DASHBOARD_STATE.menu || 'Reports';
    if (current && !menus.includes(current)) menus.push(current);
    menus.forEach(title => {
      const opt = document.createElement('option');
      opt.value = title;
      opt.textContent = title;
      menuSelect.appendChild(opt);
    });
    menuSelect.value = current;
  }

  // render the Teams checkbox list (the sole visibility control).
  // The Organizations list is removed — visibility hinges on teams only.
  // ``_fetchOrgTeamOptions`` already scopes the team list to the editor's own
  // teams server-side, so an org editor only ever sees their own teams here.
  // ``data-search`` on each row lets filterCheckboxList() narrow as the user types.
  const options = await _fetchOrgTeamOptions();
  const teamOptions = options.teams || [];
  // follow-up: a LEGACY ``show_all`` dashboard has an empty
  // ``allowed_team_esids`` but reads as "visible to everyone". Rendering it
  // with nothing ticked misrepresents that state — and a careless Save would
  // persist the empty list as ``show_teams`` and silently hide the dashboard.
  // So when the dashboard's STORED visibility is ``show_all``, pre-tick every
  // team the viewer can see (a workspace admin sees the whole workspace; an
  // org editor sees their own teams — the closest faithful representation of
  // "all" within their scope). The first Save converts it to an explicit
  // ``show_teams`` snapshot. A non-workspace-admin's Save is independently
  // blocked by the mutate-scope gate, so this pre-tick is display-only for them.
  let savedTeamEsids;
  if (DASHBOARD_STATE.visibility_mode === 'show_all') {
    savedTeamEsids = teamOptions.map(t => t.esid);
  } else {
    savedTeamEsids = DASHBOARD_STATE.allowed_team_esids || [];
  }
  _renderCheckboxList('prop-teams-list', teamOptions, savedTeamEsids);
  // Clear any prior filter input on re-open so the list isn't
  // mysteriously narrowed.
  const teamsFilter = document.getElementById('prop-teams-filter');
  if (teamsFilter) teamsFilter.value = '';

  togglePropTeamFields();
  propsPanel.style.display = '';
}

// render a checkbox-list of (esid, title) options into a
// container. Items already in ``selectedEsids`` start checked. The
// row's ``data-search`` attribute is the lowercased title so
// ``filterCheckboxList`` can do a substring match without re-reading
// the DOM text on every keystroke.
function _renderCheckboxList(containerId, items, selectedEsids) {
  const container = document.getElementById(containerId);
  if (!container) return;
  while (container.firstChild) container.removeChild(container.firstChild);
  const selectedSet = new Set(selectedEsids || []);
  (items || []).forEach(item => {
    const row = document.createElement('label');
    row.className = 'db-checkbox-row';
    row.dataset.search = (item.title || '').toLowerCase();
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = item.esid;
    cb.checked = selectedSet.has(item.esid);
    const txt = document.createElement('span');
    txt.className = 'db-checkbox-label';
    txt.textContent = item.title || '';
    row.appendChild(cb);
    row.appendChild(txt);
    container.appendChild(row);
  });
}

// filter handler shared by the org and team checkbox lists.
// Called from the ``oninput`` on each filter input.
function filterCheckboxList(containerId, query) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const q = (query || '').trim().toLowerCase();
  container.querySelectorAll('.db-checkbox-row').forEach(row => {
    if (!q || (row.dataset.search || '').includes(q)) {
      row.style.display = '';
    } else {
      row.style.display = 'none';
    }
  });
}

async function saveDashboardProperties() {
  if (!DASHBOARD_STATE.id) {
    showToast('No dashboard selected', true);
    return;
  }

  const title = document.getElementById('prop-title').value.trim();
  // menu now editable from the Properties panel.
  const menuSelectEl = document.getElementById('prop-menu');
  const menu = menuSelectEl ? menuSelectEl.value : (DASHBOARD_STATE.menu || 'Reports');

  if (!title) {
    showToast('Title is required', true);
    return;
  }

  // teams-only visibility. Always send ``show_teams`` + the ticked
  // teams + no org scope. An empty team list means "hidden from everyone"
  // (the replacement for the old Hidden mode). The server re-normalizes and
  // re-validates the team scope against the caller's own teams.
  const teamList = document.getElementById('prop-teams-list');
  const teamEsids = teamList
    ? Array.from(teamList.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value)
    : [];
  const payload = {
    title,
    menu,
    visibility_mode: 'show_teams',
    allowed_org_esids: [],
    allowed_team_esids: teamEsids,
  };

  try {
    const res = await fetch(
      DASHBOARD_BASE + 'api/dashboards/' + encodeURIComponent(DASHBOARD_STATE.id) + '/properties',
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }
    );
    const data = await res.json();
    if (data.success) {
      DASHBOARD_STATE.title = title;
      DASHBOARD_STATE.menu = menu;
      DASHBOARD_STATE.visibility_mode = 'show_teams';
      DASHBOARD_STATE.allowed_org_esids = [];
      DASHBOARD_STATE.allowed_team_esids = teamEsids;
      showToast('Properties saved.');
      await loadDashboardList();
    } else {
      showToast('Save failed: ' + data.error, true);
    }
  } catch (e) {
    showToast('Save failed: ' + e.message, true);
  }
}

function _confirmDeleteDashboard(dashboardId, title) {
  return new Promise(resolve => {
    const modal = document.getElementById('delete-dash-modal');
    const nameEl = document.getElementById('delete-dash-name');
    if (nameEl) nameEl.textContent = title;
    modal.style.display = 'flex';

    const confirmBtn = document.getElementById('delete-dash-confirm');
    const closeModal = (result) => {
      modal.style.display = 'none';
      confirmBtn.onclick = null;
      resolve(result);
    };

    confirmBtn.onclick = () => closeModal(true);
    // Cancel handled by inline onclick on the modal
    modal.querySelector('.db-modal-close').onclick = () => closeModal(false);
    modal.querySelector('.db-btn:not(#delete-dash-confirm)').onclick = () => closeModal(false);
  });
}

async function deleteDashboard(dashboardId, title) {
  const confirmed = await _confirmDeleteDashboard(dashboardId, title);
  if (!confirmed) return;

  try {
    const res = await fetch(
      DASHBOARD_BASE + 'api/dashboards/' + encodeURIComponent(dashboardId),
      { method: 'DELETE' }
    );
    const data = await res.json();
    if (data.success) {
      showToast(`Dashboard "${title}" deleted.`);
      // If the deleted dashboard was the active one, clear the canvas
      if (DASHBOARD_STATE.id === dashboardId) {
        clearCanvas();
        DASHBOARD_STATE.id = null;
        DASHBOARD_STATE.title = '';
        DASHBOARD_STATE.charts = {};
        updateCanvasEmptyState();
      }
      await loadDashboardList();
    } else {
      showToast('Delete failed: ' + data.error, true);
    }
  } catch (e) {
    showToast('Delete failed: ' + e.message, true);
  }
}

// follow-up: select a canvas document for properties editing.
// Loads its metadata into DASHBOARD_STATE (no charts — the grid stays
// empty) and opens the ordinary properties panel, where title/menu/team
// visibility save through the same PATCH /properties route as any
// dashboard. Content editing happens in the Canvas Editor, reached
// explicitly via the row ✎ / right-click / the panel button.
// the canvas preview URL carries the builder's topbar org/team/
// time filters as the same ?org_filter=/&team_filter=/&time_from=/&time_to=
// params the /dashboards page puts on its viewer iframe (canvas_view.js
// reads them at boot). Without them the preview always showed all-time,
// all-org data no matter what the topbar said.
function _canvasPreviewUrl(dashboardId) {
  const orgSelect = document.getElementById('builder-org-filter');
  const teamSelect = document.getElementById('builder-team-filter');
  const params = [];
  const orgVal = orgSelect ? orgSelect.value : '';
  const teamVal = teamSelect ? teamSelect.value : '';
  if (orgVal) params.push('org_filter=' + encodeURIComponent(orgVal));
  if (teamVal) params.push('team_filter=' + encodeURIComponent(teamVal));
  const tr = window.DASHBOARD_TIME_RANGE || {};
  if (tr.from) params.push('time_from=' + encodeURIComponent(tr.from));
  if (tr.to) params.push('time_to=' + encodeURIComponent(tr.to));
  return DASHBOARD_BASE + 'view/' + encodeURIComponent(dashboardId)
    + (params.length ? '?' + params.join('&') : '');
}

// reload the read-only canvas preview when a topbar filter
// changes (the filter handlers only requery DASHBOARD_STATE.charts, and a
// canvas selection has none). No-op unless a canvas doc is selected.
function _reloadCanvasPreview() {
  if (!_activeCanvasDoc || !DASHBOARD_STATE.id) return;
  const frame = document.querySelector('#chart-grid .db-canvas-preview-frame');
  if (frame) frame.src = _canvasPreviewUrl(DASHBOARD_STATE.id);
}

function _applyCanvasDocSelection(data) {
  clearCanvas();
  DASHBOARD_STATE.id = data.id;
  DASHBOARD_STATE.title = data.title || 'Untitled Canvas';
  DASHBOARD_STATE.menu = data.menu || 'Reports';
  DASHBOARD_STATE.visibility_mode = data.visibility_mode || 'show_teams';
  DASHBOARD_STATE.allowed_org_esids = data.allowed_org_esids || [];
  DASHBOARD_STATE.allowed_team_esids = data.allowed_team_esids || [];
  DASHBOARD_STATE.global_date_range = null;
  DASHBOARD_STATE.filters = [];
  DASHBOARD_STATE.time_field_choices = [];
  DASHBOARD_STATE.charts = {};
  _activeCanvasDoc = true;
  // Read-only preview of the canvas content in the center area (the same
  // viewer the /dashboards page embeds) — without it a selected canvas
  // page showed an empty grid, which read as "nothing here".
  const grid = document.getElementById('chart-grid');
  if (grid) {
    const frame = document.createElement('iframe');
    frame.className = 'db-canvas-preview-frame';
    frame.src = _canvasPreviewUrl(data.id); // topbar filters ride along
    frame.title = 'Canvas page preview (read-only)';
    grid.appendChild(frame);
  }
  _markClean();
  updateCanvasEmptyState();
  _highlightExplorerItem(data.id);
  showDashboardPropertiesPanel();
}

async function loadDashboard(dashboardId) {
  if (!dashboardId) { return; }
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/dashboards/' + encodeURIComponent(dashboardId));
    const data = await res.json();
    if (data.error) { showToast('Load failed: ' + data.error, true); return; }

    // follow-up: canvas documents SELECT into properties-only mode
    // (title/menu/visibility via the ordinary panel). The grid builder
    // can't edit canvas_config, and the prior auto-redirect to the
    // canvas editor made visibility unreachable for canvas pages.
    if (data.dashboard_type === 'canvas') {
      _applyCanvasDocSelection(data);
      return;
    }

    clearCanvas();

    DASHBOARD_STATE.id = data.id;
    DASHBOARD_STATE.title = data.title || 'Untitled Dashboard';
    DASHBOARD_STATE.menu = data.menu || 'Reports';
    DASHBOARD_STATE.visibility_mode = data.visibility_mode || 'hide';
    DASHBOARD_STATE.allowed_org_esids = data.allowed_org_esids || [];
    DASHBOARD_STATE.allowed_team_esids = data.allowed_team_esids || [];
    DASHBOARD_STATE.global_date_range = data.global_date_range || null;
    // hydrate dashboard-level filters; legacy docs lacking the
    // field default to [] in the loader, so this is always an array.
    DASHBOARD_STATE.filters = Array.isArray(data.filters) ? data.filters : [];
    // hydrate author-curated date-basis choices (legacy docs -> []).
    DASHBOARD_STATE.time_field_choices = Array.isArray(data.time_field_choices) ? data.time_field_choices : [];
    DASHBOARD_STATE.charts = {};

    if (data.dashboard_type === 'leaflet_map') {
      // Leaflet map dashboards store config in map_config; synthesise a single chart entry.
      const mapConfig = data.map_config || {};
      const chartId = 'map-' + data.id;
      const chart = {
        chart_id: chartId,
        title: data.title || 'Geographic Map',
        chart_type: 'leaflet_map',
        map_statistic: mapConfig.default_statistic || 'reach',
        map_statistics: MAP_STAT_DEFAULTS.map(s => ({...s})),
        width: null,
        height: null,
      };
      DASHBOARD_STATE.charts[chartId] = chart;
      renderChartSlot(chartId, chart);
    } else {
      const charts = Array.isArray(data.charts) ? data.charts : Object.values(data.charts || {});
      charts.forEach(cfg => {
        DASHBOARD_STATE.charts[cfg.chart_id] = cfg;
        renderChartSlot(cfg.chart_id, cfg);
      });
    }

    _markClean();
    updateCanvasEmptyState();
    if (typeof initSortable === 'function') initSortable();

    // Highlight the loaded dashboard in the explorer
    _highlightExplorerItem(dashboardId);

    // Show dashboard properties panel in the right panel on load
    showDashboardPropertiesPanel();

    // render the dashboard-level filter strip if any filters
    // were saved with this dashboard. Hidden when filters[] is empty.
    if (typeof renderDashboardFilterStrip === 'function') {
      renderDashboardFilterStrip();
    }
  } catch (e) {
    showToast('Load failed: ' + e.message, true);
  }
}

function clearCanvas() {
  document.getElementById('chart-grid').innerHTML = '';
  SELECTED_CHART_ID = null;
  _activeCanvasDoc = false;
  document.getElementById('config-panel').style.display = 'none';
  document.getElementById('grouped-table-config').style.display = 'none';
  document.getElementById('leaflet-map-config').style.display = 'none';
  document.getElementById('dashboard-props-panel').style.display = 'none';
  document.querySelector('.db-panel-right-placeholder').style.display = '';
  // clear the dashboard filter strip when switching dashboards
  // so stale filter buttons from the previous dashboard don't linger.
  const _filterStrip = document.getElementById('dashboard-filter-strip');
  if (_filterStrip) {
    _filterStrip.style.display = 'none';
    while (_filterStrip.firstChild) _filterStrip.removeChild(_filterStrip.firstChild);
  }
}

// ══════════════════════════════════════════════════════════════
// Modal helpers
// ══════════════════════════════════════════════════════════════

function showAddChartModal() {
  if (!DASHBOARD_STATE.id) {
    showToast('Create a new dashboard first', true);
    return;
  }
  document.getElementById('add-chart-modal').style.display = 'flex';
}

function hideAddChartModal() {
  document.getElementById('add-chart-modal').style.display = 'none';
}

// Builder Templates picker.
//
// The picker merges two sources: (1) the four hardcoded reports below
// (which are renderer-coupled chart_types like 'report' / 'canva' —
// they create a single chart that renders an entire pre-built report)
// and (2) every doc in de_dashboard_templates fetched from
// GET /api/templates. Both are rendered as uniform name + description
// rows. The filter chips group by connector_id; hardcoded entries
// declare their own connector_id so they participate in the same
// filter as registry entries from the same provider.
//
// Combined Digital Impact Report covers BOTH Meta and Google — it
// declares connector_id 'combined' so it has its own filter pill and
// doesn't get hidden when the user narrows to Meta or Google.
// each hardcoded entry declares ``required_sources`` (a list
// of connector_ids the entry needs) so the row-render path can show
// the same "Connect X to unlock" hint as registry entries. The
// ``Combined Digital Impact Report`` requires BOTH ``meta_ads`` AND
// ``google_ads`` connected; the other entries require one each.
// ``is_available`` is recomputed at modal-open time from the server's
// ``connected_providers`` response (see _annotateHardcodedAvailability).
const _HARDCODED_TEMPLATES = [
  {
    template_id: '__hardcoded_report__',
    chart_type: 'report',
    name: 'Facebook Digital Impact Report',
    description: 'Full-page report with KPI cards, funnel, geographic map, and monthly trends.',
    connector_id: 'meta_ads',
    required_sources: ['meta_ads'],
    is_available: false,
  },
  {
    template_id: '__hardcoded_google_report__',
    chart_type: 'google_report',
    name: 'Google Digital Impact Report',
    description: 'Full-page report with KPI cards, funnel, geographic map, and monthly trends using Google Ads data.',
    connector_id: 'google_ads',
    required_sources: ['google_ads'],
    is_available: false,
  },
  {
    template_id: '__hardcoded_combined_report__',
    chart_type: 'combined_report',
    name: 'Combined Digital Impact Report',
    description: 'Unified report combining Facebook Ads and Google Ads data into a single view with summed metrics.',
    connector_id: 'combined',
    required_sources: ['meta_ads', 'google_ads'],
    is_available: false,
  },
  {
    template_id: '__hardcoded_canva__',
    chart_type: 'canva',
    name: 'Summary Report',
    description: 'Four-section visual summary covering Reach, Engagement, Interaction, and Jesus Journey metrics.',
    connector_id: 'meta_ads',
    required_sources: ['meta_ads'],
    is_available: false,
  },
];

// mirror of TemplateService._CONNECTOR_TO_PROVIDER on the
// Python side. Bridges the wizard connector_id taxonomy
// (``"meta_ads"``) to the OAuth provider strings that de_connection
// stores (``"facebook"``). The server keeps the canonical map; this
// JS mirror is only used to compute is_available for the hardcoded
// Choose Template entries, which the server doesn't see.
const _CONNECTOR_TO_PROVIDER = {
  meta_ads: 'facebook',
  // google_ads / google_analytics_4 / echo currently map 1:1 (the
  // connector_id equals the provider name). Listed here for
  // discoverability; the fallback in _annotateHardcodedAvailability
  // treats an unmapped connector_id as the provider directly.
};

function _annotateHardcodedAvailability(connectedProviders) {
  // Recompute the is_available flag on each hardcoded entry based on
  // the server's list of connected providers. Mutates the entries in
  // place — they're scoped to this module so the mutation is local.
  // An empty connectedProviders list (anonymous / fail-soft) leaves
  // every entry is_available=false; the row-render path then shows
  // each with its "Connect X to unlock" hint instead of pretending
  // they're ready to use.
  const connected = new Set(connectedProviders || []);
  _HARDCODED_TEMPLATES.forEach(t => {
    const requiredProviders = (t.required_sources || []).map(
      c => _CONNECTOR_TO_PROVIDER[c] || c,
    );
    t.is_available = requiredProviders.length === 0
      || requiredProviders.every(p => connected.has(p));
  });
}

// Filter-chip labels. Connectors that show up in either the hardcoded
// list or the registry get a chip; "All" always leads.
const _CONNECTOR_LABELS = {
  '*': 'All',
  meta_ads: 'Meta',
  google_ads: 'Google Ads',
  google_analytics_4: 'Google Analytics',
  echo: 'Echo',
  combined: 'Combined',
};

let _templateState = { items: [], activeFilter: '*', pickMode: null };

// #3 — Edit-template mode state. When a superuser picks a
// template via the "Edit template" toolbar, we load its echarts_config
// into the builder canvas as a *virtual* dashboard (no de_asset_echarts_dashboards
// doc is created) and arm Save & Refresh to write back via the
// freeze endpoint with the SAME template_id (idempotent overwrite —
// _ID is the doc id in de_dashboard_templates). The banner above the
// canvas reminds the curator they're editing a template; Cancel
// restores to a clean state.
let _editingTemplate = null;

async function showTemplateModal() {
  const modal = document.getElementById('template-modal');
  modal.style.display = 'flex';
  // Render the modal in its loading state first so it appears
  // instantly; the registry fetch fills in the dynamic rows when it
  // resolves. The hardcoded entries are always visible.
  // until the registry fetch resolves, the hardcoded
  // entries render as locked (is_available=false from their
  // declaration). The fetch response carries the user's connected
  // providers and recomputes the flag below.
  _templateState.items = _HARDCODED_TEMPLATES.slice();
  _renderTemplateModal();

  try {
    const res = await fetch(DASHBOARD_BASE + 'api/templates');
    const data = await res.json();
    if (data && data.ok && Array.isArray(data.templates)) {
      // recompute is_available on the hardcoded entries
      // from the server's connected_providers list. Registry entries
      // already carry the server-computed flag in their payload.
      _annotateHardcodedAvailability(data.connected_providers || []);
      _templateState.items = _HARDCODED_TEMPLATES.concat(data.templates);
      _renderTemplateModal();
    }
  } catch (_) {
    // Fail-soft: the hardcoded list is already rendered. A failed
    // registry fetch shouldn't block the user from picking a built-in.
  }
}

function hideTemplateModal() {
  document.getElementById('template-modal').style.display = 'none';
  // #3 — clear any active pick mode so the next open of
  // the Choose Template dialog starts in default add-to-canvas mode.
  // (Leaving stale pickMode='delete' would intercept a follow-up
  // template pick as a delete confirmation.)
  _templateState.pickMode = null;
  _updateTemplateModalPrompt();
}

function _renderTemplateModal() {
  const chipsHost = document.getElementById('template-filter-chips');
  const listHost = document.getElementById('template-list');
  if (!chipsHost || !listHost) return;

  // Build the chip set from the connector_ids actually present in the
  // current item list. "All" always leads; the rest are sorted by the
  // _CONNECTOR_LABELS map's natural order.
  const presentConnectors = new Set(_templateState.items.map(t => t.connector_id).filter(Boolean));
  const chipKeys = ['*', ...Object.keys(_CONNECTOR_LABELS).filter(k => k !== '*' && presentConnectors.has(k))];

  while (chipsHost.firstChild) chipsHost.removeChild(chipsHost.firstChild);
  chipKeys.forEach(key => {
    const chip = document.createElement('button');
    chip.className = 'template-chip' + (key === _templateState.activeFilter ? ' active' : '');
    chip.textContent = _CONNECTOR_LABELS[key] || key;
    chip.dataset.connectorId = key;
    chip.onclick = () => {
      _templateState.activeFilter = key;
      _renderTemplateModal();
    };
    chipsHost.appendChild(chip);
  });

  // Render rows filtered by the active chip.
  while (listHost.firstChild) listHost.removeChild(listHost.firstChild);
  const filtered = _templateState.activeFilter === '*'
    ? _templateState.items
    : _templateState.items.filter(t => t.connector_id === _templateState.activeFilter);

  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'template-row';
    empty.style.cursor = 'default';
    const emptyDesc = document.createElement('div');
    emptyDesc.className = 'template-row-desc';
    emptyDesc.textContent = 'No templates in this category.';
    empty.appendChild(emptyDesc);
    listHost.appendChild(empty);
    return;
  }

  filtered.forEach(t => {
    const row = document.createElement('div');
    row.className = 'template-row';
    row.dataset.templateId = t.template_id;
    row.dataset.connectorId = t.connector_id || '';
    row.dataset.isAvailable = t.is_available === false ? 'false' : 'true';

    const title = document.createElement('div');
    title.className = 'template-row-title';
    title.textContent = t.name || '(untitled)';
    row.appendChild(title);

    const desc = document.createElement('div');
    desc.className = 'template-row-desc';
    desc.textContent = t.description || '';
    row.appendChild(desc);

    if (t.is_available === false) {
      const hint = document.createElement('div');
      hint.className = 'template-row-unavailable';
      const required = (t.required_sources || []).map(s => _CONNECTOR_LABELS[s] || s).join(', ');
      hint.textContent = required
        ? `Connect ${required} to unlock.`
        : 'Not available yet.';
      row.appendChild(hint);
    }

    row.onclick = () => _onTemplateRowClick(t);
    listHost.appendChild(row);
  });
}

function _onTemplateRowClick(template) {
  // #3 — Edit + Delete picker modes intercept the click
  // before the normal install/select path.
  if (_templateState.pickMode === 'delete') {
    if (template.chart_type) {
      // Hardcoded built-in reports aren't in the registry, so
      // there's nothing to delete. Block politely.
      showToast('Built-in templates can\'t be deleted.', true);
      return;
    }
    _openTemplateDeleteModal(template);
    return;
  }
  if (_templateState.pickMode === 'edit') {
    if (template.chart_type) {
      showToast('Built-in templates can\'t be edited.', true);
      return;
    }
    _enterTemplateEditMode(template);
    return;
  }
  if (template.is_available === false) {
    // Greyed-out row — the row hint already tells the user what to
    // connect. Clicking the row would do nothing useful.
    return;
  }
  if (template.chart_type) {
    // Hardcoded entry — fall through to the legacy selectTemplate path
    // so the chart_type-coupled rendering still works.
    selectTemplate(template.chart_type);
    return;
  }
  // Registry entry — install via the new endpoint.
  _installRegistryTemplate(template);
}

// ── #3 — Edit + Delete picker modes ──────────────────

function startTemplateEditPick() {
  _templateState.pickMode = 'edit';
  _updateTemplateModalPrompt();
}

function startTemplateDeletePick() {
  _templateState.pickMode = 'delete';
  _updateTemplateModalPrompt();
}

function _updateTemplateModalPrompt() {
  const promptEl = document.getElementById('template-modal-prompt');
  if (!promptEl) return;
  if (_templateState.pickMode === 'edit') {
    promptEl.textContent = 'Pick a template to edit. Its dashboard will load into the builder; saving writes back to the same template.';
  } else if (_templateState.pickMode === 'delete') {
    promptEl.textContent = 'Pick a template to delete from the registry.';
  } else {
    promptEl.textContent = 'Select a report template to add to the canvas:';
  }
}

function _openTemplateDeleteModal(template) {
  const modal = document.getElementById('template-delete-modal');
  if (!modal) return;
  const nameEl = document.getElementById('template-delete-name');
  const inputEl = document.getElementById('template-delete-confirm-input');
  const errEl = document.getElementById('template-delete-error');
  const btnEl = document.getElementById('template-delete-confirm-btn');
  if (nameEl) nameEl.textContent = template.name || template.template_id;
  if (inputEl) {
    inputEl.value = '';
    inputEl.placeholder = template.name || template.template_id;
    // Stash the template_id + expected name on the input so
    // confirmTemplateDelete() can read them back without a closure.
    inputEl.dataset.templateId = template.template_id;
    inputEl.dataset.expectedName = template.name || template.template_id;
  }
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Delete'; }
  hideTemplateModal();
  modal.style.display = 'flex';
}

function closeTemplateDeleteModal() {
  const modal = document.getElementById('template-delete-modal');
  if (modal) modal.style.display = 'none';
  // Reset pick mode so a follow-up open of the Choose Template
  // dialog starts in the default add-to-canvas state.
  _templateState.pickMode = null;
}

async function confirmTemplateDelete() {
  const inputEl = document.getElementById('template-delete-confirm-input');
  const errEl = document.getElementById('template-delete-error');
  const btnEl = document.getElementById('template-delete-confirm-btn');
  if (!inputEl) return;
  const typed = (inputEl.value || '').trim();
  const expected = inputEl.dataset.expectedName || '';
  const templateId = inputEl.dataset.templateId || '';
  if (typed !== expected) {
    if (errEl) {
      errEl.textContent = 'Name does not match. Type it exactly to confirm.';
      errEl.style.display = 'block';
    }
    return;
  }
  if (!templateId) {
    if (errEl) {
      errEl.textContent = 'Missing template id — refresh and try again.';
      errEl.style.display = 'block';
    }
    return;
  }
  if (btnEl) { btnEl.disabled = true; btnEl.textContent = 'Deleting…'; }
  try {
    const resp = await fetch(
      DASHBOARD_BASE + 'api/templates/' + encodeURIComponent(templateId),
      { method: 'DELETE' },
    );
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok || body.ok === false) {
      if (errEl) {
        errEl.textContent = body.error || `Request failed (HTTP ${resp.status}).`;
        errEl.style.display = 'block';
      }
      if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Delete'; }
      return;
    }
    closeTemplateDeleteModal();
    if (typeof showToast === 'function') {
      showToast(`Template "${expected}" deleted.`);
    }
  } catch (_) {
    if (errEl) {
      errEl.textContent = 'Network error — please try again.';
      errEl.style.display = 'block';
    }
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Delete'; }
  }
}

async function _enterTemplateEditMode(template) {
  // #3 (smoke 2026-05-25): the install-then-freeze approach
  // doesn't work for superusers because they have no primary team
  // membership — the install route refuses with "No primary team
  // membership found." Templates are global so a superuser MUST be
  // able to edit them without belonging to any workspace.
  //
  // New approach: fetch the template's full doc (including
  // echarts_config) and hydrate the builder canvas directly. No
  // dashboard doc is written; DASHBOARD_STATE.id stays null. On save
  // the curator submits to PUT /api/templates/<id> (the dedicated
  // template-update endpoint that doesn't need a source dashboard).
  hideTemplateModal();
  _templateState.pickMode = null;

  if (DASHBOARD_STATE.id && _isDirty) {
    const choice = await _confirmUnsaved();
    if (choice === 'cancel') return;
    if (choice === 'save') {
      await saveDashboard();
      if (_isDirty) return;
    }
  }

  try {
    const metaResp = await fetch(
      DASHBOARD_BASE + 'api/templates/' + encodeURIComponent(template.template_id),
    );
    const metaBody = await metaResp.json().catch(() => ({}));
    if (!metaResp.ok || !metaBody || !metaBody.ok || !metaBody.template) {
      showToast(
        (metaBody && metaBody.error) || 'Could not load template for edit.',
        true,
      );
      return;
    }
    const fullTemplate = metaBody.template;
    _hydrateBuilderFromTemplate(fullTemplate);
    _editingTemplate = {
      template_id: fullTemplate.template_id || template.template_id,
      name: fullTemplate.name || template.name || '',
      description: fullTemplate.description || template.description || '',
      pack_id: fullTemplate.pack_id || '',
      connector_id: fullTemplate.connector_id || '',
      menu_title: fullTemplate.menu_title || '',
      menu_position: fullTemplate.menu_position || 0,
      placeholder_field: fullTemplate.placeholder_field || '',
      required_sources: fullTemplate.required_sources || [],
    };
    _renderTemplateEditBanner();
  } catch (e) {
    showToast('Network error: ' + e.message, true);
  }
}

function _hydrateBuilderFromTemplate(template) {
  // Mirror of loadDashboard() for an in-memory template doc. No
  // dashboard_id is written into DASHBOARD_STATE — that's the
  // signal "this is an unsaved edit-template session." The
  // echarts_config shape is identical to a dashboard doc so the
  // existing renderChartSlot loop works unchanged.
  clearCanvas();
  const config = template.echarts_config || {};
  DASHBOARD_STATE.id = null;
  DASHBOARD_STATE.title = template.name || 'Template';
  DASHBOARD_STATE.menu = template.menu_title || 'Reports';
  // teams-only visibility. A template hydrated into a new dashboard
  // is ``show_teams`` seeded with the current user's own teams — never
  // ``show_all`` and never any org scope.
  const _tplVisDefaults = _builderVisibilityDefaults();
  DASHBOARD_STATE.visibility_mode = _tplVisDefaults.default_mode;
  DASHBOARD_STATE.allowed_org_esids = _tplVisDefaults.default_org_esids.slice();
  DASHBOARD_STATE.allowed_team_esids = _tplVisDefaults.default_team_esids.slice();
  DASHBOARD_STATE.global_date_range = config.global_date_range || null;
  DASHBOARD_STATE.filters = Array.isArray(config.filters) ? config.filters : [];
  // carry author-curated date-basis choices through template hydration.
  DASHBOARD_STATE.time_field_choices = Array.isArray(config.time_field_choices) ? config.time_field_choices : [];
  DASHBOARD_STATE.charts = {};

  const charts = Array.isArray(config.charts) ? config.charts : Object.values(config.charts || {});
  charts.forEach(cfg => {
    if (!cfg || !cfg.chart_id) return;
    DASHBOARD_STATE.charts[cfg.chart_id] = cfg;
    if (typeof renderChartSlot === 'function') renderChartSlot(cfg.chart_id, cfg);
  });

  if (typeof _markClean === 'function') _markClean();
  if (typeof updateCanvasEmptyState === 'function') updateCanvasEmptyState();
  if (typeof initSortable === 'function') initSortable();
  if (typeof showDashboardPropertiesPanel === 'function') showDashboardPropertiesPanel();
  if (typeof renderDashboardFilterStrip === 'function') renderDashboardFilterStrip();
}

function _exitTemplateEditMode() {
  _editingTemplate = null;
  const banner = document.getElementById('template-edit-banner');
  if (banner) banner.remove();
}

function _renderTemplateEditBanner() {
  if (!_editingTemplate) return;
  // Replace any previous banner — re-rendering keeps the close
  // button + text in sync if the curator switches templates.
  const existing = document.getElementById('template-edit-banner');
  if (existing) existing.remove();

  const banner = document.createElement('div');
  banner.id = 'template-edit-banner';
  banner.style.cssText = 'background:#fff3cd;border:1px solid #ffeeba;color:#856404;padding:8px 14px;display:flex;justify-content:space-between;align-items:center;gap:12px;font-size:13px;';

  const msg = document.createElement('span');
  const strong = document.createElement('strong');
  strong.textContent = 'Editing template: ';
  msg.appendChild(strong);
  msg.appendChild(document.createTextNode(_editingTemplate.name || _editingTemplate.template_id));
  msg.appendChild(document.createTextNode(' — changes will overwrite the template.'));
  banner.appendChild(msg);

  const actions = document.createElement('span');
  actions.style.cssText = 'display:flex;gap:8px;';

  const saveBtn = document.createElement('button');
  saveBtn.className = 'db-btn db-btn-primary';
  saveBtn.textContent = 'Save changes to template';
  saveBtn.onclick = _openSaveTemplateOverwriteModal;
  actions.appendChild(saveBtn);

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'db-btn';
  cancelBtn.textContent = 'Exit edit mode';
  cancelBtn.onclick = _exitTemplateEditMode;
  actions.appendChild(cancelBtn);

  banner.appendChild(actions);

  // Mount banner directly under the builder topbar so it's visible
  // without scrolling. #3 smoke-followup (2026-05-25 #2): the
  // index.html layout uses .db-topbar, not .db-toolbar — the original
  // selector silently fell back to document.body and the banner was
  // inserted before <script> tags at the top of <body>, invisible to
  // the user.
  const host = document.querySelector('.db-topbar') || document.body;
  if (host && host.parentNode) {
    host.parentNode.insertBefore(banner, host.nextSibling);
  } else {
    document.body.insertBefore(banner, document.body.firstChild);
  }
}

function _openSaveTemplateOverwriteModal() {
  // Pre-fill the Save As Template modal with the editing template's
  // metadata and lock the template_id field. Submit handler detects
  // edit-mode via _editingTemplate and POSTs to PUT /api/templates/<id>
  // (the dedicated update route) instead of POST freeze — superusers
  // have no source dashboard to freeze from.
  if (!_editingTemplate) {
    showToast('Not in template edit mode.', true);
    return;
  }
  showSaveAsTemplateModal();
  // Pre-fill fields. showSaveAsTemplateModal() resets name to the
  // current dashboard's title, so we overwrite that after the modal
  // is open.
  const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  setVal('sat-template-id', _editingTemplate.template_id);
  setVal('sat-pack-id', _editingTemplate.pack_id);
  setVal('sat-connector-id', _editingTemplate.connector_id);
  setVal('sat-menu-title', _editingTemplate.menu_title);
  setVal('sat-menu-position', String(_editingTemplate.menu_position || 0));
  setVal('sat-name', _editingTemplate.name);
  setVal('sat-description', _editingTemplate.description);
  // Lock template_id — the curator must not change it mid-edit
  // (would create a duplicate template rather than overwriting).
  const tidEl = document.getElementById('sat-template-id');
  if (tidEl) { tidEl.readOnly = true; tidEl.style.background = '#f0f0f0'; }
  // Pre-check the original placeholder_field if it's in the list.
  const phField = _editingTemplate.placeholder_field;
  if (phField) {
    document.querySelectorAll('.sat-ph-field').forEach(cb => {
      if (cb.value === phField) cb.checked = true;
    });
    // Re-apply mutex so other boxes get disabled.
    const listEl = document.getElementById('sat-placeholder-fields');
    if (listEl && typeof _satApplyMutex === 'function') _satApplyMutex(listEl);
  }
}

// Build an echarts_config object from the live DASHBOARD_STATE — used
// when overwriting a template (PUT /api/templates/<id> needs the
// config inline because there's no source dashboard doc to read).
function _collectEchartsConfigFromState() {
  return {
    global_date_range: DASHBOARD_STATE.global_date_range || null,
    filters: Array.isArray(DASHBOARD_STATE.filters) ? DASHBOARD_STATE.filters : [],
    charts: Object.values(DASHBOARD_STATE.charts || {}),
  };
}

async function _installRegistryTemplate(template) {
  hideTemplateModal();

  // Handle unsaved changes on the currently-loaded dashboard, same as
  // the hardcoded selectTemplate() does.
  if (DASHBOARD_STATE.id && _isDirty) {
    const choice = await _confirmUnsaved();
    if (choice === 'cancel') return;
    if (choice === 'save') {
      await saveDashboard();
      if (_isDirty) return;
    }
  }

  // prompt for a name + menu (the same modal New Dashboard /
  // Duplicate use) so the installed dashboard lands in the menu the USER
  // picked — not the template author's baked-in menu_title, which may not
  // exist in this workspace ("I installed it but can't find it"). The picker
  // is populated from the current workspace's menus, so the choice is always
  // a visible group. Default the name to the template's display name and the
  // menu to the currently-open dashboard's menu (else the workspace's first).
  const suggestedName = await _nextDashboardName(template.name || 'New Dashboard');
  const defaultMenu = DASHBOARD_STATE.menu || null;
  const result = await _promptDashboardName(suggestedName, defaultMenu);
  if (result === null) return;  // user cancelled — do not install

  // teamEsid is sent only when a team-less privileged user has picked
  // a team from the modal below; the normal path leaves it null and the server
  // resolves the caller's primary team.
  await _doInstallTemplate(template, null, { title: result.name, menu: result.menu });
}

// split out so the team-picker can re-invoke the install with a
// chosen team_esid without re-running the unsaved-changes guard.
// ``nameMenu`` carries the user's chosen {title, menu} from the
// prompt so the team-picker re-invoke preserves them.
async function _doInstallTemplate(template, teamEsid, nameMenu) {
  try {
    const payload = {};
    if (teamEsid) payload.team_esid = teamEsid;
    if (nameMenu && nameMenu.title) payload.title = nameMenu.title;
    if (nameMenu && nameMenu.menu) payload.menu = nameMenu.menu;
    const body = Object.keys(payload).length ? JSON.stringify(payload) : null;
    const res = await fetch(
      DASHBOARD_BASE + 'api/templates/' + encodeURIComponent(template.template_id) + '/install',
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body },
    );
    const data = await res.json();
    // a team-less privileged user must pick a team to bind the
    // template's connected-asset placeholders to. Show the picker and re-POST
    // with their choice rather than dead-ending on an error toast.
    if (data && data.need_team) {
      // the server now auto-picks/teamless, so need_team is no longer
      // returned for install — this branch is defensive. Forward the user's
      // name/menu choice so a re-introduced picker path preserves them.
      _promptTemplateTeam(template, data.teams || [], nameMenu);
      return;
    }
    if (!data || !data.ok) {
      const msg = (data && data.error) || 'Could not install template.';
      showToast(msg, true);
      return;
    }
    // #3 followup #5: distinguish a fresh install from an
    // idempotent re-hit. The route sets ``created: false`` when this
    // (team, template) pair already had a dashboard — the response
    // returned the existing id instead of writing a new doc, so
    // saying "created" misleads the user (they'd look for a new
    // entry in the sidebar that isn't there).
    if (data.created === false) {
      showToast(`Dashboard "${data.title}" already exists — opened.`);
    } else {
      showToast(`Dashboard "${data.title}" created.`);
    }
    await loadDashboardList();
    if (data.dashboard_id) {
      await loadDashboard(data.dashboard_id);
    }
  } catch (e) {
    showToast('Network error: ' + e.message, true);
  }
}

// show the team-picker modal for a team-less privileged user, then
// re-install the template against the chosen team. ``teams`` is the
// workspace-scoped [{esid, title}] list the server returned with need_team.
function _promptTemplateTeam(template, teams, nameMenu) {
  const modal = document.getElementById('template-team-modal');
  const select = document.getElementById('template-team-select');
  const confirmBtn = document.getElementById('template-team-confirm');
  if (!modal || !select || !confirmBtn) {
    showToast('No team available to install this template into.', true);
    return;
  }
  if (!teams.length) {
    showToast('No team available to install this template into.', true);
    return;
  }
  // Populate the picker.
  while (select.firstChild) select.removeChild(select.firstChild);
  teams.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.esid;
    opt.textContent = t.title || t.esid;
    select.appendChild(opt);
  });
  // Rebind the confirm handler (clone to drop any prior listener).
  const fresh = confirmBtn.cloneNode(true);
  confirmBtn.parentNode.replaceChild(fresh, confirmBtn);
  fresh.addEventListener('click', () => {
    const chosen = select.value;
    modal.style.display = 'none';
    if (chosen) _doInstallTemplate(template, chosen, nameMenu);
  });
  modal.style.display = 'flex';
}

// ── New Dashboard name modal ──────────────────────────────
let _newDashResolve = null;

const _TEMPLATE_NAMES = {
  report: 'Facebook Digital Impact Report',
  google_report: 'Google Digital Impact Report',
  combined_report: 'Combined Digital Impact Report',
  canva: 'Summary Report',
};

async function _nextDashboardName(baseName) {
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/dashboards');
    const data = await res.json();
    if (!data.success) return baseName + ' [1]';
    const titles = new Set(data.dashboards.map(d => d.title));
    let n = 1;
    while (titles.has(`${baseName} [${n}]`)) n++;
    return `${baseName} [${n}]`;
  } catch (_) {
    return baseName + ' [1]';
  }
}

function _promptDashboardName(suggestedName, defaultMenu, opts) {
  // this now resolves with ``{ name, menu }`` (or null on
  // cancel) so the caller can persist the user's menu selection.
  // with opts.withTheme (the "+ Canvas" caller) the dialog also
  // shows the built-in theme picker and the result carries ``theme``.
  const withTheme = !!(opts && opts.withTheme);
  return new Promise(resolve => {
    _newDashResolve = resolve;
    const modal = document.getElementById('new-dash-modal');
    const input = document.getElementById('new-dash-name-input');
    const menuSelect = document.getElementById('new-dash-menu-select');
    const themeGroup = document.getElementById('new-dash-theme-group');
    const themeRow = document.getElementById('new-dash-theme-row');
    if (themeGroup) {
      themeGroup.style.display = withTheme ? '' : 'none';
      if (withTheme && themeRow) {
        themeRow.dataset.theme = 'classic';
        themeRow.innerHTML = '<span style="font-size:12px;color:#888;">Loading themes…</span>';
        _fetchCanvasThemes().then(themes => _renderThemePicker(themeRow, themes));
      }
    }
    input.value = suggestedName || '';

    // Populate the menu picker from the workspace menu list. If the
    // workspace menu fetch failed and _workspaceMenus is empty, fall
    // back to a single "Reports" option so the modal still works
    // (matches the legacy hardcoded default).
    if (menuSelect) {
      while (menuSelect.firstChild) menuSelect.removeChild(menuSelect.firstChild);
      const menus = (_workspaceMenus && _workspaceMenus.length)
        ? _workspaceMenus.map(m => m.title)
        : ['Reports'];
      menus.forEach(title => {
        const opt = document.createElement('option');
        opt.value = title;
        opt.textContent = title;
        menuSelect.appendChild(opt);
      });
      const fallback = defaultMenu || menus[0];
      if (menus.includes(fallback)) menuSelect.value = fallback;
    }

    modal.style.display = 'flex';
    setTimeout(() => { input.focus(); input.select(); }, 50);

    document.getElementById('new-dash-confirm').onclick = () => {
      const name = input.value.trim() || suggestedName || 'New Dashboard [1]';
      const menu = menuSelect ? menuSelect.value : (defaultMenu || 'Reports');
      const theme = (withTheme && themeRow && themeRow.dataset.theme) || 'classic';
      modal.style.display = 'none';
      _newDashResolve = null;
      resolve({ name: name, menu: menu, theme: theme });
    };
    input.onkeydown = e => {
      if (e.key === 'Enter') document.getElementById('new-dash-confirm').click();
    };
  });
}

function hideNewDashModal() {
  document.getElementById('new-dash-modal').style.display = 'none';
  if (_newDashResolve) { _newDashResolve(null); _newDashResolve = null; }
}

// ── Add Menu modal ──────────────────────────────────
function showAddMenuModal() {
  const modal = document.getElementById('new-menu-modal');
  const input = document.getElementById('new-menu-name-input');
  const errEl = document.getElementById('new-menu-error');
  if (!modal || !input) return;
  input.value = '';
  if (errEl) {
    errEl.style.display = 'none';
    errEl.textContent = '';
  }
  modal.style.display = 'flex';
  setTimeout(() => input.focus(), 50);

  const confirmBtn = document.getElementById('new-menu-confirm');
  if (!confirmBtn) return;
  confirmBtn.onclick = async () => {
    const title = input.value.trim();
    if (!title) {
      if (errEl) {
        errEl.textContent = 'Menu name is required.';
        errEl.style.display = '';
      }
      return;
    }
    try {
      const res = await fetch(DASHBOARD_BASE + 'api/menus', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // add the menu to the topbar-selected workspace.
        body: JSON.stringify({ title: title, workspace_esid: _currentWorkspaceEsid() }),
      });
      const data = await res.json();
      if (data && data.success) {
        hideAddMenuModal();
        showToast(`Menu "${title}" added.`);
        await loadDashboardList();
      } else {
        const msg = (data && data.error) || 'Could not add menu.';
        if (errEl) {
          errEl.textContent = msg;
          errEl.style.display = '';
        }
      }
    } catch (e) {
      if (errEl) {
        errEl.textContent = 'Network error: ' + e.message;
        errEl.style.display = '';
      }
    }
  };
  input.onkeydown = e => {
    if (e.key === 'Enter') confirmBtn.click();
  };
}

function hideAddMenuModal() {
  const modal = document.getElementById('new-menu-modal');
  if (modal) modal.style.display = 'none';
}

// follow-up: delete an empty workspace menu from the
// Dashboard Builder. Triggered by the small × button on each group
// header (rendered only when the group has zero dashboards). The
// server endpoint at DELETE /api/menus/<esid> additionally refuses
// non-empty deletes with a 409 + ``dashboards_in_menu`` count — so
// we surface that error message inline via showToast if the UI's
// emptiness check somehow stales out.
async function deleteWorkspaceMenu(menuEsid, menuName) {
  if (!menuEsid) return;
  if (!confirm(`Delete the "${menuName}" menu?`)) return;
  try {
    // DELETE has no body, so pass the selected workspace as a
    // query param (the server reads body-then-query for workspace_esid).
    let url = DASHBOARD_BASE + 'api/menus/' + encodeURIComponent(menuEsid);
    const _ws = _currentWorkspaceEsid();
    if (_ws) url += '?workspace_esid=' + encodeURIComponent(_ws);
    const res = await fetch(url, { method: 'DELETE' });
    const data = await res.json();
    if (data && data.success) {
      showToast(`Menu "${menuName}" deleted.`);
      await loadDashboardList();
    } else {
      const msg = (data && data.error) || 'Could not delete menu.';
      showToast(msg, true);
    }
  } catch (e) {
    showToast('Network error: ' + e.message, true);
  }
}

// ── Template selection flow ───────────────────────────────
async function selectTemplate(chartType) {
  hideTemplateModal();

  // Handle unsaved changes
  if (DASHBOARD_STATE.id && _isDirty) {
    const choice = await _confirmUnsaved();
    if (choice === 'cancel') return;
    if (choice === 'save') {
      await saveDashboard();
      if (_isDirty) return;
    }
  }

  const baseName = _TEMPLATE_NAMES[chartType] || 'Untitled Dashboard';
  const suggested = await _nextDashboardName(baseName);
  // default to the currently-loaded dashboard's menu so a
  // template clicked from inside a menu group lands there.
  const defaultMenu = DASHBOARD_STATE.menu || null;
  const result = await _promptDashboardName(suggested, defaultMenu);
  if (result === null) return;
  const name = result.name;
  const selectedMenu = result.menu || defaultMenu || 'Reports';

  // Reset canvas and create new dashboard
  clearCanvas();
  DASHBOARD_STATE.id = null;
  DASHBOARD_STATE.title = name;
  DASHBOARD_STATE.menu = selectedMenu;
  // teams-only visibility. Reset to the current user's defaults
  // (``show_teams`` + their own teams) so a template launched after editing a
  // team-scoped dashboard doesn't inherit that dashboard's visibility. No
  // ``show_all``, no org scope.
  const _tplNewVisDefaults = _builderVisibilityDefaults();
  DASHBOARD_STATE.visibility_mode = _tplNewVisDefaults.default_mode;
  DASHBOARD_STATE.allowed_org_esids = _tplNewVisDefaults.default_org_esids.slice();
  DASHBOARD_STATE.allowed_team_esids = _tplNewVisDefaults.default_team_esids.slice();
  DASHBOARD_STATE.charts = {};
  _markClean();

  const payload = { ...DASHBOARD_STATE, charts: [] };

  try {
    const res = await fetch(DASHBOARD_BASE + 'api/dashboards', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.success) {
      DASHBOARD_STATE.id = data.id;
      _updateSaveBtn();
      await loadDashboardList();

      // Add the template chart to the new dashboard
      addChartToCanvas(chartType);
      showToast(`Dashboard "${name}" created.`);
    } else {
      showToast('Could not create dashboard: ' + data.error, true);
    }
  } catch (e) {
    showToast('Could not create dashboard: ' + e.message, true);
  }

  updateCanvasEmptyState();
}

// Backdrop click no longer dismisses the builder modals (site-wide change — an
// accidental outside-click shouldn't close a dialog or discard input). Escape is
// the deliberate keyboard dismiss; each modal's X / Cancel / Done button still
// closes it. On Escape, hide whichever .db-modal-overlay is currently open (all
// of them show via style.display = 'flex' and hide via 'none').
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  document.querySelectorAll('.db-modal-overlay').forEach(ov => {
    if (ov.style.display && ov.style.display !== 'none') {
      ov.style.display = 'none';
    }
  });
});

// ══════════════════════════════════════════════════════════════
// Utilities
// ══════════════════════════════════════════════════════════════

// Match data view IDs flexibly: "*fb_insights" matches "*_fb_insights"
// because Kibana stores the underscore but metric defaults omit it.
function _dvMatch(a, b) {
  if (!a || !b) return false;
  if (a === b) return true;
  const norm = s => s.replace(/^\*_?/, '');
  return norm(a) === norm(b);
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Returns the default aggregation for a given ES field type.
// Text/keyword fields can't be summed — use 'text' (returns raw value).
// Date fields return the raw date value via 'date' (top_hits).
function _defaultAggForType(fieldType) {
  if (fieldType === 'text' || fieldType === 'keyword') return 'text';
  if (fieldType === 'date') return 'date';
  return 'sum';
}

// Reflects the select's current selection into its filter input's
// placeholder, so a picker with a value reads as e.g.
// "campaign_name (keyword)" instead of the generic "Filter fields…".
// Empty selection (or the "— …—" sentinel, value === '') falls back to
// the filter hint. The placeholder is display-only — it never touches
// the select's value or a user's in-progress filter text, so every
// picker that shares _makeSearchableSelect keeps its filter semantics.
function _syncSearchablePlaceholder(selectEl) {
  const input = selectEl._searchInput;
  if (!input) return;
  const opt = selectEl.options[selectEl.selectedIndex];
  const hasSelection = opt && opt.value !== '';
  input.placeholder = hasSelection ? opt.textContent : 'Filter fields...';
}

// Wraps a <select> with a filter text input above it.
// Idempotent: safe to call on every options refresh.
// styled equals-value combobox. Fetch the field's distinct values
// (scoped to the topbar team/org via the endpoint) and cache them
// on the input element; the themed suggestion panel (.db-filter-popover) is
// rendered/filtered on focus + input. Suggestions only: the input is free-text,
// so a value absent from the list (or beyond the agg cap) is still accepted.
// Soft-fails to an empty suggestion set.
async function _populateEqualsValueOptions(card, columnDataView, field) {
  if (!card || !field) return;
  const input = card.querySelector('.tc-col-equals-input');
  if (!input) return;

  // Column's own data view, else the chart default.
  let dv = columnDataView;
  if (!dv && SELECTED_CHART_ID) {
    const cfg = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
    dv = cfg && cfg.data_view;
  }
  if (!dv) { input._equalsSuggestions = []; return; }

  // Scope to the topbar team/org filters (helper lives in dashboard_filters.js,
  // loaded before builder.js). Guard in case it's unavailable.
  let teamFilters = [];
  let orgFilter = '';
  if (typeof _currentTopbarFilters === 'function') {
    ({ teamFilters, orgFilter } = _currentTopbarFilters());
  }

  try {
    const params = new URLSearchParams();
    teamFilters.forEach(t => params.append('team_filter', t));
    if (orgFilter) params.append('org_filter', orgFilter);
    const qs = params.toString();
    const url = DASHBOARD_BASE + 'api/data-views/' + encodeURIComponent(dv) +
                '/field/' + encodeURIComponent(field) + '/values' + (qs ? '?' + qs : '');
    const res = await fetch(url);
    const data = await res.json();
    const values = (data && data.success) ? (data.values || []) : [];
    // Endpoint returns bare strings or {label, value}; the equals filter
    // matches the VALUE, label is shown when it differs.
    input._equalsSuggestions = values
      .map(v => (v && typeof v === 'object')
        ? { value: v.value, label: v.label }
        : { value: v, label: v })
      .filter(o => o.value !== undefined && o.value !== null && o.value !== '');
  } catch (e) {
    input._equalsSuggestions = [];  // free-text entry still works
  }
}

// Render the themed suggestion panel for an equals input, filtered by the
// current typed text. Click fills the value; typing anything is still free.
function _renderEqualsSuggest(input) {
  const panel = input.parentNode && input.parentNode.querySelector('.tc-equals-suggest');
  if (!panel) return;
  const all = input._equalsSuggestions || [];
  const q = (input.value || '').toLowerCase();
  const matches = all.filter(o =>
    !q || String(o.label).toLowerCase().includes(q) || String(o.value).toLowerCase().includes(q)
  );
  panel.innerHTML = '';
  if (!matches.length) {
    panel.style.display = 'none';
    return;
  }
  const list = document.createElement('div');
  list.className = 'db-filter-popover-list';
  matches.slice(0, 500).forEach(o => {
    const item = document.createElement('div');
    item.className = 'db-filter-popover-item';
    item.textContent = String(o.label);
    item.title = String(o.value);
    // mousedown (not click) so it fires before the input's blur/close.
    item.addEventListener('mousedown', (ev) => {
      ev.preventDefault();
      input.value = String(o.value);
      panel.style.display = 'none';
      // Persist + requery via the same path as a manual change/onchange.
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    list.appendChild(item);
  });
  panel.appendChild(list);
  panel.style.display = '';
}

function _openEqualsSuggest(colIdx, input) {
  _renderEqualsSuggest(input);
  // Close when focus/cursor leaves the combo wrapper.
  if (!input._equalsBlurBound) {
    input._equalsBlurBound = true;
    input.addEventListener('blur', () => {
      // Delay so an item mousedown lands first.
      setTimeout(() => {
        const panel = input.parentNode && input.parentNode.querySelector('.tc-equals-suggest');
        if (panel) panel.style.display = 'none';
      }, 120);
    });
  }
}

function _onEqualsInput(colIdx, input) {
  _renderEqualsSuggest(input);
}

function _makeSearchableSelect(selectEl) {
  const allOptions = Array.from(selectEl.options).map(o => ({
    value: o.value,
    text: o.textContent,
    fieldType: o.dataset.fieldType || ''
  }));

  if (selectEl._searchInput) {
    // Already wrapped — refresh stored options and clear any prior filter
    selectEl._searchInput.value = '';
    selectEl._allOptions = allOptions;
    _syncSearchablePlaceholder(selectEl);
    return;
  }

  const searchInput = document.createElement('input');
  searchInput.type = 'search';
  searchInput.className = 'field-search-input';
  searchInput.placeholder = 'Filter fields...';
  selectEl._searchInput = searchInput;
  selectEl._allOptions = allOptions;

  selectEl.parentNode.insertBefore(searchInput, selectEl);

  // Keep the placeholder in sync as the user picks a different option.
  selectEl.addEventListener('change', () => _syncSearchablePlaceholder(selectEl));
  _syncSearchablePlaceholder(selectEl);

  searchInput.addEventListener('input', () => {
    const q = searchInput.value.toLowerCase();
    const currentVal = selectEl.value;
    selectEl.innerHTML = '';
    (selectEl._allOptions || []).forEach(({ value, text, fieldType }) => {
      if (!q || value === '' || text.toLowerCase().includes(q)) {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = text;
        if (fieldType) opt.dataset.fieldType = fieldType;
        selectEl.appendChild(opt);
      }
    });
    selectEl.value = currentVal;
    _syncSearchablePlaceholder(selectEl);
  });
}

function chartTypeLabel(type) {
  const labels = { bar: 'Bar Chart', line: 'Line Chart', pie: 'Pie Chart', donut: 'Donut Chart', table: 'Table', metric: 'Metric', grouped_table: 'Grouped Table', leaflet_map: 'Geographic Map', canva: 'Canva', report: 'Report', google_report: 'Google Report', combined_report: 'Combined Report', profile: 'Profile', posts_table: 'FB Page Posts' };
  return labels[type] || type;
}

// formatNumber is defined in charts.js (loaded before builder.js)

// ══════════════════════════════════════════════════════════════
// Canva — metric override editor (right panel)
// ══════════════════════════════════════════════════════════════

// Hardcoded metric defaults — must match the M array in renderCanvaChart
const _CANVA_SECTIONS = [
  {
    label: 'How Many People Saw Our Content?',
    metrics: [
      { id: 'fb-reach',   label: 'Facebook Reach (sum of daily, not unique)',   dv: '*fb_insights', field: 'insight_reach',           agg: 'sum' },
      { id: 'ig-reach',   label: 'Instagram Reach',  dv: '*ig_insights', field: 'reach',                   agg: 'sum' },
      { id: 'web-users',  label: 'Web Users',        dv: '*_ga_metrics', field: 'totalUsers',              agg: 'sum' },
      { id: 'meta-reach', label: 'Facebook Page Viewers (sum of daily, not unique)', dv: '*_fb_page_insights',  field: 'page_total_media_view_unique',             agg: 'sum' },
      { id: 'tt-video-views', label: 'TikTok Video Views', dv: '*_tiktok_organic', field: 'video_views_gained',                    agg: 'sum' },
    ],
  },
  {
    label: 'How did they engage with our content?',
    metrics: [
      // dv must be a REAL data-view pattern; the old '*fb_page' /
      // '*youtube' shortcuts matched no index (empty-scope sentinel → tile read
      // 0). fb-shares also swapped page_post_engagements (all engagements) for
      // page_posts_shares_total (the actual shares count). Keep in sync with
      // charts.js M[] + canvas_seed.py _CANVA_SEED_METRICS.
      // lifetime-to-date snapshot over recent videos (Meta exposes
      // no daily page-level 15s metric); MAX reads the latest snapshot, sum
      // would multiply lifetime values across days.
      { id: 'vid-15s',      label: '15s Views (lifetime)', dv: '*_fb_page_insights',  field: 'page_video_views_15s_total',        agg: 'max' },
      { id: 'vid-time',     label: 'View Time (min)',    dv: '*_youtube_analytics', field: 'estimated_minutes_watched',         agg: 'sum' },
      { id: 'app-installs', label: 'App Installs',       dv: '*google_ads',  field: 'app_installs',                      agg: 'sum' },
      { id: 'fb-reactions', label: 'FB Reactions',        dv: '*_fb_page_insights',  field: 'page_actions_post_reactions_total',  agg: 'sum' },
      { id: 'fb-shares',    label: 'FB Shares',           dv: '*_fb_page_insights',  field: 'page_posts_shares_total',           agg: 'sum' },
      // paid twins (Meta ads); action_type 'post' is FB's shares action.
      { id: 'fbads-thruplays', label: 'Meta Ads ThruPlays', dv: '*fb_insights', field: 'insight_video_thruplay_watched_actions_video_view_total', agg: 'sum' },
      { id: 'fbads-reactions', label: 'Meta Ads Reactions', dv: '*fb_insights', field: 'insight_actions_post_reaction',     agg: 'sum' },
      { id: 'fbads-shares',    label: 'Meta Ads Shares',    dv: '*fb_insights', field: 'insight_actions_post',              agg: 'sum' },
      { id: 'ig-follows',   label: 'IG Follows',          dv: '*ig_insights', field: 'follows',                           agg: 'sum' },
      { id: 'ig-reactions', label: 'IG Reactions',         dv: '*ig_insights', field: 'likes',                             agg: 'sum' },
      { id: 'ig-shares',    label: 'IG Shares',            dv: '*ig_insights', field: 'shares',                            agg: 'sum' },
      { id: 'yt-subs',      label: 'YT Subscriptions',    dv: '*_youtube_analytics', field: 'subscribers_gained',                agg: 'sum' },
      { id: 'yt-reactions', label: 'YT Reactions',         dv: '*_youtube_analytics', field: 'likes',                             agg: 'sum' },
      { id: 'yt-shares',    label: 'YT Shares',            dv: '*_youtube_analytics', field: 'shares',                            agg: 'sum' },
      // TikTok (followers current = latest snapshot, max; the rest sum daily gains)
      { id: 'tt-followers', label: 'TT Followers (current)', dv: '*_tiktok_organic', field: 'follower_count',                  agg: 'max' },
      { id: 'tt-follows',   label: 'TT Follows',           dv: '*_tiktok_organic',    field: 'followers_gained',                  agg: 'sum' },
      { id: 'tt-views',     label: 'TT Views',             dv: '*_tiktok_organic',    field: 'video_views_gained',                agg: 'sum' },
      { id: 'tt-reactions', label: 'TT Reactions',         dv: '*_tiktok_organic',    field: 'likes_gained',                      agg: 'sum' },
      { id: 'tt-shares',    label: 'TT Shares',            dv: '*_tiktok_organic',    field: 'video_shares_gained',               agg: 'sum' },
    ],
  },
  {
    label: 'How did they interact with us?',
    metrics: [
      // `also`: extra sources summed into the tile (Chatwoot beside
      // Echo). The editor's override replaces only the primary dv/field/agg.
      { id: 'msg-contacts', label: 'Messaging Contacts',      dv: '*echo_conversations', field: 'client_id',                             agg: 'cardinality',
        also: [{ dv: '*_chatwoot_insights', field: 'contacts_started', agg: 'sum' }] },
      { id: 'msg-received', label: 'Messages Received',       dv: '*echo_conversations', field: 'incomingMessagesCount',                 agg: 'sum',
        also: [{ dv: '*_chatwoot_insights', field: 'incoming_messages_count', agg: 'sum' }] },
      { id: 'msg-sent',     label: 'Messages Sent',           dv: '*echo_conversations', field: 'outboundMessagesCount',                 agg: 'sum',
        also: [{ dv: '*_chatwoot_insights', field: 'outgoing_messages_count', agg: 'sum' }] },
      // Page Insights comments are retired at Meta; count comments on ads instead.
      { id: 'comments',     label: 'Comments (ads)',          dv: '*fb_insights',        field: 'insight_actions_comment',               agg: 'sum' },
      { id: 'spiritual',    label: 'Spiritual Conversations', dv: '*echo_conversations', field: 'spiritual_conversation',                agg: 'equals',
        also: [{ dv: '*_chatwoot_insights', field: 'contact_attr_spiritual_conversation', agg: 'sum' }] },
    ],
  },
  {
    label: 'Where are they on their Jesus Journey?',
    metrics: [
      // team-keyed pattern (alias retired). post_type is a
      // BARE keyword on the pull schema (no .keyword subfield) — these editor
      // defaults pre-fill the override form, so a stale .keyword here would
      // re-break the metric the moment an author touches the card. Keep the
      // field paths and equals_value defaults in sync with charts.js `M`.
      { id: 'baptisms',       label: 'Baptisms',       dv: '*_disciple_tools', field: 'meta.milestones.keyword',  agg: 'equals', equals_value: 'milestone_baptized' },
      { id: 'new-groups',     label: 'New Groups',     dv: '*_disciple_tools', field: 'post_type',                agg: 'equals', equals_value: 'groups' },
      { id: 'new-contacts',   label: 'New Contacts',   dv: '*_disciple_tools', field: 'post_type',                agg: 'equals', equals_value: 'contacts' },
      { id: 'first-meetings', label: 'First Meetings', dv: '*_disciple_tools', field: 'meta.seeker_path.keyword', agg: 'equals', equals_value: 'met' },
      { id: 'new-churches',   label: 'New Churches',   dv: '*_disciple_tools', field: 'meta.church_start_date',   agg: 'exists' },
    ],
  },
];

// Track selected section index so it persists across re-renders
let _canvaSelectedSection = 0;

async function _renderCanvaMetricsEditor(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  const container = document.getElementById('canva-metrics-editor');
  if (!container) return;

  if (!config.metric_overrides) config.metric_overrides = {};

  // Fetch data views once
  let dataViews = [];
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/data-views');
    const data = await res.json();
    if (data.success) dataViews = data.data_views || [];
  } catch (e) {}

  container.innerHTML = '';

  // Section dropdown
  const secSel = document.createElement('select');
  secSel.style.cssText = 'width:100%;font-size:12px;margin-bottom:10px;padding:5px;';
  _CANVA_SECTIONS.forEach((sec, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = sec.label;
    if (i === _canvaSelectedSection) opt.selected = true;
    secSel.appendChild(opt);
  });
  container.appendChild(secSel);

  // Metrics container for the selected section
  const metricsDiv = document.createElement('div');
  container.appendChild(metricsDiv);

  const renderSection = async (sectionIdx) => {
    metricsDiv.innerHTML = '';
    const section = _CANVA_SECTIONS[sectionIdx];
    if (!section) return;

    for (const def of section.metrics) {
      const ov = config.metric_overrides[def.id] || {};
      const currentDv = ov.dv || def.dv;
      const currentField = ov.field || def.field;
      const currentAgg = ov.agg || def.agg;

      const card = document.createElement('div');
      card.style.cssText = 'border:1px solid #444;border-radius:6px;padding:8px;margin-bottom:8px;background:#1a1a2e;';

      // Label
      const lbl = document.createElement('div');
      lbl.style.cssText = 'font-size:12px;font-weight:600;color:#ccc;margin-bottom:6px;';
      lbl.textContent = def.label;
      card.appendChild(lbl);

      // Data view select
      const dvLabel = document.createElement('label');
      dvLabel.style.cssText = 'font-size:11px;color:#999;display:block;margin-bottom:2px;';
      dvLabel.textContent = 'Data View';
      card.appendChild(dvLabel);

      const dvSel = document.createElement('select');
      dvSel.style.cssText = 'width:100%;font-size:12px;margin-bottom:6px;';
      const dvEmpty = document.createElement('option');
      dvEmpty.value = '';
      dvEmpty.textContent = '\u2014 Data view \u2014';
      dvSel.appendChild(dvEmpty);
      dataViews.forEach(dv => {
        const opt = document.createElement('option');
        opt.value = dv.id;
        opt.textContent = dv.label;
        if (_dvMatch(dv.id, currentDv)) opt.selected = true;
        dvSel.appendChild(opt);
      });
      card.appendChild(dvSel);

      // Field select
      const fLabel = document.createElement('label');
      fLabel.style.cssText = 'font-size:11px;color:#999;display:block;margin-bottom:2px;';
      fLabel.textContent = 'Field';
      card.appendChild(fLabel);

      const fieldSel = document.createElement('select');
      fieldSel.style.cssText = 'width:100%;font-size:12px;margin-bottom:6px;';
      fieldSel.innerHTML = '<option value="">— Field —</option>';
      card.appendChild(fieldSel);

      // Load fields for current data view
      if (currentDv) {
        try {
          const res = await fetch(DASHBOARD_BASE + 'api/data-views/' + encodeURIComponent(currentDv) + '/fields');
          const data = await res.json();
          if (data && data.no_data_yet) _appendNoDataYetOption(fieldSel, data.message);
          if (data.success && data.fields) {
            data.fields.forEach(f => {
              const opt = document.createElement('option');
              opt.value = f.name;
              opt.textContent = `${f.name} (${f.type})`;
              if (f.name === currentField) opt.selected = true;
              fieldSel.appendChild(opt);
            });
          }
        } catch (e) {}
        _makeSearchableSelect(fieldSel);
      }

      // Aggregation select
      const aLabel = document.createElement('label');
      aLabel.style.cssText = 'font-size:11px;color:#999;display:block;margin-bottom:2px;';
      aLabel.textContent = 'Aggregation';
      card.appendChild(aLabel);

      const aggSel = document.createElement('select');
      aggSel.style.cssText = 'width:100%;font-size:12px;';
      const aggDisplayLabels = { cardinality: 'unique' };
      ['sum', 'avg', 'min', 'max', 'count', 'cardinality', 'equals', 'exists', 'query date'].forEach(agg => {
        const opt = document.createElement('option');
        opt.value = agg;
        opt.textContent = aggDisplayLabels[agg] || agg;
        if (agg === currentAgg) opt.selected = true;
        aggSel.appendChild(opt);
      });
      card.appendChild(aggSel);

      // Equals Value input (shown only when aggregation is
      // 'equals'). Without it an author could pick equals here but never set
      // the match text; the server then ignores equals with no value and
      // silently falls back to a sum.
      const eqLabel = document.createElement('label');
      eqLabel.style.cssText = 'font-size:11px;color:#999;display:none;margin-top:4px;margin-bottom:2px;';
      eqLabel.textContent = 'Equals Value';
      card.appendChild(eqLabel);

      const eqInput = document.createElement('input');
      eqInput.type = 'text';
      eqInput.placeholder = 'Value to match';
      eqInput.style.cssText = 'display:none;width:100%;font-size:12px;box-sizing:border-box;';
      eqInput.value = ov.equals_value ?? def.equals_value ?? '';
      card.appendChild(eqInput);

      const updateEqualsVisibility = () => {
        const show = aggSel.value === 'equals';
        eqLabel.style.display = show ? 'block' : 'none';
        eqInput.style.display = show ? 'block' : 'none';
      };
      updateEqualsVisibility();

      // DT Unix Timestamp checkbox
      const unixRow = document.createElement('label');
      unixRow.style.cssText = 'display:none;font-size:11px;color:#999;margin-top:4px;cursor:pointer;align-items:center;gap:4px;';
      const unixChk = document.createElement('input');
      unixChk.type = 'checkbox';
      unixChk.checked = ov.dt_date_unix ?? def.dt_date_unix ?? false;
      unixRow.appendChild(unixChk);
      unixRow.appendChild(document.createTextNode(' DT Unix Timestamp'));
      card.appendChild(unixRow);

      const updateUnixVisibility = () => {
        // Dual-spelling: hyphen = legacy alias configs, underscore =
        // team-keyed pull indices (the routing family).
        const dvVal = dvSel.value || def.dv || '';
        const show = aggSel.value === 'query date' && (dvVal.includes('disciple-tools') || dvVal.includes('disciple_tools'));
        unixRow.style.display = show ? 'flex' : 'none';
      };
      updateUnixVisibility();

      // Tooltip text. Shown on hover over the rendered tile. When
      // left blank the viewer derives "{source} · {field}" (the placeholder
      // mirrors that default via _canvaDefaultTooltip from charts.js). The
      // placeholder tracks the current dv/field so the author sees the live
      // default before overriding.
      const ttLabel = document.createElement('label');
      ttLabel.style.cssText = 'font-size:11px;color:#999;display:block;margin-top:6px;margin-bottom:2px;';
      ttLabel.textContent = 'Tooltip text';
      card.appendChild(ttLabel);

      const ttInput = document.createElement('input');
      ttInput.type = 'text';
      ttInput.style.cssText = 'width:100%;font-size:12px;box-sizing:border-box;';
      ttInput.value = ov.tooltip_text || '';
      card.appendChild(ttInput);

      const updateTooltipPlaceholder = () => {
        const dvVal = dvSel.value || def.dv || '';
        const fVal = fieldSel.value || def.field || '';
        ttInput.placeholder = _canvaDefaultTooltip(dvVal, fVal);
      };
      updateTooltipPlaceholder();

      // Save overrides
      const metricId = def.id;
      const saveOverride = () => {
        if (!config.metric_overrides[metricId]) config.metric_overrides[metricId] = {};
        config.metric_overrides[metricId].dv = dvSel.value;
        config.metric_overrides[metricId].field = fieldSel.value;
        config.metric_overrides[metricId].agg = aggSel.value;
        config.metric_overrides[metricId].dt_date_unix = unixChk.checked;
        config.metric_overrides[metricId].equals_value = eqInput.value;
        config.metric_overrides[metricId].tooltip_text = ttInput.value;
        _markDirty();
      };

      dvSel.onchange = async () => {
        fieldSel.innerHTML = '<option value="">— Field —</option>';
        if (dvSel.value) {
          try {
            const res = await fetch(DASHBOARD_BASE + 'api/data-views/' + encodeURIComponent(dvSel.value) + '/fields');
            const data = await res.json();
            if (data && data.no_data_yet) _appendNoDataYetOption(fieldSel, data.message);
            if (data.success && data.fields) {
              data.fields.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f.name;
                opt.textContent = `${f.name} (${f.type})`;
                fieldSel.appendChild(opt);
              });
            }
          } catch (e) {}
          _makeSearchableSelect(fieldSel);
        }
        updateUnixVisibility();
        updateTooltipPlaceholder();
        saveOverride();
      };
      fieldSel.onchange = () => { updateTooltipPlaceholder(); saveOverride(); };
      aggSel.onchange = () => { updateEqualsVisibility(); updateUnixVisibility(); saveOverride(); };
      unixChk.onchange = saveOverride;
      eqInput.onchange = saveOverride;
      ttInput.onchange = saveOverride;

      metricsDiv.appendChild(card);
    }
  };

  secSel.onchange = () => {
    _canvaSelectedSection = parseInt(secSel.value, 10);
    renderSection(_canvaSelectedSection);
  };

  // Render initial section
  await renderSection(_canvaSelectedSection);
}

// ══════════════════════════════════════════════════════════════
// Report — metric definitions & config panel helpers
// ══════════════════════════════════════════════════════════════

function updateReportProp(key, value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config[key] = value;
  _markDirty();
  // Re-render report to reflect org name change
  if (key === 'org_name' && typeof renderReportChart === 'function') renderReportChart(SELECTED_CHART_ID);
}

const _REPORT_SECTIONS = [
  {
    label: 'Reach & Stewardship',
    metrics: [
      { id: 'kpi-reach',         label: 'People Reached',  dv: '*fb_insights',        field: 'insight_reach',  agg: 'sum' },
      { id: 'kpi-spend',         label: 'Total Investment', dv: '*fb_insights',        field: 'insight_spend',  agg: 'sum' },
      { id: 'kpi-conversations', label: 'Unique Conversations',    dv: '*echo_conversations', field: 'client_id', agg: 'cardinality' },
    ],
  },
  {
    label: 'The Engagement Journey',
    metrics: [
      { id: 'funnel-impressions',   label: 'Impressions',            dv: '*fb_insights',        field: 'insight_impressions',                agg: 'sum' },
      { id: 'funnel-reach',         label: 'People Reached',         dv: '*fb_insights',        field: 'insight_reach',                      agg: 'sum' },
      { id: 'funnel-clicks',        label: 'Link Clicks',            dv: '*fb_insights',        field: 'insight_inline_link_clicks',         agg: 'sum' },
      { id: 'funnel-conversations', label: 'Unique Conversations',   dv: '*echo_conversations', field: 'client_id',                          agg: 'cardinality' },
    ],
  },
  {
    label: 'Geographic Reach',
    metrics: [
      { id: 'geo', label: 'Reach by Country', dv: '*fb_insights', field: 'insight_reach', agg: 'sum' },
    ],
  },
  {
    label: 'Monthly Trends',
    metrics: [
      { id: 'trend-reach',         label: 'People Reached', dv: '*fb_insights',        field: 'insight_reach',              agg: 'sum' },
      { id: 'trend-clicks',        label: 'Link Clicks',    dv: '*fb_insights',        field: 'insight_inline_link_clicks', agg: 'sum' },
      { id: 'trend-conversations', label: 'Unique Conversations',  dv: '*echo_conversations', field: 'client_id',                 agg: 'cardinality' },
    ],
  },
];

const _GOOGLE_REPORT_SECTIONS = [
  {
    label: 'Reach & Performance',
    metrics: [
      { id: 'kpi-reach',         label: 'Impressions',     dv: '*google_ads', field: 'impressions',  agg: 'sum' },
      { id: 'kpi-spend',         label: 'Total Investment',dv: '*google_ads', field: 'cost',         agg: 'sum' },
      { id: 'kpi-conversations', label: 'App Installs',    dv: '*google_ads', field: 'app_installs', agg: 'sum' },
    ],
  },
  {
    label: 'The Engagement Journey',
    metrics: [
      { id: 'funnel-impressions', label: 'Impressions',  dv: '*google_ads', field: 'impressions',  agg: 'sum' },
      { id: 'funnel-clicks',      label: 'Clicks',       dv: '*google_ads', field: 'clicks',       agg: 'sum' },
      { id: 'funnel-conversions',  label: 'Conversions',  dv: '*google_ads', field: 'conversions',  agg: 'sum' },
      { id: 'funnel-installs',    label: 'App Installs', dv: '*google_ads', field: 'app_installs', agg: 'sum' },
    ],
  },
  {
    label: 'Geographic Reach',
    metrics: [
      { id: 'geo', label: 'Installs by Country', dv: '*google_ads', field: 'app_installs', agg: 'sum' },
    ],
  },
  {
    label: 'Monthly Trends',
    metrics: [
      { id: 'trend-reach',    label: 'Impressions',  dv: '*google_ads', field: 'impressions',  agg: 'sum' },
      { id: 'trend-clicks',   label: 'Clicks',       dv: '*google_ads', field: 'clicks',       agg: 'sum' },
      { id: 'trend-installs', label: 'App Installs', dv: '*google_ads', field: 'app_installs', agg: 'sum' },
    ],
  },
];

const _COMBINED_REPORT_SECTIONS = [
  {
    label: 'Reach & Performance',
    metrics: [
      { id: 'kpi-reach',         label: 'Total Impressions', dv: '*fb_insights', field: 'insight_impressions', agg: 'sum' },
      { id: 'kpi-spend',         label: 'Total Investment',  dv: '*fb_insights', field: 'insight_spend',       agg: 'sum' },
      { id: 'kpi-conversations', label: 'Total Responses',   dv: '*echo_conversations', field: 'client_id',    agg: 'cardinality' },
    ],
  },
  {
    label: 'The Engagement Journey',
    metrics: [
      { id: 'funnel-impressions', label: 'Impressions', dv: '*fb_insights', field: 'insight_impressions',        agg: 'sum' },
      { id: 'funnel-clicks',      label: 'Clicks',      dv: '*fb_insights', field: 'insight_inline_link_clicks', agg: 'sum' },
      { id: 'funnel-responses',   label: 'Responses',   dv: '*echo_conversations', field: 'client_id',           agg: 'cardinality' },
    ],
  },
  {
    label: 'Geographic Reach',
    metrics: [
      { id: 'geo', label: 'Reach by Country', dv: '*fb_insights', field: 'insight_reach', agg: 'sum' },
    ],
  },
  {
    label: 'Monthly Trends',
    metrics: [
      { id: 'trend-impressions', label: 'Impressions', dv: '*fb_insights', field: 'insight_impressions',        agg: 'sum' },
      { id: 'trend-clicks',      label: 'Clicks',      dv: '*fb_insights', field: 'insight_inline_link_clicks', agg: 'sum' },
      { id: 'trend-responses',   label: 'Responses',   dv: '*echo_conversations', field: 'client_id',           agg: 'cardinality' },
    ],
  },
];

let _reportSelectedSection = 0;

async function _renderReportMetricsEditor(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  const sections = config.chart_type === 'google_report' ? _GOOGLE_REPORT_SECTIONS
    : config.chart_type === 'combined_report' ? _COMBINED_REPORT_SECTIONS
    : _REPORT_SECTIONS;
  const container = document.getElementById('report-metrics-editor');
  if (!container) return;

  if (!config.metric_overrides) config.metric_overrides = {};

  let dataViews = [];
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/data-views');
    const data = await res.json();
    if (data.success) dataViews = data.data_views || [];
  } catch (e) {}

  container.innerHTML = '';

  const secSel = document.createElement('select');
  secSel.style.cssText = 'width:100%;font-size:12px;margin-bottom:10px;padding:5px;';
  sections.forEach((sec, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = sec.label;
    if (i === _reportSelectedSection) opt.selected = true;
    secSel.appendChild(opt);
  });
  container.appendChild(secSel);

  // "Show All" checkbox for toggling entire section visibility
  const showAllRow = document.createElement('div');
  showAllRow.style.cssText = 'display:flex;align-items:center;gap:6px;margin-bottom:10px;';
  const showAllCb = document.createElement('input');
  showAllCb.type = 'checkbox';
  showAllCb.id = 'rpt-show-all-section';
  const showAllLabel = document.createElement('label');
  showAllLabel.htmlFor = 'rpt-show-all-section';
  showAllLabel.style.cssText = 'font-size:12px;color:#333;cursor:pointer;user-select:none;font-weight:500;';
  showAllLabel.textContent = 'Show all in this section';
  showAllRow.appendChild(showAllCb);
  showAllRow.appendChild(showAllLabel);
  container.appendChild(showAllRow);

  const metricsDiv = document.createElement('div');
  container.appendChild(metricsDiv);

  const _updateShowAllCheckbox = (section) => {
    if (!config.hidden_metrics) config.hidden_metrics = [];
    const allHidden = section.metrics.every(m => config.hidden_metrics.includes(m.id));
    const noneHidden = section.metrics.every(m => !config.hidden_metrics.includes(m.id));
    showAllCb.checked = !allHidden && noneHidden ? true : !allHidden;
    showAllCb.indeterminate = !allHidden && !noneHidden;
  };

  showAllCb.onchange = () => {
    const section = sections[_reportSelectedSection];
    if (!section) return;
    if (!config.hidden_metrics) config.hidden_metrics = [];
    if (showAllCb.checked) {
      // Show all: remove all section metric IDs from hidden
      section.metrics.forEach(m => {
        const idx = config.hidden_metrics.indexOf(m.id);
        if (idx >= 0) config.hidden_metrics.splice(idx, 1);
      });
    } else {
      // Hide all: add all section metric IDs to hidden
      section.metrics.forEach(m => {
        if (!config.hidden_metrics.includes(m.id)) config.hidden_metrics.push(m.id);
      });
    }
    _markDirty();
    renderSection(_reportSelectedSection);
  };

  const renderSection = async (sectionIdx) => {
    metricsDiv.innerHTML = '';
    const section = sections[sectionIdx];
    if (!section) return;
    _updateShowAllCheckbox(section);

    for (const def of section.metrics) {
      const ov = config.metric_overrides[def.id] || {};
      const currentDv = ov.dv || def.dv;
      const currentField = ov.field || def.field;
      const currentAgg = ov.agg || def.agg;

      if (!config.hidden_metrics) config.hidden_metrics = [];
      const isHidden = config.hidden_metrics.includes(def.id);

      const card = document.createElement('div');
      card.style.cssText = 'border:1px solid #444;border-radius:6px;padding:8px;margin-bottom:8px;background:#1a1a2e;';

      // Header row with label input + hide/show button
      const hdr = document.createElement('div');
      hdr.style.cssText = 'display:flex;justify-content:space-between;align-items:center;gap:6px;' + (isHidden ? '' : 'margin-bottom:6px;');
      const metricId = def.id;
      const currentLabel = (config.metric_overrides[metricId] || {}).label || '';
      const lblInput = document.createElement('input');
      lblInput.type = 'text';
      lblInput.placeholder = def.label;
      lblInput.value = currentLabel;
      lblInput.style.cssText = 'flex:1;font-size:12px;font-weight:600;color:#ccc;background:transparent;border:1px solid transparent;border-radius:3px;padding:2px 4px;' + (isHidden ? 'opacity:0.4;' : '');
      lblInput.onfocus = () => { lblInput.style.borderColor = '#666'; };
      lblInput.onblur = () => {
        lblInput.style.borderColor = 'transparent';
        if (!config.metric_overrides[metricId]) config.metric_overrides[metricId] = {};
        const val = lblInput.value.trim();
        if (val && val !== def.label) {
          config.metric_overrides[metricId].label = val;
        } else {
          delete config.metric_overrides[metricId].label;
          lblInput.value = '';
        }
        _markDirty();
      };
      hdr.appendChild(lblInput);

      const hideBtn = document.createElement('button');
      hideBtn.style.cssText = 'background:none;border:1px solid #666;border-radius:4px;color:#aaa;font-size:11px;padding:2px 8px;cursor:pointer;flex-shrink:0;';
      hideBtn.textContent = isHidden ? 'Show' : 'Hide';
      hideBtn.title = isHidden ? 'Show this metric in the report' : 'Hide this metric from the report';
      hideBtn.onclick = () => {
        if (!config.hidden_metrics) config.hidden_metrics = [];
        const idx = config.hidden_metrics.indexOf(def.id);
        if (idx >= 0) {
          config.hidden_metrics.splice(idx, 1);
        } else {
          config.hidden_metrics.push(def.id);
        }
        _markDirty();
        renderSection(_reportSelectedSection);
      };
      hdr.appendChild(hideBtn);
      card.appendChild(hdr);

      // When hidden, only show the header row (label + show/hide toggle)
      if (isHidden) {
        metricsDiv.appendChild(card);
        continue;
      }

      const dvLabel = document.createElement('label');
      dvLabel.style.cssText = 'font-size:11px;color:#999;display:block;margin-bottom:2px;';
      dvLabel.textContent = 'Data View';
      card.appendChild(dvLabel);

      const dvSel = document.createElement('select');
      dvSel.style.cssText = 'width:100%;font-size:12px;margin-bottom:6px;';
      const dvEmpty = document.createElement('option');
      dvEmpty.value = '';
      dvEmpty.textContent = '\u2014 Data view \u2014';
      dvSel.appendChild(dvEmpty);
      dataViews.forEach(dv => {
        const opt = document.createElement('option');
        opt.value = dv.id;
        opt.textContent = dv.label;
        if (_dvMatch(dv.id, currentDv)) opt.selected = true;
        dvSel.appendChild(opt);
      });
      card.appendChild(dvSel);

      const fLabel = document.createElement('label');
      fLabel.style.cssText = 'font-size:11px;color:#999;display:block;margin-bottom:2px;';
      fLabel.textContent = 'Field';
      card.appendChild(fLabel);

      const fieldSel = document.createElement('select');
      fieldSel.style.cssText = 'width:100%;font-size:12px;margin-bottom:6px;';
      fieldSel.innerHTML = '<option value="">— Field —</option>';
      card.appendChild(fieldSel);

      if (currentDv) {
        try {
          const res = await fetch(DASHBOARD_BASE + 'api/data-views/' + encodeURIComponent(currentDv) + '/fields');
          const data = await res.json();
          if (data && data.no_data_yet) _appendNoDataYetOption(fieldSel, data.message);
          if (data.success && data.fields) {
            data.fields.forEach(f => {
              const opt = document.createElement('option');
              opt.value = f.name;
              opt.textContent = `${f.name} (${f.type})`;
              if (f.name === currentField) opt.selected = true;
              fieldSel.appendChild(opt);
            });
          }
        } catch (e) {}
        _makeSearchableSelect(fieldSel);
      }

      const aLabel = document.createElement('label');
      aLabel.style.cssText = 'font-size:11px;color:#999;display:block;margin-bottom:2px;';
      aLabel.textContent = 'Aggregation';
      card.appendChild(aLabel);

      const aggSel = document.createElement('select');
      aggSel.style.cssText = 'width:100%;font-size:12px;';
      ['sum', 'avg', 'min', 'max', 'count', 'cardinality'].forEach(agg => {
        const opt = document.createElement('option');
        opt.value = agg;
        opt.textContent = agg === 'cardinality' ? 'unique' : agg;
        if (agg === currentAgg) opt.selected = true;
        aggSel.appendChild(opt);
      });
      card.appendChild(aggSel);

      const saveOverride = () => {
        if (!config.metric_overrides[metricId]) config.metric_overrides[metricId] = {};
        config.metric_overrides[metricId].dv = dvSel.value;
        config.metric_overrides[metricId].field = fieldSel.value;
        config.metric_overrides[metricId].agg = aggSel.value;
        _markDirty();
      };

      dvSel.onchange = async () => {
        fieldSel.innerHTML = '<option value="">— Field —</option>';
        if (dvSel.value) {
          try {
            const res = await fetch(DASHBOARD_BASE + 'api/data-views/' + encodeURIComponent(dvSel.value) + '/fields');
            const data = await res.json();
            if (data && data.no_data_yet) _appendNoDataYetOption(fieldSel, data.message);
            if (data.success && data.fields) {
              data.fields.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f.name;
                opt.textContent = `${f.name} (${f.type})`;
                fieldSel.appendChild(opt);
              });
            }
          } catch (e) {}
          _makeSearchableSelect(fieldSel);
        }
        saveOverride();
      };
      fieldSel.onchange = saveOverride;
      aggSel.onchange = saveOverride;

      metricsDiv.appendChild(card);
    }
  };

  secSel.onchange = () => {
    _reportSelectedSection = parseInt(secSel.value, 10);
    renderSection(_reportSelectedSection);
  };

  await renderSection(_reportSelectedSection);
}

let _toastTimeout = null;
function showToast(msg, isError = false) {
  let toast = document.getElementById('db-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'db-toast';
    toast.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#2c3e50;color:#fff;padding:10px 20px;border-radius:6px;font-size:13px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.25);';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.background = isError ? '#c0392b' : '#2c3e50';
  toast.style.display = 'block';
  clearTimeout(_toastTimeout);
  _toastTimeout = setTimeout(() => { toast.style.display = 'none'; }, 3500);
}

// ══════════════════════════════════════════════════════════════
// Grouped Table — config panel helpers
// ══════════════════════════════════════════════════════════════

// Cache of fields per data view: { dataViewId: [{name, type}, ...] }
const _gtFieldsCache = {};

async function _fetchFieldsForDataView(dataViewId) {
  if (!dataViewId) return [];
  if (_gtFieldsCache[dataViewId]) return _gtFieldsCache[dataViewId];
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/data-views/' + encodeURIComponent(dataViewId) + '/fields');
    const data = await res.json();
    if (data.success) {
      _gtFieldsCache[dataViewId] = data.fields;
      return data.fields;
    }
  } catch (e) {
    console.error('_fetchFieldsForDataView error:', e);
  }
  return [];
}

// Pre-populate field select for a column row using cached or freshly fetched fields.
async function _populateGtFieldSelect(selectEl, dataViewId, selectedField) {
  selectEl.innerHTML = '<option value="">— loading —</option>';
  const fields = await _fetchFieldsForDataView(dataViewId);
  selectEl.innerHTML = '<option value="">— select field —</option>';
  fields.forEach(f => {
    const opt = document.createElement('option');
    opt.value = f.name;
    opt.textContent = f.name + ' (' + f.type + ')';
    opt.dataset.fieldType = f.type || '';
    if (f.name === selectedField) opt.selected = true;
    selectEl.appendChild(opt);
  });
}

function updateGroupedTableProp(key, value) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  config[key] = value;
  _markDirty();
}

// populate a "Time-range date field" <select> with the data view's
// date-typed fields plus a blank "Default (kibana_date)" option. Used by the
// standard-chart and grouped-table config panels so any chart can scope the
// global time-range dropdown on closed_at (Echo) instead of kibana_date.
// Restricting to date-typed fields keeps a typo / non-date pick from silently
// breaking the range filter.
// date-typed fields that are NOT meaningful time-range axes and so
// are hidden from the Time-range Date Field dropdown:
//   - kibana_date: it IS the blank "Default" option, so listing it duplicates.
//   - timestamp / import_timestamp: ingestion bookkeeping, not an event date.
//   - expiration: a token/record lifetime, not a filterable event date.
const _TIME_RANGE_FIELD_EXCLUDE = new Set([
  'kibana_date', '@timestamp', 'timestamp', 'import_timestamp', 'expiration',
]);

async function _populateTimeRangeFieldSelect(selectEl, dataViewId, selectedField) {
  if (!selectEl) return;
  // Keep the blank default; replace any previously appended field options.
  selectEl.innerHTML = '<option value="">Default (kibana_date)</option>';
  if (!dataViewId) {
    selectEl.value = selectedField || '';
    return;
  }
  const fields = await _loadTableFields(dataViewId);
  fields
    .filter(f => f.type === 'date' && !_TIME_RANGE_FIELD_EXCLUDE.has(f.name))
    .forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.name;
      opt.textContent = f.name + ' (date)';
      selectEl.appendChild(opt);
    });
  // Preserve a previously-saved field even if it isn't in the (date-only)
  // list — e.g. the mapping changed — so editing the chart doesn't silently
  // drop the override.
  if (selectedField && !Array.from(selectEl.options).some(o => o.value === selectedField)) {
    const opt = document.createElement('option');
    opt.value = selectedField;
    opt.textContent = selectedField;
    selectEl.appendChild(opt);
  }
  selectEl.value = selectedField || '';
}

// ══════════════════════════════════════════════════════════════
// author-curated Date basis choices (dashboard-level).
// Each choice is { field, label }; the first is the viewer's default.
// The field dropdown is sourced from the Echo Conversations data view's
// date fields (the feature is Echo-focused), reusing the same date-only
// list + exclude set as the per-chart Time-range Date Field control.
// ══════════════════════════════════════════════════════════════
const _DATE_BASIS_DATA_VIEW = '*echo_conversations';

async function renderDateBasisChoicesEditor() {
  const list = document.getElementById('date-basis-choices-list');
  if (!list) return;
  const choices = Array.isArray(DASHBOARD_STATE.time_field_choices) ? DASHBOARD_STATE.time_field_choices : [];

  // Candidate Echo date fields (date-typed, minus bookkeeping/redundant).
  const fields = await _loadTableFields(_DATE_BASIS_DATA_VIEW);
  const dateFields = fields
    .filter(f => f.type === 'date' && !_TIME_RANGE_FIELD_EXCLUDE.has(f.name))
    .map(f => f.name);

  list.innerHTML = '';
  if (choices.length === 0) {
    const hint = document.createElement('div');
    hint.style.cssText = 'font-size:12px;color:#999;font-style:italic;';
    hint.textContent = 'No choices — the viewer sees no Date basis selector.';
    list.appendChild(hint);
    return;
  }

  choices.forEach((choice, idx) => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:6px;align-items:center;margin-bottom:6px;';

    // Field dropdown — Echo date fields, preserving a saved-but-missing field.
    const fieldSel = document.createElement('select');
    fieldSel.style.flex = '1';
    const opts = dateFields.slice();
    if (choice.field && !opts.includes(choice.field)) opts.push(choice.field);
    opts.forEach(name => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      if (name === choice.field) opt.selected = true;
      fieldSel.appendChild(opt);
    });
    // a freshly-added choice has field:'' and no matching option, so
    // the browser shows the first option but onchange never fires unless the
    // author re-picks it — leaving field:'' in state, which the viewer parent
    // then drops as invalid. Adopt the select's displayed value into state so
    // what's shown is what's saved (no silent blank).
    if (!choice.field && fieldSel.value) {
      DASHBOARD_STATE.time_field_choices[idx].field = fieldSel.value;
    }
    fieldSel.onchange = e => { DASHBOARD_STATE.time_field_choices[idx].field = e.target.value; _markDirty(); };

    // Label text (what the viewer sees, e.g. "Closed").
    const labelInput = document.createElement('input');
    labelInput.type = 'text';
    labelInput.placeholder = 'Label (e.g. Closed)';
    // follow-up: cap the date-basis label to match the filter-title cap
    // and fit the viewer's date-basis dropdown option.
    labelInput.maxLength = 30;
    labelInput.style.flex = '1';
    labelInput.value = choice.label || '';
    labelInput.onchange = e => { DASHBOARD_STATE.time_field_choices[idx].label = e.target.value; _markDirty(); };

    // Default marker on the first row.
    const tag = document.createElement('span');
    tag.style.cssText = 'font-size:10px;color:#888;min-width:48px;';
    tag.textContent = idx === 0 ? 'default' : '';

    const rm = document.createElement('button');
    rm.className = 'db-btn db-btn-sm';
    rm.textContent = '−';
    rm.title = 'Remove choice';
    rm.onclick = () => { DASHBOARD_STATE.time_field_choices.splice(idx, 1); _markDirty(); renderDateBasisChoicesEditor(); };

    row.appendChild(fieldSel);
    row.appendChild(labelInput);
    row.appendChild(tag);
    row.appendChild(rm);
    list.appendChild(row);
  });
}

function addDateBasisChoice() {
  if (!Array.isArray(DASHBOARD_STATE.time_field_choices)) DASHBOARD_STATE.time_field_choices = [];
  DASHBOARD_STATE.time_field_choices.push({ field: '', label: '' });
  _markDirty();
  renderDateBasisChoicesEditor();
}

function addGroupedTableGroup() {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config) return;
  if (!config.groups) config.groups = [];
  config.groups.push({ label: 'Group ' + (config.groups.length + 1), columns: [] });
  _markDirty();
  renderGroupsEditor(SELECTED_CHART_ID);
}

function removeGroupedTableGroup(groupIdx) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config || !config.groups) return;
  config.groups.splice(groupIdx, 1);
  _markDirty();
  renderGroupsEditor(SELECTED_CHART_ID);
}

function addGroupedTableColumn(groupIdx) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config || !config.groups || !config.groups[groupIdx]) return;
  // New structure uses data_sources array for multi-data-view support
  config.groups[groupIdx].columns.push({ label: 'Column', data_sources: [], aggregation: 'sum' });
  _markDirty();
  renderGroupsEditor(SELECTED_CHART_ID);
}

function removeGroupedTableColumn(groupIdx, colIdx) {
  if (!SELECTED_CHART_ID) return;
  const config = DASHBOARD_STATE.charts[SELECTED_CHART_ID];
  if (!config || !config.groups || !config.groups[groupIdx]) return;
  config.groups[groupIdx].columns.splice(colIdx, 1);
  _markDirty();
  renderGroupsEditor(SELECTED_CHART_ID);
}

// Available data views list (populated at init time)
let _gtDataViews = [];

async function _ensureGtDataViews() {
  if (_gtDataViews.length) return _gtDataViews;
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/data-views');
    const data = await res.json();
    if (data.success) _gtDataViews = data.data_views || [];
  } catch (e) {}
  return _gtDataViews;
}

async function renderGroupsEditor(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  const container = document.getElementById('gt-groups-editor');
  if (!container) return;

  const dataViews = await _ensureGtDataViews();

  container.innerHTML = '';
  const groups = config.groups || [];

  if (groups.length === 0) {
    container.innerHTML = '<p class="db-hint">No groups yet. Click "+ Add Group" below.</p>';
    return;
  }

  groups.forEach((group, gi) => {
    const block = document.createElement('div');
    block.className = 'gt-group-block';

    // Group header row: label input + delete button
    const headerDiv = document.createElement('div');
    headerDiv.className = 'gt-group-header';

    const labelInput = document.createElement('input');
    labelInput.type = 'text';
    labelInput.value = group.label || '';
    labelInput.placeholder = 'Group label';
    labelInput.oninput = e => {
      config.groups[gi].label = e.target.value;
      _markDirty();
    };

    const rmGroupBtn = document.createElement('button');
    rmGroupBtn.className = 'db-btn db-btn-sm';
    rmGroupBtn.style.cssText = 'padding:2px 7px;font-size:11px;color:#c0392b;border-color:#c0392b;';
    rmGroupBtn.textContent = '✕ Remove';
    rmGroupBtn.onclick = () => removeGroupedTableGroup(gi);

    headerDiv.appendChild(labelInput);
    headerDiv.appendChild(rmGroupBtn);
    block.appendChild(headerDiv);

    // Columns list
    const colList = document.createElement('div');
    colList.className = 'gt-col-list';

    if (group.columns.length === 0) {
      const hint = document.createElement('p');
      hint.className = 'gt-no-columns';
      hint.textContent = 'No columns. Click "+ Add Column".';
      colList.appendChild(hint);
    } else {
      // Helper: create a labelled field row
      const makeFieldRow = (labelText, inputEl) => {
        const row = document.createElement('div');
        row.className = 'gt-col-field-row';
        const lbl = document.createElement('label');
        lbl.textContent = labelText;
        row.appendChild(lbl);
        row.appendChild(inputEl);
        return row;
      };

      group.columns.forEach((col, ci) => {
        const card = document.createElement('div');
        card.className = 'gt-col-card';

        // Card header: "Column N" + remove button
        const cardHeader = document.createElement('div');
        cardHeader.className = 'gt-col-card-header';
        const cardTitle = document.createElement('span');
        cardTitle.textContent = `Column ${ci + 1}`;
        const rmColBtn = document.createElement('button');
        rmColBtn.className = 'btn-rm-col';
        rmColBtn.textContent = '✕';
        rmColBtn.title = 'Remove column';
        rmColBtn.onclick = () => removeGroupedTableColumn(gi, ci);
        cardHeader.appendChild(cardTitle);
        cardHeader.appendChild(rmColBtn);
        card.appendChild(cardHeader);

        // Label input
        const colLabel = document.createElement('input');
        colLabel.type = 'text';
        colLabel.value = col.label || '';
        colLabel.placeholder = 'Column name';
        colLabel.oninput = e => { config.groups[gi].columns[ci].label = e.target.value; _markDirty(); };
        card.appendChild(makeFieldRow('Label', colLabel));

        // Backwards compatibility: migrate old single data_view/field to data_sources array
        if (!col.data_sources) {
          col.data_sources = col.data_view ? [{ data_view: col.data_view, field: col.field || '' }] : [];
        }

        // Data Sources section - multi-select dropdown + field selects for each
        const dsSection = document.createElement('div');
        dsSection.className = 'gt-data-sources-section';

        // Multi-select for data views
        const dvSelect = document.createElement('select');
        dvSelect.multiple = true;
        dvSelect.size = Math.min(5, dataViews.length);
        dvSelect.style.cssText = 'width:100%;font-size:12px;';
        dataViews.forEach(dv => {
          const opt = document.createElement('option');
          opt.value = dv.id;
          opt.textContent = dv.label;
          opt.selected = col.data_sources.some(ds => ds.data_view === dv.id);
          dvSelect.appendChild(opt);
        });
        card.appendChild(makeFieldRow('Data Views', dvSelect));

        // Container for field selects (one per selected data view)
        const fieldsContainer = document.createElement('div');
        fieldsContainer.className = 'gt-fields-container';

        // Function to rebuild field selects based on selected data views
        const rebuildFieldSelects = async () => {
          fieldsContainer.innerHTML = '';
          const selectedIds = Array.from(dvSelect.selectedOptions).map(o => o.value);

          // Sync data_sources array with selection
          // Remove deselected
          col.data_sources = col.data_sources.filter(ds => selectedIds.includes(ds.data_view));
          // Add newly selected
          selectedIds.forEach(dvId => {
            if (!col.data_sources.some(ds => ds.data_view === dvId)) {
              col.data_sources.push({ data_view: dvId, field: '' });
            }
          });

          // Create field select for each selected data view
          for (const dvId of selectedIds) {
            const dv = dataViews.find(d => d.id === dvId);
            if (!dv) continue;

            const ds = col.data_sources.find(d => d.data_view === dvId);

            const fieldSel = document.createElement('select');
            fieldSel.innerHTML = '<option value="">— field —</option>';
            await _populateGtFieldSelect(fieldSel, dvId, ds ? ds.field : '');

            fieldSel.onchange = () => {
              if (ds) ds.field = fieldSel.value;
              // Auto-set aggregation based on field type
              const selectedOpt = fieldSel.options[fieldSel.selectedIndex];
              const fieldType = selectedOpt ? (selectedOpt.dataset.fieldType || '') : '';
              if (fieldType) {
                const aggSel = card.querySelector('.gt-agg-select');
                if (aggSel) {
                  const suggestedAgg = _defaultAggForType(fieldType);
                  aggSel.value = suggestedAgg;
                  config.groups[gi].columns[ci].aggregation = suggestedAgg;
                }
              }
              _markDirty();
            };

            // Use same structure as other field rows
            const fieldRow = makeFieldRow(dv.label, fieldSel);
            fieldsContainer.appendChild(fieldRow);
            _makeSearchableSelect(fieldSel);
          }

          _markDirty();
        };

        dvSelect.onchange = rebuildFieldSelects;

        // Initial build
        rebuildFieldSelects();

        card.appendChild(fieldsContainer);

        // Aggregation select
        const aggSel = document.createElement('select');
        aggSel.className = 'gt-agg-select';
        const aggDisplayLabels = { cardinality: 'unique' };
        // 'exists' must be listed: a stored exists column (Summary
        // Report "New Churches") displayed as "sum" and any touch of the
        // dropdown silently overwrote the aggregation.
        ['sum', 'avg', 'min', 'max', 'count', 'cardinality', 'text', 'date', 'equals', 'exists', 'query date'].forEach(agg => {
          const opt = document.createElement('option');
          opt.value = agg;
          opt.textContent = aggDisplayLabels[agg] || agg;
          if (agg === (col.aggregation || 'sum')) opt.selected = true;
          aggSel.appendChild(opt);
        });
        card.appendChild(makeFieldRow('Aggregation', aggSel));

        // Equals value input (shown only when aggregation is 'equals')
        const equalsInput = document.createElement('input');
        equalsInput.type = 'text';
        equalsInput.placeholder = 'Value to match';
        equalsInput.value = col.equals_value || '';
        equalsInput.onchange = e => { config.groups[gi].columns[ci].equals_value = e.target.value; _markDirty(); };
        const equalsRow = makeFieldRow('Equals Value', equalsInput);
        equalsRow.style.display = (col.aggregation === 'equals') ? '' : 'none';
        card.appendChild(equalsRow);

        // DT Date Field — override which date field is used for time-range filtering on DT indices.
        // Leave blank to use the default (date_created.keyword).
        // Common overrides: date_modified.keyword, meta.first_contact_date.keyword
        const dtDateFieldInput = document.createElement('input');
        dtDateFieldInput.type = 'text';
        dtDateFieldInput.placeholder = 'default: date_created.keyword';
        dtDateFieldInput.value = col.dt_date_field || '';
        dtDateFieldInput.onchange = e => { config.groups[gi].columns[ci].dt_date_field = e.target.value; _markDirty(); };
        card.appendChild(makeFieldRow('DT Date Field', dtDateFieldInput));

        // DT Unix Timestamp — tick if the 'query date' field stores epoch seconds (not yyyy-MM-dd).
        const dtUnixChk = document.createElement('input');
        dtUnixChk.type = 'checkbox';
        dtUnixChk.checked = col.dt_date_unix || false;
        dtUnixChk.onchange = e => { config.groups[gi].columns[ci].dt_date_unix = e.target.checked; _markDirty(); };
        card.appendChild(makeFieldRow('DT Unix Timestamp', dtUnixChk));

        // the Echo time-range date field is now a CHART-LEVEL
        // property ("Time-range Date Field" on the Table Data panel), applying
        // to every column, not a per-column override. See cfg-table-time-range-field.

        // Helper to update visibility of aggregation-specific fields
        const updateAggFields = (aggValue) => {
          equalsRow.style.display = (aggValue === 'equals') ? '' : 'none';
        };

        aggSel.onchange = e => {
          config.groups[gi].columns[ci].aggregation = e.target.value;
          updateAggFields(e.target.value);
          _markDirty();
        };

        colList.appendChild(card);
      });
    }

    block.appendChild(colList);

    // Add column button
    const addColBtn = document.createElement('button');
    addColBtn.className = 'db-btn db-btn-sm gt-add-col-btn';
    addColBtn.textContent = '+ Add Column';
    addColBtn.onclick = () => addGroupedTableColumn(gi);
    block.appendChild(addColBtn);

    container.appendChild(block);
  });
}

// ══════════════════════════════════════════════════════════════
// Leaflet Map — config panel helpers
// ══════════════════════════════════════════════════════════════

function _getMapConfig(chartId) {
  const config = DASHBOARD_STATE.charts[chartId || SELECTED_CHART_ID];
  if (!config) return null;
  if (!config.map_statistics || config.map_statistics.length === 0) config.map_statistics = MAP_STAT_DEFAULTS.map(s => ({...s}));
  return config;
}

function renderMapStatDropdown(chartId) {
  const config = _getMapConfig(chartId);
  if (!config) return;
  const sel = document.getElementById('cfg-map-statistic');
  if (!sel) return;
  const current = config.map_statistic || 'reach';
  sel.innerHTML = '';
  (config.map_statistics || []).forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.key;
    opt.textContent = s.label || s.key;
    if (s.key === current) opt.selected = true;
    sel.appendChild(opt);
  });
}

async function renderMapFieldEditor(chartId) {
  const config = _getMapConfig(chartId);
  if (!config) return;
  const stats = config.map_statistics || [];
  const currentKey = config.map_statistic || (stats[0] && stats[0].key) || '';
  const stat = stats.find(s => s.key === currentKey) || stats[0];
  if (!stat) return;

  document.getElementById('cfg-map-field-label').value = stat.label || '';

  // Populate data view dropdown
  const dvSel = document.getElementById('cfg-map-field-dv');
  const dataViews = await _ensureGtDataViews();
  dvSel.innerHTML = '<option value="">— Select data view —</option>';
  dataViews.forEach(dv => {
    const opt = document.createElement('option');
    opt.value = dv.id;
    opt.textContent = dv.label;
    if (dv.id === stat.data_view) opt.selected = true;
    dvSel.appendChild(opt);
  });

  // Populate field dropdown from the stat's data view
  await _populateGtFieldSelect(document.getElementById('cfg-map-field-field'), stat.data_view, stat.field || '');

  // Set aggregation
  const aggSel = document.getElementById('cfg-map-field-agg');
  if (aggSel) aggSel.value = stat.aggregation || 'sum';
}

function onMapStatisticChange(key) {
  if (!SELECTED_CHART_ID) return;
  const config = _getMapConfig(SELECTED_CHART_ID);
  if (!config) return;
  config.map_statistic = key;
  _markDirty();
  renderMapFieldEditor(SELECTED_CHART_ID);
  requeryLeafletMap(SELECTED_CHART_ID);
}

async function onMapFieldDvChange(dvId) {
  updateMapFieldProp('data_view', dvId);
  updateMapFieldProp('field', '');
  await _populateGtFieldSelect(document.getElementById('cfg-map-field-field'), dvId, '');
}

function updateMapFieldProp(prop, value) {
  if (!SELECTED_CHART_ID) return;
  const config = _getMapConfig(SELECTED_CHART_ID);
  if (!config) return;
  const currentKey = config.map_statistic || '';
  const stat = (config.map_statistics || []).find(s => s.key === currentKey);
  if (!stat) return;
  stat[prop] = value;
  // If label changed, sync the dropdown option text
  if (prop === 'label') {
    const sel = document.getElementById('cfg-map-statistic');
    const opt = sel && sel.querySelector(`option[value="${CSS.escape(currentKey)}"]`);
    if (opt) opt.textContent = value;
  }
  _markDirty();
}

function addMapField() {
  if (!SELECTED_CHART_ID) return;
  const config = _getMapConfig(SELECTED_CHART_ID);
  if (!config) return;
  const newKey = 'custom_' + Date.now();
  config.map_statistics.push({ key: newKey, label: 'New Field', data_view: '', field: '', aggregation: 'sum' });
  config.map_statistic = newKey;
  _markDirty();
  renderMapStatDropdown(SELECTED_CHART_ID);
  renderMapFieldEditor(SELECTED_CHART_ID);
}

function removeMapField() {
  if (!SELECTED_CHART_ID) return;
  const config = _getMapConfig(SELECTED_CHART_ID);
  if (!config) return;
  const stats = config.map_statistics || [];
  if (stats.length === 0) return;
  const currentKey = config.map_statistic;
  const stat = stats.find(s => s.key === currentKey);
  const label = stat ? (stat.label || stat.key) : currentKey;
  if (!confirm(`Remove the field "${label}" from the map? This cannot be undone.`)) return;
  config.map_statistics = stats.filter(s => s.key !== currentKey);
  config.map_statistic = config.map_statistics.length > 0 ? config.map_statistics[0].key : '';
  _markDirty();
  renderMapStatDropdown(SELECTED_CHART_ID);
  renderMapFieldEditor(SELECTED_CHART_ID);
}

// ══════════════════════════════════════════════════════════════
// Save as template
// ══════════════════════════════════════════════════════════════
// Open the modal, populate the placeholder-field checklist from the
// current dashboard's filter fields, and submit to
// POST /dashboard-builder/api/templates/freeze. CSRF is injected
// globally by app.js's fetch wrapper (X-CSRF-Token from the meta tag).

// Filter fields recognized as asset-bound — pre-checked in the
// placeholder picker. Mirrors the placeholder_field values used by
// first_look_templates.py so the curator's defaults match what the
// migration job will produce.
const _SAT_ASSET_BOUND_FIELDS = new Set([
  'ad_account_id',
  'team_esid',
  'google_ads_customer_id',
  'property_id',
  'page_id',
  'ig_account_id',
]);

function _satCollectFilterFields() {
  // Walk every chart's filters + the dashboard-level filter strip,
  // dedupe by field name, and return the sorted list with whether
  // we think each one is asset-bound.
  const fields = new Map();
  const add = (f) => {
    if (!f || !f.field) return;
    if (!fields.has(f.field)) {
      fields.set(f.field, { suggested: _SAT_ASSET_BOUND_FIELDS.has(f.field) });
    }
  };
  (DASHBOARD_STATE.filters || []).forEach(add);
  Object.values(DASHBOARD_STATE.charts || {}).forEach(chart => {
    (chart.filters || []).forEach(add);
  });
  return Array.from(fields.entries())
    .map(([field, meta]) => ({ field, suggested: meta.suggested }))
    .sort((a, b) => a.field.localeCompare(b.field));
}

// slice 2 follow-up (2026-05-22) — checkbox-mutex behavior:
// at most one ``.sat-ph-field`` checkbox can be checked at a time.
// When one is checked, the others are disabled. When the last one
// is unchecked, all re-enable. Matches the server-side gate that
// refuses payloads with more than one placeholder field.
//
// Why not radio buttons: radios force "exactly one" semantics and
// require a separate "None" option to express zero-selection. The
// checkbox-mutex pattern naturally expresses 0-or-1 — which is
// what the template registry actually wants.
function _satApplyMutex(listEl) {
  if (!listEl) return;
  const boxes = listEl.querySelectorAll('input.sat-ph-field');
  const checked = listEl.querySelector('input.sat-ph-field:checked');
  boxes.forEach(b => {
    if (checked && b !== checked) {
      b.disabled = true;
    } else {
      b.disabled = false;
    }
  });
}

function showSaveAsTemplateModal() {
  const modal = document.getElementById('save-as-template-modal');
  if (!modal) return;

  // Reset error + pre-fill name from the current dashboard.
  const errEl = document.getElementById('sat-error');
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  const nameEl = document.getElementById('sat-name');
  if (nameEl) nameEl.value = DASHBOARD_STATE.title || '';

  // Build the placeholder-field checklist via safe DOM construction
  // (no innerHTML interpolation — every node is created explicitly so
  // a malicious filter field name can't break out into HTML).
  //
  // Mutex semantics: pre-check ONLY the first recognized asset-bound
  // field. The mutex handler then disables the rest. If no
  // recognized field is present, no box is pre-checked and all
  // remain enabled.
  const listEl = document.getElementById('sat-placeholder-fields');
  if (listEl) {
    while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
    const fields = _satCollectFilterFields();
    if (fields.length === 0) {
      const p = document.createElement('p');
      p.style.color = 'var(--db-text-muted,#666)';
      p.style.padding = '8px';
      p.textContent = 'No filter fields detected — the template will instantiate as-is.';
      listEl.appendChild(p);
    } else {
      let preselected = false;
      fields.forEach(f => {
        const label = document.createElement('label');
        label.className = 'db-checkbox-row';
        label.style.display = 'block';
        label.style.padding = '4px 0';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'sat-ph-field';
        cb.value = f.field;
        // Pre-check only the FIRST recognized asset-bound field.
        // Mutex enforces at-most-one even if multiple were suggested.
        if (f.suggested && !preselected) {
          cb.checked = true;
          preselected = true;
        }
        cb.addEventListener('change', () => _satApplyMutex(listEl));
        label.appendChild(cb);
        label.appendChild(document.createTextNode(' '));

        const code = document.createElement('code');
        code.textContent = f.field;
        label.appendChild(code);

        if (f.suggested) {
          const badge = document.createElement('span');
          badge.style.fontSize = '10px';
          badge.style.color = '#0a8043';
          badge.style.marginLeft = '6px';
          badge.textContent = 'recognized';
          label.appendChild(badge);
        }
        listEl.appendChild(label);
      });
      // Apply mutex initial state — disables the non-selected boxes
      // if one was pre-checked above.
      _satApplyMutex(listEl);
    }
  }

  modal.style.display = 'flex';
}

function closeSaveAsTemplateModal() {
  const modal = document.getElementById('save-as-template-modal');
  if (modal) modal.style.display = 'none';
  // #3 — undo the template_id lock applied by
  // _openSaveTemplateOverwriteModal so a follow-up fresh save (not in
  // edit mode) starts with an editable id field again.
  const tidEl = document.getElementById('sat-template-id');
  if (tidEl) { tidEl.readOnly = false; tidEl.style.background = ''; }
}

async function submitSaveAsTemplate() {
  const errEl = document.getElementById('sat-error');
  const showError = (msg) => {
    if (errEl) { errEl.textContent = msg; errEl.style.display = 'block'; }
  };
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }

  // #3: in edit-template mode there is no source dashboard.
  // Route to the PUT update endpoint with echarts_config inline.
  // Otherwise the legacy POST freeze flow runs (requires a real
  // DASHBOARD_STATE.id source).
  const inEditMode = !!_editingTemplate;
  if (!inEditMode && !DASHBOARD_STATE.id) {
    showError('No dashboard loaded.');
    return;
  }

  const templateIdField = (document.getElementById('sat-template-id').value || '').trim();
  const payloadCommon = {
    template_id: templateIdField,
    pack_id: (document.getElementById('sat-pack-id').value || '').trim(),
    connector_id: (document.getElementById('sat-connector-id').value || '').trim(),
    menu_title: (document.getElementById('sat-menu-title').value || '').trim(),
    name: (document.getElementById('sat-name').value || '').trim(),
    description: (document.getElementById('sat-description').value || '').trim(),
    menu_position: parseInt(document.getElementById('sat-menu-position').value || '0', 10) || 0,
    placeholder_fields: Array.from(document.querySelectorAll('.sat-ph-field:checked')).map(el => el.value),
  };

  const missing = ['template_id', 'pack_id', 'connector_id', 'menu_title', 'name']
    .filter(k => !payloadCommon[k]);
  if (missing.length) {
    showError(`Missing required field(s): ${missing.join(', ')}`);
    return;
  }
  if (!/^[a-z0-9_]+$/.test(payloadCommon.template_id)) {
    showError('Template id must be lowercase letters, digits, and underscores only.');
    return;
  }
  if (!/^[a-z0-9_]+$/.test(payloadCommon.pack_id)) {
    showError('Pack id must be lowercase letters, digits, and underscores only.');
    return;
  }

  const submitBtn = document.getElementById('sat-submit');
  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Saving…'; }

  try {
    let resp;
    if (inEditMode) {
      const putPayload = {
        ...payloadCommon,
        echarts_config: _collectEchartsConfigFromState(),
      };
      resp = await fetch(
        `${DASHBOARD_BASE}api/templates/${encodeURIComponent(payloadCommon.template_id)}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(putPayload),
        },
      );
    } else {
      const freezePayload = {
        ...payloadCommon,
        dashboard_id: DASHBOARD_STATE.id,
      };
      resp = await fetch(`${DASHBOARD_BASE}api/templates/freeze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(freezePayload),
      });
    }
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok || body.ok === false) {
      const errMsg = body.error || `Request failed (HTTP ${resp.status}).`;
      showError(errMsg);
      return;
    }
    closeSaveAsTemplateModal();
    if (typeof showToast === 'function') {
      showToast(`Template "${body.name || payloadCommon.name}" saved.`);
    }
    // #3 — clear the edit banner on a successful overwrite.
    if (inEditMode) {
      _exitTemplateEditMode();
    }
  } catch (e) {
    showError('Network error — please try again.');
  } finally {
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Save template'; }
  }
}
