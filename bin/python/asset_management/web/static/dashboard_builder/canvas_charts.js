/**
 * live geo (world map) and trend chart objects for canvas pages.
 *
 * ONE shared file loaded by all three canvas surfaces (canvas.html editor,
 * canvas_view.html iframe viewer, public_report/canvas_viewer.html) — the
 * same sharing pattern as dashboard_filters.js, so unlike the per-page
 * renderer sets there is nothing to keep byte-identical by hand.
 *
 * The renderers are ports of the Digital Impact Report's geo/trends
 * sections in charts.js (_rptQueryGeo / _rptQueryTrends) restated over
 * canvas chart objects:
 *
 *   { object_type: "chart",
 *     chart: {
 *       kind: "geo",                      // world map + location list
 *       statistic: "reach",               //   map-data/cities statistic
 *       show_list: true,                  //   right-hand location column
 *       geo: {data_view, field, aggregation, x_field, filters}  // country fallback
 *     } | {
 *       kind: "trend",                    // small-multiples line charts
 *       series: [{id, label, color, data_view, field, aggregation} |
 *                {id, label, color, sources: [{data_view, field, aggregation}]}]
 *     } }
 *
 * Liveness contract: nothing is baked — every render queries
 * through the page's transport (ctx.apiBase [+ ctx.publicToken]) and merges
 * the SAME three live-filter layers bindingQuery applies (org/team extra
 * filters dashboard filters with the '__ALL__' rebind, global time
 * range). The page scripts re-invoke window.cvRenderChartInto from their
 * resolveAllFields, so charts follow every filter/time change.
 *
 * The trend window defaults to the report's fixed last-3-months and is
 * OVERRIDDEN by the page's live time range when one is set — a canvas page
 * is a live document, unlike the static report template.
 *
 * Safety: all dynamic text is escaped (_cvcEsc) before entering the SVG /
 * list markup; the map SVG itself is our own static asset.
 */
