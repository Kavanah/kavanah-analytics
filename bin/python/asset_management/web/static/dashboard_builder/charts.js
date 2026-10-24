/**
 * charts.js — Query + render logic for the Dashboard Builder
 * Handles ECharts, table, and metric/KPI chart rendering.
 */

// Registry of active ECharts instances keyed by chart_id
const CHART_INSTANCES = {};

// a data view is "Disciple Tools" if it targets the cross-tenant
// alias `disciple-tools` (hyphen) OR a team-keyed `{team}_disciple_tools`
// index / `*disciple_tools` wildcard (underscore). DT docs use DT-specific
// date fields (date_created_ms etc.), NOT kibana_date — so this gate decides
// which date field the viewer time-range filter uses. Matching only the
// hyphen form silently routed team-keyed DT views to kibana_date (absent on
// DT docs) → every DT metric returned 0.
function _isDtDataView(dv) {
  const s = (dv || '');
  return s.includes('disciple-tools') || s.includes('disciple_tools');
}

// First Look templates bind a chart to the consumer's asset via a
// per-chart filter whose value is this placeholder, swapped at install time.
// Defined here (not filters.js) because charts.js loads first and is the only
// script the read-only viewer loads; renderGroupedTable drops these filters
// (they match no real doc until installed) and the editor (filters.js) renders
// them read-only. Matches the bare string or a list containing it.
const FIRST_LOOK_PLACEHOLDER = '__FIRST_LOOK_ASSET_PLACEHOLDER__';

function _isPlaceholderFilter(filter) {
  if (!filter) return false;
  const v = filter.value;
  if (Array.isArray(v)) return v.some(x => String(x) === FIRST_LOOK_PLACEHOLDER);
  return String(v) === FIRST_LOOK_PLACEHOLDER;
}

// Format a raw date string from ES using a format token.
// ES date fields are typically ISO 8601 (e.g. "2024-01-15T00:00:00.000Z" or "2024-01-15").
function _applyDateFormat(str, fmt) {
  if (!str || !fmt) return str;
  const d = new Date(str);
  if (isNaN(d.getTime())) return str;
  const year  = d.getUTCFullYear();
  const month = d.getUTCMonth(); // 0-indexed
  const day   = d.getUTCDate();
  const pad   = n => String(n).padStart(2, '0');
  const SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const LONG  = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  switch (fmt) {
    case 'YYYY-MM-DD':    return `${year}-${pad(month+1)}-${pad(day)}`;
    case 'DD/MM/YYYY':    return `${pad(day)}/${pad(month+1)}/${year}`;
    case 'MM/DD/YYYY':    return `${pad(month+1)}/${pad(day)}/${year}`;
    case 'MMM DD, YYYY':  return `${SHORT[month]} ${day}, ${year}`;
    case 'MMMM DD, YYYY': return `${LONG[month]} ${day}, ${year}`;
    case 'MMM dd':        return `${SHORT[month]} ${day}`;
    case 'MMM DD':        return `${SHORT[month]} ${pad(day)}`;
    case 'MMM YYYY':      return `${SHORT[month]} ${year}`;
    case 'MMMM YYYY':     return `${LONG[month]} ${year}`;
    case 'YYYY':          return String(year);
    case 'MM/YYYY':       return `${pad(month+1)}/${year}`;
    case 'DD':            return String(day);
    case 'MMM':           return SHORT[month];
    case 'MM':            return pad(month + 1);  // back-compat for saved configs
    default:              return str;
  }
}

// format a date X-axis label for line/bar charts. Prefers the
// shared _applyDateFormat (the DATE_FORMAT_OPTIONS token set now used by the
// "Data Format" dropdown). Falls back to the legacy 6 line/bar labels saved on
// older dashboards (Mon YYYY / Month YYYY / YYYY-MM / MM/YYYY / DD Mon YYYY /
// DD Mon) so existing charts keep rendering unchanged. Unknown/blank → raw val.
const _LEGACY_AXIS_DATE_FORMATS = {
  'Mon YYYY': 'MMM YYYY',
  'Month YYYY': 'MMMM YYYY',
  'YYYY-MM': 'MM/YYYY',       // closest token in the shared set (year-month)
  'DD Mon YYYY': 'MMM DD, YYYY',
  'DD Mon': 'MMM DD',
};
function _formatDateAxisLabel(val, fmt) {
  if (!fmt) return val;
  // YYYY-MM legacy label has no exact token equivalent — render it directly.
  if (fmt === 'YYYY-MM') {
    const d = new Date(val);
    if (isNaN(d.getTime())) return val;
    return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
  }
  const mapped = _LEGACY_AXIS_DATE_FORMATS[fmt] || fmt;
  const out = _applyDateFormat(String(val), mapped);
  return out || val;
}

// default X-axis label formatter for a date chart bucketed by DAY
// (no explicit Data Format chosen). Shows the day number on every tick, and the
// 3-letter month on a SECOND line only when the month changes — so a Jun→Jul
// boundary is labeled once, not on every day. Returns a stateful formatter (a
// closure) so it can re-label each month exactly once even when ECharts thins
// the rendered ticks.
//
// ECharts renders a "\n" in an axisLabel as a real line break, so "07\nJun"
// stacks day-over-month.
const _SHORT_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function _makeDayAxisFormatter() {
  // ECharts auto-thins category ticks, so a month boundary day may not be a
  // rendered tick. To still label each month once, track the last (year,month)
  // we actually emitted a label for and re-label whenever a rendered tick lands
  // in a not-yet-labeled month. ECharts calls the formatter once per rendered
  // tick in left-to-right order, so this running state is reliable. val is the
  // tick's ISO yyyy-MM-dd category key.
  let lastLabeledKey = null;
  return (val) => {
    const d = new Date(val);
    if (isNaN(d.getTime())) return val;
    const day = String(d.getUTCDate()).padStart(2, '0');
    const monKey = `${d.getUTCFullYear()}-${d.getUTCMonth()}`;
    const showMonth = monKey !== lastLabeledKey;
    if (showMonth) lastLabeledKey = monKey;
    return showMonth ? `${day}\n${_SHORT_MONTHS[d.getUTCMonth()]}` : day;
  };
}

// SELECTED_CHART_ID is defined in builder.js but not in the dashboard viewer (view.html).
// Use this helper so charts.js works safely in both contexts.
function _isSelectedChart(chartId) {
  return typeof SELECTED_CHART_ID !== 'undefined' && SELECTED_CHART_ID === chartId;
}

// fetchJsonWithTimeout — guard against backend stalls and HTML error pages.
//
// nginx returns an HTML 504 page when an upstream times out. The browser
// then hits await res.json() on text/html and surfaces the cryptic
// "Unexpected token '<'" error. This helper:
//   - aborts the request after timeoutMs (default 20s) so a slow backend
//     can't outlive the user's patience,
//   - rejects on non-2xx responses with the status text,
//   - rejects on non-JSON content-type before attempting .json(), so the
//     caller never has to interpret an HTML error page as data.
async function fetchJsonWithTimeout(url, timeoutMs = 20000, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    if (!res.ok) {
      throw new Error(`Request failed (HTTP ${res.status})`);
    }
    const ct = (res.headers.get('content-type') || '').toLowerCase();
    if (!ct.startsWith('application/json')) {
      throw new Error('Server returned a non-JSON response (likely a gateway timeout). Try again.');
    }
    return await res.json();
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

// Register a Cloudscape-inspired ECharts theme.
// guarded: the canvas pages load charts.js for renderTable /
// queryAndRender (table objects) WITHOUT the ECharts library. An unguarded
// top-level call would throw and abort this whole script's evaluation,
// leaving every later `const` (e.g. _tableSortState) permanently in TDZ.
// Pages that draw echarts (builder, view, public viewer, snapshot) always
// load the library first, so the theme still registers everywhere it's used.
if (typeof echarts !== 'undefined') echarts.registerTheme('kibana', {
  textStyle: {
    fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif",
    color: '#000716'
  },
  title: {
    textStyle: { fontWeight: 700, fontSize: 16, color: '#000716' },
    subtextStyle: { fontSize: 13, color: '#5F6B7A' }
  },
  legend: {
    textStyle: { fontSize: 12, color: '#5F6B7A' },
    itemGap: 16
  },
  tooltip: {
    backgroundColor: 'rgba(255,255,255,0.96)',
    borderColor: '#D5DBDB',
    borderWidth: 1,
    textStyle: { color: '#000716', fontSize: 13 },
    extraCssText: 'border-radius:8px;box-shadow:0 4px 12px rgba(0,7,22,0.12);padding:10px 14px;'
  },
  categoryAxis: {
    axisLine: { lineStyle: { color: '#D5DBDB' } },
    axisTick: { lineStyle: { color: '#D5DBDB' } },
    axisLabel: { color: '#5F6B7A', fontSize: 11 },
    splitLine: { lineStyle: { color: '#E9EBED' } }
  },
  valueAxis: {
    axisLine: { lineStyle: { color: '#D5DBDB' } },
    axisTick: { lineStyle: { color: '#D5DBDB' } },
    axisLabel: { color: '#5F6B7A', fontSize: 11 },
    splitLine: { lineStyle: { color: '#E9EBED', type: 'dashed' } }
  }
});

function formatNumber(n) {
  if (n === null || n === undefined) return '—';
  if (typeof n === 'number') {
    const abs = Math.abs(n);
    if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (abs >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  // a non-number here is stored data (a keyword value) and most
  // callers interpolate the result into innerHTML — escape it.
  return escHtml(String(n));
}

// render a count of SECONDS as a human duration, matching GA4's
// "Average engagement time" tile (e.g. 1380 -> "23m 0s", 4275 -> "1h 11m 15s").
// Used by metric cards whose config sets value_format: "duration" (the GA4
// averageSessionDuration / userEngagementDuration metrics are seconds, which
// formatNumber would otherwise abbreviate to a meaningless "1.4K").
function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || typeof seconds !== 'number' || isNaN(seconds)) return '—';
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// metric-card value formatter selector. A chart config may set
// value_format to pick how the single metric value renders. Defaults to the
// legacy K/M abbreviation (formatNumber) when unset/unknown, so every existing
// metric card is unchanged.
function formatMetricValue(value, valueFormat) {
  if (valueFormat === 'duration') return formatDuration(value);
  return formatNumber(value);
}

// ── format-string-driven number rendering for table columns ──────
// Format syntax (a small subset of spreadsheet number formats):
//   "0.00"       → 1234.567 → "1234.57"
//   "0.0"        → 1234.567 → "1234.6"
//   "0,0.00"     → 1234.567 → "1,234.57"   (any "," in the digit run → thousands)
//   "$0.00"      → 1234.567 → "$1234.57"
//   "$0,0.00"    → 1234.567 → "$1,234.57"
//   "0.00%"      → 0.0567   → "5.67%"      (% multiplies by 100 — fraction → percent)
//   "0.00\%"     → 5.67     → "5.67%"      (\% is a literal percent sign — value is
//                                            already a percentage, no multiply)
//   "0 pts"      → 1234     → "1234 pts"
//
// `0`, `#`, `1` (and any other digit) inside the digit portion are all
// treated as placeholders — only the count of fraction digits and the
// presence of a thousands `,` matter. Anything before the first
// digit-or-`#` is the prefix; anything after the last digit is the
// suffix. A bare `%` anywhere outside the digit run triggers
// multiply-by-100; an escaped `\%` is a literal sign with no multiply.
//
// Parsing is intentionally permissive — unknown patterns fall back to
// the raw number's toLocaleString. This keeps the cost of typo'd format
// strings small (you see the raw number, not "NaN").

const _FORMAT_PARSE_CACHE = {};
// Sentinel char standing in for "\%" while we parse — won't appear in
// any real format string. Swapped back to "%" in the rendered output.
const _LITERAL_PERCENT = '';

function _parseFormatString(format) {
  if (!format || typeof format !== 'string') return null;
  if (_FORMAT_PARSE_CACHE[format] !== undefined) return _FORMAT_PARSE_CACHE[format];

  // Replace escaped "\%" with the sentinel BEFORE the digit-run regex
  // runs, so a bare "%" in suffix means multiply and the sentinel
  // means literal. The sentinel survives the prefix/suffix split and
  // gets swapped back to "%" at render time.
  const normalized = format.replace(/\\%/g, _LITERAL_PERCENT);

  // Locate the digit run (first run of [0-9#] characters); split into
  // prefix / digit-run / suffix. If there's no digit run the format is
  // unusable — return null and let the caller fall back.
  const m = normalized.match(/^([^0-9#]*)([0-9#][0-9#,]*(?:\.[0-9#]*)?)(.*)$/);
  if (!m) {
    _FORMAT_PARSE_CACHE[format] = null;
    return null;
  }
  const [, rawPrefix, digitRun, rawSuffix] = m;

  const dotIdx = digitRun.indexOf('.');
  const intPart = dotIdx === -1 ? digitRun : digitRun.slice(0, dotIdx);
  const fracPart = dotIdx === -1 ? '' : digitRun.slice(dotIdx + 1);

  // Fraction digit count = number of placeholder chars after the dot
  // (we treat 0/1/#/digits all as placeholders).
  const fractionDigits = fracPart.length;
  const useGrouping = intPart.includes(',');
  // Bare % in the un-normalized prefix/suffix triggers multiply.
  // Sentinel-replaced "\%" doesn't.
  const isPercent = rawPrefix.includes('%') || rawSuffix.includes('%');
  // Restore the literal "%" from the sentinel so the rendered output
  // shows a real percent sign with no multiply.
  const prefix = rawPrefix.split(_LITERAL_PERCENT).join('%');
  const suffix = rawSuffix.split(_LITERAL_PERCENT).join('%');

  const parsed = { prefix, suffix, fractionDigits, useGrouping, isPercent };
  _FORMAT_PARSE_CACHE[format] = parsed;
  return parsed;
}

function _applyFormatString(value, format) {
  if (typeof value !== 'number' || !isFinite(value)) return String(value ?? '');
  const parsed = _parseFormatString(format);
  if (!parsed) {
    // Unparseable format → fall back to a safe default render.
    return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  const { prefix, suffix, fractionDigits, useGrouping, isPercent } = parsed;
  const v = isPercent ? value * 100 : value;
  const body = v.toLocaleString(undefined, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
    useGrouping,
  });
  return prefix + body + suffix;
}

// Resolve a column's effective format string. Prefers col.format (the
// new field added Phase 17 T1); falls back to deriving from
// the legacy col.decimals so existing dashboards keep rendering
// identically without a save. Default if neither is set: 2 decimals,
// matching the earlier default.
function _columnEffectiveFormat(col) {
  if (col && typeof col.format === 'string' && col.format.length > 0) return col.format;
  if (col && typeof col.decimals === 'number' && col.decimals >= 0) {
    return col.decimals === 0 ? '0' : '0.' + '0'.repeat(col.decimals);
  }
  return '0.00';
}

// ECharts default color palette (matches echarts source)
const ECHARTS_PALETTE = [
  '#5470c6','#91cc75','#fac858','#ee6666','#73c0de',
  '#3ba272','#fc8452','#9a60b4','#ea7ccc',
];

// ══════════════════════════════════════════════════════════════
// Extra filter injection (org/team filter from viewer URL params)
// ══════════════════════════════════════════════════════════════

/**
 * Resolve an ES date math expression (e.g. 'now-30d/d', 'now') to a plain
 * 'yyyy-MM-dd' string.  Non-date-math values are returned unchanged.
 * upperBound=true adds one day so 'lte' includes all records from the current day.
 *
 * Needed for Disciple Tools indices whose date fields are stored as text/keyword
 * rather than ES date type — ES cannot resolve date math on text fields.
 */
function _resolveDateMath(expr, upperBound = false) {
  if (!expr) return expr;
  const now = new Date();
  if (/^now$/i.test(expr)) {
    const d = upperBound ? new Date(now.getTime() + 86400000) : now;
    return d.toISOString().slice(0, 10);
  }
  const m = expr.match(/^now-(\d+)([dMy])(?:\/[dMy])?$/i);
  if (!m) return expr;
  const n = parseInt(m[1], 10);
  const unit = m[2].toLowerCase();
  const d = new Date(now);
  if      (unit === 'd') d.setDate(d.getDate() - n);
  else if (unit === 'm') d.setMonth(d.getMonth() - n);
  else if (unit === 'y') d.setFullYear(d.getFullYear() - n);
  return d.toISOString().slice(0, 10);
}

// sentinel data_view for an "All Data Views" dashboard filter.
// Such a filter carries a shared (cross-data-view) keyword field — e.g.
// team_title — and is rebound to each chart's OWN data_view at query time (see
// _withExtraFilters below) so a single filter constrains every data view in a
// multi-source report (the canva Summary Report spans 8). The editor lists this
// as the first Data View option; the server never sees '__ALL__' (rebound first).
const DASHBOARD_FILTER_ALL_DV = '__ALL__';

// the data views the canva "Summary Report" template aggregates over,
// used by the dashboard-filters editor to compute the "All Data Views"
// shared-field intersection on a dashboard whose only chart is the canva report
// (a canva chart config carries no top-level data_view, so its sources aren't
// otherwise discoverable from DASHBOARD_STATE.charts).
//
// These MUST be the DATA-VIEW index patterns (as in create-data-views.sh /
// what get_mapping resolves). Historical trap: M[] once used loose shortcuts
// ('*ga', '*fb_page', '*youtube') that matched NO indices — the real indices
// end in '_ga_metrics' / '_fb_page_insights' / '_youtube_analytics' — which
// emptied the /fields intersection here ("no shared fields") and, worse,
// resolved those tiles to the empty-scope sentinel at query time.
// M[] now uses these same canonical patterns; keep both lists in sync. Each
// pattern below resolves to real indices that carry team_title /
// organization_title / team_esid.
const CANVA_SUMMARY_DVS = [
  '*_fb_insights', '*_ig_insights', '*_ga_metrics', '*_fb_page_insights',
  '*_youtube_analytics', '*_google_ads', '*_echo_conversations', '*_disciple_tools',
  '*_chatwoot_insights',  // Chatwoot sources for the interact band
  '*_tiktok_organic',     // TikTok card in the engage band
  '*_conversation_rollups',  // archived Echo months the retention sweep rolled up
];

/**
 * Return a query config with any DASHBOARD_EXTRA_FILTERS merged in.
 * DASHBOARD_EXTRA_FILTERS is set by view.html from URL params (?org_filter=&team_filter=).
 * In the builder (no extra filters), window.DASHBOARD_EXTRA_FILTERS is undefined → no-op.
 *
 * also attaches dashboard-level filters (DASHBOARD_STATE.filters in the
 * builder; window.DASHBOARD_FILTERS in the viewer) as a sibling top-level
 * ``dashboard_filters`` array on the request body. The server merges them by
 * matching data_view; mismatched-DV charts are unaffected.
 *
 * a filter whose data_view is the '__ALL__' sentinel is rebound here
 * to the active chart's own data_view, so an "All Data Views" filter on a shared
 * field applies to every chart/tile regardless of its source.
 */
function _withExtraFilters(queryConfig) {
  const extra = (typeof window !== 'undefined' && window.DASHBOARD_EXTRA_FILTERS) || [];
  const timeRange = (typeof window !== 'undefined' && window.DASHBOARD_TIME_RANGE) || {};
  const publicToken = (typeof window !== 'undefined' && window.PUBLIC_REPORT_TOKEN) || '';
  // viewer-selected date basis. When the dashboard declares
  // time_field_choices, the viewer picks one and view.html sets
  // window.DASHBOARD_TIME_FIELD. It overrides the chart's builder-time
  // time_range_field, which in turn overrides the kibana_date
  // default — see the non-DT branch below. Empty/absent -> no override.
  const viewerTimeField = (typeof window !== 'undefined' && window.DASHBOARD_TIME_FIELD) || '';
  // dashboard-level filters: builder reads from DASHBOARD_STATE,
  // viewer side will set window.DASHBOARD_FILTERS (Phase 21).
  const dashboardFilters =
    (typeof DASHBOARD_STATE !== 'undefined' && Array.isArray(DASHBOARD_STATE.filters) && DASHBOARD_STATE.filters) ||
    (typeof window !== 'undefined' && Array.isArray(window.DASHBOARD_FILTERS) && window.DASHBOARD_FILTERS) ||
    [];

  let result = queryConfig;

  if (extra.length) {
    result = { ...result, filters: [...(result.filters || []), ...extra] };
  }

  if (dashboardFilters.length) {
    // rebind any "All Data Views" filter to THIS chart's data_view so
    // the shared field applies here too. The server only honors a dashboard
    // filter whose data_view matches the chart's; an unrebound '__ALL__' would
    // match nothing. Specific-DV filters pass through untouched.
    const activeDv = result.data_view || '';
    const rebound = activeDv
      ? dashboardFilters.map(f =>
          (f && f.data_view === DASHBOARD_FILTER_ALL_DV)
            ? { ...f, data_view: activeDv }
            : f)
      : dashboardFilters;
    result = { ...result, dashboard_filters: rebound };
  }

  // public viewer: inject the token into every query. The public API
  // rejects requests without it and ignores any other filters the client tried
  // to forge onto organization_title / team_title.
  if (publicToken) {
    result = { ...result, token: publicToken };
  }

  // Inject global time range as date_range unless the chart already has one set.
  if (!result.date_range && (timeRange.from || timeRange.to)) {
    const dataView = result.data_view || '';
    if (dataView) {
      const isDT = _isDtDataView(dataView);
      if (isDT) {
        // DT _ms fields are ES date type (epoch_millis) — pass date math directly.
        // dt_date_field can be set per-column to override the default.
        // Legacy keyword fields still need _resolveDateMath.
        const dtField = result.dt_date_field || 'date_created_ms';
        if (dtField.endsWith('_ms')) {
          result = { ...result, date_range: {
            field: dtField,
            from: timeRange.from || null,
            to:   timeRange.to || null,
          }};
        } else {
          result = { ...result, date_range: {
            field: dtField,
            from: _resolveDateMath(timeRange.from) || null,
            to:   _resolveDateMath(timeRange.to, true) || null,
          }};
        }
      } else {
        // chart-level time_range_field lets any chart pick the date
        // field the global time-range dropdown filters on, instead of the
        // kibana_date default. Echo charts set it to closed_at so they count
        // conversations resolved in the window, matching Echo's own dashboard
        // (kibana_date derives from createdAt;). The field is a date
        // with a custom stored format on Echo, but the range clause carries
        // its own format hint for ISO bounds and ES evaluates date math
        // natively, so no extra handling is needed here. Empty/absent ->
        // unchanged kibana_date (or date-typed x_field) default.
        const dateField = viewerTimeField
          || result.time_range_field
          || ((result.x_field_type === 'date' && result.x_field) ? result.x_field : 'kibana_date');
        result = { ...result, date_range: { field: dateField, from: timeRange.from || null, to: timeRange.to || null } };
      }
    }
  }

  return result;
}

// ══════════════════════════════════════════════════════════════
// Main query + render function
// ══════════════════════════════════════════════════════════════

async function queryAndRender(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;

  // Canva chart — renders as an iframe embedding a visual report template
  if (config.chart_type === 'canva') {
    return renderCanvaChart(chartId);
  }

  // Report chart — self-contained impact report (Facebook or Google)
  if (config.chart_type === 'report' || config.chart_type === 'google_report' || config.chart_type === 'combined_report') {
    return renderReportChart(chartId);
  }

  // Grouped table handles its own per-cell queries
  if (config.chart_type === 'grouped_table') {
    return renderGroupedTable(chartId);
  }

  // Leaflet map uses its own endpoint
  if (config.chart_type === 'leaflet_map') {
    return requeryLeafletMap(chartId);
  }

  // Profile card uses its own single-doc endpoint
  if (config.chart_type === 'profile') {
    return renderProfileCard(chartId);
  }

  // Trend line uses its own multi-source endpoint
  if (config.chart_type === 'trend_line') {
    return queryAndRenderTrendLine(chartId);
  }

  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  // Show loading state
  renderArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#aaa;font-size:13px;">Loading…</div>';

  try {
    const res = await fetch(DASHBOARD_BASE + 'api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_withExtraFilters(config)),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      showChartError(chartId, err.error || 'Query failed');
      return;
    }

    const option = await res.json();

    if (option.error) {
      showChartError(chartId, option.error);
      return;
    }

    if (option._warnings && option._warnings.length) {
      console.warn(`[chart ${chartId}] server warnings:`, option._warnings);
    }

    if (option.chart_type === 'table') {
      renderTable(chartId, option);
    } else if (option.chart_type === 'metric') {
      renderMetric(chartId, option);
    } else {
      renderEChart(chartId, option);
    }

    // show/clear the inflated-reach warning. For tables the ⚠ is on
    // the reach column header (handled in renderTable); for other chart types
    // this paints/clears the slot-corner badge. Runs after the renderer so the
    // badge sits over the freshly rendered slot.
    applyReachWarning(chartId, option.reach_warning, option.chart_type);

    // notify the viewer's first-look syncing banner so it can
    // auto-dismiss once any chart has reported non-empty data. Detect
    // "has data" against the option shape ECharts already received:
    //   - metric → option.value is a finite, non-zero number
    //   - table  → option.rows / option.data has at least one entry
    //   - chart  → at least one series has a non-empty data array
    try {
      let hasData = false;
      if (option.chart_type === 'metric') {
        hasData = typeof option.value === 'number' && isFinite(option.value) && option.value !== 0;
      } else if (option.chart_type === 'table') {
        const rows = option.rows || option.data || [];
        hasData = Array.isArray(rows) && rows.length > 0;
      } else {
        const series = option.series || [];
        hasData = Array.isArray(series) && series.some(s => Array.isArray(s.data) && s.data.length > 0);
      }
      document.dispatchEvent(new CustomEvent('chart-rendered', {
        detail: { chartId, hasData },
      }));
    } catch (_e) { /* event hook is best-effort */ }
  } catch (e) {
    showChartError(chartId, e.message);
  }
}

// ══════════════════════════════════════════════════════════════
// ECharts (bar / line / pie)
// ══════════════════════════════════════════════════════════════

function renderEChart(chartId, option) {
  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  renderArea.innerHTML = '';

  // Dispose previous instance if exists
  if (CHART_INSTANCES[chartId]) {
    try { CHART_INSTANCES[chartId].dispose(); } catch (e) {}
  }

  const chart = echarts.init(renderArea, 'kibana');
  CHART_INSTANCES[chartId] = chart;

  // Apply saved display_toggles (hide deselected series)
  const config = DASHBOARD_STATE.charts[chartId];
  if (config.display_toggles && option.legend) {
    const toggles = config.display_toggles;
    // Apply initial legend selection state
    const legendSelected = {};
    (option.legend.data || []).forEach(name => {
      legendSelected[name] = toggles[name] !== false;
    });
    option.legend.selected = legendSelected;
  }

  // Apply Data Format to X-axis labels when the X field is a date type.
  // route through the shared _applyDateFormat so the chart's
  // "Data Format" dropdown renders dates identically to a table date column
  // (same DATE_FORMAT_OPTIONS token set: MMM YYYY, MMMM DD, YYYY, etc.).
  // Legacy 6-option labels (Mon YYYY, DD Mon, …) saved on older dashboards are
  // also handled below for back-compat. The ES bucket key is ISO (yyyy-MM-dd),
  // which _applyDateFormat / Date() parse cleanly.
  if (config && config.x_field_type === 'date' && option.xAxis) {
    if (!option.xAxis.axisLabel) option.xAxis.axisLabel = {};
    const fmt = config.date_format;
    // the "DD (day number)" option, and the no-format DAY-interval
    // default, both render day-over-3-letter-month with the month line only on
    // a month change (a bare repeated day number is unreadable on a time axis).
    const useDayAxis =
      fmt === 'DD' || (!fmt && (config.calendar_interval || 'day') === 'day');
    if (useDayAxis) {
      option.xAxis.axisLabel.formatter = _makeDayAxisFormatter();
      // Stacked day-over-month labels read best horizontal; the transformer
      // sets rotate:30 for keyword axes, which would skew the two-line label.
      option.xAxis.axisLabel.rotate = 0;
      option.xAxis.axisLabel.lineHeight = 14;
    } else if (fmt) {
      // Any other explicit Data Format — honor it verbatim.
      option.xAxis.axisLabel.formatter = val => _formatDateAxisLabel(val, fmt);
    }
    // (no fmt + week/month interval): keep the raw ISO key, which reads fine.
  }

  // Ensure Y-axis labels are never clipped by the chart container edge.
  // containLabel: true tells ECharts to include axis labels in its grid sizing.
  option.grid = Object.assign(
    { containLabel: true, left: '8px', right: '16px', top: '36px', bottom: '48px' },
    option.grid || {}
  );

  // Abbreviate large numbers on the Y axis: 1K, 1.5M, etc.
  if (option.yAxis) {
    if (!option.yAxis.axisLabel) option.yAxis.axisLabel = {};
    option.yAxis.axisLabel.formatter = v => {
      const abs = Math.abs(v);
      if (abs >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
      if (abs >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
      return v;
    };
    // Apply custom min/max if set
    if (config.y_min !== '' && config.y_min !== null && config.y_min !== undefined) {
      option.yAxis.min = parseFloat(config.y_min);
    }
    if (config.y_max !== '' && config.y_max !== null && config.y_max !== undefined) {
      option.yAxis.max = parseFloat(config.y_max);
    }
  }

  chart.setOption(option);
  chart.resize();

  // Populate display toggles in right panel
  if (_isSelectedChart(chartId)) {
    populateDisplayToggles(chartId, option);
  }

  // Render inline legend below the chart
  const legendNames = option.legend && option.legend.data ? option.legend.data : [];
  renderChartLegend(chartId, legendNames);

  // Listen to legend toggle events to keep state in sync
  chart.on('legendselectchanged', params => {
    const cfg = DASHBOARD_STATE.charts[chartId];
    if (!cfg) return;
    cfg.display_toggles = { ...cfg.display_toggles, ...params.selected };
    if (_isSelectedChart(chartId)) {
      populateDisplayToggles(chartId, chart.getOption());
    }
    // Sync inline legend checkboxes without re-rendering
    const leg = document.getElementById(`legend-${chartId}`);
    if (leg) {
      leg.querySelectorAll('.chart-legend-item').forEach(item => {
        const name = item.dataset.name;
        const cb = item.querySelector('input[type="checkbox"]');
        if (name && cb && name in params.selected) cb.checked = params.selected[name];
      });
    }
  });

  // Resize on window resize
  window.addEventListener('resize', () => {
    if (CHART_INSTANCES[chartId]) {
      try { CHART_INSTANCES[chartId].resize(); } catch (e) {}
    }
  });
}

// ══════════════════════════════════════════════════════════════
// Inline chart legend (sits below the render area)
// ══════════════════════════════════════════════════════════════

function renderChartLegend(chartId, names) {
  const container = document.getElementById(`legend-${chartId}`);
  if (!container) return;

  if (!names || !names.length) {
    container.style.display = 'none';
    container.innerHTML = '';
    return;
  }

  const config = DASHBOARD_STATE.charts[chartId];
  const chart = CHART_INSTANCES[chartId];

  // Use the actual color palette from the ECharts instance if available
  let palette = ECHARTS_PALETTE;
  if (chart) {
    const opt = chart.getOption();
    if (opt.color && opt.color.length) palette = opt.color;
  }

  container.style.display = 'flex';
  container.innerHTML = names.map((name, i) => {
    const visible = !config.display_toggles || config.display_toggles[name] !== false;
    const color = palette[i % palette.length];
    return `<label class="chart-legend-item" data-name="${escHtml(name)}">` +
      `<input type="checkbox" ${visible ? 'checked' : ''}>` +
      `<span class="chart-legend-swatch" style="background:${color};"></span>` +
      `<span class="chart-legend-label">${escHtml(name)}</span>` +
      `</label>`;
  }).join('');

  // Wire checkboxes → dispatch to ECharts + update state
  container.querySelectorAll('.chart-legend-item').forEach(item => {
    const name = item.dataset.name;
    item.querySelector('input').addEventListener('change', e => {
      const visible = e.target.checked;
      if (chart) chart.dispatchAction({ type: 'legendToggleSelect', name });
      if (!config.display_toggles) config.display_toggles = {};
      config.display_toggles[name] = visible;
      if (typeof _markDirty === 'function') _markDirty();
      // Sync right-panel toggles if this chart is selected
      if (_isSelectedChart(chartId) && chart) {
        populateDisplayToggles(chartId, chart.getOption());
      }
    });
  });
}

// ══════════════════════════════════════════════════════════════
// Table
// ══════════════════════════════════════════════════════════════

// Sort state keyed by chartId: { colIndex, direction: 'asc'|'desc' }.
// Transient — populated either when the user clicks a sort menu option,
// or hydrated from config.sort_col_field / config.sort_direction on
// first render. Phase 17 T6.
const _tableSortState = {};

// Phase 17 T6 — resolve sort persistence between
// data-column-index (used at runtime to sort rows) and field-name
// (used in storage so the saved sort survives column reorder/removal).
function _numLabelCols(config) {
  return config.x_field ? (config.x_field_2 ? 2 : 1) : 0;
}

function _resolveSortFieldFromColIndex(config, colIndex) {
  const n = _numLabelCols(config);
  if (colIndex < n) return colIndex === 0 ? '__row_label_1' : '__row_label_2';
  const tcol = (config.table_columns || [])[colIndex - n];
  return tcol && tcol.field ? tcol.field : null;
}

function _resolveColIndexFromSortField(config, sortField, dataColumns) {
  if (!sortField || !Array.isArray(dataColumns)) return -1;
  const n = _numLabelCols(config);
  if (sortField === '__row_label_1') return n >= 1 ? 0 : -1;
  if (sortField === '__row_label_2') return n >= 2 ? 1 : -1;
  // Match against the table_columns[i].field of each value column.
  // dataColumns[i+n] corresponds to table_columns[i].
  const tcols = config.table_columns || [];
  for (let i = 0; i < tcols.length; i++) {
    if (tcols[i].field === sortField && (i + n) < dataColumns.length) return i + n;
  }
  return -1;
}

// R5 (UI) — dedup empty-cell reason copy. Mirrors the server source of
// truth in services/dedup_routing.py DEDUP_EMPTY_REASONS; the per-cell payload
// carries only the reason CODE, so the client maps it to copy here. Keep in sync
// with the server table (the column-level path sends `message` pre-rendered).
const _DEDUP_EMPTY_COPY = {
  not_built: "Deduplicated data isn't available for this source yet.",
  no_window: 'No deduplicated data has been pulled for this account.',
  no_data: 'No deduplicated data for this row in the selected window.',
  // unlike the others, multi_grain carries a VALUE (the daily sum):
  // the scope spans 2+ dedup grains (2+ GA4 properties), so the number shown is
  // the un-deduplicated upper bound. Select one property for an exact count.
  multi_grain: 'approx upper bound — not deduplicated (select one property for an exact count)',
  unknown: 'Deduplicated data unavailable for this range.',
};

function _dedupEmptyCopy(reason) {
  return _DEDUP_EMPTY_COPY[reason] || _DEDUP_EMPTY_COPY.unknown;
}

// column-HEADER guidance tooltip for the non-additive (deduplicated)
// window fields. These are read per ad set with `max` (FB only deduplicates reach
// within one API call's grain), so they are only correct in a table that breaks
// out one row per ad set — never summed into a total/rollup. This is usage
// guidance shown on EVERY such column header (full or empty); it is distinct from
// the empty-CELL reason copy above, which explains why a particular cell is blank.
// Keyed by the registry's live FB dedup fields (DEDUP_WINDOW_REGISTRY).
const _DEDUP_FIELD_HEADER_TOOLTIP = {
  insight_reach: 'Reach is calculated per Ad Set and should only be used in tables with Ad Sets broken out',
  insight_frequency: 'Frequency is calculated per Ad Set and should only be used in tables with Ad Sets broken out',
  insight_full_view_reach: 'Full-view reach is calculated per Ad Set and should only be used in tables with Ad Sets broken out',
  insight_estimated_ad_recallers: 'Estimated ad recallers is calculated per Ad Set and should only be used in tables with Ad Sets broken out',
  insight_estimated_ad_recall_rate: 'Estimated ad recall rate is calculated per Ad Set and should only be used in tables with Ad Sets broken out',
};

// R5 (UI) — build the caption rendered under a dedup table: the
// snapped-window label (R3) and/or a column-level empty reason (R4), plus an
// inline "Refresh for this range" link when the dashboards page exposes the
// dedup-refresh kickoff (window.refreshDedupData, set in dashboards.html). In
// the builder (no refreshDedupData) the link is omitted — the label/reason still
// render. Returns '' when there's no dedup metadata (unrouted / flag-off).
function _dedupCaptionHtml(data) {
  const dw = data && data.dedup_window;
  const de = data && data.dedup_empty;
  if (!dw && !de) return '';

  const parts = [];
  if (dw && dw.label) {
    // when the served window differs from the selected range, say
    // so in the caption too (the ≈ header marker points at the columns).
    const mismatchNote = dw.exact === false ? ' — differs from your selected range' : '';
    parts.push(
      `<span class="gt-dedup-window">Deduplicated window: ${escHtml(dw.label)}${escHtml(mismatchNote)}</span>`
    );
  }
  if (de && de.message) {
    parts.push(`<span class="gt-dedup-reason">${escHtml(de.message)}</span>`);
  }

  // Inline refresh — only on surfaces that wired the kickoff (dashboards page).
  const canRefresh = typeof window !== 'undefined' && typeof window.refreshDedupData === 'function';
  if (canRefresh) {
    parts.push(
      `<a href="#" class="gt-dedup-refresh" onclick="event.preventDefault();window.refreshDedupData();">` +
        `Refresh for the exact range</a>`
    );
  }

  if (!parts.length) return '';
  return `<div class="gt-dedup-caption" style="color:#888;font-size:11px;padding:4px 4px 2px;">` +
    parts.join(' · ') + `</div>`;
}

// inflated-reach warning triangle for NON-table charts. The server
// attaches a ``reach_warning`` ({fields, message}) to the query response of any
// chart bound to a FB reach field showing the un-deduplicated daily sum (a
// per-day sum overstates true reach — people reached on multiple days are
// counted once per day). Bar/line/pie/metric have no column to hang the warning
// on, so we render a small ⚠ in the chart slot's top-left corner with the
// server's message as a native hover tooltip. TABLES are handled inside
// renderTable instead, where the ⚠ goes on the specific reach column header
// (pointing at the exact inflated value rather than a corner badge that reads as
// flagging the first/label column). Re-applied on every query so a config change
// that drops the reach field — or switches to a window/deduplicated view —
// clears the triangle.
function applyReachWarning(chartId, warning, chartType) {
  const renderArea = document.getElementById(`chart-${chartId}`);
  // The slot is the positioned ancestor that already hosts the absolute
  // remove button (top-right); we anchor the badge to the same element so it
  // works identically in the builder and the viewer.
  const slot = renderArea && renderArea.closest
    ? (renderArea.closest('.chart-slot') || renderArea.parentElement)
    : (renderArea && renderArea.parentElement);
  if (!slot) return;

  // Clear any prior badge first so a now-deduplicated/changed chart loses it.
  const existing = slot.querySelector(':scope > .reach-warning-badge');
  if (existing) existing.remove();

  // Tables render the warning on the reach column header (renderTable) — never
  // a corner badge — so bail here to avoid a duplicate, mis-pointed indicator.
  if (chartType === 'table') return;

  if (!warning || !warning.message) return;

  const badge = document.createElement('span');
  badge.className = 'reach-warning-badge';
  badge.textContent = '⚠';
  badge.setAttribute('title', warning.message);
  badge.setAttribute('aria-label', warning.message);
  badge.setAttribute('role', 'img');
  slot.appendChild(badge);
}

function renderTable(chartId, data) {
  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  const leg = document.getElementById(`legend-${chartId}`);
  if (leg) { leg.style.display = 'none'; leg.innerHTML = ''; }

  const config = DASHBOARD_STATE.charts[chartId];
  const hiddenCols = new Set();
  if (config.display_toggles) {
    Object.entries(config.display_toggles).forEach(([col, visible]) => {
      if (!visible) hiddenCols.add(col);
    });
  }

  // Number of leading label columns (x_field + optional x_field_2)
  // When x_field is absent (auto-promoted text col), there are no leading label cols.
  const numLabelCols = config.x_field ? (config.x_field_2 ? 2 : 1) : 0;

  // Build display-name map: raw field → human label
  // table_columns is authoritative; fall back to x_label/y_labels for converted charts
  const colLabels = {};
  if (config.x_label && data.columns[0]) colLabels[data.columns[0]] = config.x_label;
  if (config.x_label_2 && data.columns[1]) colLabels[data.columns[1]] = config.x_label_2;
  if (config.table_columns && config.table_columns.length) {
    config.table_columns.forEach((col, i) => {
      const dataCol = data.columns[i + numLabelCols];
      if (dataCol && col.label) colLabels[dataCol] = col.label;
    });
  } else if (config.y_labels && config.y_labels.length) {
    config.y_labels.forEach((label, i) => {
      const col = data.columns[i + numLabelCols];
      if (col) colLabels[col] = label;
    });
  }

  // Build per-column format-string + date-format maps from table_columns.
  // Format string is the Phase 17 T1 replacement for col.decimals;
  // _columnEffectiveFormat() falls back to the legacy decimals key so
  // existing dashboards keep rendering identically without a save.
  const colFormats = {};
  const colDateFormats = {};
  if (config.table_columns && config.table_columns.length) {
    config.table_columns.forEach((col, i) => {
      const dataCol = data.columns[i + numLabelCols];
      if (dataCol !== undefined) {
        colFormats[dataCol] = _columnEffectiveFormat(col);
        if (col.aggregation === 'date' && col.date_format) colDateFormats[dataCol] = col.date_format;
      }
    });
  }

  // Apply sort. In-memory _tableSortState wins (covers in-session
  // clicks); if empty, hydrate from saved config.sort_col_field +
  // config.sort_direction so reloads / dashboard-view renders honour
  // a previously-saved sort. Resolve sort_col_field → colIndex against
  // the current data.columns: a missing field (column removed) is
  // simply ignored — the table renders unsorted, no JS error.
  // Phase 17 T6.
  if (!_tableSortState[chartId] && config.sort_col_field && config.sort_direction) {
    const idx = _resolveColIndexFromSortField(config, config.sort_col_field, data.columns);
    if (idx >= 0) {
      _tableSortState[chartId] = { colIndex: idx, direction: config.sort_direction };
    }
  }
  const sortState = _tableSortState[chartId];
  // R5 (UI) — pair each row with its parallel dedup_empty_cells entry
  // ({output-col-index: reason}) BEFORE sorting, so the explained-empty markers
  // stay aligned to their row after a sort. Absent on the unrouted path → all
  // empty maps default to {} and the table renders exactly as before.
  const emptyCells = Array.isArray(data.dedup_empty_cells) ? data.dedup_empty_cells : [];
  // carry the row's ORIGINAL index so a server-baked totals row
  // (data.totals_row_index, set only on report snapshots) can be styled
  // distinctly even after a sort reorders the body rows.
  let displayPairs = data.rows.map((r, k) => ({ row: r, empties: emptyCells[k] || {}, srcIndex: k }));
  if (sortState) {
    const { colIndex, direction } = sortState;
    displayPairs.sort((pa, pb) => {
      const va = pa.row[colIndex], vb = pb.row[colIndex];
      let cmp;
      if (typeof va === 'number' && typeof vb === 'number') {
        cmp = va - vb;
      } else {
        cmp = String(va ?? '').localeCompare(String(vb ?? ''));
      }
      return direction === 'asc' ? cmp : -cmp;
    });
  }
  const displayRows = displayPairs.map(p => p.row);

  const headerBg = escHtml(config.title_bg_color || '#2c6da0');
  const headerColor = '#ffffff';

  // Phase 17 T2 — per-column resizable widths.
  // Storage shape on chart config: column_widths is a map keyed by either
  //   - the table_columns[i].field name (for value columns), or
  //   - "__row_label_1" / "__row_label_2" sentinels (for x_field row labels)
  // Field-name keys survive column reorder/removal cleanly.
  // Build a data-column-name → field-key resolver so the renderer + drag
  // handler can write back into config.column_widths after a resize.
  const colKeyByDataCol = {};
  data.columns.forEach((dataCol, i) => {
    if (i < numLabelCols) {
      colKeyByDataCol[dataCol] = i === 0 ? '__row_label_1' : '__row_label_2';
    } else if (config.table_columns && config.table_columns[i - numLabelCols]) {
      const fieldName = config.table_columns[i - numLabelCols].field;
      if (fieldName) colKeyByDataCol[dataCol] = fieldName;
    }
  });
  const widthMap = (config.column_widths && typeof config.column_widths === 'object') ? config.column_widths : {};
  const hasAnyWidth = Object.keys(widthMap).length > 0;

  // Switch to table-layout: fixed only when at least one column has a
  // saved width — otherwise the browser would redistribute the explicit
  // width across other columns and the resize would feel broken.
  const tableStyle = hasAnyWidth ? 'table-layout:fixed;' : '';

  // inflated-reach warning, per column. The server attaches
  // data.reach_warning = {fields, message} when the table shows an
  // un-deduplicated daily-sum FB reach field. We stamp a ⚠ on the header of
  // each warned column (matched by table_columns[i].field, which is widthKey)
  // rather than a chart-corner badge, so the warning points at the exact
  // inflated value. Empty set / message → no ⚠ rendered.
  const reachWarnFields = new Set(
    (data.reach_warning && Array.isArray(data.reach_warning.fields)) ? data.reach_warning.fields : []
  );
  const reachWarnMsg = (data.reach_warning && data.reach_warning.message) || '';

  // snapped-window mismatch marker. When the dedup values came from
  // a NEARBY pulled window rather than the user's exact selected range
  // (dedup_window.exact === false), stamp ≈ on each affected column header so
  // the mismatch is visible AT the value, not only in the caption below the
  // table. The number is real and deduplicated — just for the labeled window.
  const dwSnap = data.dedup_window;
  const snapMismatch = !!(dwSnap && dwSnap.exact === false);
  const snapFields = new Set(snapMismatch && Array.isArray(dwSnap.fields) ? dwSnap.fields : []);
  const snapMsg = snapMismatch
    ? `Deduplicated for ${dwSnap.label || 'a nearby pulled window'} — the closest pulled window, not your selected range. Refresh to pull the exact range.`
    : '';

  let html = `<table class="gt-table" style="${tableStyle}"><thead><tr>`;
  data.columns.forEach((col, i) => {
    if (hiddenCols.has(col)) return;
    const align = 'text-align:left;';
    const displayName = colLabels[col] || col;
    const sortIndicator = sortState && sortState.colIndex === i
      ? (sortState.direction === 'asc' ? ' ↑' : ' ↓') : '';
    const widthKey = colKeyByDataCol[col];
    const savedWidth = widthKey && widthMap[widthKey];
    const widthStyle = savedWidth ? `width:${parseInt(savedWidth, 10)}px;` : '';
    // Resize handle on every column except the last visible one — dragging
    // the right edge of the last column has no neighbour to push, so the
    // affordance would be misleading.
    const isLastVisible = i === data.columns.length - 1;
    const resizeHandle = (!isLastVisible && widthKey)
      ? `<div class="gt-col-resize-handle" data-col-key="${escHtml(widthKey)}" data-chart-id="${escHtml(chartId)}"></div>`
      : '';
    // guidance tooltip on a deduplicated (window) field's header.
    // widthKey doubles as the table_columns[i].field name for value columns; if
    // it's a live dedup field, hover explains the per-ad-set caveat.
    const headerTip = _DEDUP_FIELD_HEADER_TOOLTIP[widthKey];
    const headerTipAttr = headerTip ? ` title="${escHtml(headerTip)}"` : '';
    // inflated-reach warning. The server reports which fields show an
    // un-deduplicated daily sum (reachWarnFields, built below). When this column
    // IS one of them, prefix its label with a ⚠ whose hover tooltip carries the
    // server's explanation — pointing at the exact inflated column rather than a
    // chart-corner badge that reads as flagging the first (label) column.
    const isReachWarn = widthKey && reachWarnFields.has(widthKey);
    const reachWarnSpan = isReachWarn
      ? `<span class="reach-warning-inline" title="${escHtml(reachWarnMsg)}" aria-label="${escHtml(reachWarnMsg)}" role="img">⚠</span> `
      : '';
    // ≈ marker on columns served from a snapped (inexact) window.
    const isSnapCol = widthKey && snapFields.has(widthKey);
    const snapSpan = isSnapCol
      ? `<span class="dedup-snap-inline" title="${escHtml(snapMsg)}" aria-label="${escHtml(snapMsg)}" role="img">≈</span> `
      : '';
    html += `<th class="gt-col-cell"${headerTipAttr} style="background:${headerBg};color:${headerColor};${align}${widthStyle}">` +
      `<div class="gt-col-header-wrap">` +
      `<span class="gt-col-header-label">${reachWarnSpan}${snapSpan}${escHtml(displayName)}${sortIndicator}</span>` +
      `<button class="gt-sort-btn" data-col-idx="${i}" data-chart-id="${escHtml(chartId)}">⋮</button>` +
      `</div>${resizeHandle}</th>`;
  });
  html += '</tr></thead><tbody>';

  displayPairs.forEach(pair => {
    const row = pair.row;
    const empties = pair.empties;
    // a report snapshot bakes its totals row into data.rows and
    // stamps data.totals_row_index (the live builder appends + styles its own
    // row below instead, and never sets this key). Style the baked row to match.
    const isBakedTotals = typeof data.totals_row_index === 'number' && pair.srcIndex === data.totals_row_index;
    html += isBakedTotals
      ? '<tr class="gt-totals-row" style="font-weight:600;background:#f0f3f7;">'
      : '<tr>';
    data.columns.forEach((col, i) => {
      if (hiddenCols.has(col)) return;
      const val = row[i];
      // R5 (UI) — explained-empty dedup cell: the routed read path
      // deleted the (would-be daily-summed) value and stamped a reason code.
      // Render an em-dash with the reason copy on hover instead of a bare
      // blank, so the user knows WHY there's no dedup value (never a silent
      // daily-sum fallback). Reason copy mirrors the server DEDUP_EMPTY_REASONS.
      //
      // multi_grain is the EXCEPTION: it KEEPS the daily-summed value
      // (the labeled upper bound), so it must NOT blank to an em-dash like the
      // others. Fall through to the normal value render, but mark the cell amber
      // with the "approx upper bound" tooltip so the number reads as a ceiling,
      // not an exact dedup figure. (Blanking it here was the blank-tables bug:
      // the value column rendered all em-dashes even though the data was present.)
      const emptyReason = empties[i];
      if (emptyReason && emptyReason !== 'multi_grain') {
        const reasonCopy = _dedupEmptyCopy(emptyReason);
        html += `<td class="gt-data-cell gt-dedup-empty" style="text-align:left;color:#999;" ` +
          `title="${escHtml(reasonCopy)}">—</td>`;
        return;
      }
      let displayVal;
      if (colDateFormats[col]) {
        displayVal = _applyDateFormat(String(val ?? ''), colDateFormats[col]);
      } else if (i >= numLabelCols && typeof val === 'number') {
        displayVal = _applyFormatString(val, colFormats[col] || '0.00');
      } else if (i >= numLabelCols && val === null) {
        // Phase 18 — calculation columns return null on
        // div-by-zero / missing field. Render an em-dash so the
        // value column is visibly "no data" rather than empty.
        displayVal = '—';
      } else {
        displayVal = String(val ?? '');
      }
      // a multi_grain value cell shows the number but flagged as the
      // upper bound (amber + tooltip), distinguishing it from an exact dedup value.
      // follow-up — prefer the server's column-level message (it varies:
      // "select one property" for multi-property vs "this breakdown can't be
      // deduplicated" for a sub-dimension table) over the generic client copy, so
      // the tooltip isn't misleading after the user already selected a property.
      if (emptyReason === 'multi_grain') {
        const mgMsg = (data.dedup_empty && data.dedup_empty.message) || _dedupEmptyCopy('multi_grain');
        html += `<td class="gt-data-cell gt-dedup-upper-bound" style="text-align:left;color:#b8860b;" ` +
          `title="${escHtml(mgMsg)}">${escHtml(displayVal)}</td>`;
      } else {
        html += `<td class="gt-data-cell" style="text-align:left;">${escHtml(displayVal)}</td>`;
      }
    });
    html += '</tr>';
  });

  // optional grand-totals row. Appended after the data rows (after
  // sorting), before </tbody>, only when the chart opted in. Each numeric,
  // not-opted-out column is summed or averaged (per config.totals_aggregation)
  // over the rendered rows; non-numeric and opted-out columns are blank. The
  // first label cell reads "Totals"; a second label cell (x_field_2) is blank.
  // Totals cells reuse the same per-column format string as the data cells so
  // they match decimals / $ / % exactly.
  if (config.include_totals_row === true && displayRows.length > 0) {
    const totalsAgg = config.totals_aggregation === 'avg' ? 'avg' : 'sum';
    // prefer the server-computed totals (data.totals, aligned with
    // data.columns; null = blank cell). The server totals cover the WHOLE
    // filtered scope and compute ratio columns as ratio-of-sums (CTR = total
    // clicks / total impressions, cost-per = total spend / total results),
    // matching Ads Manager's totals row. The legacy client-side sum/avg over
    // rendered rows — which mis-totaled ratio and deduplicated columns — runs
    // only as fallback for payloads without data.totals (old snapshots).
    const serverTotals = Array.isArray(data.totals) ? data.totals : null;
    html += '<tr class="gt-totals-row" style="font-weight:600;background:#f0f3f7;">';
    data.columns.forEach((col, i) => {
      if (hiddenCols.has(col)) return;
      // Leading label columns: "Totals" in the first, blank in the rest.
      if (i < numLabelCols) {
        html += `<td class="gt-data-cell" style="text-align:left;">${i === 0 ? 'Totals' : ''}</td>`;
        return;
      }
      // Per-column opt-out (default-on). table_columns aligns positionally to
      // value columns: data column i → table_columns[i - numLabelCols].
      const tcol = (config.table_columns && config.table_columns[i - numLabelCols]) || null;
      const includedInTotals = !tcol || tcol.include_in_totals !== false;
      let cellHtml = '';
      if (includedInTotals && serverTotals) {
        const v = serverTotals[i];
        if (typeof v === 'number' && isFinite(v)) {
          cellHtml = escHtml(_applyFormatString(v, colFormats[col] || '0.00'));
        }
      } else if (includedInTotals) {
        let sum = 0;
        let count = 0;
        displayRows.forEach(row => {
          const v = row[i];
          if (typeof v === 'number' && isFinite(v)) { sum += v; count += 1; }
        });
        // count === 0 → non-numeric column → blank cell.
        if (count > 0) {
          const total = totalsAgg === 'avg' ? sum / count : sum;
          cellHtml = escHtml(_applyFormatString(total, colFormats[col] || '0.00'));
        }
      }
      html += `<td class="gt-data-cell" style="text-align:left;">${cellHtml}</td>`;
    });
    html += '</tr>';
  }

  html += '</tbody></table>';
  if (displayRows.length === 0) {
    const hint = config.x_field_2
      ? ' Sub-row field "' + escHtml(config.x_field_2) + '" may be absent from these documents. Try a different field (e.g. location_cities.name).'
      : '';
    html += `<div style="color:#999;font-size:12px;padding:6px 4px;">No results.${hint}</div>`;
  }

  // R5 (UI) — dedup caption: the snapped-window label and/or a
  // column-level empty reason, with an inline "refresh for the exact range"
  // affordance. Only emitted when the routed read path attached the metadata
  // (flag ON + a dedup field); absent → no caption, table renders as before.
  html += _dedupCaptionHtml(data);

  renderArea.innerHTML = html;

  // Attach sort button handlers
  renderArea.querySelectorAll('.gt-sort-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      _showTableSortMenu(btn, parseInt(btn.dataset.colIdx, 10), btn.dataset.chartId);
    });
  });

  // Phase 17 T2 — column resize drag handlers (builder only).
  // Dashboards-page render uses the same renderTable but with a stub
  // markDirty/saveDashboard, so the resize is interactive on dashboards
  // but doesn't persist (T3). On the builder, the drag commits a width
  // into config.column_widths and marks dirty so Save picks it up.
  renderArea.querySelectorAll('.gt-col-resize-handle').forEach(handle => {
    handle.addEventListener('mousedown', e => _onColResizeStart(e, handle, chartId));
  });

  // per-table font size. Inline-style on the rendered.gt-table
  // so it overrides the stylesheet's 14px. Legacy tables (no
  // table_font_size field) are treated as 12 — the project chose to
  // resize all existing tables to a denser default rather than preserve
  // the original 14px on a per-chart basis.
  const _tblEl = renderArea.querySelector('table.gt-table');
  if (_tblEl) {
    const _rawSize = config.table_font_size != null ? config.table_font_size : 12;
    const _px = parseInt(_rawSize, 10);
    if (!isNaN(_px) && _px > 0) {
      _tblEl.style.fontSize = _px + 'px';
    }
  }

  // cell shading gate. Prefer the new explicit cell_gradient_enabled
  // flag; fall back to the legacy cell_gradient boolean so existing shaded
  // tables stay shaded after this ships. New tables saved with
  // cell_gradient_enabled=false explicitly turn shading off.
  const _shadingOn = config.cell_gradient_enabled != null
    ? !!config.cell_gradient_enabled
    : !!config.cell_gradient;
  if (_shadingOn) {
    applyTableGradient(renderArea, data, config);
  }

  if (_isSelectedChart(chartId)) {
    populateTableToggles(chartId, data.columns, config.display_toggles || {});
  }
}

function _showTableSortMenu(btn, colIndex, chartId) {
  document.querySelectorAll('.gt-sort-menu').forEach(m => m.remove());

  const menu = document.createElement('div');
  menu.className = 'gt-sort-menu';
  menu.innerHTML =
    '<div class="gt-sort-opt" data-dir="asc">↑ Sort Ascending</div>' +
    '<div class="gt-sort-opt" data-dir="desc">↓ Sort Descending</div>';

  const rect = btn.getBoundingClientRect();
  menu.style.cssText = `position:fixed;top:${rect.bottom + 2}px;left:${rect.left}px;z-index:9999;`;

  menu.querySelectorAll('.gt-sort-opt').forEach(opt => {
    opt.addEventListener('click', e => {
      e.stopPropagation();
      const direction = opt.dataset.dir;
      _tableSortState[chartId] = { colIndex, direction };

      // Phase 17 T6 — persist by field name (not column index)
      // so the saved sort survives column reorder/removal. Same logic
      // as T2's column_widths key.
      // Builder: writes config + marks dirty so Save picks it up.
      // Dashboards view: in-memory mutation still happens but
      // _markDirty isn't defined there, so the change isn't saved.
      const cfg = (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE && DASHBOARD_STATE.charts)
        ? DASHBOARD_STATE.charts[chartId] : null;
      if (cfg) {
        const sortField = _resolveSortFieldFromColIndex(cfg, colIndex);
        if (sortField) {
          cfg.sort_col_field = sortField;
          cfg.sort_direction = direction;
          if (typeof _markDirty === 'function') _markDirty();
        }
      }

      menu.remove();
      const cached = _lastTableData[chartId];
      if (cached) (window.renderTable || renderTable)(chartId, cached);
    });
  });

  document.body.appendChild(menu);
  setTimeout(() => {
    document.addEventListener('click', () => menu.remove(), { once: true });
  }, 0);
}

// Phase 17 T2/T3 — column resize drag handler.
// Builder: writes the new width into config.column_widths[colKey] and
// marks the dashboard dirty.
// Dashboards view: resizes live (good UX during reading) but does NOT
// persist — _markDirty / saveDashboard are builder-only globals; the
// commit step is guarded with `typeof _markDirty === 'function'`.
//
// The handle's pointer-events is enabled by CSS only when the cursor is
// over the right edge of the <th>; mousedown captures pointer + initial
// width, mousemove streams updates to th.style.width, mouseup commits.
const _COL_MIN_WIDTH = 40;

function _onColResizeStart(e, handle, chartId) {
  e.preventDefault();
  e.stopPropagation();
  const th = handle.closest('th');
  if (!th) return;
  const colKey = handle.dataset.colKey;
  const startX = e.clientX;
  const startW = th.offsetWidth;

  // While dragging, force the table into fixed layout so the explicit
  // width on the <th> is honoured live. Browsers ignore inline width on
  // <th> when the table is in default content-driven (auto) layout.
  // Once the user resizes once, fixed layout stays for the rest of the
  // session — the next renderTable pass also enables fixed because at
  // least one column now has a saved width.
  const tableEl = th.closest('table');
  if (tableEl) tableEl.style.tableLayout = 'fixed';

  // Add a body-level class so the cursor stays in resize-mode even when
  // the pointer leaves the <th> during a fast drag.
  document.body.classList.add('gt-col-resizing');

  function onMove(ev) {
    const dx = ev.clientX - startX;
    const newW = Math.max(_COL_MIN_WIDTH, startW + dx);
    th.style.width = newW + 'px';
  }

  function onUp(ev) {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.body.classList.remove('gt-col-resizing');
    const dx = ev.clientX - startX;
    const finalW = Math.max(_COL_MIN_WIDTH, startW + dx);

    // Mutate the in-memory chart config so the next renderTable pass
    // (e.g., after sort, hide column, or save) honours the new width.
    // Then mark the dashboard dirty so Save picks it up.
    //
    // Dashboards-view fallthrough (T3): _markDirty is a builder-only
    // global. On the view, the in-memory mutation still happens, so
    // the width persists for the rest of the page session, but no save
    // is ever attempted — reload restores the saved widths.
    if (colKey && typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE && DASHBOARD_STATE.charts) {
      const cfg = DASHBOARD_STATE.charts[chartId];
      if (cfg) {
        if (!cfg.column_widths || typeof cfg.column_widths !== 'object') cfg.column_widths = {};
        cfg.column_widths[colKey] = finalW;
        if (typeof _markDirty === 'function') _markDirty();
      }
    }
  }

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

/**
 * Apply a green gradient background to numeric table cells, matching Kibana's
 * value-heat colour palette (#e5f5e0 → #006d2c).
 * The first column (row label / bucket) is skipped; only numeric metric columns
 * receive colour treatment.
 */
function applyTableGradient(renderArea, data, config) {
  // gradient stops derived from a user-chosen end color
  // (config.cell_gradient_color). The dark stop (t=1) is the chosen
  // color; lighter stops are mixed toward white. Falls back to the
  // original Kibana-green palette (#e5f5e0 to #006d2c) when no color
  // is set, so existing shaded tables look identical.
  function _hexToRgb(hex) {
    const m = String(hex || '').match(/^#?([0-9a-f]{6})$/i);
    if (!m) return null;
    const n = parseInt(m[1], 16);
    return { r: (n >> 16) & 0xff, g: (n >> 8) & 0xff, b: n & 0xff };
  }
  function _mixWithWhite(rgb, weight) {
    // weight = 1 -> fully white, 0 -> fully rgb. Lighter for low data values.
    return {
      r: Math.round(rgb.r + (255 - rgb.r) * weight),
      g: Math.round(rgb.g + (255 - rgb.g) * weight),
      b: Math.round(rgb.b + (255 - rgb.b) * weight),
    };
  }

  const _customDark = _hexToRgb(config && config.cell_gradient_color);
  const STOPS = _customDark
    ? [
        { t: 0,    ..._mixWithWhite(_customDark, 0.90) },
        { t: 0.25, ..._mixWithWhite(_customDark, 0.65) },
        { t: 0.5,  ..._mixWithWhite(_customDark, 0.40) },
        { t: 0.75, ..._mixWithWhite(_customDark, 0.20) },
        { t: 1,    r: _customDark.r, g: _customDark.g, b: _customDark.b },
      ]
    : [
        // Legacy Kibana-green palette — preserves the look of existing
        // shaded tables that don't have cell_gradient_color set.
        { t: 0,    r: 229, g: 245, b: 224 },  // #e5f5e0
        { t: 0.25, r: 161, g: 217, b: 155 },  // #a1d99b
        { t: 0.5,  r: 116, g: 196, b: 118 },  // #74c476
        { t: 0.75, r: 65,  g: 171, b: 93  },  // #41ab5d
        { t: 1,    r: 0,   g: 109, b: 44  },  // #006d2c
      ];

  function lerp(a, b, t) { return a + (b - a) * t; }

  function gradientColor(t) {
    // Clamp t to [0, 1]
    t = Math.max(0, Math.min(1, t));
    // Find the two surrounding stops
    let lo = STOPS[0], hi = STOPS[STOPS.length - 1];
    for (let i = 0; i < STOPS.length - 1; i++) {
      if (t >= STOPS[i].t && t <= STOPS[i + 1].t) {
        lo = STOPS[i];
        hi = STOPS[i + 1];
        break;
      }
    }
    const span = hi.t - lo.t || 1;
    const f = (t - lo.t) / span;
    return {
      r: Math.round(lerp(lo.r, hi.r, f)),
      g: Math.round(lerp(lo.g, hi.g, f)),
      b: Math.round(lerp(lo.b, hi.b, f)),
    };
  }

  const hiddenCols = new Set();
  // Number of leading label columns to skip (x_field + optional x_field_2)
  const numLabelCols = config && config.x_field_2 ? 2 : 1;
  // Build ordered list of visible column indices
  const visibleColIndices = data.columns
    .map((col, i) => i)
    .filter(i => !hiddenCols.has(data.columns[i]));

  // per-column shade opt-out. table_columns[i].shade === false
  // skips the heatmap on that column even when the table-level toggle is
  // on. Useful for ratio / cost columns where higher isn't always better.
  // The N-th metric column lines up with table_columns[N] (label columns
  // come first; metric columns follow in declaration order).
  const tableCols = (config && config.table_columns) || [];
  function _shadeAllowedForMetricIdx(metricIdx) {
    const col = tableCols[metricIdx];
    // Default to true when the field is undefined (legacy columns saved
    // before the per-column toggle existed) or when there's no
    // table_columns entry to consult.
    if (!col) return true;
    return col.shade !== false;
  }

  // For each visible column index (skip label columns — bucket/group fields),
  // compute min/max across all rows and apply inline background-color.
  const tbodyRows = renderArea.querySelectorAll('tbody tr');

  visibleColIndices.slice(numLabelCols).forEach((colIdx, metricIdx) => {
    // Per-column heatmap opt-out.
    if (!_shadeAllowedForMetricIdx(metricIdx)) return;

    // Gather numeric values for this column from the raw row data —
    // NOT from the rendered cell text. Rendered cells carry currency /
    // percent formatting (e.g. "$1,234.50", "1.84%") which Number()
    // returns NaN for, so the original rendered-text path skipped every
    // formatted column. Reading raw row[colIdx] keeps shading consistent
    // across all numeric column types.
    const values = [];
    data.rows.forEach(row => {
      const v = Number(row[colIdx]);
      if (!isNaN(v)) values.push(v);
    });
    if (values.length === 0) return;

    const minVal = Math.min(...values);
    const maxVal = Math.max(...values);
    const range = maxVal - minVal || 1;

    // Map colIdx to DOM column position (accounting for hidden cols before it)
    // visibleColIndices are sorted; find the position of colIdx within them
    const domPos = visibleColIndices.indexOf(colIdx);
    if (domPos < 0) return;

    tbodyRows.forEach((tr, rowIdx) => {
      const tds = tr.querySelectorAll('td');
      const td = tds[domPos];
      if (!td) return;
      // Read raw value from the source row (matches the min/max pass
      // above). Falls back to NaN for missing rows so the cell is left
      // un-shaded rather than mis-shaded by a stale parse.
      const row = data.rows[rowIdx];
      const v = row != null ? Number(row[colIdx]) : NaN;
      if (isNaN(v)) return;

      const t = (v - minVal) / range;
      const { r, g, b } = gradientColor(t);
      td.style.backgroundColor = `rgb(${r},${g},${b})`;
      // Use white text for darker cells (roughly t > 0.55)
      td.style.color = t > 0.55 ? '#ffffff' : '#2c3e50';
    });
  });
}

// ══════════════════════════════════════════════════════════════
// Metric / KPI
// ══════════════════════════════════════════════════════════════

// track which metric tiles have ever rendered with real
// data on the current page. On a first-look dashboard, the first
// empty render shows "Syncing…" (the backfill is in progress); any
// subsequent empty render on the same tile shows the normal "No
// data" treatment (data has been seen, then disappeared — likely a
// filter change, not a syncing state). Keyed by chartId; cleared on
// page reload.
const __FIRST_LOOK_RENDERED_WITH_DATA = new Set();

// Last-seen breakdown bucket keys per metric chart, keyed by chartId.
// Populated by renderMetric whenever a breakdown response lands; read by
// the builder's per-tile label editor so it can list the actual buckets
// the user can rename without issuing its own query. Cleared on reload.
const __METRIC_BREAKDOWN_KEYS = {};

function renderMetric(chartId, data) {
  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  // Hide the inline legend — metrics have a single value
  const leg = document.getElementById(`legend-${chartId}`);
  if (leg) { leg.style.display = 'none'; leg.innerHTML = ''; }

  if (data.breakdowns && data.breakdowns.length) {
    const config = (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE.charts[chartId]) || {};
    const maxCols = config.max_columns || 2;
    const maxRows = config.max_rows || 10;
    // Stash the raw bucket keys (full list, pre-slice) so the builder's
    // per-tile label editor can list every bucket the user might rename —
    // not just the ones currently visible under max_rows.
    __METRIC_BREAKDOWN_KEYS[chartId] = data.breakdowns
      .map(b => b.label)
      .filter(k => k != null && k !== '');
    const visible = data.breakdowns.slice(0, maxRows);
    const hidden  = data.breakdowns.slice(maxRows);
    const rows = [...visible];
    if (config.other_bucket && hidden.length > 0) {
      const otherValue = hidden.reduce((sum, b) => sum + (b.value || 0), 0);
      rows.push({ label: 'Other', value: otherValue });
    }
    // Per-tile label overrides (config.breakdown_labels = { rawKey: custom })
    // let the user rename a tile whose raw bucket value is unhelpful (e.g.
    // a bare number like "1.0"). Empty/missing override falls back to the
    // raw key with underscores spaced out, matching the legacy display.
    const tileLabels = config.breakdown_labels || {};
    const tiles = rows.map(b => {
      const override = (tileLabels[b.label] || '').trim();
      const label = override
        ? escHtml(override)
        : escHtml(String(b.label).replace(/_/g, ' '));
      return `
      <div class="metric-breakdown-tile">
        <div class="metric-breakdown-label">${label}</div>
        <div class="metric-breakdown-value">${formatNumber(b.value)}</div>
      </div>`;
    }).join('');
    const gridStyle = `grid-template-columns: repeat(${maxCols}, 1fr);`;
    renderArea.innerHTML = `<div class="metric-breakdown-grid" style="${gridStyle}">${tiles}</div>`;
    __FIRST_LOOK_RENDERED_WITH_DATA.add(chartId);
  } else {
    const hasValue = data.value !== null && data.value !== undefined;
    if (hasValue) {
      __FIRST_LOOK_RENDERED_WITH_DATA.add(chartId);
      // Resolve display label: config.y_field_labels (set in the
      // builder's Y Fields editor) overrides the raw field name the
      // back-end echoes back as data.label. Matches the bar/line path
      // in echarts_transformer.py which already reads y_field_labels.
      const cfg = (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE.charts[chartId]) || {};
      const labels = cfg.y_field_labels || {};
      const displayLabel = labels[data.label] || data.label || '';
      // a multi-grain dedup tile (e.g. "Active users" over 2+ GA4
      // properties / no property selected) shows the un-deduplicated daily sum.
      // Stamp the "approx upper bound" caption UNDER the value so the headline
      // number is honestly labeled rather than presented as exact. Single-grain
      // tiles carry no dedup_empty, so the caption is absent (real number).
      const de = data.dedup_empty;
      const upperBoundNote = (de && de.reason === 'multi_grain')
        ? `<div class="metric-dedup-note" style="color:#b8860b;font-size:11px;margin-top:4px;">${escHtml(de.message || _dedupEmptyCopy('multi_grain'))}</div>`
        : '';
      // honor the chart's value_format (e.g. "duration" for the GA4
      // engagement-time tile); defaults to the K/M abbreviation when unset.
      // Label renders ABOVE the value (GA4-report style); .metric-card flex
      // order + the .metric-label margin handle the spacing.
      renderArea.innerHTML = `<div class="metric-card">
           <div class="metric-label">${escHtml(displayLabel)}</div>
           <div class="metric-value">${formatMetricValue(data.value, cfg.value_format)}</div>
           ${upperBoundNote}
         </div>`;
    } else {
      // on a first-look dashboard, a metric tile that has
      // never rendered with real data renders "Syncing…" instead of
      // "No data" while the initial backfill is in flight. Once the
      // tile has shown real data, a subsequent empty response reverts
      // to "No data" (likely a filter change, not a syncing state).
      const isFirstLook = (typeof window !== 'undefined') && window.__FIRST_LOOK_DASHBOARD === true;
      const hasEverHadData = __FIRST_LOOK_RENDERED_WITH_DATA.has(chartId);
      const emptyText = (isFirstLook && !hasEverHadData) ? 'Syncing…' : 'No data';
      const emptyClass = (isFirstLook && !hasEverHadData) ? 'metric-no-data metric-syncing' : 'metric-no-data';
      renderArea.innerHTML = `<div class="metric-card">
           <div class="metric-value ${emptyClass}">${emptyText}</div>
         </div>`;
    }
  }
}

// ══════════════════════════════════════════════════════════════
// Profile card (Instagram account)
// ══════════════════════════════════════════════════════════════

// Allow only http(s) URLs into src/href attributes. Stored IG fields are
// user-influenced; escHtml stops attribute-breakout but not a
// ``javascript:``/``data:`` scheme, so gate the scheme here and drop
// anything else to '' (renders no image / no link rather than a vector).
function _safeUrl(url) {
  const s = String(url || '').trim();
  if (/^https?:\/\//i.test(s)) return s;
  return '';
}

async function renderProfileCard(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;

  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  renderArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#aaa;font-size:13px;">Loading…</div>';

  try {
    const res = await fetch(DASHBOARD_BASE + 'api/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_withExtraFilters(config)),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      showChartError(chartId, err.error || 'Query failed');
      return;
    }

    const data = await res.json();
    if (data.error) {
      showChartError(chartId, data.error);
      return;
    }

    const p = data.profile;
    if (!p) {
      renderArea.innerHTML = '<div class="profile-card profile-card--empty">Requires Instagram profile data. Make sure you import Instagram Insights before using.</div>';
      document.dispatchEvent(new CustomEvent('chart-rendered', { detail: { chartId, hasData: false } }));
      return;
    }

    const picUrl = _safeUrl(p.ig_user_profile_picture_url);
    const siteUrl = _safeUrl(p.ig_user_website);
    const name = p.ig_user_name || '';
    const username = p.ig_user_username || '';
    const bio = p.ig_user_biography || '';
    const followers = (p.ig_user_followers_count != null) ? formatNumber(p.ig_user_followers_count) : '—';
    const mediaCount = (p.ig_user_media_count != null) ? formatNumber(p.ig_user_media_count) : '—';

    // Every interpolation is escaped (escHtml) or scheme-gated (_safeUrl).
    // Instagram avatars are signed CDN URLs the browser won't hot-link, so
    // route them through the same-origin proxy. onerror hides a broken
    // <img> (expired URL) and reveals the placeholder behind it instead of
    // showing the alt-text / broken-image glyph.
    const proxiedPic = picUrl ? (DASHBOARD_BASE + 'api/profile-image?url=' + encodeURIComponent(picUrl)) : '';
    const avatar = proxiedPic
      ? `<img class="profile-card-avatar" src="${escHtml(proxiedPic)}" alt="${escHtml(name)} profile picture" loading="lazy" onerror="this.style.display='none'">`
      : '<div class="profile-card-avatar profile-card-avatar--placeholder" aria-hidden="true"></div>';

    const usernameRow = username
      ? `<div class="profile-card-row"><span class="profile-card-key">Username:</span> <span class="profile-card-val">@${escHtml(username)}</span></div>`
      : '';

    const bioRow = bio
      ? `<div class="profile-card-row profile-card-bio"><span class="profile-card-key">Bio:</span> <span class="profile-card-val">${escHtml(bio)}</span></div>`
      : '';

    const websiteRow = siteUrl
      ? `<div class="profile-card-row"><span class="profile-card-key">Website:</span> <a class="profile-card-val profile-card-link" href="${escHtml(siteUrl)}" target="_blank" rel="noopener noreferrer">${escHtml(p.ig_user_website)}</a></div>`
      : '';

    renderArea.innerHTML = `
      <div class="profile-card">
        <div class="profile-card-head">
          ${avatar}
          <div class="profile-card-name">${escHtml(name)}</div>
        </div>
        <div class="profile-card-body">
          ${usernameRow}
          ${bioRow}
          <div class="profile-card-row"><span class="profile-card-key">Followers:</span> <span class="profile-card-val">${escHtml(followers)}</span></div>
          <div class="profile-card-row"><span class="profile-card-key">Media count:</span> <span class="profile-card-val">${escHtml(mediaCount)}</span></div>
          ${websiteRow}
        </div>
      </div>
    `;

    document.dispatchEvent(new CustomEvent('chart-rendered', { detail: { chartId, hasData: true } }));
  } catch (e) {
    showChartError(chartId, e.message);
  }
}

// ══════════════════════════════════════════════════════════════
// Error state
// ══════════════════════════════════════════════════════════════

function showChartError(chartId, message) {
  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;
  renderArea.innerHTML = `
    <div class="chart-error">
      <span>⚠ ${escHtml(message)}</span>
      <button class="db-btn db-btn-sm" data-chart-id="${escHtml(chartId)}" onclick="queryAndRender(this.dataset.chartId)">Retry</button>
    </div>
  `;
}

// ══════════════════════════════════════════════════════════════
// Display Toggles (series / columns) — right config panel
// ══════════════════════════════════════════════════════════════

function populateDisplayToggles(chartId, option) {
  const config = DASHBOARD_STATE.charts[chartId];
  const container = document.getElementById('display-toggles-list');
  if (!container) return;

  const names = option.legend && option.legend.data ? option.legend.data : [];
  if (!names.length) {
    container.innerHTML = '<p class="db-hint">No series found.</p>';
    return;
  }

  container.innerHTML = '';
  names.forEach(name => {
    const isVisible = config.display_toggles[name] !== false;
    const item = document.createElement('label');
    item.className = 'db-toggle-item';
    item.innerHTML = `
      <input type="checkbox" ${isVisible ? 'checked' : ''}> ${escHtml(name)}
    `;
    item.querySelector('input').addEventListener('change', e => {
      const chart = CHART_INSTANCES[chartId];
      const visible = e.target.checked;
      if (chart) {
        chart.dispatchAction({ type: 'legendToggleSelect', name });
      }
      config.display_toggles[name] = visible;
    });
    container.appendChild(item);
  });
}

function populateTableToggles(chartId, columns, toggles) {
  const config = DASHBOARD_STATE.charts[chartId];
  const container = document.getElementById('display-toggles-list');
  if (!container) return;

  container.innerHTML = '';
  columns.forEach(col => {
    const isVisible = toggles[col] !== false;
    const item = document.createElement('label');
    item.className = 'db-toggle-item';
    item.innerHTML = `<input type="checkbox" ${isVisible ? 'checked' : ''}> ${escHtml(col)}`;
    item.querySelector('input').addEventListener('change', e => {
      config.display_toggles[col] = e.target.checked;
      // Re-render the table with updated column visibility
      const cachedData = _lastTableData[chartId];
      if (cachedData) renderTable(chartId, cachedData);
    });
    container.appendChild(item);
  });
}

// Cache last table data for toggle re-renders
const _lastTableData = {};
const _origRenderTable = renderTable;
// Wrap renderTable to cache its data
window.renderTable = function(chartId, data) {
  _lastTableData[chartId] = data;
  _origRenderTable(chartId, data);
};

// ══════════════════════════════════════════════════════════════
// Grouped Table
// ══════════════════════════════════════════════════════════════

async function renderGroupedTable(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;

  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  const groups = config.groups || [];
  const totalCols = groups.reduce((sum, g) => sum + (g.columns ? g.columns.length : 0), 0);

  // chart-level per-chart filters apply to every cell of a grouped
  // table. The per-cell metric queries previously hardcoded `filters: []`, so
  // a grouped table silently ignored its own filter. Pass the chart's real
  // filters through (field+op present). Placeholder (template-managed) filters
  // are dropped here: they only resolve to a concrete asset id at install
  // time, so querying them verbatim in the builder/viewer would match nothing.
  const _chartFilters = (config.filters || []).filter(
    f => f && f.field && f.op && !_isPlaceholderFilter(f),
  );

  if (totalCols === 0) {
    renderArea.innerHTML = '<p class="db-hint" style="padding:16px;">No columns configured. Select this chart and add groups &amp; columns in the right panel.</p>';
    return;
  }

  renderArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:80px;color:#aaa;font-size:13px;">Loading…</div>';

  // Helper to resolve relative date expressions to ISO timestamps
  const resolveDate = (expr, roundDown) => {
    if (!expr) return null;
    if (expr.startsWith('now')) {
      let d = new Date();
      const match = expr.match(/^now(?:([+-])(\d+)([smhdwMy]))?(?:\/([smhdwMy]))?$/);
      if (match) {
        const [, sign, amount, unit, roundUnit] = match;
        if (sign && amount && unit) {
          const delta = parseInt(amount, 10) * (sign === '-' ? -1 : 1);
          switch (unit) {
            case 's': d.setSeconds(d.getSeconds() + delta); break;
            case 'm': d.setMinutes(d.getMinutes() + delta); break;
            case 'h': d.setHours(d.getHours() + delta); break;
            case 'd': d.setDate(d.getDate() + delta); break;
            case 'w': d.setDate(d.getDate() + delta * 7); break;
            case 'M': d.setMonth(d.getMonth() + delta); break;
            case 'y': d.setFullYear(d.getFullYear() + delta); break;
          }
        }
        if (roundUnit === 'd') {
          if (roundDown) d.setHours(0, 0, 0, 0);
          else d.setHours(23, 59, 59, 999);
        }
      }
      return d.toISOString();
    }
    return expr;
  };

  // Normalise column to data_sources array (backwards compat with old single data_view/field)
  const getDataSources = (col) => {
    if (col.data_sources && col.data_sources.length > 0) {
      return col.data_sources;
    }
    // Legacy single data_view/field format
    if (col.data_view && col.field) {
      return [{ data_view: col.data_view, field: col.field }];
    }
    return [];
  };

  // Build list of all queries needed (multiple per cell if multi-data-source)
  const queryTasks = [];  // { gi, ci, dsIdx, queryConfig }
  groups.forEach((group, gi) => {
    (group.columns || []).forEach((col, ci) => {
      const dataSources = getDataSources(col);
      if (dataSources.length === 0) {
        // No data sources - will show null
        queryTasks.push({ gi, ci, dsIdx: 0, hasData: false, queryConfig: null });
      } else {
        dataSources.forEach((ds, dsIdx) => {
          // Build date_range for 'query date' aggregation
          let cellDateRange = null;
          if (col.aggregation === 'query date') {
            const viewerTimeRange = (typeof window !== 'undefined' && window.DASHBOARD_TIME_RANGE) || {};
            const gdr = DASHBOARD_STATE.global_date_range || {};
            const timeFrom = viewerTimeRange.from || gdr.from || null;
            const timeTo = viewerTimeRange.to || gdr.to || null;
            if (timeFrom || timeTo) {
              const isDT_col = _isDtDataView(ds.data_view);
              if (isDT_col) {
                // Prefer dt_date_field if set, otherwise default to date_created_ms.
                const dtField = col.dt_date_field || 'date_created_ms';
                if (dtField.endsWith('_ms')) {
                  // _ms fields are ES date type — pass date math directly.
                  cellDateRange = { field: dtField, from: timeFrom, to: timeTo };
                } else if (col.dt_date_unix) {
                  // Legacy: field stores Unix epoch seconds — convert date math to epoch strings.
                  const toEpoch = (expr, upper) => {
                    if (!expr) return null;
                    const d = new Date();
                    const m2 = expr.match(/^now-(\d+)([dMy])(?:\/[dMy])?$/i);
                    if (m2) {
                      const n = parseInt(m2[1], 10), u = m2[2].toLowerCase();
                      if (u === 'd') d.setDate(d.getDate() - n);
                      else if (u === 'm') d.setMonth(d.getMonth() - n);
                      else if (u === 'y') d.setFullYear(d.getFullYear() - n);
                    } else if (/^now$/i.test(expr) && upper) {
                      d.setDate(d.getDate() + 1);
                    }
                    return String(Math.floor(d.getTime() / 1000));
                  };
                  cellDateRange = { field: dtField, from: toEpoch(timeFrom, false), to: toEpoch(timeTo, true) };
                } else {
                  // Legacy: text/keyword field — resolve date math to plain strings.
                  const kwField = dtField.endsWith('.keyword') ? dtField : dtField + '.keyword';
                  cellDateRange = { field: kwField, from: _resolveDateMath(timeFrom), to: _resolveDateMath(timeTo, true) };
                }
              } else {
                cellDateRange = {
                  field: ds.field,
                  from: resolveDate(timeFrom, true),
                  to: resolveDate(timeTo, false),
                };
              }
            }
          }

          queryTasks.push({
            gi, ci, dsIdx,
            hasData: !!(ds.data_view && ds.field),
            queryConfig: {
              chart_id: `${chartId}_g${gi}_c${ci}_ds${dsIdx}`,
              chart_type: 'metric',
              data_view: ds.data_view || '',
              x_field: null,
              x_field_type: null,
              y_fields: ds.field ? [ds.field] : [],
              aggregation: col.aggregation || 'sum',
              equals_value: col.equals_value || null,
              date_range: cellDateRange,
              dt_date_field: col.dt_date_field || null,
              // chart-level time_range_field (e.g. closed_at) applies
              // to every column. When a column has no own date_range (sum /
              // equals / count / cardinality), _withExtraFilters scopes the
              // global time range on this field instead of kibana_date, so an
              // Echo outcomes table counts conversations resolved in the window.
              time_range_field: config.time_range_field || null,
              breakdown: null,
              date_format: '',
              y_min: '',
              y_max: '',
              // apply the chart's per-chart filters to each cell.
              filters: _chartFilters,
              display_toggles: {},
            },
          });
        });
      }
    });
  });

  // Fire all queries in parallel
  const fetchPromises = queryTasks.map(task => {
    if (!task.hasData) return Promise.resolve({ chart_type: 'metric', value: null });
    return fetch(DASHBOARD_BASE + 'api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_withExtraFilters(task.queryConfig)),
    })
      .then(r => r.json())
      .catch(() => ({ error: true, value: null }));
  });

  const results = await Promise.all(fetchPromises);

  // Aggregate results by cell (gi_ci) - sum values from multiple data sources
  const valueMap = {};
  queryTasks.forEach((task, idx) => {
    const key = `${task.gi}_${task.ci}`;
    const resultValue = results[idx] && results[idx].value !== undefined ? results[idx].value : null;

    if (valueMap[key] === undefined) {
      valueMap[key] = resultValue;
    } else if (resultValue !== null) {
      // Sum the values from multiple data sources
      if (valueMap[key] === null) {
        valueMap[key] = resultValue;
      } else {
        valueMap[key] += resultValue;
      }
    }
  });

  // Colours + text styles
  const titleBg        = escHtml(config.title_bg_color           || '#2c6da0');
  const groupBg        = escHtml(config.group_header_bg_color     || '#5a9cc5');
  const colBg          = escHtml(config.col_header_bg_color       || '#2c6da0');
  const textColor      = escHtml(config.header_text_color         || '#ffffff');
  const groupTextColor = escHtml(config.group_text_color          || textColor);
  const colTextColor   = escHtml(config.col_text_color            || textColor);
  const groupFontSize  = config.group_font_size ? `font-size:${parseInt(config.group_font_size)}px;` : '';
  const colFontSize    = config.col_font_size   ? `font-size:${parseInt(config.col_font_size)}px;`   : '';
  const groupFontWeight = config.group_bold === false ? 'font-weight:normal;' : '';
  const colFontWeight   = config.col_bold   === false ? 'font-weight:normal;' : '';

  let html = '<table class="gt-table"><thead>';

  // Row 1: table title (full-width span)
  if (config.table_title) {
    html += `<tr><th colspan="${totalCols}" class="gt-title-cell" style="background:${titleBg};color:${textColor};">${escHtml(config.table_title)}</th></tr>`;
  }

  // Row 2: group headers (each spans its columns)
  html += '<tr>';
  groups.forEach((group, gi) => {
    const span = (group.columns || []).length;
    if (span > 0) {
      const sep = gi > 0 ? ' gt-group-sep' : '';
      html += `<th colspan="${span}" class="gt-group-cell${sep}" style="background:${groupBg};color:${groupTextColor};${groupFontSize}${groupFontWeight}">${escHtml(group.label || '')}</th>`;
    }
  });
  html += '</tr>';

  // Row 3: column headers
  html += '<tr>';
  groups.forEach((group, gi) => {
    (group.columns || []).forEach((col, ci) => {
      const sep = gi > 0 && ci === 0 ? ' gt-group-sep' : '';
      html += `<th class="gt-col-cell${sep}" style="background:${colBg};color:${colTextColor};${colFontSize}${colFontWeight}">${escHtml(col.label || '')}</th>`;
    });
  });
  html += '</tr>';

  html += '</thead><tbody><tr>';

  // Row 4: data values
  groups.forEach((group, gi) => {
    (group.columns || []).forEach((col, ci) => {
      const sep = gi > 0 && ci === 0 ? ' gt-group-sep' : '';
      const val = valueMap[`${gi}_${ci}`];
      html += `<td class="gt-data-cell${sep}">${formatNumber(val)}</td>`;
    });
  });

  html += '</tr></tbody></table>';
  renderArea.innerHTML = html;
}

// ══════════════════════════════════════════════════════════════
// Leaflet Geographic Map
// ══════════════════════════════════════════════════════════════

// Registry of active Leaflet map instances keyed by chart_id
const LEAFLET_INSTANCES = {};

const _MAP_STAT_LABELS = {
  app_installs:            'App installs',
  baptisms:                'Baptisms',
  dt_contacts:             'New Contacts',
  echo_multiplier:         'Echo multiplier',
  first_meetings:          '1st Meetings',
  google_ads_app_installs: 'Installs',
  messages:                'Messages Received',
  messaging_contacts:      'Messaging Contacts',
  messages_sent:           'Messages Sent',
  multiplier_contacts:     'Multiplier contacts',
  new_churches:            'New Churches',
  new_groups:              'New Groups',
  page_visits:             'Page visits',
  reach:                   'Meta',
  salvation:               'Salvation decisions',
  spiritual_conversations: 'Spiritual Conversations',
  spiritual_convos_echo:  'Spiritual Conversations',
  video_views:             'Video views',
  // Organic reach
  organic_facebook:        'Organic Facebook',
  organic_instagram:       'Organic Instagram',
  organic_web:             'Organic Web',
  // Video engagement
  videos_15s:              '15s Video Views',
  view_time:               'Video View Time',
  // Follows / subscriptions
  follows_facebook:        'Facebook Follows',
  follows_instagram:       'Instagram Follows',
  follows_youtube:         'YouTube Subscribers',
  // Reactions / shares
  fb_reactions:            'FB Reactions',
  fb_shares:               'FB Shares',
  ig_reactions:            'IG Reactions',
  ig_shares:               'IG Shares',
  yt_reactions:            'YT Likes',
  yt_shares:               'YT Shares',
  comments:                'Comments (ads)',
};

// Country name aliases: maps alternate/modern names → Natural Earth GeoJSON name.
// Used as fallback when ISO3 code is unavailable.
const COUNTRY_NAME_ALIASES = {
  'turkiye': 'turkey',
  'türkiye': 'turkey',
  'eswatini': 'swaziland',
  'north macedonia': 'macedonia',
  'cabo verde': 'cape verde',
  'timor-leste': 'east timor',
  'myanmar': 'burma',
  'czechia': 'czech republic',
  'democratic republic of the congo': 'dem. rep. congo',
  'republic of the congo': 'congo',
  "côte d'ivoire": 'ivory coast',
  "cote d'ivoire": 'ivory coast',
  'south korea': 'korea',
  'north korea': 'north korea',
  // Natural Earth name mismatches for common team countries
  'united states': 'united states of america',
  'russia': 'russian federation',
};

// Normalise an admin-region name so FB's variant matches admin1.geojson's.
// Handles three real-world divergences observed in the data:
//   - trailing suffix:  "Cairo Governorate"   ↔ "Cairo"
//   - leading prefix:   "Emirate of Fujairah" ↔ "Fujairah"
//   - punctuation/case: "Ras al-Khaimah"      ↔ "Ras Al Khaimah"
//
// KEEP IN SYNC with `_REGION_SUFFIX_TOKENS` / `_REGION_PREFIX_TOKENS` in
// bin/python/lib/geocoding.py — the Python side strips the same noise from
// FB region names before sending to Nominatim during the import / prewarm.
// Diverging lists produce inconsistent matches between the import path and
// the render path. Multi-word suffixes are written before single-word ones
// so the alternation prefers the longer match (autonomous region > region).
function _stripAdminSuffix(name) {
  if (!name) return '';
  return name
    .toLowerCase()
    .replace(/^(emirate|state|province|republic|district|governorate|region|territory|prefecture|kingdom)\s+of\s+/i, '')
    .replace(/\s+(autonomous\s+region|special\s+region|metropolitan\s+city|governorate|province|state|region|prefecture|county|district|department|division|territory|oblast|emirate|republic|voivodeship|municipality)s?$/i, '')
    .replace(/[-_]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// Cached world GeoJSON for choropleth (loaded once, reused on statistic changes)
let _worldGeoJSON = null;
let _regionGeoJSON = null;

async function _loadWorldGeoJSON() {
  if (_worldGeoJSON) return _worldGeoJSON;
  try {
    // Natural Earth 50m — includes Taiwan and other smaller territories; has iso_a2 property
    const resp = await fetch('https://d2ad6b4ur7yvpq.cloudfront.net/naturalearth-3.3.0/ne_50m_admin_0_countries.geojson');
    _worldGeoJSON = await resp.json();
  } catch (e) {
    _worldGeoJSON = null;
  }
  return _worldGeoJSON;
}

async function _loadRegionGeoJSON() {
  if (_regionGeoJSON) return _regionGeoJSON;
  try {
    // Filtered admin-1 boundaries for countries with campaign data (India, Egypt, Turkey, US, etc.)
    const resp = await fetch(DASHBOARD_BASE + 'static/dashboard_builder/admin1.geojson?v=20260425a');
    _regionGeoJSON = await resp.json();
  } catch (e) {
    _regionGeoJSON = null;
  }
  return _regionGeoJSON;
}

/**
 * Fetch map data and render Leaflet map for the given chart.
 * Can be called with an explicit chartId, or falls back to the current
 * selected chart (used by the statistic dropdown onchange handler).
 */
async function requeryLeafletMap(chartId) {
  chartId = chartId || (typeof SELECTED_CHART_ID !== 'undefined' ? SELECTED_CHART_ID : null);
  if (!chartId) return;

  const config = DASHBOARD_STATE.charts[chartId];
  if (!config || config.chart_type !== 'leaflet_map') return;

  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  const statistic = config.map_statistic || 'reach';
  const statLabel = _MAP_STAT_LABELS[statistic] || statistic;
  const params = new URLSearchParams({ statistic });

  // Pass org/team filters from DASHBOARD_EXTRA_FILTERS (set by view.html from URL params)
  const extra = (typeof window !== 'undefined' && window.DASHBOARD_EXTRA_FILTERS) || [];
  for (const f of extra) {
    if (f.field === 'organization_title' && f.value) params.set('org', f.value);
    if (f.field === 'team_title') {
      if (f.op === 'match_phrase' && f.value) {
        params.append('team', f.value);
      } else if (f.op === 'in' && Array.isArray(f.value)) {
        f.value.forEach(t => params.append('team', t));
      }
    }
  }

  // Pass time range from DASHBOARD_TIME_RANGE (set by view.html from URL params)
  const timeRange = (typeof window !== 'undefined' && window.DASHBOARD_TIME_RANGE) || {};
  if (timeRange.from) params.set('date_from', timeRange.from);
  if (timeRange.to)   params.set('date_to',   timeRange.to);

  renderArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#aaa;font-size:13px;">Loading map…</div>';

  // Two-phase render:
  //   1. Country + region polygons paint immediately from a pure-ES query.
  //   2. City markers load via a separate cache-only endpoint and are
  //      added to the existing Leaflet instance once they arrive.
  //
  // Phase 2 failure is silent — the country view stays. Both phases use
  // fetchJsonWithTimeout so an nginx 504 HTML page never reaches the
  // user as a JSON parse error.
  const baseQs = params.toString();
  // follow-up: the public viewer's map endpoints
  // (/public/api/map-data and /public/api/map-data/cities) require the
  // publish token in the query string to look up the dashboard's scope.
  // The auth'd builder URLs ignore an extra ``token`` param so appending
  // it unconditionally is safe.
  const _publicToken = (typeof window !== 'undefined' && window.PUBLIC_REPORT_TOKEN) || '';
  const _qsWithToken = _publicToken
    ? baseQs + '&token=' + encodeURIComponent(_publicToken)
    : baseQs;
  let mapData;
  try {
    mapData = await fetchJsonWithTimeout(DASHBOARD_BASE + 'api/map-data?' + _qsWithToken);
  } catch (e) {
    renderArea.innerHTML = `<div style="padding:16px;color:#c00;">Map load failed: ${escHtml(e.message)}</div>`;
    return;
  }
  if (!mapData.success) {
    renderArea.innerHTML = `<div style="padding:16px;color:#c00;">Map error: ${escHtml(mapData.error || 'unknown')}</div>`;
    return;
  }

  // a published link may carry a map-detail choice. The server
  // already withheld the geography; this only decides what to paint.
  //   hidden  → a notice instead of a map (no tiles, no fetches).
  //   country → paint the choropleth, skip the city fetch entirely.
  //   city / absent (authenticated viewer, pre-1513 links) → unchanged.
  const mapDetail = (typeof mapData.map_detail === 'string') ? mapData.map_detail : 'city';
  if (mapDetail === 'hidden') {
    if (LEAFLET_INSTANCES[chartId]) {
      try { LEAFLET_INSTANCES[chartId].remove(); } catch (e) {}
      delete LEAFLET_INSTANCES[chartId];
    }
    // Same inline shape as the "Loading map…" placeholder above.
    renderArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#6b7280;font-size:13px;text-align:center;padding:16px;">The publisher has hidden the map on this shared report.</div>';
    return;
  }

  const cities    = mapData.cities    || [];
  const regions   = mapData.regions   || [];
  const countries = mapData.countries || [];
  renderLeafletMap(chartId, cities, regions, countries, statLabel);

  if (mapDetail !== 'city') return;

  // Phase 2: cache-only city markers. Failure here is silent.
  try {
    const cityData = await fetchJsonWithTimeout(DASHBOARD_BASE + 'api/map-data/cities?' + _qsWithToken);
    if (!cityData.success) return;
    if (cityData.cities && cityData.cities.length) {
      _addCityLayerToMap(chartId, cityData.cities, statLabel);
    }
    if (cityData.total_cities > 0 && cityData.resolved_cities < cityData.total_cities) {
      _showCityCoverageIndicator(chartId, cityData.resolved_cities, cityData.total_cities);
    }
  } catch (_) {
    // City fetch failed (timeout, 504, network). Country render stays.
  }
}

// Add city circle markers to an already-rendered Leaflet map. Used by
// the second phase of the progressive map render. Mutates the
// existing instance — does not re-fit bounds.
function _addCityLayerToMap(chartId, cities, statLabel) {
  const map = (typeof LEAFLET_INSTANCES !== 'undefined') ? LEAFLET_INSTANCES[chartId] : null;
  if (!map || !cities || !cities.length) return;
  const label = statLabel || 'Value';
  const cityVals = cities.map(p => p.value || 0);
  const maxCityVal = Math.max(...cityVals) || 1;
  cities.forEach(point => {
    if (point.lat == null || point.lon == null) return;
    const radius = 6 + (point.value / maxCityVal) * 16;
    const marker = L.circleMarker([point.lat, point.lon], {
      radius,
      fillColor: '#0972D3',
      color: '#FFFFFF',
      weight: 2,
      fillOpacity: 0.8,
      className: 'map-marker-shadow',
    }).addTo(map);
    marker.bindTooltip(
      `<div class="map-stat-box">` +
        `<div class="map-stat-name">City: ${escHtml(point.name)}</div>` +
        `<div class="map-stat-value">${escHtml(label)}: <strong>${formatNumber(point.value)}</strong></div>` +
      `</div>`,
      { sticky: true, direction: 'top', offset: [0, -radius], className: 'map-stat-tooltip' }
    );
  });
}

// "27 of 40 cities shown" hint — surfaces partial coverage without an
// error UI. Replaces any prior indicator on the same chart so re-queries
// don't stack pills.
function _showCityCoverageIndicator(chartId, resolved, total) {
  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;
  const existing = renderArea.querySelector('.map-city-coverage');
  if (existing) existing.remove();
  const el = document.createElement('div');
  el.className = 'map-city-coverage';
  el.style.cssText = 'position:absolute;bottom:8px;left:8px;background:rgba(255,255,255,0.92);padding:4px 10px;border-radius:4px;font-size:11px;color:#5F6B7A;z-index:600;box-shadow:0 1px 3px rgba(0,0,0,0.08);pointer-events:none;';
  el.textContent = `${resolved} of ${total} cities shown`;
  renderArea.appendChild(el);
}

async function renderLeafletMap(chartId, cities, regions, countries, statLabel) {
  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  const showLabels = (DASHBOARD_STATE.charts[chartId]?.show_labels !== false);

  // Dispose previous Leaflet instance if exists
  if (LEAFLET_INSTANCES[chartId]) {
    try { LEAFLET_INSTANCES[chartId].remove(); } catch (e) {}
    delete LEAFLET_INSTANCES[chartId];
  }

  renderArea.innerHTML = '';
  renderArea.style.position = 'relative';

  const map = L.map(renderArea, {
    zoomControl: true,
    minZoom: 2,
    maxBounds: [[-90, -180], [90, 180]],
    maxBoundsViscosity: 1.0,
    attributionControl: false,
  }).setView([20, 0], 2);
  LEAFLET_INSTANCES[chartId] = map;

  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
    attribution: '© Esri',
    maxZoom: 18,
    // Pre-load extra tile rings around the viewport so fast pans don't
    // expose the container backdrop before the new tiles arrive.
    keepBuffer: 8,
    updateWhenIdle: false,
    updateInterval: 100,
  }).addTo(map);

  const label = statLabel || 'Value';
  const allPoints = [];

  // ── Country choropleth layer ──────────────────────────────────
  if (countries.length) {
    // Build lookup maps: by ISO3, ISO2, and lowercase country name.
    // ISO3 is most reliable (avoids name mismatches like 'Turkiye' vs 'Turkey').
    // COUNTRY_NAME_ALIASES handles remaining name differences as a final fallback.
    const countryByIso3 = {};
    const countryByIso2 = {};
    const countryByName = {};
    countries.forEach(c => {
      const val = c.value || 0;
      if (c.iso3) countryByIso3[c.iso3.toUpperCase()] = val;
      if (c.iso2) countryByIso2[c.iso2.toUpperCase()] = val;
      if (c.name) {
        const lowerName = c.name.toLowerCase();
        countryByName[lowerName] = val;
        const alias = COUNTRY_NAME_ALIASES[lowerName];
        if (alias) countryByName[alias] = val;
      }
    });
    const allVals = Object.values(countryByIso3)
      .concat(Object.values(countryByIso2))
      .concat(Object.values(countryByName));
    const maxCountryVal = Math.max(...allVals) || 1;

    const _resolveCountryVal = (feature) => {
      const iso3 = (feature.properties.iso_a3 || '').toUpperCase();
      const iso2 = (feature.properties.iso_a2 || '').toUpperCase();
      const name = (feature.properties.name || '').toLowerCase();
      return countryByIso3[iso3] ?? countryByIso2[iso2] ?? countryByName[name] ?? null;
    };

    const geoJSON = await _loadWorldGeoJSON();
    if (geoJSON) {
      L.geoJSON(geoJSON, {
        style(feature) {
          const val = _resolveCountryVal(feature);
          if (!val) return { fillColor: 'transparent', fillOpacity: 0, color: 'rgba(0,0,0,0.06)', weight: 0.5 };
          // Square-root scale so countries with small values still get a
          // visible tint when one outlier (e.g. Egypt) dominates the max.
          const t = Math.sqrt(Math.min(val / maxCountryVal, 1));
          const fr = Math.round(220 - (220 - 9)   * t);
          const fg = Math.round(237 - (237 - 90)  * t);
          const fb = Math.round(250 - (250 - 195)  * t);
          return {
            fillColor: `rgb(${fr},${fg},${fb})`,
            fillOpacity: 0.6,
            color: 'rgba(9,114,211,0.4)',
            weight: 1,
          };
        },
        onEachFeature(feature, layer) {
          const val = _resolveCountryVal(feature);
          if (!val) return;
          const countryName = feature.properties.name || feature.properties.iso_a2 || '';
          try {
            const geom = feature.geometry;
            const rings = geom.type === 'Polygon'
              ? [geom.coordinates[0]]
              : geom.coordinates.map(p => p[0]).sort((a, b) => b.length - a.length).slice(0, 1);
            // Shoelace formula: area-weighted centroid — true geographic center,
            // unaffected by vertex density or bounding box irregularities.
            const ring = rings[0];
            let area = 0, latC = 0, lonC = 0;
            for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
              const cross = ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
              area += cross;
              lonC  += (ring[j][0] + ring[i][0]) * cross;
              latC  += (ring[j][1] + ring[i][1]) * cross;
            }
            area /= 2;
            lonC /= (6 * area);
            latC /= (6 * area);
            const center = L.latLng(latC, lonC);
            if (showLabels) {
              L.tooltip({ permanent: true, direction: 'center', className: 'map-stat-tooltip' })
                .setContent(
                  `<div class="map-stat-box">` +
                    `<div class="map-stat-name">Country: ${escHtml(countryName)}</div>` +
                    `<div class="map-stat-value">${escHtml(label)}: <strong>${formatNumber(val)}</strong></div>` +
                  `</div>`
                )
                .setLatLng(center)
                .addTo(map);
            }
            allPoints.push(center);
          } catch (e) {
            try { allPoints.push(layer.getBounds().getCenter()); } catch (_) {}
          }
          // When permanent labels are on, skip the hover tooltip — it duplicates the label.
          if (!showLabels) {
            layer.bindTooltip(
              `<div class="map-stat-box">` +
                `<div class="map-stat-name">Country: ${escHtml(countryName)}</div>` +
                `<div class="map-stat-value">${escHtml(label)}: <strong>${formatNumber(val)}</strong></div>` +
              `</div>`,
              { sticky: true, className: 'map-stat-tooltip' }
            );
          }
        },
      }).addTo(map);
    }
  }

  // ── Region polygon choropleth (amber/orange gradient) ────────
  if (regions.length) {
    // Index each FB region by its raw lowercase name AND by a suffix-stripped
    // form so "Cairo Governorate" (FB) matches admin1.geojson's name_en "Cairo".
    const regionByName = {};
    const keyToRawName = {};   // lookup key -> FB raw lowercase name (for match set below)
    regions.forEach(r => {
      if (!r.name) return;
      const raw = r.name.toLowerCase();
      regionByName[raw] = r.value || 0;
      keyToRawName[raw] = raw;
      const norm = _stripAdminSuffix(r.name);
      if (norm && norm !== raw) {
        regionByName[norm] = r.value || 0;
        keyToRawName[norm] = raw;
      }
    });
    const maxRegionVal = Math.max(...Object.values(regionByName)) || 1;

    const _resolveRegionVal = (feature) => {
      const keys = [
        (feature.properties.name || '').toLowerCase(),
        (feature.properties.name_en || '').toLowerCase(),
        _stripAdminSuffix(feature.properties.name || ''),
        _stripAdminSuffix(feature.properties.name_en || ''),
      ];
      for (const k of keys) {
        if (regionByName[k] != null) return regionByName[k];
      }
      return null;
    };

    const regionGeoJSON = await _loadRegionGeoJSON();
    if (regionGeoJSON) {
      L.geoJSON(regionGeoJSON, {
        // Only keep admin1 polygons that have data. Otherwise every invisible
        // no-data admin1 still catches mouse events, blocking country hover
        // everywhere inside a country that has any region data at all.
        filter: (feature) => _resolveRegionVal(feature) !== null,
        style(feature) {
          const val = _resolveRegionVal(feature);
          // Square-root scale: keeps small-value regions visible even when a
          // single high-value region (e.g. an Egyptian governorate at ~3M)
          // dominates the max.
          const t = Math.sqrt(Math.min(val / maxRegionVal, 1));
          // Darker amber start (240,200,130) so small values still read warm
          // when stacked on top of the blue country polygon underneath.
          const fr = Math.round(240 - (240 - 180) * t);
          const fg = Math.round(200 - (200 - 75)  * t);
          const fb = Math.round(130 - (130 - 15)   * t);
          return {
            fillColor: `rgb(${fr},${fg},${fb})`,
            fillOpacity: 0.8,
            color: 'rgba(141,102,5,0.45)',
            weight: 1,
          };
        },
        onEachFeature(feature, layer) {
          const val = _resolveRegionVal(feature);
          // Prefer the English transliteration so display stays consistent
          // with what the dot (below) shows for the same region.
          const regionName = feature.properties.name_en || feature.properties.name || '';
          layer.bindTooltip(
            `<div class="map-stat-box">` +
              `<div class="map-stat-name">Region: ${escHtml(regionName)}</div>` +
              `<div class="map-stat-value">${escHtml(label)}: <strong>${formatNumber(val)}</strong></div>` +
            `</div>`,
            { sticky: true, className: 'map-stat-tooltip' }
          );
          try {
            const bounds = layer.getBounds();
            allPoints.push(bounds.getCenter());
          } catch (e) {}
        },
      }).addTo(map);
    }

    // Walk matched admin1 features once to build two lookups keyed by the FB
    // raw name: one to know which FB regions have a polygon (so we render a
    // subtle dot on top instead of a big circle), and one to get the English
    // display name so the dot's tooltip matches the polygon's tooltip.
    const matchedFbNames = new Set();
    const fbNameToDisplayName = {};
    if (regionGeoJSON) {
      regionGeoJSON.features.forEach(f => {
        if (_resolveRegionVal(f) === null) return;
        const keys = [
          (f.properties.name || '').toLowerCase(),
          (f.properties.name_en || '').toLowerCase(),
          _stripAdminSuffix(f.properties.name || ''),
          _stripAdminSuffix(f.properties.name_en || ''),
        ];
        const display = f.properties.name_en || f.properties.name || '';
        for (const k of keys) {
          const raw = keyToRawName[k];
          if (raw) {
            matchedFbNames.add(raw);
            if (display && !fbNameToDisplayName[raw]) fbNameToDisplayName[raw] = display;
          }
        }
      });
    }
    regions.forEach(r => {
      if (!r.name || r.lat == null || r.lon == null) return;
      const rawKey = r.name.toLowerCase();
      // Scale every region dot by value, same treatment as city dots.
      const radius = 6 + (r.value / maxRegionVal) * 14;
      const displayName = fbNameToDisplayName[rawKey] || r.name;
      const marker = L.circleMarker([r.lat, r.lon], {
        radius,
        fillColor: '#EC7211',
        color: '#FFFFFF',
        weight: 2,
        fillOpacity: 0.9,
        className: 'map-marker-shadow',
      }).addTo(map);
      marker.bindTooltip(
        `<div class="map-stat-box">` +
          `<div class="map-stat-name">Region: ${escHtml(displayName)}</div>` +
          `<div class="map-stat-value">${escHtml(label)}: <strong>${formatNumber(r.value)}</strong></div>` +
        `</div>`,
        { sticky: true, direction: 'top', offset: [0, -radius], className: 'map-stat-tooltip' }
      );
      allPoints.push([r.lat, r.lon]);
    });
  }

  // ── City circle markers (blue) ────────────────────────────────
  if (cities.length) {
    const cityVals = cities.map(p => p.value || 0);
    const maxCityVal = Math.max(...cityVals) || 1;
    cities.forEach(point => {
      if (point.lat == null || point.lon == null) return;
      const radius = 6 + (point.value / maxCityVal) * 16;
      const marker = L.circleMarker([point.lat, point.lon], {
        radius,
        fillColor: '#0972D3',
        color: '#FFFFFF',
        weight: 2,
        fillOpacity: 0.8,
        className: 'map-marker-shadow',
      }).addTo(map);
      marker.bindTooltip(
        `<div class="map-stat-box">` +
          `<div class="map-stat-name">City: ${escHtml(point.name)}</div>` +
          `<div class="map-stat-value">${escHtml(label)}: <strong>${formatNumber(point.value)}</strong></div>` +
        `</div>`,
        { sticky: true, direction: 'top', offset: [0, -radius], className: 'map-stat-tooltip' }
      );
      allPoints.push([point.lat, point.lon]);
    });
  }

  // Recalculate size now that all layers are rendered, then fit bounds.
  // Use ResizeObserver so we fire only when the container actually has real dimensions.
  const _fitMap = () => {
    // Save scroll position — fitBounds can cause the parent page to jump
    const scrollY = window.scrollY || window.pageYOffset;
    map.invalidateSize();
    if (allPoints.length) {
      map.fitBounds(allPoints, { padding: [40, 40], maxZoom: 4, animate: false });
    }
    // Restore scroll position after a tick
    requestAnimationFrame(() => { window.scrollTo(0, scrollY); });
  };

  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect.width > 0 && entry.contentRect.height > 0) {
          ro.disconnect();
          _fitMap();
          return;
        }
      }
    });
    ro.observe(renderArea);
    // Also call immediately in case the container already has dimensions
    _fitMap();
  } else {
    // Fallback for browsers without ResizeObserver
    _fitMap();
    setTimeout(_fitMap, 300);
  }
}