(function () {
  'use strict';

  // ── Country centroid table — MIRROR of _RPT_COUNTRIES in charts.js
  // (canvas pages never load charts.js). A release-plan test pins the two
  // tables' entries equal: change one, change both. ──
  const _CVC_COUNTRIES = {
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

  const _CVC_DOT = 'rgba(232,76,61,0.75)';
  const _CVC_DOT_RING = 'rgba(232,76,61,0.35)';

  // Clear a node's content while PRESERVING editor chrome (selection/resize
  // handles carry .cv-chrome) — the editor re-renders charts in place, and
  // a plain innerHTML wipe would strip the selected object's handles.
  function _cvcClear(node) {
    Array.from(node.childNodes).forEach((c) => {
      if (c.nodeType === 1 && c.classList && c.classList.contains('cv-chrome')) return;
      c.remove();
    });
  }

  function _cvcSetHtml(node, html) {
    _cvcClear(node);
    const tpl = document.createElement('div');
    tpl.innerHTML = html; // our own generated markup; dynamic values escaped
    while (tpl.firstChild) node.appendChild(tpl.firstChild);
  }

  function _cvcEsc(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  // Fitted lat/lon → SVG projection for the 2000×1280 Fla-shop map — same
  // constants as charts.js _lonLatToSvg.
  function _cvcLonLatToSvg(lon, lat) {
    return {
      x: 5.310 * lon + 969.1,
      y: -0.01752 * lat * lat - 5.371 * lat + 914.33,
    };
  }

  // Client-side date-math resolution for the cities endpoint (it takes ISO
  // dates) — same rules as charts.js _resolveDateMath.
  function _cvcResolveDateMath(expr, upperBound) {
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
    if (unit === 'd') d.setDate(d.getDate() - n);
    else if (unit === 'm') d.setMonth(d.getMonth() - n);
    else if (unit === 'y') d.setFullYear(d.getFullYear() - n);
    return d.toISOString().slice(0, 10);
  }

  // The same three live-filter layers bindingQuery merges:
  // org/team extra filters, dashboard filters ('__ALL__' rebound to the
  // query's own data view), and the global time range (only when the config
  // didn't set its own date_range).
  function _cvcWithLiveFilters(config) {
    const q = { ...config, filters: [...(config.filters || [])] };
    const extra = window.DASHBOARD_EXTRA_FILTERS || [];
    if (extra.length) q.filters = q.filters.concat(extra);
    const dfs = (window.DASHBOARD_STATE && Array.isArray(window.DASHBOARD_STATE.filters))
      ? window.DASHBOARD_STATE.filters : [];
    if (dfs.length && q.data_view) {
      q.dashboard_filters = dfs.map((f) => (
        (f && f.data_view === window.DASHBOARD_FILTER_ALL_DV)
          ? { ...f, data_view: q.data_view }
          : f
      ));
    }
    const tr = window.DASHBOARD_TIME_RANGE || {};
    if (!q.date_range && (tr.from || tr.to) && q.data_view) {
      const dv = q.data_view || '';
      const isDt = dv.includes('disciple-tools') || dv.includes('disciple_tools');
      q.date_range = {
        field: isDt ? 'date_created_ms' : 'kibana_date',
        from: tr.from || null,
        to: tr.to || null,
      };
    }
    return q;
  }

  async function _cvcQuery(ctx, config) {
    const body = ctx.publicToken ? { ...config, token: ctx.publicToken } : config;
    const res = await fetch(ctx.apiBase + 'query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': ctx.csrf || '' },
      body: JSON.stringify(body),
    });
    return res.json();
  }

  // Card chrome from the object's style — the seeded sections sit on white
  // cards like the report's .rpt-map / .rpt-chart.
  function _cvcApplyCardStyle(node, style) {
    style = style || {};
    node.style.background = style.bg_color || '#ffffff';
    node.style.borderRadius = ((style.radius === undefined || style.radius === null) ? 14 : Number(style.radius) || 0) + 'px';
    node.style.border = '1px solid #e2ddd4';
    node.style.padding = ((style.padding === undefined || style.padding === null) ? 16 : Number(style.padding) || 0) + 'px';
    node.style.boxSizing = 'border-box';
    node.style.fontFamily = "-apple-system, 'Segoe UI', Roboto, sans-serif";
  }

  // ── Geo: world map + location list (port of _rptQueryGeo) ──
  async function _cvcRenderGeo(node, obj, ctx) {
    const chart = obj.chart || {};
    const showList = chart.show_list !== false;
    _cvcClear(node);
    _cvcApplyCardStyle(node, obj.style);
    const grid = document.createElement('div');
    grid.style.display = 'grid';
    grid.style.gridTemplateColumns = showList ? '1fr 220px' : '1fr';
    grid.style.gap = '16px';
    grid.style.alignItems = 'start';
    grid.style.height = '100%';
    node.appendChild(grid);
    const mapHost = document.createElement('div');
    mapHost.style.color = '#aaa';
    mapHost.style.fontSize = '13px';
    mapHost.textContent = 'Loading map…';
    grid.appendChild(mapHost);
    let listEl = null;
    if (showList) {
      listEl = document.createElement('div');
      listEl.style.display = 'flex';
      listEl.style.flexDirection = 'column';
      listEl.style.gap = '10px';
      listEl.style.overflow = 'hidden';
      listEl.style.maxHeight = '100%';
      listEl.innerHTML = '<div style="color:#aaa;font-size:12px;">Loading…</div>';
      grid.appendChild(listEl);
    }

    // Step 1: inject the map SVG inline (dots share its coordinate space).
    let dotsEl = null;
    try {
      const svgResp = await fetch(ctx.mapUrl);
      const svgText = await svgResp.text();
      const svgDoc = new DOMParser().parseFromString(svgText, 'image/svg+xml');
      const svgEl = svgDoc.querySelector('svg');
      if (!svgEl) throw new Error('bad svg');
      svgEl.style.width = '100%';
      svgEl.style.height = 'auto';
      svgEl.style.borderRadius = '10px';
      svgEl.style.display = 'block';
      dotsEl = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      svgEl.appendChild(dotsEl);
      mapHost.textContent = '';
      mapHost.style.color = '';
      mapHost.style.fontSize = '';
      mapHost.appendChild(svgEl);
    } catch (err) {
      mapHost.textContent = 'Failed to load map';
      return;
    }

    // Step 2: city-level data first…
    const params = new URLSearchParams({ statistic: chart.statistic || 'reach' });
    const extra = window.DASHBOARD_EXTRA_FILTERS || [];
    for (const f of extra) {
      if (f.field === 'organization_title' && f.value) params.set('org', f.value);
      if (f.field === 'team_title') {
        if (f.op === 'match_phrase' && f.value) params.append('team', f.value);
        else if ((f.op === 'in' || f.op === 'match_phrase_any') && Array.isArray(f.value)) {
          f.value.forEach((t) => params.append('team', t));
        }
      }
    }
    const tr = window.DASHBOARD_TIME_RANGE || {};
    if (tr.from) params.set('date_from', _cvcResolveDateMath(tr.from) || tr.from);
    if (tr.to) params.set('date_to', _cvcResolveDateMath(tr.to, true) || tr.to);
    if (ctx.publicToken) params.set('token', ctx.publicToken);

    try {
      let cities = [];
      let mapDetail = 'city';
      try {
        const res = await fetch(ctx.apiBase + 'map-data/cities?' + params.toString());
        const data = await res.json();
        if (data.success) cities = data.cities || [];
        // the public cities endpoint reports the publish's choice.
        if (data && typeof data.map_detail === 'string') mapDetail = data.map_detail;
      } catch (err) { /* fall through to the country fallback */ }

      // a hidden map shows a notice and never runs the country
      // fallback query either — no geography reaches the browser.
      if (mapDetail === 'hidden') {
        mapHost.textContent = 'The publisher has hidden the map on this shared report.';
        mapHost.style.color = '#6b7280';
        mapHost.style.fontSize = '13px';
        if (listEl) listEl.innerHTML = '';
        return;
      }

      if (cities.length > 0) {
        const sorted = cities.sort((a, b) => (b.value || 0) - (a.value || 0)).slice(0, 30);
        const withCoords = sorted.filter((c) => c.lat != null && c.lon != null);
        const r = 20;
        dotsEl.innerHTML = withCoords.map((c) => {
          const p = _cvcLonLatToSvg(c.lon, c.lat);
          return '<circle cx="' + p.x + '" cy="' + p.y + '" r="' + r + '" fill="' + _CVC_DOT + '" stroke="white" stroke-width="3">'
            + '<animate attributeName="r" values="' + r + ';' + (r + 5) + ';' + r + '" dur="3s" repeatCount="indefinite"/></circle>'
            + '<circle cx="' + p.x + '" cy="' + p.y + '" r="' + (r + 16) + '" fill="none" stroke="' + _CVC_DOT_RING + '" stroke-width="2">'
            + '<animate attributeName="r" values="' + (r + 8) + ';' + (r + 24) + ';' + (r + 8) + '" dur="3s" repeatCount="indefinite"/>'
            + '<animate attributeName="opacity" values="0.5;0;0.5" dur="3s" repeatCount="indefinite"/></circle>';
        }).join('');
        if (listEl) {
          listEl.innerHTML = sorted.slice(0, 8).map((c) => {
            const meta = (c.country && String(c.country).toLowerCase() !== 'unknown') ? _CVC_COUNTRIES[c.country] : null;
            const country = meta ? meta.name : ((c.country && String(c.country).toLowerCase() !== 'unknown') ? c.country : '');
            const label = country ? c.name + ', ' + country : c.name;
            return '<div style="display:flex;gap:8px;align-items:center;">'
              + '<div style="font-size:16px;">\u{1F4CD}</div>'
              + '<div style="font-size:12px;font-weight:600;color:#1a2744;">' + _cvcEsc(label) + '</div></div>';
          }).join('');
        }
        return;
      }

      // …fall back to country centroids from the binding stored on the object.
      const geo = chart.geo || {};
      const data = await _cvcQuery(ctx, _cvcWithLiveFilters({
        chart_type: 'bar',
        data_view: geo.data_view,
        x_field: geo.x_field || 'team_country',
        y_fields: [geo.field],
        aggregation: geo.aggregation || 'sum',
        filters: [...(geo.filters || [])],
      }));
      const categories = (data.xAxis && data.xAxis.data) || [];
      const values = (data.series && data.series[0] && data.series[0].data) || [];
      const merged = {};
      categories.forEach((code, i) => {
        // skip the importer's "unknown" sentinel.
        if (!code || String(code).toLowerCase() === 'unknown') return;
        const meta = _CVC_COUNTRIES[code] || null;
        const name = meta ? meta.name : code;
        const reach = values[i] || 0;
        if (reach <= 0) return;
        if (merged[name]) {
          merged[name].reach += reach;
        } else {
          merged[name] = {
            name: name,
            reach: reach,
            flag: meta ? meta.flag : '\u{1F30D}',
            x: meta ? meta.x : 0,
            y: meta ? meta.y : 0,
            hasCoords: !!meta && meta.x > 0,
          };
        }
      });
      const countries = Object.values(merged).sort((a, b) => b.reach - a.reach).slice(0, 15);
      if (!countries.length) {
        if (listEl) listEl.innerHTML = '<div style="color:#aaa;font-size:12px;">No geographic data available</div>';
        return;
      }
      const maxReach = countries[0].reach;
      dotsEl.innerHTML = countries.filter((c) => c.hasCoords).map((c) => {
        const r = Math.max(18, (c.reach / maxReach) * 60);
        return '<circle cx="' + c.x + '" cy="' + c.y + '" r="' + r + '" fill="' + _CVC_DOT + '" stroke="white" stroke-width="4">'
          + '<animate attributeName="r" values="' + r + ';' + (r + 6) + ';' + r + '" dur="3s" repeatCount="indefinite"/></circle>'
          + '<circle cx="' + c.x + '" cy="' + c.y + '" r="' + (r + 20) + '" fill="none" stroke="' + _CVC_DOT_RING + '" stroke-width="3">'
          + '<animate attributeName="r" values="' + (r + 10) + ';' + (r + 30) + ';' + (r + 10) + '" dur="3s" repeatCount="indefinite"/>'
          + '<animate attributeName="opacity" values="0.5;0;0.5" dur="3s" repeatCount="indefinite"/></circle>';
      }).join('');
      if (listEl) {
        listEl.innerHTML = countries.slice(0, 8).map((c) => (
          '<div style="display:flex;gap:8px;align-items:flex-start;">'
          + '<div style="font-size:16px;">' + c.flag + '</div>'
          + '<div style="flex:1;min-width:0;">'
          + '<div style="font-size:12px;font-weight:600;color:#1a2744;">' + _cvcEsc(c.name) + '</div>'
          + '<div style="font-size:11px;color:#5a6a80;">' + c.reach.toLocaleString() + ' people reached</div>'
          + '<div style="height:4px;background:#f0ece4;border-radius:2px;margin-top:3px;">'
          + '<div style="height:4px;border-radius:2px;background:#c9953c;width:' + ((c.reach / maxReach) * 100) + '%"></div></div>'
          + '</div></div>'
        )).join('');
      }
    } catch (err) {
      if (listEl) listEl.innerHTML = '<div style="color:#c0392b;font-size:12px;">Failed to load geographic data</div>';
    }
  }

  // ── Trend: small-multiples line charts (port of _rptQueryTrends) ──
  function _cvcTrendDateLabel(lbl) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(lbl));
    if (!m) return String(lbl);
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return MONTHS[parseInt(m[2], 10) - 1] + ' ' + m[3];
  }

  // `stale` gates every post-await write to the shared node: geo's late
  // writes only touch its own (by then detached) children, but trend
  // replaces node.innerHTML — without the guard a slow superseded render
  // would clobber the newer one.
  async function _cvcRenderTrend(node, obj, ctx, stale) {
    const chart = obj.chart || {};
    const series = Array.isArray(chart.series) ? chart.series : [];
    _cvcApplyCardStyle(node, obj.style);
    if (!series.length) {
      _cvcSetHtml(node, '<div style="color:#aaa;font-size:13px;padding:20px;text-align:center;">No trend metrics selected</div>');
      return;
    }
    _cvcSetHtml(node, '<div style="color:#aaa;font-size:13px;padding:20px;text-align:center;">Loading trends…</div>');

    // The report's fixed last-3-months window, overridden by the page's live
    // time range when one is set (a canvas page is a live document).
    const tr = window.DASHBOARD_TIME_RANGE || {};
    const dateRange = (tr.from || tr.to)
      ? { field: 'kibana_date', from: tr.from || null, to: tr.to || null }
      : { field: 'kibana_date', from: 'now-3M/d', to: 'now' };

    // a series may carry {field, value} row filters (same shape
    // as data-field binding filters); they map to eq clauses here.
    const seriesFilters = (s) => (Array.isArray(s.filters) ? s.filters : [])
      .filter((f) => f && f.field && f.value !== undefined && f.value !== null && f.value !== '')
      .map((f) => ({ field: f.field, op: 'eq', value: f.value }));

    const queryOne = (dv, field, agg, filters) => _cvcQuery(ctx, _cvcWithLiveFilters({
      chart_type: 'line',
      data_view: dv,
      x_field: 'kibana_date',
      x_field_type: 'date',
      y_fields: [field],
      aggregation: agg || 'sum',
      calendar_interval: 'week',
      date_range: { ...dateRange },
      filters: filters || [],
    }));

    const results = await Promise.allSettled(series.map(async (s) => {
      if (Array.isArray(s.sources) && s.sources.length) {
        const subs = await Promise.all(s.sources.map((src) => queryOne(src.data_view, src.field, src.aggregation, seriesFilters(s))));
        const labelSet = new Map();
        for (const sub of subs) {
          const labels = (sub.xAxis && sub.xAxis.data) || [];
          const vals = (sub.series && sub.series[0] && sub.series[0].data) || [];
          labels.forEach((lbl, i) => labelSet.set(lbl, (labelSet.get(lbl) || 0) + (vals[i] || 0)));
        }
        const sortedLabels = [...labelSet.keys()].sort();
        return { xAxis: { data: sortedLabels }, series: [{ data: sortedLabels.map((l) => labelSet.get(l)) }] };
      }
      return queryOne(s.data_view, s.field, s.aggregation, seriesFilters(s));
    }));

    if (stale && stale()) return;
    let longestLabels = [];
    const seriesData = results.map((result, i) => {
      if (result.status === 'fulfilled' && !result.value.error) {
        const labels = (result.value.xAxis && result.value.xAxis.data) || [];
        const values = (result.value.series && result.value.series[0] && result.value.series[0].data) || [];
        if (labels.length > longestLabels.length) longestLabels = labels;
        return { ...series[i], values: values, labels: labels };
      }
      return { ...series[i], values: [], labels: [] };
    });
    if (!longestLabels.length) {
      _cvcSetHtml(node, '<div style="color:#aaa;font-size:13px;padding:20px;text-align:center;">No trend data available</div>');
      return;
    }

    // Small multiples — same geometry as the report (800-wide viewBox,
    // 120px rows, area + line + dots, peak label, x labels on the last row).
    const svgW = 800;
    const rowH = 120;
    const labelW = 50;
    const rightPad = 15;
    const totalH = rowH * seriesData.length;
    const chartW = svgW - labelW - rightPad;
    let svgContent = '';
    seriesData.forEach((s, si) => {
      const yOff = si * rowH;
      const padT = 22;
      const padB = 28;
      const cH = rowH - padT - padB;
      const vals = longestLabels.map((lbl) => {
        const idx = s.labels.indexOf(lbl);
        return idx >= 0 ? (s.values[idx] || 0) : 0;
      });
      const maxVal = Math.max(...vals, 1) * 1.15;
      const numPts = longestLabels.length;
      const color = _cvcEsc(s.color || '#2a8a7a');
      svgContent += '<rect x="0" y="' + yOff + '" width="' + svgW + '" height="' + rowH + '" fill="' + (si % 2 === 0 ? '#faf8f5' : '#ffffff') + '"/>';
      const peak = Math.max(...vals, 0);
      const peakLabel = peak >= 1000 ? (peak / 1000).toFixed(1).replace(/\.0$/, '') + 'K' : String(peak);
      svgContent += '<text x="' + labelW + '" y="' + (yOff + 14) + '" fill="' + color + '" font-size="12" font-weight="600" font-family="DM Sans, sans-serif">' + _cvcEsc(s.label || s.id || '') + '</text>';
      svgContent += '<text x="' + (svgW - rightPad) + '" y="' + (yOff + 14) + '" fill="#8da0b8" font-size="11" font-family="DM Sans, sans-serif" text-anchor="end">peak: ' + peakLabel + '</text>';
      const yBottom = yOff + padT + cH;
      const yTop = yOff + padT;
      svgContent += '<line x1="' + labelW + '" y1="' + yBottom + '" x2="' + (svgW - rightPad) + '" y2="' + yBottom + '" stroke="#e2ddd4" stroke-width="0.5"/>';
      svgContent += '<line x1="' + labelW + '" y1="' + yTop + '" x2="' + (svgW - rightPad) + '" y2="' + yTop + '" stroke="#e2ddd4" stroke-width="0.5" stroke-dasharray="3,3"/>';
      const maxLabel = maxVal >= 1000 ? (maxVal / 1000).toFixed(0) + 'K' : Math.round(maxVal);
      svgContent += '<text x="' + (labelW - 6) + '" y="' + (yBottom + 3) + '" text-anchor="end" fill="#8da0b8" font-size="9" font-family="DM Sans, sans-serif">0</text>';
      svgContent += '<text x="' + (labelW - 6) + '" y="' + (yTop + 3) + '" text-anchor="end" fill="#8da0b8" font-size="9" font-family="DM Sans, sans-serif">' + maxLabel + '</text>';
      const points = vals.map((v, i) => {
        const x = labelW + (chartW / Math.max(numPts - 1, 1)) * i;
        const y = yOff + padT + cH - ((v || 0) / maxVal) * cH;
        return x + ',' + y;
      });
      svgContent += '<polygon points="' + labelW + ',' + yBottom + ' ' + points.join(' ') + ' ' + (labelW + chartW) + ',' + yBottom + '" fill="' + color + '" opacity="0.08"/>';
      svgContent += '<polyline points="' + points.join(' ') + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>';
      vals.forEach((v, i) => {
        const x = labelW + (chartW / Math.max(numPts - 1, 1)) * i;
        const y = yOff + padT + cH - ((v || 0) / maxVal) * cH;
        svgContent += '<circle cx="' + x + '" cy="' + y + '" r="3" fill="white" stroke="' + color + '" stroke-width="1.5"/>';
      });
      if (si === seriesData.length - 1) {
        const step = Math.max(1, Math.ceil(numPts / 6));
        longestLabels.forEach((lbl, i) => {
          if (i % step !== 0 && i !== numPts - 1) return;
          const x = labelW + (chartW / Math.max(numPts - 1, 1)) * i;
          svgContent += '<text x="' + x + '" y="' + (yOff + rowH - 4) + '" text-anchor="middle" fill="#8da0b8" font-size="10" font-family="DM Sans, sans-serif">' + _cvcEsc(_cvcTrendDateLabel(lbl)) + '</text>';
        });
      }
    });
    if (stale && stale()) return;
    _cvcSetHtml(node, '<svg viewBox="0 0 ' + svgW + ' ' + totalH + '" style="width:100%;height:auto;display:block;">' + svgContent + '</svg>');
  }

  // ── Bar: grouped/split column chart ──
  // chart: { kind: 'bar', x_field, split_field?, series: [{ label, color?,
  //          axis? ('left'|'right'), data_view, field, aggregation,
  //          equals_value?, filters? }] }
  // Two query modes, mirroring the server's build_query capabilities:
  //   * split_field (exactly one series) → ONE query with breakdown —
  //     the transformer returns one series per split value;
  //   * multi-metric → one query per series (trend's pattern), categories
  //     unioned client-side.
  // A series with axis:'right' scales against its own right-hand max —
  // hand-rolled dual axis (nothing upstream supports one; we own the SVG).
  const _CVC_BAR_COLORS = ['#2a8a7a', '#c9953c', '#d4654a', '#6b5ce7', '#1a2744', '#8da0b8'];

  function _cvcFmtVal(v) {
    const n = Number(v) || 0;
    if (Math.abs(n) >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
    if (Math.abs(n) >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
    return String(Math.round(n * 100) / 100);
  }

  async function _cvcRenderBar(node, obj, ctx, stale) {
    const chart = obj.chart || {};
    const series = Array.isArray(chart.series) ? chart.series : [];
    _cvcApplyCardStyle(node, obj.style);
    if (!series.length || !chart.x_field) {
      _cvcSetHtml(node, '<div style="color:#aaa;font-size:13px;padding:20px;text-align:center;">No bar chart metrics selected</div>');
      return;
    }
    _cvcSetHtml(node, '<div style="color:#aaa;font-size:13px;padding:20px;text-align:center;">Loading chart…</div>');

    const tr = window.DASHBOARD_TIME_RANGE || {};
    const dateRange = (tr.from || tr.to)
      ? { field: 'kibana_date', from: tr.from || null, to: tr.to || null }
      : { field: 'kibana_date', from: 'now-3M/d', to: 'now' };
    const bindingFilters = (s) => (Array.isArray(s.filters) ? s.filters : [])
      .filter((f) => f && f.field && f.value !== undefined && f.value !== null && f.value !== '')
      .map((f) => ({ field: f.field, op: 'eq', value: f.value }));

    // One display series = { label, color, axis, byCat: Map(category -> value) }.
    let drawSeries = [];
    const catSet = [];
    const addCat = (c) => { if (catSet.indexOf(c) < 0) catSet.push(c); };

    if (chart.split_field) {
      const s = series[0];
      const data = await _cvcQuery(ctx, _cvcWithLiveFilters({
        chart_type: 'bar',
        data_view: s.data_view,
        x_field: chart.x_field,
        y_field: s.field,
        y_fields: [s.field],
        aggregation: s.aggregation || 'sum',
        equals_value: s.equals_value,
        breakdown: chart.split_field,
        date_range: { ...dateRange },
        filters: bindingFilters(s),
      }));
      const cats = (data.xAxis && data.xAxis.data) || [];
      cats.forEach(addCat);
      (data.series || []).forEach((sd, i) => {
        const byCat = new Map();
        cats.forEach((c, ci) => byCat.set(c, (sd.data && sd.data[ci]) || 0));
        drawSeries.push({
          label: sd.name || 'Series ' + (i + 1),
          color: _CVC_BAR_COLORS[i % _CVC_BAR_COLORS.length],
          axis: 'left',
          byCat: byCat,
        });
      });
    } else {
      const results = await Promise.allSettled(series.map((s) => _cvcQuery(ctx, _cvcWithLiveFilters({
        chart_type: 'bar',
        data_view: s.data_view,
        x_field: chart.x_field,
        y_fields: [s.field],
        aggregation: s.aggregation || 'sum',
        equals_value: s.equals_value,
        date_range: { ...dateRange },
        filters: bindingFilters(s),
      }))));
      drawSeries = results.map((result, i) => {
        const s = series[i];
        const byCat = new Map();
        if (result.status === 'fulfilled' && !result.value.error) {
          const cats = (result.value.xAxis && result.value.xAxis.data) || [];
          const vals = (result.value.series && result.value.series[0] && result.value.series[0].data) || [];
          cats.forEach((c, ci) => { addCat(c); byCat.set(c, vals[ci] || 0); });
        }
        return {
          label: s.label || s.field,
          color: s.color || _CVC_BAR_COLORS[i % _CVC_BAR_COLORS.length],
          axis: s.axis === 'right' ? 'right' : 'left',
          byCat: byCat,
        };
      });
    }

    if (stale && stale()) return;
    if (!catSet.length) {
      _cvcSetHtml(node, '<div style="color:#aaa;font-size:13px;padding:20px;text-align:center;">No data available for this breakdown</div>');
      return;
    }

    // Geometry: 800-wide viewBox like trend; legend row, plot area, x labels.
    const svgW = 800;
    const svgH = 300;
    const padL = 52;
    const hasRight = drawSeries.some((s) => s.axis === 'right');
    const padR = hasRight ? 52 : 16;
    const padT = 34;
    const padB = 36;
    const plotW = svgW - padL - padR;
    const plotH = svgH - padT - padB;
    const maxOf = (axis) => Math.max(1, ...drawSeries.filter((s) => s.axis === axis)
      .flatMap((s) => catSet.map((c) => s.byCat.get(c) || 0))) * 1.1;
    const maxL = maxOf('left');
    const maxR = hasRight ? maxOf('right') : 1;

    let svg = '';
    // Legend.
    let lx = padL;
    drawSeries.forEach((s) => {
      svg += '<rect x="' + lx + '" y="10" width="10" height="10" rx="2" fill="' + _cvcEsc(s.color) + '"/>';
      svg += '<text x="' + (lx + 14) + '" y="19" fill="#1a2744" font-size="11" font-family="DM Sans, sans-serif">'
        + _cvcEsc(s.label + (s.axis === 'right' ? ' (right)' : '')) + '</text>';
      lx += 24 + (s.label.length + (s.axis === 'right' ? 8 : 0)) * 6;
    });
    // Axes + gridlines (0 / max on each active axis).
    const yBottom = padT + plotH;
    svg += '<line x1="' + padL + '" y1="' + yBottom + '" x2="' + (svgW - padR) + '" y2="' + yBottom + '" stroke="#e2ddd4" stroke-width="1"/>';
    svg += '<line x1="' + padL + '" y1="' + padT + '" x2="' + (svgW - padR) + '" y2="' + padT + '" stroke="#e2ddd4" stroke-width="0.5" stroke-dasharray="3,3"/>';
    svg += '<text x="' + (padL - 6) + '" y="' + (yBottom + 3) + '" text-anchor="end" fill="#8da0b8" font-size="9" font-family="DM Sans, sans-serif">0</text>';
    svg += '<text x="' + (padL - 6) + '" y="' + (padT + 3) + '" text-anchor="end" fill="#8da0b8" font-size="9" font-family="DM Sans, sans-serif">' + _cvcFmtVal(maxL) + '</text>';
    if (hasRight) {
      svg += '<text x="' + (svgW - padR + 6) + '" y="' + (yBottom + 3) + '" fill="#8da0b8" font-size="9" font-family="DM Sans, sans-serif">0</text>';
      svg += '<text x="' + (svgW - padR + 6) + '" y="' + (padT + 3) + '" fill="#8da0b8" font-size="9" font-family="DM Sans, sans-serif">' + _cvcFmtVal(maxR) + '</text>';
    }
    // Bars: per category, one group of n bars.
    const n = drawSeries.length;
    const slotW = plotW / catSet.length;
    const groupW = Math.min(slotW * 0.72, n * 46);
    const barW = groupW / n;
    catSet.forEach((cat, ci) => {
      const x0 = padL + slotW * ci + (slotW - groupW) / 2;
      drawSeries.forEach((s, si) => {
        const v = s.byCat.get(cat) || 0;
        const max = s.axis === 'right' ? maxR : maxL;
        const h = Math.max(v > 0 ? 2 : 0, (v / max) * plotH);
        const x = x0 + si * barW;
        const y = yBottom - h;
        svg += '<rect x="' + x + '" y="' + y + '" width="' + Math.max(barW - 2, 1) + '" height="' + h + '" rx="2" fill="' + _cvcEsc(s.color) + '">'
          + '<title>' + _cvcEsc(cat + ' — ' + s.label + ': ' + (Number(v) || 0).toLocaleString()) + '</title></rect>';
      });
      svg += '<text x="' + (padL + slotW * ci + slotW / 2) + '" y="' + (yBottom + 16) + '" text-anchor="middle" fill="#5a6a80" font-size="10" font-family="DM Sans, sans-serif">'
        + _cvcEsc(String(cat).length > 14 ? String(cat).slice(0, 13) + '…' : String(cat)) + '</text>';
    });

    if (stale && stale()) return;
    _cvcSetHtml(node, '<svg viewBox="0 0 ' + svgW + ' ' + svgH + '" style="width:100%;height:auto;display:block;">' + svg + '</svg>');
  }

  // ── Entry point the page scripts call (and re-call on every
  // resolveAllFields pass). Renders are generation-guarded per node so a
  // stale slow response can never paint over a newer one. ──
  window.cvRenderChartInto = function (node, obj, ctx) {
    const gen = (Number(node.dataset.cvcGen) || 0) + 1;
    node.dataset.cvcGen = String(gen);
    const stale = () => Number(node.dataset.cvcGen) !== gen;
    const kind = (obj.chart && obj.chart.kind) || '';
    let work;
    if (kind === 'geo') work = _cvcRenderGeo(node, obj, ctx);
    else if (kind === 'trend') work = _cvcRenderTrend(node, obj, ctx, stale);
    else if (kind === 'bar') work = _cvcRenderBar(node, obj, ctx, stale);
    else {
      node.textContent = 'Unsupported chart';
      return Promise.resolve();
    }
    return work.catch(() => {
      if (!stale()) _cvcSetHtml(node, '<div style="color:#c0392b;font-size:12px;padding:12px;">Chart failed to load</div>');
    });
  };

  // ── canvas table objects. ──
  //
  // A table object's payload (obj.table) is a verbatim LEGACY table chart
  // config, and it renders through the REAL charts.js pipeline: register the
  // config under DASHBOARD_STATE.charts[object_id], give the object a
  // `chart-<object_id>` container, and let queryAndRender() do everything it
  // does on the builder/viewer pages (api/query fetch via _withExtraFilters,
  // renderTable, sort menu, column resize, totals row, gradient, font size).
  // charts.js is loaded by all three canvas surfaces for exactly this; when
  // it's somehow absent the object degrades to a labeled placeholder rather
  // than a JS error. Liveness contract: nothing is baked — the
  // page scripts re-invoke cvUpdateTableEls from resolveAllFields so tables
  // follow every filter/time change like charts and data fields do.
  //
  // Bare DASHBOARD_STATE resolves to builder.js' lexical const on the editor
  // page and to window.DASHBOARD_STATE on the viewers — the same lookup
  // charts.js itself performs, so both always address the same store.
  window.cvRenderTableInto = function (node, obj) {
    const id = (obj && obj.object_id) || '';
    const config = obj && obj.table;
    if (!id || !config || typeof config !== 'object') {
      node.textContent = 'Table (no config)';
      return;
    }
    if (typeof queryAndRender !== 'function' || typeof DASHBOARD_STATE === 'undefined') {
      node.textContent = 'Table';
      return;
    }
    DASHBOARD_STATE.charts[id] = config;
    let holder = node.querySelector(':scope > .cv-table-holder');
    if (!holder) {
      holder = document.createElement('div');
      holder.className = 'cv-table-holder';
      holder.id = 'chart-' + id;
      // Overflow decision: a table taller/wider than its frame
      // SCROLLS inside the frame — it never clips silently.
      holder.style.width = '100%';
      holder.style.height = '100%';
      holder.style.overflow = 'auto';
      node.insertBefore(holder, node.firstChild);
    }
    if (!config.data_view) {
      holder.innerHTML = '<div style="color:#999;font-size:12px;padding:10px;">Table — pick a Data View in the panel to load data.</div>';
      return;
    }
    // bug fix: the editor/viewer build objects DETACHED and append
    // after (renderPageChildren evaluates buildObjectEl inside appendChild),
    // so a synchronous queryAndRender here looks up `chart-<id>` before the
    // holder is in the document and silently returns — every full re-render
    // (theme apply, page resize, rulers toggle) blanked all tables until the
    // next resolveAllFields pass. One microtask later the whole synchronous
    // rebuild has attached the node; for already-attached callers
    // (cvUpdateTableEls) the defer is a harmless no-op.
    Promise.resolve().then(function () { queryAndRender(id); });
  };

  // Re-render every table object — the page scripts call this from their
  // resolveAllFields next to updateChartEls, so filter/time changes re-query.
  window.cvUpdateTableEls = function (doc, rootEl) {
    if (!doc || !Array.isArray(doc.objects) || !rootEl) return;
    doc.objects.forEach(function (obj) {
      if (!obj || obj.object_type !== 'table') return;
      const el = rootEl.querySelector('[data-object-id="' + obj.object_id + '"]');
      if (el) window.cvRenderTableInto(el, obj);
    });
  };

  // ══ calculated-field formula engine (parse + evaluate). ══
  //
  // ONE implementation shared by the editor and both viewers (this file is
  // loaded by all three canvas surfaces), NOT a per-page byte-identical
  // copy. The grammar is the calculation-column grammar —
  // binary + - * /, unary +/-, parentheses, numeric literals, and
  // identifiers [A-Za-z_][A-Za-z0-9_.]* — and the error messages match
  // builder.js _parseFormulaJS so the two formula editors read the same.
  // This client parser is a UX nicety only: the authoritative gate is
  // services/dashboard_formula.py parse_formula, which the server runs on
  // every canvas save (_validate_canvas_payload).
  //
  // Evaluation contract (AC, same semantics as dashboard_formula
  // evaluate): a missing/non-numeric variable or a division by zero yields
  // null — the renderer maps null to its placeholder ('—'), never NaN.
  window.cvParseFormula = function (formula) {
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
      return { ok: false, error: 'Unexpected character "' + c + '" at position ' + (i + 1) + '.' };
    }
    if (tokens.length === 0) return { ok: false, error: 'Formula is empty.' };

    // Recursive-descent parser building a small eval tree:
    //   {t:'num', v} | {t:'id', name} | {t:'un', op, operand} |
    //   {t:'bin', op, left, right}
    let pos = 0;
    const fields = [];
    const seen = new Set();
    const peek = () => tokens[pos];
    const eat = () => tokens[pos++];

    function parseExpr() {
      let r = parseTerm();
      if (r.error) return r;
      while (peek() && (peek().kind === '+' || peek().kind === '-')) {
        const op = eat().kind;
        const right = parseTerm();
        if (right.error) return right;
        r = { t: 'bin', op: op, left: r, right: right };
      }
      return r;
    }

    function parseTerm() {
      let r = parseFactor();
      if (r.error) return r;
      while (peek() && (peek().kind === '*' || peek().kind === '/')) {
        const op = eat().kind;
        const right = parseFactor();
        if (right.error) return right;
        r = { t: 'bin', op: op, left: r, right: right };
      }
      return r;
    }

    function parseFactor() {
      const t = peek();
      if (!t) return { error: 'Unexpected end of formula.' };
      if (t.kind === '+' || t.kind === '-') {
        const op = eat().kind;
        const operand = parseFactor();
        if (operand.error) return operand;
        return { t: 'un', op: op, operand: operand };
      }
      if (t.kind === '(') {
        eat();
        const inner = parseExpr();
        if (inner.error) return inner;
        const close = eat();
        if (!close || close.kind !== ')') return { error: 'Expected ")" near position ' + ((close && close.pos + 1) || src.length) + '.' };
        return inner;
      }
      if (t.kind === 'num') { eat(); return { t: 'num', v: parseFloat(t.value) }; }
      if (t.kind === 'id') {
        eat();
        if (!seen.has(t.value)) { seen.add(t.value); fields.push(t.value); }
        return { t: 'id', name: t.value };
      }
      return { error: 'Unexpected "' + (t.value || t.kind) + '" at position ' + (t.pos + 1) + '.' };
    }

    const tree = parseExpr();
    if (tree.error) return { ok: false, error: tree.error };
    if (pos < tokens.length) {
      const t = tokens[pos];
      return { ok: false, error: 'Unexpected "' + (t.value || t.kind) + '" at position ' + (t.pos + 1) + '.' };
    }
    return { ok: true, fields: fields, tree: tree };
  };

  window.cvEvalFormula = function (node, values) {
    if (!node || typeof node !== 'object') return null;
    if (node.t === 'num') return isFinite(node.v) ? node.v : null;
    if (node.t === 'id') {
      const v = values ? values[node.name] : undefined;
      return (typeof v === 'number' && isFinite(v)) ? v : null;
    }
    if (node.t === 'un') {
      const operand = window.cvEvalFormula(node.operand, values);
      if (operand === null) return null;
      return node.op === '-' ? -operand : operand;
    }
    if (node.t === 'bin') {
      const left = window.cvEvalFormula(node.left, values);
      const right = window.cvEvalFormula(node.right, values);
      if (left === null || right === null) return null;
      if (node.op === '+') return left + right;
      if (node.op === '-') return left - right;
      if (node.op === '*') return left * right;
      if (node.op === '/') {
        if (right === 0) return null; // div-by-zero → renderer shows the placeholder
        return left / right;
      }
    }
    return null;
  };

  // ══ per-binding time-range override resolver. ══
  //
  // ONE implementation shared by the editor and both viewers (the same
  // sharing pattern as the formula engine above), called by bindingQuery in
  // canvas_editor.js and canvas_view.js. A binding's time_override is:
  //   {preset: ""}                      → all time (no date bound at all)
  //   {preset: "now-7d/d" | ...}        → live date-math window (rolling —
  //                                       the same meaning the option has in
  //                                       the page's time-range dropdown)
  //   {preset: "cal:this_month" | ...}  → calendar window, resolved to an
  //                                       explicit ISO from/to at query time
  //   {preset: "custom", from, to}      → fixed ISO window
  //   {from, to}                        → fixed ISO window (pre-preset shape)
  // Returns the {from, to} range to query with ({} means unbounded). The
  // cal:* math mirrors calendarPresetRange in canvas_editor.js /
  // _calendarPresetRange in dashboards.html.
  window.cvResolveTimeOverride = function (ov) {
    if (!ov || typeof ov !== 'object') return {};
    const preset = typeof ov.preset === 'string' ? ov.preset : null;
    if (preset === null || preset === 'custom') {
      return { from: ov.from || null, to: ov.to || null };
    }
    if (preset === '') return {}; // all time
    if (preset.indexOf('cal:') === 0) {
      const d = new Date();
      const ref = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
      const iso = (x) => x.getUTCFullYear() + '-'
        + String(x.getUTCMonth() + 1).padStart(2, '0') + '-'
        + String(x.getUTCDate()).padStart(2, '0');
      const y = ref.getUTCFullYear();
      const m = ref.getUTCMonth();
      if (preset === 'cal:this_month') {
        return { from: iso(new Date(Date.UTC(y, m, 1))), to: iso(ref) };
      }
      if (preset === 'cal:last_month') {
        return { from: iso(new Date(Date.UTC(y, m - 1, 1))), to: iso(new Date(Date.UTC(y, m, 0))) };
      }
      const qStartMonth = Math.floor(m / 3) * 3;
      if (preset === 'cal:this_quarter') {
        return { from: iso(new Date(Date.UTC(y, qStartMonth, 1))), to: iso(ref) };
      }
      if (preset === 'cal:last_quarter') {
        return { from: iso(new Date(Date.UTC(y, qStartMonth - 3, 1))), to: iso(new Date(Date.UTC(y, qStartMonth, 0))) };
      }
      return {}; // unknown sentinel — fail open to all time, never a crash
    }
    return { from: preset, to: 'now' }; // date-math preset, same as the toolbar
  };
})();