// ══════════════════════════════════════════════════════════════
// Trend Line — multi-source date histogram chart
// ══════════════════════════════════════════════════════════════

async function queryAndRenderTrendLine(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;

  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  renderArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#aaa;font-size:13px;">Loading…</div>';

  // Build request — apply org/team extra filters and global time range
  const extra     = (typeof window !== 'undefined' && window.DASHBOARD_EXTRA_FILTERS) || [];
  const timeRange = (typeof window !== 'undefined' && window.DASHBOARD_TIME_RANGE) || {};
  // dashboard-level filters: builder reads from DASHBOARD_STATE;
  // viewer will set window.DASHBOARD_FILTERS (Phase 21). The trends route
  // applies them per-source by matching data_view (Phase 19 wiring).
  const dashboardFilters =
    (typeof DASHBOARD_STATE !== 'undefined' && Array.isArray(DASHBOARD_STATE.filters) && DASHBOARD_STATE.filters) ||
    (typeof window !== 'undefined' && Array.isArray(window.DASHBOARD_FILTERS) && window.DASHBOARD_FILTERS) ||
    [];

  const reqBody = {
    sources:       config.sources || [],
    date_interval: config.date_interval || 'month',
    filters:       extra,
    dashboard_filters: dashboardFilters,
  };
  if (timeRange.from || timeRange.to) {
    reqBody.date_range = { field: 'kibana_date', from: timeRange.from || null, to: timeRange.to || null };
  }

  try {
    const res = await fetch(DASHBOARD_BASE + 'api/trend-line-data', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(reqBody),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      showChartError(chartId, err.error || 'Trend line query failed');
      return;
    }

    const data = await res.json();
    if (data.error) { showChartError(chartId, data.error); return; }

    renderTrendLine(chartId, data, config);
  } catch (e) {
    showChartError(chartId, e.message);
  }
}

function renderTrendLine(chartId, data, config) {
  const renderArea = document.getElementById(`chart-${chartId}`);
  if (!renderArea) return;

  renderArea.innerHTML = '';

  // Hide standard legend (sidebar replaces it)
  const leg = document.getElementById(`legend-${chartId}`);
  if (leg) { leg.style.display = 'none'; }

  // Make render area a flex container so children can fill height
  renderArea.style.display       = 'flex';
  renderArea.style.flexDirection = 'column';
  renderArea.style.padding       = '0';

  // Inject larger spinner-arrow CSS once per page load (remove stale version if present)
  const _existingSpinStyle = document.getElementById('tl-spinner-style');
  if (_existingSpinStyle) _existingSpinStyle.remove();
  {
    const styleEl = document.createElement('style');
    styleEl.id = 'tl-spinner-style';
    styleEl.textContent = [
      'input[id^="tl-min-"], input[id^="tl-max-"] {',
      '  height: 28px;',
      '  box-sizing: border-box;',
      '}',
      'input[id^="tl-min-"]::-webkit-inner-spin-button,',
      'input[id^="tl-max-"]::-webkit-inner-spin-button,',
      'input[id^="tl-min-"]::-webkit-outer-spin-button,',
      'input[id^="tl-max-"]::-webkit-outer-spin-button {',
      '  -webkit-appearance: inner-spin-button;',
      '  height: 28px;',
      '  opacity: 1;',
      '  cursor: pointer;',
      '}',
    ].join('\n');
    document.head.appendChild(styleEl);
  }

  // ── Outer flex-row wrapper ────────────────────────────────────
  const wrapper = document.createElement('div');
  wrapper.style.cssText = 'display:flex;flex:1;min-height:0;';

  // ── Left sidebar ─────────────────────────────────────────────
  const sidebar = document.createElement('div');
  sidebar.className = 'tl-sidebar';
  sidebar.style.cssText = [
    'width:190px;flex-shrink:0;',
    'background:#232F3E;border-right:1px solid #37475A;',
    'display:flex;flex-direction:column;overflow:hidden;',
    'transition:width 0.2s ease;',
  ].join('');

  let sidebarCollapsed = false;

  // Combined header row (always visible — collapse button + title + uncheck all)
  const headerRow = document.createElement('div');
  headerRow.style.cssText = [
    'display:flex;align-items:center;gap:4px;flex-shrink:0;',
    'padding:5px 6px 5px 8px;border-bottom:1px solid #37475A;',
  ].join('');

  const toggleBtn = document.createElement('button');
  toggleBtn.title = 'Collapse sidebar';
  toggleBtn.textContent = '◀';
  toggleBtn.style.cssText = [
    'background:none;border:none;color:#D1D5DB;cursor:pointer;flex-shrink:0;',
    'font-size:13px;padding:1px 3px;border-radius:3px;line-height:1;',
    'transition:color 0.15s;',
  ].join('');
  toggleBtn.addEventListener('mouseenter', () => { toggleBtn.style.color = '#ecf0f1'; });
  toggleBtn.addEventListener('mouseleave', () => { toggleBtn.style.color = '#95a5a6'; });

  const sideTitle = document.createElement('div');
  sideTitle.textContent = 'Trend Lines';
  sideTitle.style.cssText = 'font-size:12px;font-weight:700;color:#FFFFFF;flex:1;';

  const uncheckAllBtn = document.createElement('button');
  uncheckAllBtn.textContent = 'Uncheck All';
  uncheckAllBtn.style.cssText = [
    'background:none;border:1px solid #4A5568;border-radius:3px;flex-shrink:0;',
    'color:#D1D5DB;font-size:10px;padding:2px 5px;cursor:pointer;',
    'transition:color 0.15s,border-color 0.15s;white-space:nowrap;',
  ].join('');
  uncheckAllBtn.addEventListener('mouseenter', () => { uncheckAllBtn.style.color = '#ecf0f1'; uncheckAllBtn.style.borderColor = '#5a7a99'; });
  uncheckAllBtn.addEventListener('mouseleave', () => { uncheckAllBtn.style.color = '#95a5a6'; uncheckAllBtn.style.borderColor = '#445'; });
  uncheckAllBtn.addEventListener('click', () => {
    seriesList.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      if (cb.checked) {
        cb.checked = false;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  });

  headerRow.append(toggleBtn, sideTitle, uncheckAllBtn);
  sidebar.appendChild(headerRow);

  // Collapsible content wrapper
  const sideContent = document.createElement('div');
  sideContent.style.cssText = 'display:flex;flex-direction:column;gap:5px;padding:6px 8px 6px;flex:1;min-height:0;overflow:hidden;';

  const seriesList = document.createElement('div');
  seriesList.className = 'tl-series-list';
  seriesList.style.cssText = 'display:flex;flex-direction:column;gap:4px;flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;padding-right:2px;';

  // Group series by category, preserving insertion order
  const _catOrder = [];
  const _catGroups = {};
  (data.series || []).forEach(s => {
    const cat = s.category || '';
    if (!_catGroups[cat]) { _catGroups[cat] = []; _catOrder.push(cat); }
    _catGroups[cat].push(s);
  });

  const _hasCats = _catOrder.some(c => c !== '');

  _catOrder.forEach((cat, idx) => {
    if (_hasCats && cat) {
      // Category header row (clickable — toggles the group)
      const catHeader = document.createElement('div');
      catHeader.style.cssText = [
        idx > 0 ? 'margin-top:8px;' : '',
        'display:flex;align-items:center;justify-content:space-between;',
        'cursor:pointer;user-select:none;',
        'padding-bottom:3px;border-bottom:1px solid #37475A;',
      ].join('');

      const catLabel = document.createElement('span');
      catLabel.textContent = cat.toUpperCase();
      catLabel.style.cssText = 'font-size:10px;font-weight:700;color:#D1D5DB;letter-spacing:.5px;';

      const catArrow = document.createElement('span');
      catArrow.textContent = '▾';
      catArrow.style.cssText = 'font-size:14px;color:#D1D5DB;transition:transform 0.15s;line-height:1;';

      catHeader.append(catLabel, catArrow);
      seriesList.appendChild(catHeader);

      // Container for this category's series items
      const catItems = document.createElement('div');
      catItems.style.cssText = 'display:flex;flex-direction:column;gap:6px;';
      seriesList.appendChild(catItems);

      // Open this category if it has any visible series (respects display_toggles when set),
      // otherwise fall back to the default: only Reach starts open.
      const _dTogglesForCat = config.display_toggles || {};
      const _hasCustomToggles = Object.keys(_dTogglesForCat).length > 0;
      const _catHasVisible = _catGroups[cat].some(s =>
        _hasCustomToggles
          ? (_dTogglesForCat[s.name] !== false && s.name in _dTogglesForCat)
          : s.default_visible !== false
      );
      let _catOpen = _catHasVisible;
      if (!_catOpen) {
        catItems.style.display = 'none';
        catArrow.style.transform = 'rotate(-90deg)';
      }
      catHeader.addEventListener('click', () => {
        _catOpen = !_catOpen;
        catItems.style.display = _catOpen ? 'flex' : 'none';
        catArrow.style.transform = _catOpen ? '' : 'rotate(-90deg)';
      });

      _catGroups[cat].forEach(s => {
        const label = document.createElement('label');
        label.style.cssText = 'display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#FFFFFF;user-select:none;';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        const _dToggles = config.display_toggles || {};
        cb.checked = (s.name in _dToggles) ? (_dToggles[s.name] !== false) : (s.default_visible !== false);
        cb.style.cssText = 'cursor:pointer;flex-shrink:0;';

        const swatch = document.createElement('span');
        swatch.style.cssText = `display:inline-block;width:14px;height:14px;border-radius:3px;flex-shrink:0;background:${escHtml(s.color)};`;

        const nameSpan = document.createElement('span');
        nameSpan.textContent = s.name;
        nameSpan.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';

        label.append(cb, swatch, nameSpan);
        catItems.appendChild(label);

        cb.addEventListener('change', () => {
          if (!config.display_toggles) config.display_toggles = {};
          config.display_toggles[s.name] = cb.checked;
          const chart = CHART_INSTANCES[chartId];
          if (chart) chart.dispatchAction({ type: 'legendToggleSelect', name: s.name });
        });
      });
    } else {
      // Ungrouped series (no category)
      _catGroups[cat].forEach(s => {
        const label = document.createElement('label');
        label.style.cssText = 'display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#FFFFFF;user-select:none;';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        const _dTogglesU = config.display_toggles || {};
        cb.checked = (s.name in _dTogglesU) ? (_dTogglesU[s.name] !== false) : (s.default_visible !== false);
        cb.style.cssText = 'cursor:pointer;flex-shrink:0;';

        const swatch = document.createElement('span');
        swatch.style.cssText = `display:inline-block;width:14px;height:14px;border-radius:3px;flex-shrink:0;background:${escHtml(s.color)};`;

        const nameSpan = document.createElement('span');
        nameSpan.textContent = s.name;
        nameSpan.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';

        label.append(cb, swatch, nameSpan);
        seriesList.appendChild(label);

        cb.addEventListener('change', () => {
          if (!config.display_toggles) config.display_toggles = {};
          config.display_toggles[s.name] = cb.checked;
          const chart = CHART_INSTANCES[chartId];
          if (chart) chart.dispatchAction({ type: 'legendToggleSelect', name: s.name });
        });
      });
    }
  });

  sideContent.appendChild(seriesList);

  // ── Persist trend-line prefs in localStorage ────────────────────
  const _prefsKey = 'trendPrefs_' + chartId;
  function _loadPrefs() {
    try {
      const raw = localStorage.getItem(_prefsKey);
      if (raw) {
        const p = JSON.parse(raw);
        if (typeof p.separate_trends === 'boolean') config.separate_trends = p.separate_trends;
        if (typeof p.max_trend_charts === 'number') config.max_trend_charts = p.max_trend_charts;
      }
    } catch (e) {}
  }
  function _savePrefs() {
    try {
      localStorage.setItem(_prefsKey, JSON.stringify({
        separate_trends: config.separate_trends,
        max_trend_charts: config.max_trend_charts,
      }));
    } catch (e) {}
  }
  _loadPrefs();

  // ── Separate trend lines controls ──────────────────────────────
  const separateDiv = document.createElement('div');
  separateDiv.style.cssText = 'flex-shrink:0;display:flex;flex-direction:column;gap:4px;padding-bottom:5px;border-bottom:1px solid #37475A;';

  const sepLabel = document.createElement('label');
  sepLabel.style.cssText = 'display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11px;color:#FFFFFF;user-select:none;';
  const sepCb = document.createElement('input');
  sepCb.type = 'checkbox';
  sepCb.checked = config.separate_trends !== false;
  sepCb.style.cssText = 'cursor:pointer;flex-shrink:0;';
  const sepText = document.createElement('span');
  sepText.textContent = 'Separate Trend Lines';
  sepLabel.append(sepCb, sepText);
  separateDiv.appendChild(sepLabel);

  const maxRow = document.createElement('div');
  maxRow.style.cssText = 'display:flex;align-items:center;gap:6px;padding-left:22px;';
  const maxChartLabel = document.createElement('span');
  maxChartLabel.textContent = 'Max Charts';
  maxChartLabel.style.cssText = 'font-size:10px;color:#D1D5DB;';
  const maxChartsInput = document.createElement('input');
  maxChartsInput.type = 'number';
  maxChartsInput.min = '1';
  maxChartsInput.max = '20';
  maxChartsInput.value = config.max_trend_charts || 3;
  maxChartsInput.style.cssText = 'width:48px;padding:2px 4px;border:1px solid #444;border-radius:3px;background:#37475A;color:#FFFFFF;font-size:11px;text-align:center;';
  maxRow.append(maxChartLabel, maxChartsInput);
  separateDiv.appendChild(maxRow);

  sideContent.insertBefore(separateDiv, seriesList);

  // ── Y-axis range controls ─────────────────────────────────────
  const rangeDiv = document.createElement('div');
  rangeDiv.style.cssText = 'flex-shrink:0;display:none;flex-direction:column;gap:3px;padding-top:5px;border-top:1px solid #2d3e50;';

  const inputStyle = 'width:100%;padding:3px 4px;height:24px;box-sizing:border-box;border:1px solid #444;border-radius:3px;background:#37475A;color:#FFFFFF;font-size:11px;';

  rangeDiv.innerHTML = `
    <div style="font-size:10px;font-weight:700;color:#D1D5DB;letter-spacing:.5px;">Y-AXIS RANGE</div>
    <div style="display:flex;gap:6px;">
      <div style="flex:1;display:flex;flex-direction:column;gap:2px;">
        <div style="font-size:10px;color:#D1D5DB;">Min</div>
        <input type="number" id="tl-min-${escHtml(chartId)}" placeholder="auto" min="0" style="${inputStyle}">
      </div>
      <div style="flex:1;display:flex;flex-direction:column;gap:2px;">
        <div style="font-size:10px;color:#D1D5DB;">Max</div>
        <input type="number" id="tl-max-${escHtml(chartId)}" placeholder="auto" min="0" style="${inputStyle}">
      </div>
    </div>
    <button id="tl-reset-${escHtml(chartId)}"
      style="padding:3px 8px;background:#37475A;border:1px solid #4A5568;border-radius:3px;color:#D1D5DB;font-size:11px;cursor:pointer;text-align:center;">
      ↺ Reset
    </button>
  `;
  sideContent.appendChild(rangeDiv);

  sidebar.appendChild(sideContent);

  // Toggle collapse/expand
  toggleBtn.addEventListener('click', () => {
    sidebarCollapsed = !sidebarCollapsed;
    if (sidebarCollapsed) {
      sideContent.style.display = 'none';
      sideTitle.style.display = 'none';
      uncheckAllBtn.style.display = 'none';
      sidebar.style.width = '28px';
      toggleBtn.textContent = '▶';
      toggleBtn.title = 'Expand sidebar';
    } else {
      sideContent.style.display = 'flex';
      sideTitle.style.display = '';
      uncheckAllBtn.style.display = '';
      sidebar.style.width = '190px';
      toggleBtn.textContent = '◀';
      toggleBtn.title = 'Collapse sidebar';
    }
    setTimeout(() => {
      const chart = CHART_INSTANCES[chartId];
      if (chart) chart.resize();
    }, 220);
  });

  // ── Right chart area ──────────────────────────────────────────
  const chartArea = document.createElement('div');
  chartArea.className = 'tl-chart-area';
  chartArea.id = `tl-canvas-${chartId}`;
  chartArea.style.cssText = 'flex:1;min-width:0;';

  wrapper.appendChild(sidebar);
  wrapper.appendChild(chartArea);
  renderArea.appendChild(wrapper);

  // Dispose previous ECharts instances (single + multi)
  if (CHART_INSTANCES[chartId]) {
    try { CHART_INSTANCES[chartId].dispose(); } catch (e) {}
  }
  (CHART_INSTANCES[chartId + '__multi'] || []).forEach(c => { try { c.dispose(); } catch (e) {} });

  // Track stacked mini-chart instances for cleanup / resize
  let _multiInstances = [];

  // ── Shared helpers ────────────────────────────────────────────
  function getVisibleSeries() {
    return (data.series || []).filter(s => {
      const dt = config.display_toggles || {};
      return (s.name in dt) ? (dt[s.name] !== false) : (s.default_visible !== false);
    });
  }

  /** Group series by similar max-value scales when count > maxCharts.
   *  Always produces exactly min(maxCharts, series.length) groups with
   *  items distributed as evenly as possible (contiguous for similar scales). */
  function groupByScale(series, maxCharts) {
    if (series.length <= maxCharts) return series.map(s => [s]);
    const sorted = [...series].sort((a, b) => {
      const maxA = Math.max(0, ...(a.data || []));
      const maxB = Math.max(0, ...(b.data || []));
      return maxA - maxB;
    });
    const groups = [];
    const baseSize = Math.floor(sorted.length / maxCharts);
    const remainder = sorted.length % maxCharts;
    let idx = 0;
    for (let g = 0; g < maxCharts; g++) {
      const size = baseSize + (g < remainder ? 1 : 0);
      groups.push(sorted.slice(idx, idx + size));
      idx += size;
    }
    return groups;
  }

  // Format x-axis labels from YYYY-MM → "Mon YYYY"
  const xLabels = (data.x_data || []).map(d => {
    try {
      const dt = new Date(d + '-02');   // day 2 avoids timezone-rollback to previous month
      return dt.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
    } catch (e) { return d; }
  });

  const yAxisFormatter = v => {
    const abs = Math.abs(v);
    if (abs >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (abs >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return v;
  };

  // Chart area height
  const slotEl = document.getElementById(`slot-${chartId}`);
  const slotH  = slotEl ? slotEl.offsetHeight : 0;
  const cfgH   = config && config.height ? parseInt(config.height) : 0;
  const areaH  = (slotH > 60 ? slotH - 60 : 0) || cfgH || 400;

  // ── Render dispatcher ─────────────────────────────────────────
  function renderChartArea() {
    // Dispose existing instances
    if (CHART_INSTANCES[chartId]) {
      try { CHART_INSTANCES[chartId].dispose(); } catch (e) {}
      CHART_INSTANCES[chartId] = null;
    }
    _multiInstances.forEach(c => { try { c.dispose(); } catch (e) {} });
    _multiInstances = [];

    chartArea.innerHTML = '';

    const isSeparate = sepCb.checked;
    config.separate_trends = isSeparate;
    config.max_trend_charts = parseInt(maxChartsInput.value) || 3;

    // Y-axis range controls — hidden for now (kept for future use)
    // rangeDiv.style.display = isSeparate ? 'none' : '';

    if (isSeparate) {
      renderMultiChart();
    } else {
      renderSingleChart();
    }

    CHART_INSTANCES[chartId + '__multi'] = _multiInstances;
  }

  // ── Single chart (original behaviour) ─────────────────────────
  function renderSingleChart() {
    chartArea.style.height = areaH + 'px';
    chartArea.style.overflowY = '';

    const option = {
      legend: {
        show: false,
        data: (data.series || []).map(s => s.name),
        selected: Object.fromEntries((data.series || []).map(s => {
          const dt = config.display_toggles || {};
          const vis = (s.name in dt) ? (dt[s.name] !== false) : (s.default_visible !== false);
          return [s.name, vis];
        })),
      },
      tooltip: {
        trigger: 'axis',
        formatter: params => {
          let html = `<div style="font-weight:600;margin-bottom:4px;">${escHtml(params[0] && params[0].axisValue)}</div>`;
          params.forEach(p => {
            html += `<div style="display:flex;align-items:center;gap:6px;">` +
              `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${p.color};"></span>` +
              `<span>${escHtml(p.seriesName)}</span>` +
              `<span style="margin-left:auto;font-weight:600;">${formatNumber(p.value)}</span></div>`;
          });
          return html;
        },
      },
      grid: { containLabel: true, left: '8px', right: '16px', top: '36px', bottom: '48px' },
      xAxis: {
        type: 'category',
        data: xLabels,
        axisLabel: { rotate: 30, fontSize: 11 },
      },
      yAxis: {
        type: 'value',
        axisLabel: { formatter: yAxisFormatter },
      },
      series: (data.series || []).map(s => ({
        name:      s.name,
        type:      'line',
        data:      s.data,
        itemStyle: { color: s.color },
        lineStyle: { color: s.color },
      })),
    };

    const chart = echarts.init(chartArea, 'kibana');
    CHART_INSTANCES[chartId] = chart;
    chart.setOption(option);

    // Track series visibility for export
    chart.on('legendselectchanged', params => {
      const cfg = DASHBOARD_STATE.charts[chartId];
      if (!cfg) return;
      cfg.display_toggles = { ...(cfg.display_toggles || {}), ...params.selected };
    });

    requestAnimationFrame(() => {
      chart.resize();
      setTimeout(() => chart.resize(), 150);
    });
  }

  // ── Stacked multi-chart mode ──────────────────────────────────
  function renderMultiChart() {
    const visible = getVisibleSeries();
    if (visible.length === 0) {
      chartArea.style.height = areaH + 'px';
      chartArea.style.overflowY = '';
      chartArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#aaa;font-size:13px;">No trend lines selected</div>';
      return;
    }

    const maxCharts = parseInt(maxChartsInput.value) || 3;
    const groups = groupByScale(visible, maxCharts);

    // Height per mini chart — containLabel handles date label space
    const chartH = Math.max(220, Math.min(350, Math.floor(areaH / groups.length)));
    const totalH = groups.length * (chartH + 28);          // 28px title bar per group
    chartArea.style.height = areaH + 'px';
    chartArea.style.overflowY = totalH > areaH ? 'auto' : '';

    groups.forEach((group, gi) => {
      const isLast = gi === groups.length - 1;

      // Title bar showing series names + color swatches
      const titleBar = document.createElement('div');
      titleBar.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 8px;background:#1a2028;border-bottom:1px solid #37475A;flex-wrap:wrap;';
      group.forEach(s => {
        const chip = document.createElement('span');
        chip.style.cssText = 'display:inline-flex;align-items:center;gap:4px;font-size:11px;color:#FFFFFF;';
        const sw = document.createElement('span');
        sw.style.cssText = `display:inline-block;width:10px;height:10px;border-radius:2px;background:${escHtml(s.color)};flex-shrink:0;`;
        chip.appendChild(sw);
        chip.appendChild(document.createTextNode(s.name));
        titleBar.appendChild(chip);
      });
      chartArea.appendChild(titleBar);

      // Mini chart container
      const miniDiv = document.createElement('div');
      miniDiv.style.cssText = `width:100%;height:${chartH}px;${!isLast ? 'border-bottom:1px solid #37475A;' : ''}`;
      chartArea.appendChild(miniDiv);

      const option = {
        legend: { show: false },
        tooltip: {
          trigger: 'axis',
          formatter: params => {
            let html = `<div style="font-weight:600;margin-bottom:4px;">${escHtml(params[0] && params[0].axisValue)}</div>`;
            params.forEach(p => {
              html += `<div style="display:flex;align-items:center;gap:6px;">` +
                `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${p.color};"></span>` +
                `<span>${escHtml(p.seriesName)}</span>` +
                `<span style="margin-left:auto;font-weight:600;">${formatNumber(p.value)}</span></div>`;
            });
            return html;
          },
        },
        grid: { containLabel: true, left: '8px', right: '16px', top: '8px', bottom: '4px' },
        xAxis: {
          type: 'category',
          data: xLabels,
          axisLabel: { rotate: 30, fontSize: 11 },
        },
        yAxis: {
          type: 'value',
          axisLabel: { formatter: yAxisFormatter },
        },
        series: group.map(s => ({
          name:      s.name,
          type:      'line',
          data:      s.data,
          itemStyle: { color: s.color },
          lineStyle: { color: s.color },
        })),
      };

      const inst = echarts.init(miniDiv, 'kibana');
      inst.setOption(option);
      _multiInstances.push(inst);
    });

    requestAnimationFrame(() => {
      _multiInstances.forEach(c => c.resize());
      setTimeout(() => _multiInstances.forEach(c => c.resize()), 150);
    });
  }

  // Clamp max-charts input to the number of visible series
  function syncMaxChartsLimit() {
    const count = getVisibleSeries().length || 1;
    maxChartsInput.max = count;
    // Default to 3 (or fewer if not enough visible series)
    if (!config.max_trend_charts) {
      const def = Math.min(3, count);
      maxChartsInput.value = def;
      config.max_trend_charts = def;
    } else if (config.max_trend_charts > count) {
      maxChartsInput.value = count;
      config.max_trend_charts = count;
    }
  }

  // Show/hide max-charts row based on separate checkbox
  function syncMaxRowVisibility() {
    maxRow.style.display = sepCb.checked ? 'flex' : 'none';
  }

  // ── Initial render ────────────────────────────────────────────
  syncMaxChartsLimit();
  syncMaxRowVisibility();
  renderChartArea();

  // ── Wire separate-trend controls ──────────────────────────────
  sepCb.addEventListener('change', () => {
    config.separate_trends = sepCb.checked;
    _savePrefs();
    syncMaxRowVisibility();
    renderChartArea();
  });
  maxChartsInput.addEventListener('change', () => {
    const limit = parseInt(maxChartsInput.max) || 1;
    let val = parseInt(maxChartsInput.value);
    // Empty or invalid → reset to 1
    if (isNaN(val) || maxChartsInput.value.trim() === '') val = 1;
    if (val < 1) val = 1;
    if (val > limit) val = limit;
    maxChartsInput.value = val;
    config.max_trend_charts = val;
    _savePrefs();
    if (sepCb.checked) renderChartArea();
  });
  // Also catch blur to reset empty field
  maxChartsInput.addEventListener('blur', () => {
    if (maxChartsInput.value.trim() === '') {
      maxChartsInput.value = 1;
      config.max_trend_charts = 1;
      _savePrefs();
      if (sepCb.checked) renderChartArea();
    }
  });

  // Re-render stacked charts when series visibility changes (debounced)
  let _multiRenderTimer = null;
  seriesList.addEventListener('change', (e) => {
    if (e.target.type === 'checkbox') {
      syncMaxChartsLimit();
      if (sepCb.checked) {
        clearTimeout(_multiRenderTimer);
        _multiRenderTimer = setTimeout(() => renderChartArea(), 50);
      }
    }
  });

  // ── Y-axis validation & wiring (single-chart mode only) ──────
  const yMinInput = document.getElementById(`tl-min-${chartId}`);
  const yMaxInput = document.getElementById(`tl-max-${chartId}`);
  const resetBtn  = document.getElementById(`tl-reset-${chartId}`);

  function applyYAxisRange() {
    const chart = CHART_INSTANCES[chartId];
    if (!chart) return;   // multi-chart mode — controls are hidden

    let minVal = yMinInput.value !== '' ? parseFloat(yMinInput.value) : null;
    let maxVal = yMaxInput.value !== '' ? parseFloat(yMaxInput.value) : null;

    // Clamp negatives to 0
    if (minVal !== null && minVal < 0) { minVal = 0; yMinInput.value = 0; }
    if (maxVal !== null && maxVal < 0) { maxVal = 0; yMaxInput.value = 0; }

    // Max must be > min — auto-correct if violated
    if (minVal !== null && maxVal !== null && maxVal <= minVal) {
      maxVal = minVal + 1;
      yMaxInput.value = maxVal;
    }
    if (minVal !== null && maxVal !== null && minVal >= maxVal) {
      minVal = maxVal - 1;
      yMinInput.value = minVal < 0 ? 0 : minVal;
    }

    yMinInput.style.borderColor = '#444';
    yMaxInput.style.borderColor = '#444';

    chart.setOption({ yAxis: { min: minVal, max: maxVal } });
  }

  if (yMinInput) yMinInput.addEventListener('change', applyYAxisRange);
  if (yMaxInput) yMaxInput.addEventListener('change', applyYAxisRange);

  if (resetBtn) {
    resetBtn.addEventListener('mouseenter', () => { resetBtn.style.color = '#ecf0f1'; resetBtn.style.borderColor = '#5a7a99'; });
    resetBtn.addEventListener('mouseleave', () => { resetBtn.style.color = '#95a5a6'; resetBtn.style.borderColor = '#445'; });
    resetBtn.addEventListener('click', () => {
      const chart = CHART_INSTANCES[chartId];
      if (!chart) return;
      if (yMinInput) { yMinInput.value = ''; yMinInput.style.borderColor = '#444'; }
      if (yMaxInput) { yMaxInput.value = ''; yMaxInput.style.borderColor = '#444'; }
      chart.setOption({ yAxis: { min: null, max: null } });
    });
  }

  // Resize all chart instances on sidebar collapse and window resize
  toggleBtn.addEventListener('click', () => {
    setTimeout(() => {
      _multiInstances.forEach(c => { try { c.resize(); } catch (e) {} });
    }, 220);
  });

  window.addEventListener('resize', () => {
    if (CHART_INSTANCES[chartId]) {
      try { CHART_INSTANCES[chartId].resize(); } catch (e) {}
    }
    _multiInstances.forEach(c => { try { c.resize(); } catch (e) {} });
  });
}


// ══════════════════════════════════════════════════════════════
// Canva — hardcoded Summary Report visual
// ══════════════════════════════════════════════════════════════

// Platform icons — all uploaded icon files
const _ICON_BASE = DASHBOARD_BASE + '../static/dashboard_builder/icons/';
const _ICON = {
  fb:    `<img src="${_ICON_BASE}facebook.svg" width="36" height="36">`,
  ig:    `<img src="${_ICON_BASE}instagram.svg" width="36" height="36" style="border-radius:8px;">`,
  yt:    `<img src="${_ICON_BASE}youtube.svg" width="36" height="36">`,
  tt:    `<img src="${_ICON_BASE}tiktok-icon-white-1-logo.svg" width="36" height="36">`,
  globe: `<img src="${_ICON_BASE}web.svg" width="36" height="36">`,
  meta:  `<img src="${_ICON_BASE}meta.svg" width="36" height="36">`,
  ga:    `<img src="${_ICON_BASE}google-analytics-3-logo.svg" width="36" height="36">`,
};

// Section badge icons — reach + journey keep inline SVGs; engage + interact use uploaded files
const _SEC_ICON = {
  reach:   '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polygon points="10,8 16,12 10,16"/></svg>',
  engage:  `<img src="${_ICON_BASE}like.svg" width="22" height="22" style="filter:brightness(0) invert(1);">`,
  interact:`<img src="${_ICON_BASE}messages.svg" width="22" height="22" style="filter:brightness(0) invert(1);">`,
  journey: '<svg viewBox="0 0 24 24" fill="white"><path d="M12.5 2C9.5 2 7 4.5 7 7.5c0 4 5.5 10.5 5.5 10.5s5.5-6.5 5.5-10.5C18 4.5 15.5 2 12.5 2zm0 7a2 2 0 1 1 0-4 2 2 0 0 1 0 4z"/></svg>',
};

function _cvId(chartId, key) { return `cv-${chartId}-${key}`; }

// friendly source name for the default tooltip. Keyed by a substring
// of the data-view pattern (the `dv` strings the Canva metrics use, e.g.
// `*fb_insights`). Falls back to the raw dv when no friendly name matches, so a
// future data source still gets a sensible (if terse) default. Must stay in
// sync with the same map in builder.js (_canvaDefaultTooltip).
const _CV_SOURCE_LABELS = [
  ['fb_insights',        'Facebook Ads'],
  ['ig_insights',        'Instagram'],
  ['fb_page',            'Facebook Page'],
  ['google_ads',         'Google Ads'],
  ['echo_conversations', 'Echo'],
  ['chatwoot',           'Chatwoot'],
  ['conversation_rollups', 'Echo archive'],  // monthly rollups of deleted raw conversations
  ['disciple_tools',     'Disciple Tools'],
  ['youtube',            'YouTube'],
  ['tiktok',             'TikTok'],
  ['ga',                 'Google Analytics'],
];

function _cvSourceLabel(dv) {
  const raw = String(dv || '').replace(/^\*_?/, '');
  for (const [needle, label] of _CV_SOURCE_LABELS) {
    if (raw.includes(needle)) return label;
  }
  return raw || '';
}

// default tooltip text for a Canva metric tile — "{source} · {field}".
// Used both as the rendered title when no override is set and as the editor
// placeholder, so the two surfaces always show the same default.
function _canvaDefaultTooltip(dv, field) {
  const src = _cvSourceLabel(dv);
  const f = String(field || '').replace(/\.keyword$/, '');
  if (src && f) return `${src} · ${f}`;
  return src || f || '';
}

async function renderCanvaChart(chartId) {
  const config = (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE.charts[chartId]) || null;
  if (!config) return;
  const area = document.getElementById(`chart-${chartId}`);
  if (!area) return;

  const _ = (key) => _cvId(chartId, key);  // shorthand for element IDs

  // ── Build the full mockup HTML ──
  area.innerHTML = `
<style>
  .cv { font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; padding:16px; }
  /* flex (was a fixed 1fr/2.2fr grid) so a hidden section lets the survivor fill the row */
  .cv-top { display:flex; gap:16px; margin-bottom:16px; }
  .cv-top .cv-reach { flex:1 1 0; min-width:0; }
  .cv-top .cv-engage { flex:2.2 1 0; min-width:0; }
  .cv-sec { border-radius:16px; padding:0; position:relative; overflow:visible; }
  .cv-sec-icon { position:absolute; top:-18px; left:50%; transform:translateX(-50%); width:44px; height:44px; border-radius:50%; display:flex; align-items:center; justify-content:center; z-index:2; border:3px solid #f0f2f5; }
  .cv-sec-icon svg { width:22px; height:22px; }
  .cv-sec-body { padding:28px 16px 16px; }
  .cv-sec-title { text-align:center; font-size:16px; font-weight:700; margin-bottom:14px; }

  /* Section 1: Reach */
  .cv-reach { background:#8db4d2; }
  .cv-reach .cv-sec-icon { background:#6a9bbf; }
  .cv-reach-tbl { width:100%; border-collapse:collapse; }
  .cv-reach-tbl th { font-size:12px; font-weight:600; color:#1a3a5c; padding:4px 8px; text-align:center; }
  .cv-reach-tbl td { padding:8px; text-align:center; font-size:22px; font-weight:700; color:#1a3a5c; }
  .cv-reach-tbl tr+tr { border-top:1px solid rgba(255,255,255,0.3); }
  /* what each reach number is; the FB/IG rows sum daily reach (not unique people) */
  .cv-reach-tbl .cv-rlbl { font-size:11px; font-weight:600; color:#2f4f6f; margin-top:2px; }
  .cv-reach-note { font-size:11px; font-weight:600; color:#2f4f6f; text-align:center; margin-top:10px; }
  .cv-reach-tbl td:first-child { width:50px; }

  /* Section 2: Engage */
  .cv-engage { background:#c5d8e8; }
  .cv-engage .cv-sec-icon { background:#7ba7c9; }
  /* flex (was a fixed 4-col grid) so hidden cards don't leave gaps; survivors center */
  .cv-engage-grid { display:flex; flex-wrap:wrap; justify-content:center; gap:10px; }
  .cv-engage-grid .cv-ecard { flex:1 1 150px; max-width:320px; min-width:130px; }
  .cv-ecard { background:#dce8f0; border-radius:12px; padding:12px 8px; text-align:center; }
  .cv-ecard-icon { margin-bottom:6px; }
  .cv-ecard-icon svg { width:32px; height:32px; }
  .cv-ecard .cv-elbl { font-size:11px; font-weight:600; color:#555; margin-bottom:2px; }
  .cv-ecard .cv-esep { font-size:9px; font-weight:700; color:#777; letter-spacing:.5px; text-transform:uppercase; border-top:1px solid #b8cad8; margin:6px 6px 4px; padding-top:6px; }
  .cv-ecard .cv-eval { font-size:22px; font-weight:700; color:#1a3a5c; margin-bottom:6px; }
  .cv-ecard-title { font-size:13px; font-weight:700; color:#1a3a5c; margin-bottom:8px; }

  /* Section 3: Interact */
  .cv-interact { background:#2c3e50; margin-bottom:16px; }
  .cv-interact .cv-sec-icon { background:#3d566e; }
  .cv-interact .cv-sec-title { color:#fff; }
  .cv-interact .cv-sec-body { padding:24px 16px 10px; }
  .cv-interact .cv-sec-title { margin-bottom:8px; }
  /* flex (was repeat(5,1fr)) so hidden cells center the survivors */
  .cv-int-grid { display:flex; flex-wrap:wrap; justify-content:center; column-gap:8px; padding:0 12px 8px; }
  .cv-int-cell { flex:1 1 130px; max-width:240px; }
  .cv-int-cell { text-align:center; padding:4px 8px; }
  .cv-int-cell .cv-ilbl { font-size:11px; font-weight:600; color:#a0b4c8; margin-bottom:2px; }
  .cv-int-cell .cv-ival { font-size:24px; font-weight:700; color:#fff; }

  /* Section 4: Journey */
  .cv-journey { background:#d4a535; }
  .cv-journey .cv-sec-icon { background:#c49520; }
  .cv-journey .cv-sec-title { color:#3a2a00; }
  .cv-journey .cv-sec-body { padding:24px 16px 10px; }
  .cv-journey .cv-sec-title { margin-bottom:8px; }
  .cv-jrn-grid { display:grid; grid-template-columns:repeat(5,1fr); padding:0 12px 8px; }
  .cv-jrn-cell { text-align:center; padding:4px 8px; }
  .cv-jrn-cell .cv-jlbl { font-size:11px; font-weight:600; color:#5a4000; margin-bottom:2px; }
  .cv-jrn-cell .cv-jval { font-size:24px; font-weight:700; color:#3a2a00; }

  @media print {
    .cv *, .cv *::before, .cv *::after { -webkit-print-color-adjust:exact!important; print-color-adjust:exact!important; }
  }
</style>
<div class="cv">
  <!-- Sections 1 & 2 side-by-side -->
  <div class="cv-top">
    <!-- SECTION 1: Reach -->
    <div class="cv-sec cv-reach" id="${_('sec-reach')}">
      <div class="cv-sec-icon">${_SEC_ICON.reach}</div>
      <div class="cv-sec-body">
        <div class="cv-sec-title" style="color:#1a3a5c;">How Many People Saw<br>Our Content?</div>
        <table class="cv-reach-tbl">
          <tbody>
            <tr id="${_('row-fb-reach')}"><td>${_ICON.fb}</td><td><div id="${_('fb-reach')}">...</div><div class="cv-rlbl">Facebook ads reach</div></td></tr>
            <tr id="${_('row-ig-reach')}"><td>${_ICON.ig}</td><td><div id="${_('ig-reach')}">...</div><div class="cv-rlbl">Instagram reach</div></td></tr>
            <tr id="${_('row-web-users')}"><td>${_ICON.ga}</td><td><div id="${_('web-users')}">...</div><div class="cv-rlbl">Website users</div></td></tr>
            <tr id="${_('row-meta-reach')}"><td>${_ICON.fb}</td><td><div id="${_('meta-reach')}">...</div><div class="cv-rlbl">Facebook Page viewers</div></td></tr>
            <tr id="${_('row-tt-video-views')}"><td>${_ICON.tt}</td><td><div id="${_('tt-video-views')}">...</div><div class="cv-rlbl">TikTok video views</div></td></tr>
          </tbody>
        </table>
        <div class="cv-reach-note" id="${_('reach-note')}">Facebook and Instagram numbers are daily totals added up over the selected dates — not unique people.</div>
      </div>
    </div>
    <!-- SECTION 2: Engage -->
    <div class="cv-sec cv-engage" id="${_('sec-engage')}">
      <div class="cv-sec-icon">${_SEC_ICON.engage}</div>
      <div class="cv-sec-body">
        <div class="cv-sec-title" style="color:#1a3a5c;">How did they engage with our content?</div>
        <div class="cv-engage-grid">
          <div class="cv-ecard" style="grid-row:span 2;" id="${_('card-videos')}">
            <div class="cv-ecard-title">Videos Watched</div>
            <div class="cv-elbl">15s Views (lifetime)</div><div class="cv-eval" id="${_('vid-15s')}">...</div>
            <div class="cv-elbl">View Time (Minutes)</div><div class="cv-eval" id="${_('vid-time')}">...</div>
            <div style="height:10px;"></div>
            <div class="cv-ecard-title">App Installs</div>
            <div class="cv-eval" id="${_('app-installs')}">...</div>
          </div>
          <div class="cv-ecard" style="grid-row:span 2;" id="${_('card-fb')}">
            <div class="cv-ecard-icon">${_ICON.fb}</div>
            <div class="cv-elbl">Reactions</div><div class="cv-eval" id="${_('fb-reactions')}">...</div>
            <div class="cv-elbl">Shares</div><div class="cv-eval" id="${_('fb-shares')}">...</div>
            <div class="cv-esep">Meta Ads (paid)</div>
            <div class="cv-elbl">ThruPlays</div><div class="cv-eval" id="${_('fbads-thruplays')}">...</div>
            <div class="cv-elbl">Reactions</div><div class="cv-eval" id="${_('fbads-reactions')}">...</div>
            <div class="cv-elbl">Shares</div><div class="cv-eval" id="${_('fbads-shares')}">...</div>
          </div>
          <div class="cv-ecard" style="grid-row:span 2;" id="${_('card-ig')}">
            <div class="cv-ecard-icon">${_ICON.ig}</div>
            <div class="cv-elbl">Follows</div><div class="cv-eval" id="${_('ig-follows')}">...</div>
            <div class="cv-elbl">Reactions</div><div class="cv-eval" id="${_('ig-reactions')}">...</div>
            <div class="cv-elbl">Shares</div><div class="cv-eval" id="${_('ig-shares')}">...</div>
          </div>
          <div class="cv-ecard" style="grid-row:span 2;" id="${_('card-yt')}">
            <div class="cv-ecard-icon">${_ICON.yt}</div>
            <div class="cv-elbl">Subscriptions</div><div class="cv-eval" id="${_('yt-subs')}">...</div>
            <div class="cv-elbl">Reactions</div><div class="cv-eval" id="${_('yt-reactions')}">...</div>
            <div class="cv-elbl">Shares</div><div class="cv-eval" id="${_('yt-shares')}">...</div>
          </div>
          <div class="cv-ecard" style="grid-row:span 2;" id="${_('card-tt')}">
            <div class="cv-ecard-icon">${_ICON.tt}</div>
            <div class="cv-elbl">Followers (current)</div><div class="cv-eval" id="${_('tt-followers')}">...</div>
            <div class="cv-elbl">Follows</div><div class="cv-eval" id="${_('tt-follows')}">...</div>
            <div class="cv-elbl">Views</div><div class="cv-eval" id="${_('tt-views')}">...</div>
            <div class="cv-elbl">Reactions</div><div class="cv-eval" id="${_('tt-reactions')}">...</div>
            <div class="cv-elbl">Shares</div><div class="cv-eval" id="${_('tt-shares')}">...</div>
          </div>
        </div>
      </div>
    </div>
  </div>
  <!-- SECTION 3: Interact -->
  <div class="cv-sec cv-interact" id="${_('sec-interact')}">
    <div class="cv-sec-icon">${_SEC_ICON.interact}</div>
    <div class="cv-sec-body">
      <div class="cv-sec-title">How did they interact with us?</div>
      <div class="cv-int-grid">
        <div class="cv-int-cell" id="${_('cell-msg-contacts')}"><div class="cv-ilbl">Messaging Contacts</div><div class="cv-ival" id="${_('msg-contacts')}">...</div></div>
        <div class="cv-int-cell" id="${_('cell-msg-received')}"><div class="cv-ilbl">Messages Received</div><div class="cv-ival" id="${_('msg-received')}">...</div></div>
        <div class="cv-int-cell" id="${_('cell-msg-sent')}"><div class="cv-ilbl">Messages Sent</div><div class="cv-ival" id="${_('msg-sent')}">...</div></div>
        <div class="cv-int-cell" id="${_('cell-comments')}"><div class="cv-ilbl">Comments</div><div class="cv-ival" id="${_('comments')}">...</div></div>
        <div class="cv-int-cell" id="${_('cell-spiritual')}"><div class="cv-ilbl">Spiritual Conversations</div><div class="cv-ival" id="${_('spiritual')}">...</div></div>
      </div>
    </div>
  </div>
  <!-- SECTION 4: Journey -->
  <div class="cv-sec cv-journey" id="${_('sec-journey')}">
    <div class="cv-sec-icon">${_SEC_ICON.journey}</div>
    <div class="cv-sec-body">
      <div class="cv-sec-title">Where are they on their Jesus Journey?</div>
      <div class="cv-jrn-grid">
        <div class="cv-jrn-cell"><div class="cv-jlbl">Baptisms</div><div class="cv-jval" id="${_('baptisms')}">...</div></div>
        <div class="cv-jrn-cell"><div class="cv-jlbl">New Groups</div><div class="cv-jval" id="${_('new-groups')}">...</div></div>
        <div class="cv-jrn-cell"><div class="cv-jlbl">New Contacts</div><div class="cv-jval" id="${_('new-contacts')}">...</div></div>
        <div class="cv-jrn-cell"><div class="cv-jlbl">First Meetings</div><div class="cv-jval" id="${_('first-meetings')}">...</div></div>
        <div class="cv-jrn-cell"><div class="cv-jlbl">New Churches</div><div class="cv-jval" id="${_('new-churches')}">...</div></div>
      </div>
    </div>
  </div>
</div>`;

  // ── Metric definitions: element ID → query config ──
  // Uses EXACTLY the same query pattern as grouped_table: build a base config
  // with data_view set, then pass through _withExtraFilters which injects
  // DASHBOARD_TIME_RANGE (kibana_date) and DASHBOARD_EXTRA_FILTERS (org/team).
  // Metrics can be overridden via config.metric_overrides[id] = { dv, field, agg }
  const ov = config.metric_overrides || {};
  const _m = (id, dv, field, agg, extra) => {
    const o = ov[id];
    // equals_value override channel — the metrics editor persists it
    // alongside dv/field/agg. Non-empty only, so an untouched override (the
    // editor writes '' for non-equals metrics) keeps the default from `extra`.
    const m = {
      id, dv: o?.dv || dv, field: o?.field || field, agg: o?.agg || agg, ...extra,
      ...(o?.dt_date_unix !== undefined ? { dt_date_unix: o.dt_date_unix } : {}),
      ...(o?.equals_value ? { equals_value: o.equals_value } : {}),
    };
    // per-tile hover tooltip. The author's override wins; otherwise
    // default to "{source} · {field}" derived from the resolved dv/field (so an
    // overridden dv/field changes the default too). Trim guards against a
    // whitespace-only override falling back to nothing.
    const t = (o?.tooltip_text || '').trim();
    m.tooltip_text = t || [m, ...(m.also || [])].map(src => _canvaDefaultTooltip(src.dv, src.field)).join(' + ');
    return m;
  };
  const M = [
    // Section 1: Reach
    _m('fb-reach',     '*fb_insights', 'insight_reach',           'sum'),
    _m('ig-reach',     '*ig_insights', 'reach',                   'sum'),
    _m('web-users',    '*_ga_metrics', 'totalUsers',              'sum'),
    _m('meta-reach',   '*_fb_page_insights',  'page_total_media_view_unique',  'sum'),
    _m('tt-video-views', '*_tiktok_organic',  'video_views_gained',            'sum'),

    // Section 2: Engage
    // dv patterns must be the REAL data-view patterns (see the
    // CANVA_SUMMARY_DVS comment): the old '*fb_page' / '*youtube' / '*ga'
    // shortcuts matched no index, so the Layer-1 scope rewrite resolved them
    // to the empty-scope sentinel and these tiles read 0 unless an author had
    // saved a metric_override. fb-shares also read page_post_engagements (all
    // engagements) — page_posts_shares_total is the shares count every other
    // surface reports.
    _m('vid-15s',       '*_fb_page_insights',  'page_video_views_15s_total',        'max'),  // lifetime snapshot — latest value only
    _m('vid-time',      '*_youtube_analytics', 'estimated_minutes_watched',         'sum'),
    _m('app-installs',  '*google_ads',  'app_installs',                      'sum'),
    _m('fb-reactions',  '*_fb_page_insights',  'page_actions_post_reactions_total',  'sum'),
    _m('fb-shares',     '*_fb_page_insights',  'page_posts_shares_total',           'sum'),
    // paid twins (Meta ads, FB+IG placements) inside the FB card:
    // the organic tiles above can never match Ads Manager; these can.
    // action_type 'post' IS FB's shares action; thruplay data lands from
    // 2026-08-19 so that tile reads 0/— for earlier ranges until
    // the backfill runs.
    _m('fbads-thruplays', '*fb_insights', 'insight_video_thruplay_watched_actions_video_view_total', 'sum'),
    _m('fbads-reactions', '*fb_insights', 'insight_actions_post_reaction',     'sum'),
    _m('fbads-shares',    '*fb_insights', 'insight_actions_post',              'sum'),
    _m('ig-follows',    '*ig_insights', 'follows',                           'sum'),
    _m('ig-reactions',  '*ig_insights', 'likes',                             'sum'),
    _m('ig-shares',     '*ig_insights', 'shares',                            'sum'),
    _m('yt-subs',       '*_youtube_analytics', 'subscribers_gained',                'sum'),
    _m('yt-reactions',  '*_youtube_analytics', 'likes',                             'sum'),
    _m('yt-shares',     '*_youtube_analytics', 'shares',                            'sum'),
    // TikTok. The Display API reports lifetime totals, so the account
    // doc carries daily gains: every tile sums a *_gained field EXCEPT the current
    // follower count, a snapshot read with max (never summed across days).
    _m('tt-followers',  '*_tiktok_organic',    'follower_count',                    'max'),
    _m('tt-follows',    '*_tiktok_organic',    'followers_gained',                  'sum'),
    _m('tt-views',      '*_tiktok_organic',    'video_views_gained',                'sum'),
    _m('tt-reactions',  '*_tiktok_organic',    'likes_gained',                      'sum'),
    _m('tt-shares',     '*_tiktok_organic',    'video_shares_gained',               'sum'),

    // Section 3: Interact
    // messaging tiles add Chatwoot to Echo (`also`: extra sources whose
    // values are summed into the tile; an author's metric_override replaces only
    // the primary source). Chatwoot's contacts_started is distinct contacts who
    // started a conversation per inbox-day, so a person writing on two days counts
    // twice — the closest match to Echo's unique client_id Chatwoot data allows.
    _m('msg-contacts',  '*echo_conversations', 'client_id',                             'cardinality',
       { also: [{ dv: '*_chatwoot_insights', field: 'contacts_started', agg: 'sum' }] }),
    // the retention sweep rolls raw conversations older than 24
    // months into *_conversation_rollups (one doc per team-month, additive
    // counters only) before deleting them, so the additive tiles read the
    // rollups too and the all-time period keeps its history. Messaging
    // Contacts stays raw-only: a distinct count cannot be summed across months.
    _m('msg-received',  '*echo_conversations', 'incomingMessagesCount',                 'sum',
       { also: [{ dv: '*_chatwoot_insights', field: 'incoming_messages_count', agg: 'sum' },
                { dv: '*_conversation_rollups', field: 'incomingMessagesCount', agg: 'sum' }] }),
    _m('msg-sent',      '*echo_conversations', 'outboundMessagesCount',                 'sum',
       { also: [{ dv: '*_chatwoot_insights', field: 'outgoing_messages_count', agg: 'sum' },
                { dv: '*_conversation_rollups', field: 'outboundMessagesCount', agg: 'sum' }] }),
    // Page Insights comments are retired at Meta; count comments on ads instead.
    _m('comments',      '*fb_insights',        'insight_actions_comment',               'sum'),
    // spiritual_conversation is an INTEGER 0/1 flag — 'true' made ES reject the query.
    _m('spiritual',     '*echo_conversations', 'spiritual_conversation',                'equals',
       { equals_value: '1', also: [{ dv: '*_chatwoot_insights', field: 'contact_attr_spiritual_conversation', agg: 'sum' },
                                   { dv: '*_conversation_rollups', field: 'spiritual_conversation', agg: 'sum' }] }),

    // Section 4: Journey — team-keyed DT wildcard (matches
    // `{team}_disciple_tools`) so DT scopes per-team via the
    // allowlist, like the other sources. NOT the cross-tenant
    // `disciple-tools` alias (empty-sentinel-blocked for everyone;).
    // post_type is a BARE keyword on the pull schema (no.keyword
    // subfield — querying it returned 0). meta.* fields keep .keyword
    // (text+keyword on both the pull schema and the demo index).
    _m('baptisms',       '*_disciple_tools', 'meta.milestones.keyword',  'equals', { equals_value: 'milestone_baptized' }),
    _m('new-groups',     '*_disciple_tools', 'post_type',                'equals', { equals_value: 'groups' }),
    _m('new-contacts',   '*_disciple_tools', 'post_type',                'equals', { equals_value: 'contacts' }),
    _m('first-meetings', '*_disciple_tools', 'meta.seeker_path.keyword', 'equals', { equals_value: 'met' }),
    _m('new-churches',   '*_disciple_tools', 'meta.church_start_date',   'exists', { dt_date_unix: true }),
  ];

  // per-section/card auto-hide. Each visibility UNIT is a DOM
  // container (Reach row, Engage card, Interact cell, or a whole section)
  // bound to the metric ids it displays; a unit hides when EVERY one of its
  // metrics' resolved data views is invisible for the current team scope
  // (no docs at all, or the team's ds_* source toggle is off — the server
  // folds both). Existence-based, deliberately NOT range-scoped: a quiet
  // week still renders zeros; only true absence hides. A section hides when
  // all of its units hide. Fail-open everywhere: fetch error, missing
  // pattern answer, or an overridden dv the server doesn't recognize all
  // mean "show".
  const CV_UNITS = [
    { el: 'row-fb-reach',     metrics: ['fb-reach'],                                        sec: 'sec-reach' },
    { el: 'row-ig-reach',     metrics: ['ig-reach'],                                        sec: 'sec-reach' },
    { el: 'row-web-users',    metrics: ['web-users'],                                       sec: 'sec-reach' },
    { el: 'row-meta-reach',   metrics: ['meta-reach'],                                      sec: 'sec-reach' },
    { el: 'row-tt-video-views', metrics: ['tt-video-views'],                                sec: 'sec-reach' },
    { el: 'card-videos',      metrics: ['vid-15s', 'vid-time', 'app-installs'],             sec: 'sec-engage' },
    { el: 'card-fb',          metrics: ['fb-reactions', 'fb-shares', 'fbads-thruplays', 'fbads-reactions', 'fbads-shares'], sec: 'sec-engage' },
    { el: 'card-ig',          metrics: ['ig-follows', 'ig-reactions', 'ig-shares'],         sec: 'sec-engage' },
    { el: 'card-yt',          metrics: ['yt-subs', 'yt-reactions', 'yt-shares'],            sec: 'sec-engage' },
    { el: 'card-tt',          metrics: ['tt-followers', 'tt-follows', 'tt-views', 'tt-reactions', 'tt-shares'], sec: 'sec-engage' },
    { el: 'cell-msg-contacts', metrics: ['msg-contacts'],                                   sec: 'sec-interact' },
    { el: 'cell-msg-received', metrics: ['msg-received'],                                   sec: 'sec-interact' },
    { el: 'cell-msg-sent',    metrics: ['msg-sent'],                                        sec: 'sec-interact' },
    { el: 'cell-comments',    metrics: ['comments'],                                        sec: 'sec-interact' },
    { el: 'cell-spiritual',   metrics: ['spiritual'],                                       sec: 'sec-interact' },
    { el: 'sec-journey',      metrics: ['baptisms', 'new-groups', 'new-contacts', 'first-meetings', 'new-churches'], sec: 'sec-journey' },
  ];
  // Kicked off BEFORE the data batch so both round-trips overlap; awaited
  // after the values have painted, before the reveal event fires.
  const cvVisibilityPromise = _cvFetchPatternVisibility(M);

  // ONE batch request for all metrics. The per-metric /api/query
  // fan-out (25 round-trips through the browser's per-origin connection
  // limit, each re-resolving authz scope server-side) was the page's
  // load-time bottleneck.
  // a tile with extra sources (`also`) sends one query per source;
  // `parts[i]` holds the query indexes whose values sum into M[i].
  const queries = [];
  const parts = M.map(m => [m, ...(m.also || [])].map(src => queries.push(_cvBuildConfig(src)) - 1));
  let results = [];
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/query_batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ queries }),
    });
    const data = await res.json();
    results = Array.isArray(data.results) ? data.results : [];
  } catch (e) {
    // results stays empty — every cell falls through to the em-dash below.
  }
  let hasData = false;
  M.forEach((m, i) => {
    const el = document.getElementById(_cvId(chartId, m.id));
    if (!el) return;
    // native hover tooltip per tile. style:cursor:help hints it.
    if (m.tooltip_text) {
      el.setAttribute('title', m.tooltip_text);
      el.style.cursor = 'help';
    }
    // Sum every source that answered; '—' only when none did.
    const ok = parts[i].map(q => results[q]).filter(r => r && r.success !== false);
    if (ok.length) {
      const v = ok.reduce((sum, r) => {
        const x = r.value ?? r.doc_count ?? 0;
        return sum + (typeof x === 'number' && isFinite(x) ? x : 0);
      }, 0);
      if (v !== 0) hasData = true;
      el.textContent = formatNumber(v);
    } else {
      el.textContent = '—';
    }
  });

  // apply section/card visibility before the reveal event so
  // hidden sections never flash. patVis === null (fetch failed / endpoint
  // said nothing) shows everything.
  const cvVis = await cvVisibilityPromise;
  if (cvVis) {
    const patVis = cvVis.patterns;
    const unitOverrides = cvVis.units || {};
    const dvsById = {};
    M.forEach(m => { dvsById[m.id] = [m.dv, ...(m.also || []).map(src => src.dv)]; });
    // Fail-open per pattern: only an explicit visible:false hides.
    const dvVisible = dv => { const e = patVis[dv]; return !e || e.visible !== false; };
    const secAlive = {};
    const hiddenUnits = [];
    CV_UNITS.forEach(u => {
      // Tier 2 — an explicit per-team override wins over the
      // automatic data/toggle check in BOTH directions.
      const ov = unitOverrides[u.el];
      const alive = ov === 'show' ? true
        : ov === 'hide' ? false
        : u.metrics.some(id => (dvsById[id] || []).some(dvVisible));
      if (!alive) hiddenUnits.push(u.el);
      secAlive[u.sec] = Boolean(secAlive[u.sec]) || alive;
    });
    const anyAlive = Object.keys(secAlive).some(sec => secAlive[sec]);
    if (!anyAlive) {
      // EVERY section is dark for this selection. Hiding them all
      // would leave a silent blank page (found on staging 2026-08-27:
      // one org + its 5 teams, zero staging data anywhere). Replace
      // the report body with an explicit empty state instead — honest, and
      // distinguishable from a load that never finished.
      const cv = area.querySelector('.cv');
      if (cv) {
        cv.innerHTML =
          '<div class="cv-empty" style="padding:48px 24px;text-align:center;color:#5a6b7c;">' +
          '<div style="font-size:18px;font-weight:700;margin-bottom:8px;">No data for this selection yet</div>' +
          '<div style="font-size:13px;max-width:520px;margin:0 auto;">None of the connected data sources hold ' +
          'any data for the selected team(s). Sections appear here automatically as soon as a source imports data.</div>' +
          '</div>';
      }
    } else {
      hiddenUnits.forEach(elKey => {
        const el = document.getElementById(_cvId(chartId, elKey));
        if (el) el.style.display = 'none';
      });
      // the "not unique people" note describes the FB/IG rows only;
      // drop it when none of them is showing.
      if (['row-fb-reach', 'row-ig-reach', 'row-meta-reach'].every(k => hiddenUnits.includes(k))) {
        const note = document.getElementById(_cvId(chartId, 'reach-note'));
        if (note) note.style.display = 'none';
      }
      Object.keys(secAlive).forEach(sec => {
        if (!secAlive[sec]) {
          const el = document.getElementById(_cvId(chartId, sec));
          if (el) el.style.display = 'none';
        }
      });
    }
  }

  // reveal contract: the parent dashboards page keeps the viewer
  // iframe hidden until it receives 'echarts-content-ready', which view.html
  // emits on the first 'chart-rendered'. Standard charts dispatch this; the
  // canva report rendered its DOM but never did, so the parent fell through
  // to its 4s reveal fallback — the report sat fully-rendered-but-hidden for
  // ~5s, then "popped in". Dispatch now so the reveal fires on real content.
  try {
    document.dispatchEvent(new CustomEvent('chart-rendered', { detail: { chartId, hasData } }));
  } catch (_e) { /* event hook is best-effort */ }
}

/**
 * ask the platform-availability endpoint which
 * of the canva report's data-view patterns are VISIBLE for the current scope:
 * visible = the pattern holds any docs for the selected team(s) AND the
 * team's ds_* source toggle is on (folded server-side). Sends the resolved
 * (override-aware) dv of every metric; team/org scope is lifted from
 * DASHBOARD_EXTRA_FILTERS — the same values every data query on the page is
 * scoped by. Returns the endpoint's `patterns` map, or null on any failure
 * (the caller fails open and shows everything).
 */
async function _cvFetchPatternVisibility(M) {
  try {
    const params = [];
    [...new Set(M.flatMap(m => [m.dv, ...(m.also || []).map(src => src.dv)]).filter(Boolean))]
      .forEach(p => params.push('pattern=' + encodeURIComponent(p)));
    const extra = (typeof window !== 'undefined' && window.DASHBOARD_EXTRA_FILTERS) || [];
    extra.forEach(f => {
      if (f.field === 'team_title') {
        (Array.isArray(f.value) ? f.value : [f.value]).filter(Boolean)
          .forEach(t => params.push('team=' + encodeURIComponent(t)));
      } else if (f.field === 'organization_title' && f.value) {
        params.push('org=' + encodeURIComponent(f.value));
      }
    });
    // Prefix-safe root: DASHBOARD_BASE is pathname-derived (view.html) and
    // ends with 'dashboard-builder/', so stripping that yields the app root
    // under any mount prefix (bare or /assets).
    const root = DASHBOARD_BASE.replace(/dashboard-builder\/?$/, '');
    const res = await fetch(root + 'api/dashboards/platform-availability?' + params.join('&'));
    const data = await res.json();
    if (!data || !data.success || !data.patterns) return null;
    // Tier 2 — units carries explicit per-team card overrides
    // ({unit_key: 'show'|'hide'}); explicit wins over the automatic check.
    return { patterns: data.patterns, units: data.units || {} };
  } catch (_e) {
    return null;
  }
}

function _cvBuildConfig(m) {
  // Build metric query — same shape as grouped_table uses
  let dateRange = null;

  // 'query date' aggregation: the date_range must use the metric's own field
  // (not kibana_date) so the backend builds a filter agg on that field's values.
  if (m.agg === 'query date') {
    const viewerTimeRange = (typeof window !== 'undefined' && window.DASHBOARD_TIME_RANGE) || {};
    const gdr = DASHBOARD_STATE.global_date_range || {};
    const timeFrom = viewerTimeRange.from || gdr.from || null;
    const timeTo = viewerTimeRange.to || gdr.to || null;
    if (timeFrom || timeTo) {
      const isDT = _isDtDataView(m.dv);
      if (isDT) {
        // Use _ms variant of the field — ES date type, pass date math directly.
        const msField = m.field.replace(/\.keyword$/, '') + '_ms';
        dateRange = { field: msField, from: timeFrom, to: timeTo };
      } else {
        dateRange = { field: m.field, from: timeFrom, to: timeTo };
      }
    }
  }

  const baseConfig = {
    chart_type: 'metric',
    data_view: m.dv,
    y_fields: [m.field],
    aggregation: m.agg,
    date_range: dateRange,  // null for non-'query date' so _withExtraFilters injects kibana_date
    filters: [],
  };
  if (m.equals_value !== undefined) baseConfig.equals_value = m.equals_value;

  // _withExtraFilters injects:
  //   - DASHBOARD_EXTRA_FILTERS (org/team) into filters[]
  //   - DASHBOARD_TIME_RANGE into date_range using kibana_date (only if date_range is still null)
  return _withExtraFilters(baseConfig);
}

// ══════════════════════════════════════════════════════════════
// Report — Impact Report chart type
// ══════════════════════════════════════════════════════════════

function _rptId(chartId, key) {
  return `rpt-${chartId}-${key}`;
}

// Country metadata — keyed by ISO2 code (what team_country stores).
// 10 reference countries positioned by user drag-test on Fla-shop SVG.
// Remaining countries computed via fitted projection:
//   x = 5.310 * lon + 969.1,  y = -0.01752 * lat^2 - 5.371 * lat + 914.33
const _RPT_COUNTRIES = {
  'US': { name: 'United States',  flag: '\u{1F1FA}\u{1F1F8}', x: 441,  y: 677 },
  'ID': { name: 'Indonesia',      flag: '\u{1F1EE}\u{1F1E9}', x: 1541, y: 944 },
  'PH': { name: 'Philippines',    flag: '\u{1F1F5}\u{1F1ED}', x: 1616, y: 842 },
  'IN': { name: 'India',          flag: '\u{1F1EE}\u{1F1F3}', x: 1389, y: 777 },
  'NG': { name: 'Nigeria',        flag: '\u{1F1F3}\u{1F1EC}', x: 1012, y: 864 },
  'BR': { name: 'Brazil',         flag: '\u{1F1E7}\u{1F1F7}', x: 703,  y: 970 },
  'GB': { name: 'United Kingdom', flag: '\u{1F1EC}\u{1F1E7}', x: 959,  y: 563 },
  'JP': { name: 'Japan',          flag: '\u{1F1EF}\u{1F1F5}', x: 1717, y: 700 },
  'AU': { name: 'Australia',      flag: '\u{1F1E6}\u{1F1FA}', x: 1696, y: 1039 },
  'DE': { name: 'Germany',        flag: '\u{1F1E9}\u{1F1EA}', x: 1024, y: 593 },
  'CA': { name: 'Canada',         flag: '\u{1F1E8}\u{1F1E6}', x: 405,  y: 558 },
  'MX': { name: 'Mexico',         flag: '\u{1F1F2}\u{1F1FD}', x: 424,  y: 778 },
  'KE': { name: 'Kenya',          flag: '\u{1F1F0}\u{1F1EA}', x: 1170, y: 914 },
  'KR': { name: 'South Korea',    flag: '\u{1F1F0}\u{1F1F7}', x: 1648, y: 699 },
  'EG': { name: 'Egypt',          flag: '\u{1F1EA}\u{1F1EC}', x: 1126, y: 756 },
  'ZA': { name: 'South Africa',   flag: '\u{1F1FF}\u{1F1E6}', x: 1091, y: 1062 },
  'CO': { name: 'Colombia',       flag: '\u{1F1E8}\u{1F1F4}', x: 575,  y: 889 },
  'TH': { name: 'Thailand',       flag: '\u{1F1F9}\u{1F1ED}', x: 1505, y: 825 },
  'FR': { name: 'France',         flag: '\u{1F1EB}\u{1F1F7}', x: 981,  y: 629 },
  'ES': { name: 'Spain',          flag: '\u{1F1EA}\u{1F1F8}', x: 949,  y: 668 },
  'IT': { name: 'Italy',          flag: '\u{1F1EE}\u{1F1F9}', x: 1036, y: 659 },
  'TR': { name: 'Turkey',         flag: '\u{1F1F9}\u{1F1F7}', x: 1156, y: 679 },
  'PK': { name: 'Pakistan',       flag: '\u{1F1F5}\u{1F1F0}', x: 1337, y: 735 },
  'BD': { name: 'Bangladesh',     flag: '\u{1F1E7}\u{1F1E9}', x: 1449, y: 777 },
  'VN': { name: 'Vietnam',        flag: '\u{1F1FB}\u{1F1F3}', x: 1544, y: 835 },
  'MY': { name: 'Malaysia',       flag: '\u{1F1F2}\u{1F1FE}', x: 1510, y: 891 },
  'ET': { name: 'Ethiopia',       flag: '\u{1F1EA}\u{1F1F9}', x: 1184, y: 864 },
  'GH': { name: 'Ghana',          flag: '\u{1F1EC}\u{1F1ED}', x: 964,  y: 871 },
  'TZ': { name: 'Tanzania',       flag: '\u{1F1F9}\u{1F1FF}', x: 1154, y: 948 },
  'UG': { name: 'Uganda',         flag: '\u{1F1FA}\u{1F1EC}', x: 1141, y: 907 },
  'PE': { name: 'Peru',           flag: '\u{1F1F5}\u{1F1EA}', x: 571,  y: 962 },
  'AR': { name: 'Argentina',      flag: '\u{1F1E6}\u{1F1F7}', x: 631,  y: 1095 },
  'CL': { name: 'Chile',          flag: '\u{1F1E8}\u{1F1F1}', x: 589,  y: 1084 },
  'CN': { name: 'China',          flag: '\u{1F1E8}\u{1F1F3}', x: 1522, y: 714 },
  'RU': { name: 'Russia',         flag: '\u{1F1F7}\u{1F1FA}', x: 1528, y: 518 },
  'NZ': { name: 'New Zealand',    flag: '\u{1F1F3}\u{1F1FF}', x: 1898, y: 1105 },
  'SG': { name: 'Singapore',      flag: '\u{1F1F8}\u{1F1EC}', x: 1520, y: 907 },
  'AE': { name: 'United Arab Emirates', flag: '\u{1F1E6}\u{1F1EA}', x: 1255, y: 779 },
  'SA': { name: 'Saudi Arabia',   flag: '\u{1F1F8}\u{1F1E6}', x: 1209, y: 776 },
  'MM': { name: 'Myanmar',        flag: '\u{1F1F2}\u{1F1F2}', x: 1483, y: 801 },
  'KH': { name: 'Cambodia',       flag: '\u{1F1F0}\u{1F1ED}', x: 1527, y: 844 },
  'NP': { name: 'Nepal',          flag: '\u{1F1F3}\u{1F1F5}', x: 1416, y: 748 },
  'LK': { name: 'Sri Lanka',      flag: '\u{1F1F1}\u{1F1F0}', x: 1398, y: 871 },
  'UA': { name: 'Ukraine',       flag: '\u{1F1FA}\u{1F1E6}', x: 1135, y: 613 },
  // Aliases for non-ISO values found in team_country data
  'United States': { name: 'United States', flag: '\u{1F1FA}\u{1F1F8}', x: 441, y: 677 },
  'USA':           { name: 'United States', flag: '\u{1F1FA}\u{1F1F8}', x: 441, y: 677 },
  'Cairo':         { name: 'Egypt',         flag: '\u{1F1EA}\u{1F1EC}', x: 1126, y: 756 },
};

// ── Platform defaults for report templates ──
const _RPT_PLATFORM = {
  report: {
    title: 'Facebook Ads Impact Report',
    subtitle: 'How your faithful giving is reaching people through social media',
    kpiSection: 'Reach & Stewardship',
    kpiSub: 'How far your dollars go',
    kpis: [
      { id: 'kpi-reach',         label: 'People Reached',       color: 'teal', ctx: 'across your target regions',  dv: '*fb_insights',        field: 'insight_reach',  agg: 'sum' },
      { id: 'kpi-spend',         label: 'Total Investment',     color: 'gold', ctx: 'supporter-funded ad spend',   dv: '*fb_insights',        field: 'insight_spend',  agg: 'sum', isCurrency: true },
      { id: 'kpi-cpc',           label: 'Cost Per Conversation',color: 'coral',ctx: 'cost efficiency metric',      computed: true },
      { id: 'kpi-conversations', label: 'Unique Conversations', color: 'navy', ctx: 'real people, real dialogue',  dv: '*echo_conversations', field: 'client_id',      agg: 'cardinality' },
    ],
    computedKpi: { id: 'kpi-cpc', numerator: 'kpi-spend', denominator: 'kpi-conversations' },
    funnel: [
      { id: 'funnel-impressions',   label: 'Impressions',          dv: '*fb_insights',        field: 'insight_impressions',        agg: 'sum' },
      { id: 'funnel-reach',         label: 'People Reached',       dv: '*fb_insights',        field: 'insight_reach',              agg: 'sum' },
      { id: 'funnel-clicks',        label: 'Link Clicks',          dv: '*fb_insights',        field: 'insight_inline_link_clicks', agg: 'sum' },
      { id: 'funnel-conversations', label: 'Unique Conversations', dv: '*echo_conversations', field: 'client_id',                  agg: 'cardinality' },
    ],
    geo: { dv: '*fb_insights', field: 'insight_reach', agg: 'sum', xField: 'team_country' },
    trends: [
      { id: 'trend-reach',         label: 'People Reached',       color: '#2a8a7a', dv: '*fb_insights',        field: 'insight_reach',              agg: 'sum' },
      { id: 'trend-clicks',        label: 'Link Clicks',          color: '#c9953c', dv: '*fb_insights',        field: 'insight_inline_link_clicks', agg: 'sum' },
      { id: 'trend-conversations', label: 'Unique Conversations', color: '#d4654a', dv: '*echo_conversations', field: 'client_id',                  agg: 'cardinality' },
    ],
  },
  combined_report: {
    title: 'Combined Digital Impact Report',
    subtitle: 'Facebook Ads and Google Ads performance at a glance',
    kpiSection: 'Reach & Performance',
    kpiSub: 'Combined results across all platforms',
    kpis: [
      { id: 'kpi-reach',         label: 'Total Impressions',  color: 'teal', ctx: 'Facebook + Google combined',   sources: [{ dv: '*fb_insights', field: 'insight_impressions', agg: 'sum' }, { dv: '*google_ads', field: 'impressions', agg: 'sum' }] },
      { id: 'kpi-spend',         label: 'Total Investment',   color: 'gold', ctx: 'supporter-funded ad spend',    sources: [{ dv: '*fb_insights', field: 'insight_spend', agg: 'sum' }, { dv: '*google_ads', field: 'cost', agg: 'sum' }], isCurrency: true },
      { id: 'kpi-cpc',           label: 'Cost Per Response',  color: 'coral',ctx: 'cost efficiency metric',       computed: true },
      { id: 'kpi-conversations', label: 'Total Responses',    color: 'navy', ctx: 'conversations + app installs', sources: [{ dv: '*echo_conversations', field: 'client_id', agg: 'cardinality' }, { dv: '*google_ads', field: 'app_installs', agg: 'sum' }] },
    ],
    computedKpi: { id: 'kpi-cpc', numerator: 'kpi-spend', denominator: 'kpi-conversations' },
    funnel: [
      { id: 'funnel-impressions', label: 'Impressions', sources: [{ dv: '*fb_insights', field: 'insight_impressions', agg: 'sum' }, { dv: '*google_ads', field: 'impressions', agg: 'sum' }] },
      { id: 'funnel-clicks',      label: 'Clicks',      sources: [{ dv: '*fb_insights', field: 'insight_inline_link_clicks', agg: 'sum' }, { dv: '*google_ads', field: 'clicks', agg: 'sum' }] },
      { id: 'funnel-responses',   label: 'Responses',   sources: [{ dv: '*echo_conversations', field: 'client_id', agg: 'cardinality' }, { dv: '*google_ads', field: 'app_installs', agg: 'sum' }] },
    ],
    geo: { dv: '*fb_insights', field: 'insight_reach', agg: 'sum', xField: 'team_country' },
    trends: [
      { id: 'trend-impressions', label: 'Impressions', color: '#2a8a7a', sources: [{ dv: '*fb_insights', field: 'insight_impressions', agg: 'sum' }, { dv: '*google_ads', field: 'impressions', agg: 'sum' }] },
      { id: 'trend-clicks',      label: 'Clicks',      color: '#c9953c', sources: [{ dv: '*fb_insights', field: 'insight_inline_link_clicks', agg: 'sum' }, { dv: '*google_ads', field: 'clicks', agg: 'sum' }] },
      { id: 'trend-responses',   label: 'Responses',   color: '#d4654a', sources: [{ dv: '*echo_conversations', field: 'client_id', agg: 'cardinality' }, { dv: '*google_ads', field: 'app_installs', agg: 'sum' }] },
    ],
  },
  google_report: {
    title: 'Google Ads Impact Report',
    subtitle: 'How your faithful giving is reaching people through Google advertising',
    kpiSection: 'Reach & Performance',
    kpiSub: 'How far your dollars go',
    kpis: [
      { id: 'kpi-reach',         label: 'Impressions',          color: 'teal', ctx: 'across your target regions',  dv: '*google_ads', field: 'impressions',   agg: 'sum' },
      { id: 'kpi-spend',         label: 'Total Investment',     color: 'gold', ctx: 'supporter-funded ad spend',   dv: '*google_ads', field: 'cost',          agg: 'sum', isCurrency: true },
      { id: 'kpi-cpc',           label: 'Cost Per Install',     color: 'coral',ctx: 'cost efficiency metric',      computed: true },
      { id: 'kpi-conversations', label: 'App Installs',         color: 'navy', ctx: 'real app downloads',          dv: '*google_ads', field: 'app_installs',  agg: 'sum' },
    ],
    computedKpi: { id: 'kpi-cpc', numerator: 'kpi-spend', denominator: 'kpi-conversations' },
    funnel: [
      { id: 'funnel-impressions',   label: 'Impressions',   dv: '*google_ads', field: 'impressions',   agg: 'sum' },
      { id: 'funnel-clicks',        label: 'Clicks',        dv: '*google_ads', field: 'clicks',        agg: 'sum' },
      { id: 'funnel-conversions',   label: 'Conversions',   dv: '*google_ads', field: 'conversions',   agg: 'sum' },
      { id: 'funnel-installs',      label: 'App Installs',  dv: '*google_ads', field: 'app_installs',  agg: 'sum' },
    ],
    geo: { dv: '*google_ads', field: 'app_installs', agg: 'sum', xField: 'geo_country_code', filters: [{ field: 'record_type', op: 'match_phrase', value: 'geo' }] },
    trends: [
      { id: 'trend-reach',    label: 'Impressions',   color: '#2a8a7a', dv: '*google_ads', field: 'impressions',  agg: 'sum' },
      { id: 'trend-clicks',   label: 'Clicks',        color: '#c9953c', dv: '*google_ads', field: 'clicks',       agg: 'sum' },
      { id: 'trend-installs', label: 'App Installs',  color: '#d4654a', dv: '*google_ads', field: 'app_installs', agg: 'sum' },
    ],
  },
};

async function renderReportChart(chartId) {
  const config = (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE.charts[chartId]) || null;
  if (!config) return;
  const area = document.getElementById(`chart-${chartId}`);
  if (!area) return;

  // paint a Loading placeholder immediately, like every other
  // chart renderer (std charts, grouped_table, canva, map all do this).
  // The Impact Report builds a large HTML template synchronously and only
  // injects it after computing the date label + metric layout, so without
  // this the slot sat BLANK (not even a spinner) for ~5s on a switch —
  // unlike other dashboards which show "Loading…" in the slot.
  area.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;min-height:200px;color:#aaa;font-size:13px;">Loading…</div>';

  const platform = _RPT_PLATFORM[config.chart_type] || _RPT_PLATFORM.report;
  const _ = (key) => _rptId(chartId, key);
  const orgName = config.org_name || 'Your Organization';
  const ov = config.metric_overrides || {};
  const _lbl = (id, def) => ov[id]?.label || def;
  const hidden = new Set(config.hidden_metrics || []);

  // Check if a KPI should be hidden — directly hidden, or computed with hidden deps
  const comp = platform.computedKpi || {};
  const _kpiHidden = (k) => {
    if (hidden.has(k.id)) return true;
    if (k.computed && comp.id === k.id) {
      return hidden.has(comp.numerator) || hidden.has(comp.denominator);
    }
    return false;
  };

  // Resolve current date range for display
  const tr = (typeof window !== 'undefined' && window.DASHBOARD_TIME_RANGE) || {};
  let dateLabel = 'All Time';
  if (tr.from) {
    const SHORT_MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    const fmtDate = (s) => {
      const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s) : null;
      if (d && !isNaN(d)) return `${SHORT_MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
      // Parse "now-Xd/d" style
      const m = String(s).match(/^now-(\d+)([dMy])(?:\/[dMy])?$/i);
      if (m) {
        const n = parseInt(m[1], 10), u = m[2].toLowerCase(), dt = new Date();
        if (u === 'd') dt.setDate(dt.getDate() - n);
        else if (u === 'm') dt.setMonth(dt.getMonth() - n);
        else if (u === 'y') dt.setFullYear(dt.getFullYear() - n);
        return `${SHORT_MONTHS[dt.getMonth()]} ${dt.getDate()}, ${dt.getFullYear()}`;
      }
      if (/^now$/i.test(s)) { const dt = new Date(); return `${SHORT_MONTHS[dt.getMonth()]} ${dt.getDate()}, ${dt.getFullYear()}`; }
      return s;
    };
    dateLabel = `${fmtDate(tr.from)} \u2014 ${fmtDate(tr.to || 'now')}`;
  }

  area.innerHTML = `
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@400;500;600;700&display=swap');
  .rpt { --rpt-navy:#1a2744; --rpt-gold:#c9953c; --rpt-gold-lt:#e8c77b; --rpt-teal:#2a8a7a; --rpt-coral:#d4654a;
         --rpt-bg:#f0ece4; --rpt-card:#fff; --rpt-border:#e2ddd4; --rpt-txt:#1a2744; --rpt-txt2:#5a6a80; --rpt-muted:#8da0b8;
         font-family:'DM Sans',sans-serif; background:var(--rpt-bg); color:var(--rpt-txt); padding:0; }
  .rpt * { box-sizing:border-box; margin:0; padding:0; }

  /* Header */
  .rpt-header { background:var(--rpt-navy); color:#fff; padding:40px 36px 32px; position:relative; overflow:hidden; border-radius:16px 16px 0 0; }
  .rpt-header::before { content:''; position:absolute; top:-60%; right:-10%; width:500px; height:500px; background:radial-gradient(circle,rgba(201,149,60,0.15) 0%,transparent 70%); border-radius:50%; }
  .rpt-org { font-size:11px; letter-spacing:3px; text-transform:uppercase; color:var(--rpt-gold-lt); margin-bottom:10px; font-weight:600; position:relative; z-index:1; }
  .rpt-title { font-family:'DM Serif Display',serif; font-size:36px; font-weight:400; line-height:1.15; margin-bottom:6px; position:relative; z-index:1; }
  .rpt-subtitle { font-size:14px; color:rgba(255,255,255,0.6); position:relative; z-index:1; }
  .rpt-date { display:inline-block; margin-top:16px; padding:6px 16px; background:rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.12); border-radius:100px; font-size:12px; color:rgba(255,255,255,0.7); font-weight:500; position:relative; z-index:1; }

  /* Body */
  .rpt-body { padding:28px 36px 40px; }

  /* Section headers */
  .rpt-sec-hdr { display:flex; align-items:center; gap:12px; margin-bottom:16px; margin-top:32px; }
  .rpt-sec-hdr:first-child { margin-top:0; }
  .rpt-sec-icon { width:36px; height:36px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:16px; }
  .rpt-sec-icon.reach { background:rgba(42,138,122,0.12); }
  .rpt-sec-icon.funnel { background:rgba(201,149,60,0.12); }
  .rpt-sec-icon.geo { background:rgba(212,101,74,0.12); }
  .rpt-sec-icon.trend { background:rgba(26,39,68,0.1); }
  .rpt-sec-title { font-family:'DM Serif Display',serif; font-size:22px; font-weight:400; }
  .rpt-sec-sub { font-size:12px; color:var(--rpt-txt2); }

  /* KPI grid */
  .rpt-kpi-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; }
  .rpt-kpi { background:var(--rpt-card); border-radius:14px; padding:20px; box-shadow:0 2px 16px rgba(26,39,68,0.06); border:1px solid var(--rpt-border); position:relative; overflow:hidden; }
  .rpt-kpi::after { content:''; position:absolute; top:0; left:0; right:0; height:3px; }
  .rpt-kpi.gold::after { background:var(--rpt-gold); }
  .rpt-kpi.teal::after { background:var(--rpt-teal); }
  .rpt-kpi.coral::after { background:var(--rpt-coral); }
  .rpt-kpi.navy::after { background:var(--rpt-navy); }
  .rpt-kpi-label { font-size:11px; text-transform:uppercase; letter-spacing:1.5px; color:var(--rpt-muted); font-weight:600; margin-bottom:8px; }
  .rpt-kpi-val { font-family:'DM Serif Display',serif; font-size:32px; line-height:1; margin-bottom:4px; }
  .rpt-kpi-ctx { font-size:12px; color:var(--rpt-txt2); }
  .rpt-kpi-ctx .hl { font-weight:700; color:var(--rpt-teal); }

  /* Funnel */
  .rpt-funnel { background:var(--rpt-card); border-radius:14px; padding:28px; box-shadow:0 2px 16px rgba(26,39,68,0.06); border:1px solid var(--rpt-border); }
  .rpt-funnel-steps { display:flex; align-items:stretch; }
  .rpt-funnel-step { flex:1; text-align:center; position:relative; padding:16px 12px; }
  .rpt-funnel-bar-wrap { height:110px; display:flex; align-items:flex-end; justify-content:center; margin-bottom:14px; }
  .rpt-funnel-bar { width:65%; border-radius:8px 8px 4px 4px; min-height:10px; transition:height 0.6s cubic-bezier(0.22,1,0.36,1); }
  .rpt-funnel-bar.s1 { background:linear-gradient(180deg,#1a2744,#2a3d66); }
  .rpt-funnel-bar.s2 { background:linear-gradient(180deg,#2a8a7a,#348f80); }
  .rpt-funnel-bar.s3 { background:linear-gradient(180deg,#c9953c,#d4a84e); }
  .rpt-funnel-bar.s4 { background:linear-gradient(180deg,#d4654a,#da7560); }
  .rpt-funnel-bar.s5 { background:linear-gradient(180deg,#6b5ce7,#8577ed); }
  .rpt-funnel-val { font-family:'DM Serif Display',serif; font-size:24px; margin-bottom:3px; }
  .rpt-funnel-lbl { font-size:12px; color:var(--rpt-txt2); font-weight:500; }
  .rpt-funnel-rate { font-size:10px; font-weight:700; color:var(--rpt-teal); margin-top:3px; }
  .rpt-funnel-arrow { position:absolute; right:-10px; top:50%; transform:translateY(-50%); color:var(--rpt-muted); font-size:18px; z-index:2; opacity:0.4; }

  /* Map */
  .rpt-map { background:var(--rpt-card); border-radius:14px; padding:28px; box-shadow:0 2px 16px rgba(26,39,68,0.06); border:1px solid var(--rpt-border); }
  .rpt-map-grid { display:grid; grid-template-columns:1fr 260px; gap:24px; align-items:start; }
  .rpt-map-svg { width:100%; height:auto; border-radius:10px; }
  .rpt-country-list { display:flex; flex-direction:column; gap:10px; }
  .rpt-country-row { display:flex; align-items:center; gap:10px; padding:10px 14px; background:var(--rpt-bg); border-radius:8px; }
  .rpt-country-flag { font-size:20px; width:32px; height:32px; display:flex; align-items:center; justify-content:center; background:#fff; border-radius:6px; }
  .rpt-country-info { flex:1; }
  .rpt-country-name { font-weight:600; font-size:13px; margin-bottom:1px; }
  .rpt-country-reach { font-size:11px; color:var(--rpt-txt2); }
  .rpt-country-bar-bg { width:100%; height:4px; background:var(--rpt-border); border-radius:2px; margin-top:4px; overflow:hidden; }
  .rpt-country-bar-fill { height:100%; border-radius:2px; background:var(--rpt-teal); }

  /* Trend chart */
  .rpt-chart { background:var(--rpt-card); border-radius:14px; padding:28px; box-shadow:0 2px 16px rgba(26,39,68,0.06); border:1px solid var(--rpt-border); }
  .rpt-legend { display:flex; gap:20px; margin-bottom:20px; flex-wrap:wrap; }
  .rpt-legend-item { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--rpt-txt2); font-weight:500; }
  .rpt-legend-dot { width:8px; height:8px; border-radius:50%; }

  /* Stewardship banner */
  .rpt-steward { background:var(--rpt-navy); color:#fff; border-radius:14px; padding:28px 36px; margin-top:32px; display:flex; align-items:center; gap:32px; position:relative; overflow:hidden; }
  .rpt-steward::before { content:''; position:absolute; right:-50px; top:-50px; width:200px; height:200px; background:radial-gradient(circle,rgba(201,149,60,0.2),transparent 70%); }
  .rpt-steward-stat { text-align:center; flex:1; position:relative; z-index:1; }
  .rpt-steward-big { font-family:'DM Serif Display',serif; font-size:36px; color:var(--rpt-gold-lt); line-height:1; margin-bottom:4px; }
  .rpt-steward-lbl { font-size:12px; opacity:0.7; }
  .rpt-steward-div { width:1px; height:50px; background:rgba(255,255,255,0.15); }

  @media (max-width:900px) {
    .rpt-kpi-grid { grid-template-columns:repeat(2,1fr); }
    .rpt-map-grid { grid-template-columns:1fr; }
    .rpt-steward { flex-direction:column; gap:16px; }
    .rpt-steward-div { width:50px; height:1px; }
  }
  @media print {
    .rpt *, .rpt *::before, .rpt *::after { -webkit-print-color-adjust:exact!important; print-color-adjust:exact!important; }

    @page { size: landscape; margin: 0.5cm; }
    .rpt { font-size: 11px; overflow: visible !important; }
    .rpt-header { padding: 16px 24px; }
    .rpt-org { font-size: 13px; }
    .rpt-title { font-size: 22px; }
    .rpt-subtitle { font-size: 10px; }
    .rpt-date { font-size: 10px; }
    .rpt-body { padding: 10px 20px; }
    .rpt-sec-hdr { margin-top: 14px !important; margin-bottom: 8px; }
    .rpt-sec-title { font-size: 16px; }
    .rpt-sec-sub { font-size: 10px; }
    .rpt-sec-icon { width: 28px; height: 28px; font-size: 13px; }
    .rpt-kpi-grid { gap: 10px; }
    .rpt-kpi { padding: 10px 14px; }
    .rpt-kpi-label { font-size: 11px; }
    .rpt-kpi-val { font-size: 28px; }
    .rpt-kpi-ctx { font-size: 10px; }
    .rpt-funnel { padding: 14px; }
    .rpt-funnel-bar-wrap { height: 70px; }
    .rpt-funnel-val { font-size: 18px; }
    .rpt-funnel-lbl { font-size: 10px; }
    .rpt-funnel-rate { font-size: 8px; }
    .rpt-map { padding: 14px; }
    .rpt-map-svg { min-height: 120px !important; }
    .rpt-chart { padding: 14px; break-inside: avoid; page-break-inside: avoid; }
    .rpt-funnel { break-inside: avoid; page-break-inside: avoid; }
    .rpt-map { break-inside: avoid; page-break-inside: avoid; }
    .rpt-page-break { break-before: page; page-break-before: always; }
    .rpt-steward { padding: 14px 20px; margin-top: 14px; }
    .rpt-steward-big { font-size: 24px; }
    .rpt-steward-lbl { font-size: 10px; }
  }
</style>
<div class="rpt">
  <div class="rpt-header">
    <div class="rpt-org">${escHtml(orgName)}</div>
    <div class="rpt-title">${escHtml(platform.title)}</div>
    <div class="rpt-subtitle">${escHtml(platform.subtitle)}</div>
    <div class="rpt-date">${escHtml(dateLabel)}</div>
  </div>
  <div class="rpt-body">

    <!-- KPI Cards -->
    <div id="${_('section-kpi')}" style="${platform.kpis.every(k => _kpiHidden(k)) ? 'display:none;' : ''}">
    <div class="rpt-sec-hdr">
      <div class="rpt-sec-icon reach">\u{1F4E1}</div>
      <div><div class="rpt-sec-title">${escHtml(platform.kpiSection)}</div><div class="rpt-sec-sub">${escHtml(platform.kpiSub)}</div></div>
    </div>
    <div class="rpt-kpi-grid">
      ${platform.kpis.filter(k => !_kpiHidden(k)).map(k => `<div class="rpt-kpi ${k.color}">
        <div class="rpt-kpi-label">${escHtml(_lbl(k.id, k.label))}</div>
        <div class="rpt-kpi-val" id="${_(k.id)}">...</div>
        <div class="rpt-kpi-ctx">${escHtml(k.ctx)}</div>
      </div>`).join('\n      ')}
    </div>
    </div>

    <!-- Funnel -->
    <div id="${_('section-funnel')}" style="${platform.funnel.every(f => hidden.has(f.id)) ? 'display:none;' : ''}">
    <div class="rpt-sec-hdr">
      <div class="rpt-sec-icon funnel">\u{1F3BD}</div>
      <div><div class="rpt-sec-title">The Engagement Journey</div><div class="rpt-sec-sub">From first impression to real conversation</div></div>
    </div>
    <div class="rpt-funnel">
      <div class="rpt-funnel-steps" id="${_('funnel')}"></div>
    </div>
    </div>

    <!-- Geographic Reach -->
    <div id="${_('section-geo')}" style="${hidden.has('geo') ? 'display:none;' : ''}">
    <div class="rpt-sec-hdr rpt-page-break">
      <div class="rpt-sec-icon geo">\u{1F30D}</div>
      <div><div class="rpt-sec-title">Geographic Reach</div><div class="rpt-sec-sub">Where your message is landing</div></div>
    </div>
    <div class="rpt-map">
      <div class="rpt-map-grid">
        <div>
          <div id="${_('map-container')}" class="rpt-map-svg" style="min-height:200px;display:flex;align-items:center;justify-content:center;color:#aaa;font-size:13px;">Loading map...</div>
        </div>
        <div class="rpt-country-list" id="${_('country-list')}">
          <div style="color:#aaa;font-size:12px;padding:12px;">Loading...</div>
        </div>
      </div>
    </div>
    </div>

    <!-- Monthly Trends -->
    <div id="${_('section-trend')}" style="${platform.trends.every(t => hidden.has(t.id)) ? 'display:none;' : ''}">
    <div class="rpt-sec-hdr rpt-page-break">
      <div class="rpt-sec-icon trend">\u{1F4C8}</div>
      <div><div class="rpt-sec-title">Monthly Trends</div><div class="rpt-sec-sub">Growth and cost efficiency over time</div></div>
    </div>
    <div class="rpt-chart" id="${_('trend-container')}">
      <div style="color:#aaa;font-size:13px;padding:20px;text-align:center;">Loading trends...</div>
    </div>
    </div>

  </div>
</div>`;

  // ── Metric definitions ──
  const _m = (id, dv, field, agg, sources) => {
    const o = ov[id];
    if (sources && !o?.dv) return { id, sources };  // multi-source, no user override
    return { id, dv: o?.dv || dv, field: o?.field || field, agg: o?.agg || agg };
  };

  // All default funnel steps (order matters) — driven by platform
  const allFunnelSteps = platform.funnel.map(f => ({
    id: f.id, label: _lbl(f.id, f.label), dv: f.dv, field: f.field, agg: f.agg, sources: f.sources,
  }));
  const funnelColors = ['#1a2744','#2a8a7a','#c9953c','#d4654a','#6b5ce7'];
  const visibleFunnel = allFunnelSteps.filter(s => !hidden.has(s.id));

  // Build funnel HTML dynamically
  const funnelContainer = document.getElementById(_rptId(chartId, 'funnel'));
  if (funnelContainer) {
    funnelContainer.innerHTML = visibleFunnel.map((step, i) => {
      const color = funnelColors[i % funnelColors.length];
      const pct = visibleFunnel.length > 1 ? Math.max(100 - i * (90 / (visibleFunnel.length - 1)), 6) : 100;
      const arrow = i > 0 ? '<div class="rpt-funnel-arrow">\u25BA</div>' : '';
      return `<div class="rpt-funnel-step">${arrow}<div class="rpt-funnel-bar-wrap"><div class="rpt-funnel-bar" style="height:${pct}%;background:linear-gradient(180deg,${color},${color}cc)"></div></div><div class="rpt-funnel-val" id="${escHtml(_rptId(chartId, step.id))}">...</div><div class="rpt-funnel-lbl">${escHtml(step.label)}</div></div>`;
    }).join('');
  }

  const metrics = [
    ...platform.kpis.filter(k => !k.computed).map(k => _m(k.id, k.dv, k.field, k.agg, k.sources)),
    ...visibleFunnel.map(s => _m(s.id, s.dv, s.field, s.agg, s.sources)),
  ].filter(m => !hidden.has(m.id));

  // Collect raw values for computed metrics
  const rawValues = {};

  // Helper: build a single metric-source config (same request shape the
  // old per-source fetches sent)
  const _rptBuildConfig = (dv, field, agg) =>
    _withExtraFilters({ chart_type: 'metric', data_view: dv, y_fields: [field], aggregation: agg, filters: [] });

  // ONE batch request for all metric sources. The per-source
  // /api/query fan-out (one round-trip per KPI/funnel source through the
  // browser's per-origin connection limit, each re-resolving authz scope
  // server-side) is the same bottleneck the canva summary report had.
  // Multi-source metrics flatten into consecutive batch slots; `tasks`
  // maps each metric back to its slot range so sources sum per metric.
  const tasks = [];
  const queries = [];
  metrics.forEach(m => {
    const sources = m.sources || [m];
    tasks.push({ m, start: queries.length, count: sources.length });
    sources.forEach(s => queries.push(_rptBuildConfig(s.dv, s.field, s.agg)));
  });

  let results = [];
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/query_batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ queries }),
    });
    const data = await res.json();
    results = Array.isArray(data.results) ? data.results : [];
  } catch (e) {
    // results stays empty \u2014 every metric falls through to the em-dash below.
  }

  tasks.forEach(({ m, start, count }) => {
    const el = document.getElementById(_rptId(chartId, m.id));
    if (!el) return;

    let failed = false;
    let val = 0;
    for (let i = start; i < start + count; i++) {
      const r = results[i];
      if (!r || r.success === false) { failed = true; break; }
      val += (r.value ?? r.doc_count ?? 0);
    }
    if (failed) {
      el.textContent = '\u2014';
      rawValues[m.id] = 0;
      return;
    }
    rawValues[m.id] = val;

    // Format display
    const kpiDef = platform.kpis.find(k => k.id === m.id);
    if (kpiDef?.isCurrency) {
      el.textContent = '$' + (typeof val === 'number' ? val.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 }) : val);
    } else {
      el.textContent = formatNumber(val);
    }
  });

  // ── Computed KPIs ──
  if (platform.computedKpi) {
    const ck = platform.computedKpi;
    const num = rawValues[ck.numerator] || 0;
    const den = rawValues[ck.denominator] || 0;
    const ckEl = document.getElementById(_rptId(chartId, ck.id));
    if (ckEl) ckEl.textContent = den > 0 ? '$' + (num / den).toFixed(2) : '\u2014';
  }

  // ── Update funnel bars proportionally ──
  const funnelVals = visibleFunnel.map(s => rawValues[s.id] || 0);
  // scale against the actual max — funnelVals[0] is conceptually the
  // top-of-funnel (impressions), but a team with 0 impressions and non-zero
  // downstream metrics (e.g. Echo conversations from a different data view)
  // would otherwise produce bars >> 100% (visually "infinitely tall").
  const maxFunnel = Math.max(...funnelVals, 1);
  const funnelStepEls = document.querySelectorAll(`#${CSS.escape(_rptId(chartId, 'funnel'))} .rpt-funnel-step`);
  funnelStepEls.forEach((step, i) => {
    const bar = step.querySelector('.rpt-funnel-bar');
    if (bar) bar.style.height = Math.max((funnelVals[i] / maxFunnel) * 100, 6) + '%';
    // Add conversion rates. Only render when this is a valid funnel transition
    // (downstream <= upstream). cross-data-view funnels (e.g. FB clicks
    // \u2192 Echo conversations) can produce downstream > upstream, which would
    // display nonsense rates like "5000.0% conversion".
    if (i > 0 && funnelVals[i - 1] > 0 && funnelVals[i] <= funnelVals[i - 1]) {
      let rateEl = step.querySelector('.rpt-funnel-rate');
      if (!rateEl) {
        rateEl = document.createElement('div');
        rateEl.className = 'rpt-funnel-rate';
        step.appendChild(rateEl);
      }
      rateEl.textContent = '\u2192 ' + ((funnelVals[i] / funnelVals[i - 1]) * 100).toFixed(1) + '% conversion';
    }
  });

  // reveal contract: signal that the report's primary content (KPIs +
  // funnel) has painted, so the parent dashboards page reveals the viewer
  // iframe now instead of waiting out its 4s fallback. Like the canva report,
  // renderReportChart rendered its DOM but never dispatched 'chart-rendered',
  // so a report-only dashboard sat hidden ~5s then popped in. The geo/trends
  // queries below are secondary (they populate the map/trend strip
  // asynchronously) and must not gate the reveal.
  const reportHasData = Object.values(rawValues).some(v => typeof v === 'number' && isFinite(v) && v !== 0);
  try {
    document.dispatchEvent(new CustomEvent('chart-rendered', { detail: { chartId, hasData: reportHasData } }));
  } catch (_e) { /* event hook is best-effort */ }

  // ── Geographic query (terms aggregation on team_country) ──
  _rptQueryGeo(chartId, config, platform);

  // ── Trends query (monthly date histogram) ──
  _rptQueryTrends(chartId, config, platform);
}

async function _rptQueryGeo(chartId, config, platform) {
  const mapContainer = document.getElementById(_rptId(chartId, 'map-container'));
  const listEl = document.getElementById(_rptId(chartId, 'country-list'));
  if (!mapContainer || !listEl) return;

  // Toggle location list visibility based on config
  const showGeoList = config.show_geo_list !== false;
  listEl.style.display = showGeoList ? '' : 'none';
  const gridEl = listEl.parentElement;
  if (gridEl) gridEl.style.gridTemplateColumns = showGeoList ? '1fr 260px' : '1fr';

  // ── Step 1: Fetch SVG and inject it inline (guarantees dot alignment) ──
  try {
    const mapUrl = DASHBOARD_BASE + '../static/dashboard_builder/map_with_countries.svg';
    const svgResp = await fetch(mapUrl);
    const svgText = await svgResp.text();

    // Parse SVG and inject into container
    const parser = new DOMParser();
    const svgDoc = parser.parseFromString(svgText, 'image/svg+xml');
    const svgEl = svgDoc.querySelector('svg');
    if (!svgEl) { mapContainer.textContent = 'Failed to load map'; return; }

    // Style the SVG to fit the container
    svgEl.style.width = '100%';
    svgEl.style.height = 'auto';
    svgEl.style.borderRadius = '10px';
    svgEl.style.display = 'block';

    // Add a dots group inside the SVG itself — same coordinate space, zero alignment issues
    const dotsGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    dotsGroup.id = _rptId(chartId, 'map-dots');
    svgEl.appendChild(dotsGroup);

    mapContainer.innerHTML = '';
    mapContainer.style.minHeight = '';
    mapContainer.style.display = '';
    mapContainer.style.alignItems = '';
    mapContainer.style.justifyContent = '';
    mapContainer.style.color = '';
    mapContainer.style.fontSize = '';
    mapContainer.appendChild(svgEl);
  } catch (e) {
    mapContainer.textContent = 'Failed to load map';
    return;
  }

  const dotsEl = document.getElementById(_rptId(chartId, 'map-dots'));
  if (!dotsEl) return;

  // ── Helper: convert lat/lon to SVG coordinates (2000×1280 viewBox) ──
  // Fitted projection from 10 reference countries on Fla-shop SVG:
  //   x = 5.310 * lon + 969.1
  //   y = -0.01752 * lat² - 5.371 * lat + 914.33
  function _lonLatToSvg(lon, lat) {
    return {
      x: 5.310 * lon + 969.1,
      y: -0.01752 * lat * lat - 5.371 * lat + 914.33,
    };
  }

  // ── Step 2: Try city-level data first, fall back to country centroids ──
  const ov = config.metric_overrides || {};
  const geoOv = ov['geo'] || {};

  // Build map-data query params with the same org/team/date filters as other report sections
  const mapParams = new URLSearchParams({ statistic: 'reach' });
  const extra = (typeof window !== 'undefined' && window.DASHBOARD_EXTRA_FILTERS) || [];
  for (const f of extra) {
    if (f.field === 'organization_title' && f.value) mapParams.set('org', f.value);
    if (f.field === 'team_title') {
      if (f.op === 'match_phrase' && f.value) mapParams.append('team', f.value);
      else if (f.op === 'in' && Array.isArray(f.value)) f.value.forEach(t => mapParams.append('team', t));
    }
  }
  const timeRange = (typeof window !== 'undefined' && window.DASHBOARD_TIME_RANGE) || {};
  if (timeRange.from) mapParams.set('date_from', _resolveDateMath(timeRange.from) || timeRange.from);
  if (timeRange.to)   mapParams.set('date_to',   _resolveDateMath(timeRange.to, true) || timeRange.to);

  try {
    // ── Try city-level geo data (cache-only) ──
    // Falls back to the country-centroid path below on any failure or if
    // the cache has no resolved cities yet.
    let cities = [];
    try {
      // follow-up: append the publish token for the public viewer
      // so /public/api/map-data/cities can resolve the dashboard scope.
      const _publicToken = (typeof window !== 'undefined' && window.PUBLIC_REPORT_TOKEN) || '';
      let _ipCitiesUrl = DASHBOARD_BASE + 'api/map-data/cities?' + mapParams.toString();
      if (_publicToken) _ipCitiesUrl += '&token=' + encodeURIComponent(_publicToken);
      const mapData = await fetchJsonWithTimeout(_ipCitiesUrl);
      if (mapData.success) cities = mapData.cities || [];
    } catch (_) { /* fall through to country fallback */ }

    if (cities.length > 0) {
      // ── City-level rendering ──
      const sorted = cities.sort((a, b) => (b.value || 0) - (a.value || 0)).slice(0, 30);

      // Only plot dots for cities that have coordinates — uniform size since
      // reach is a document-level metric, not per-city
      const withCoords = sorted.filter(c => c.lat != null && c.lon != null);
      const r = 20;
      dotsEl.innerHTML = withCoords.map(c => {
        const { x, y } = _lonLatToSvg(c.lon, c.lat);
        return `
        <circle cx="${x}" cy="${y}" r="${r}" fill="rgba(232,76,61,0.75)" stroke="white" stroke-width="3">
          <animate attributeName="r" values="${r};${r + 5};${r}" dur="3s" repeatCount="indefinite"/>
        </circle>
        <circle cx="${x}" cy="${y}" r="${r + 16}" fill="none" stroke="rgba(232,76,61,0.35)" stroke-width="2">
          <animate attributeName="r" values="${r + 8};${r + 24};${r + 8}" dur="3s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="0.5;0;0.5" dur="3s" repeatCount="indefinite"/>
        </circle>`;
      }).join('');

      // Sidebar: show city + country (no reach numbers — FB reach is per-ad, not per-city)
      const _countryName = (code) => {
        if (!code || code.toLowerCase() === 'unknown') return '';
        const meta = _RPT_COUNTRIES[code];
        return meta ? meta.name : code;
      };

      listEl.innerHTML = sorted.slice(0, 8).map(c => {
        const country = _countryName(c.country);
        const label = country ? `${c.name}, ${country}` : c.name;
        return `
        <div class="rpt-country-row">
          <div class="rpt-country-flag">\u{1F4CD}</div>
          <div class="rpt-country-info">
            <div class="rpt-country-name">${escHtml(label)}</div>
          </div>
        </div>`;
      }).join('');
      return;
    }

    // ── Fallback: country-centroid rendering (no city-level data available) ──
    const geoDef = platform.geo || _RPT_PLATFORM.report.geo;
    const dv = geoOv.dv || geoDef.dv;
    const field = geoOv.field || geoDef.field;
    const agg = geoOv.agg || geoDef.agg;

    const geoXField = geoDef.xField || 'team_country';
    const geoFilters = geoDef.filters || [];
    const queryConfig = _withExtraFilters({
      chart_type: 'bar',
      data_view: dv,
      x_field: geoXField,
      y_fields: [field],
      aggregation: agg,
      filters: [...geoFilters],
    });

    const res = await fetch(DASHBOARD_BASE + 'api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(queryConfig),
    });
    const data = await res.json();

    const categories = data.xAxis?.data || [];
    const values = data.series?.[0]?.data || [];

    // Build entries and merge duplicates (e.g. EG + Cairo both map to "Egypt")
    const merged = {};
    categories.forEach((code, i) => {
      // skip the "unknown" sentinel emitted by the importer when a
      // team has no team_country configured (get_insights.py:760). Otherwise
      // a single "Unknown" row swallows every team_country=="unknown" doc and
      // misleads viewers into thinking the geo data is real.
      if (!code || String(code).toLowerCase() === 'unknown') return;
      const meta = _RPT_COUNTRIES[code] || null;
      const name = meta ? meta.name : code;
      const reach = values[i] || 0;
      if (reach <= 0) return;
      if (merged[name]) {
        merged[name].reach += reach;
      } else {
        merged[name] = {
          code, name, reach,
          flag: meta ? meta.flag : '\u{1F30D}',
          x: meta ? meta.x : 0,
          y: meta ? meta.y : 0,
          hasCoords: !!meta && meta.x > 0,
        };
      }
    });
    const countries = Object.values(merged).sort((a, b) => b.reach - a.reach).slice(0, 15);

    if (countries.length === 0) {
      listEl.innerHTML = '<div style="color:#aaa;font-size:12px;padding:12px;">No geographic data available</div>';
      return;
    }

    const maxReach = countries[0].reach;

    // Render map dots (sized for 2000x1280 viewBox)
    dotsEl.innerHTML = countries.filter(c => c.hasCoords).map(c => {
      const r = Math.max(18, (c.reach / maxReach) * 60);
      return `
      <circle cx="${c.x}" cy="${c.y}" r="${r}" fill="rgba(232,76,61,0.75)" stroke="white" stroke-width="4">
        <animate attributeName="r" values="${r};${r + 6};${r}" dur="3s" repeatCount="indefinite"/>
      </circle>
      <circle cx="${c.x}" cy="${c.y}" r="${r + 20}" fill="none" stroke="rgba(232,76,61,0.35)" stroke-width="3">
        <animate attributeName="r" values="${r + 10};${r + 30};${r + 10}" dur="3s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="0.5;0;0.5" dur="3s" repeatCount="indefinite"/>
      </circle>`;
    }).join('');

    // Render country list (top 8 for sidebar space)
    listEl.innerHTML = countries.slice(0, 8).map(c => `
      <div class="rpt-country-row">
        <div class="rpt-country-flag">${c.flag}</div>
        <div class="rpt-country-info">
          <div class="rpt-country-name">${escHtml(c.name)}</div>
          <div class="rpt-country-reach">${c.reach.toLocaleString()} people reached</div>
          <div class="rpt-country-bar-bg"><div class="rpt-country-bar-fill" style="width:${(c.reach / maxReach) * 100}%"></div></div>
        </div>
      </div>
    `).join('');
  } catch (e) {
    listEl.innerHTML = '<div style="color:#c0392b;font-size:12px;padding:12px;">Failed to load geographic data</div>';
  }
}

async function _rptQueryTrends(chartId, config, platform) {
  const container = document.getElementById(_rptId(chartId, 'trend-container'));
  if (!container) return;

  const ov = config.metric_overrides || {};
  const hidden = new Set(config.hidden_metrics || []);
  const _tLbl = (id, def) => ov[id]?.label || def;
  const trendDefs = platform.trends || _RPT_PLATFORM.report.trends;
  const allSeries = trendDefs.map(t => ({
    id: t.id,
    name: _tLbl(t.id, t.label),
    color: t.color,
    dv: ov[t.id]?.dv || t.dv,
    field: ov[t.id]?.field || t.field,
    agg: ov[t.id]?.agg || t.agg,
    sources: t.sources,
  }));
  const series = allSeries.filter(s => !hidden.has(s.id));

  if (series.length === 0) {
    container.innerHTML = '<div style="color:#aaa;font-size:13px;padding:20px;text-align:center;">No trend metrics selected</div>';
    return;
  }

  // Helper: query one trend source
  const _queryTrendSource = async (dv, field, agg) => {
    const baseConfig = {
      chart_type: 'line', data_view: dv, x_field: 'kibana_date', x_field_type: 'date',
      y_fields: [field], aggregation: agg, calendar_interval: 'week',
      date_range: { field: 'kibana_date', from: 'now-3M/d', to: 'now' }, filters: [],
    };
    const res = await fetch(DASHBOARD_BASE + 'api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_withExtraFilters(baseConfig)),
    });
    return res.json();
  };

  // Query each series — multi-source series query each source and sum values per bucket
  const results = await Promise.allSettled(series.map(async (s) => {
    if (s.sources && !ov[s.id]?.dv) {
      const subResults = await Promise.all(s.sources.map(src => _queryTrendSource(src.dv, src.field, src.agg)));
      // Merge: union all labels, sum values at each label
      const labelSet = new Map();
      for (const sub of subResults) {
        const labels = sub.xAxis?.data || [];
        const vals = sub.series?.[0]?.data || [];
        labels.forEach((lbl, i) => { labelSet.set(lbl, (labelSet.get(lbl) || 0) + (vals[i] || 0)); });
      }
      const sortedLabels = [...labelSet.keys()].sort();
      return { xAxis: { data: sortedLabels }, series: [{ data: sortedLabels.map(l => labelSet.get(l)) }] };
    }
    return _queryTrendSource(s.dv, s.field, s.agg);
  }));

  // Extract labels and values
  let longestLabels = [];
  const seriesData = results.map((result, i) => {
    if (result.status === 'fulfilled' && !result.value.error) {
      const data = result.value;
      const labels = data.xAxis?.data || [];
      const values = data.series?.[0]?.data || [];
      if (labels.length > longestLabels.length) longestLabels = labels;
      return { ...series[i], values, labels };
    }
    return { ...series[i], values: [], labels: [] };
  });

  if (longestLabels.length === 0) {
    container.innerHTML = '<div style="color:#aaa;font-size:13px;padding:20px;text-align:center;">No trend data available</div>';
    return;
  }

  // ── Render small multiples ──
  const svgW = 800, rowH = 120, labelW = 50, rightPad = 15;
  const totalH = rowH * seriesData.length;
  const chartW = svgW - labelW - rightPad;

  container.innerHTML = `<svg viewBox="0 0 ${svgW} ${totalH}" style="width:100%;height:auto;"></svg>`;
  const svg = container.querySelector('svg');
  let svgContent = '';

  seriesData.forEach((s, si) => {
    const yOff = si * rowH;
    const pad = { top: 22, bottom: 28 };
    const cH = rowH - pad.top - pad.bottom;

    // Align values to longestLabels
    const vals = longestLabels.map(lbl => {
      const idx = s.labels.indexOf(lbl);
      return idx >= 0 ? (s.values[idx] || 0) : 0;
    });
    const maxVal = (Math.max(...vals, 1)) * 1.15;
    const numPts = longestLabels.length;

    // Background
    svgContent += `<rect x="0" y="${yOff}" width="${svgW}" height="${rowH}" fill="${si % 2 === 0 ? '#faf8f5' : '#ffffff'}" rx="0"/>`;

    // Series label + peak value
    const peak = Math.max(...vals, 0);
    const peakLabel = peak >= 1000 ? (peak / 1000).toFixed(1).replace(/\.0$/, '') + 'K' : String(peak);
    svgContent += `<text x="${labelW}" y="${yOff + 14}" fill="${escHtml(s.color)}" font-size="12" font-weight="600" font-family="DM Sans">${escHtml(s.name)}</text>`;
    svgContent += `<text x="${svgW - rightPad}" y="${yOff + 14}" fill="#8da0b8" font-size="11" font-family="DM Sans" text-anchor="end">peak: ${peakLabel}</text>`;

    // Y-axis: 2 grid lines (0 and max)
    const yBottom = yOff + pad.top + cH;
    const yTop = yOff + pad.top;
    svgContent += `<line x1="${labelW}" y1="${yBottom}" x2="${svgW - rightPad}" y2="${yBottom}" stroke="#e2ddd4" stroke-width="0.5"/>`;
    svgContent += `<line x1="${labelW}" y1="${yTop}" x2="${svgW - rightPad}" y2="${yTop}" stroke="#e2ddd4" stroke-width="0.5" stroke-dasharray="3,3"/>`;
    // Y labels
    const maxLabel = maxVal >= 1000 ? (maxVal / 1000).toFixed(0) + 'K' : Math.round(maxVal);
    svgContent += `<text x="${labelW - 6}" y="${yBottom + 3}" text-anchor="end" fill="#8da0b8" font-size="9" font-family="DM Sans">0</text>`;
    svgContent += `<text x="${labelW - 6}" y="${yTop + 3}" text-anchor="end" fill="#8da0b8" font-size="9" font-family="DM Sans">${maxLabel}</text>`;

    // Data line + area
    const points = vals.map((v, i) => {
      const x = labelW + (chartW / Math.max(numPts - 1, 1)) * i;
      const y = yOff + pad.top + cH - ((v || 0) / maxVal) * cH;
      return `${x},${y}`;
    });
    svgContent += `<polygon points="${labelW},${yBottom} ${points.join(' ')} ${labelW + chartW},${yBottom}" fill="${s.color}" opacity="0.08"/>`;
    svgContent += `<polyline points="${points.join(' ')}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
    // Dots
    vals.forEach((v, i) => {
      const x = labelW + (chartW / Math.max(numPts - 1, 1)) * i;
      const y = yOff + pad.top + cH - ((v || 0) / maxVal) * cH;
      svgContent += `<circle cx="${x}" cy="${y}" r="3" fill="white" stroke="${s.color}" stroke-width="1.5"/>`;
    });

    // X labels (only on last row)
    if (si === seriesData.length - 1) {
      // Show ~6 evenly spaced labels max for readability
      const step = Math.max(1, Math.ceil(numPts / 6));
      longestLabels.forEach((lbl, i) => {
        if (i % step !== 0 && i !== numPts - 1) return;
        const x = labelW + (chartW / Math.max(numPts - 1, 1)) * i;
        const display = typeof lbl === 'string' ? _applyDateFormat(lbl, 'MMM dd') || lbl : lbl;
        svgContent += `<text x="${x}" y="${yOff + rowH - 4}" text-anchor="middle" fill="#8da0b8" font-size="10" font-family="DM Sans">${escHtml(String(display))}</text>`;
      });
    }
  });

  svg.innerHTML = svgContent;
}
