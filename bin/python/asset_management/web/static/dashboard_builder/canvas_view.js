/**
 * follow-up — read-only canvas viewer.
 *
 * Renders a ``dashboard_type: "canvas"`` document inside the dashboards.html
 * iframe (served by /dashboard-builder/view/<id> when the doc is a canvas).
 * Same contract as view.html: posts {type:'echarts-content-ready'} to the
 * parent once the page has painted so the iframe reveal fires.
 *
 * Keep the object rendering in sync with buildObjectEl in canvas_editor.js
 * (owns consolidating the two into one shared renderer when the
 * published viewer lands). Data fields resolve LIVE through api/query_batch
 * — values are never baked into the document.
 *
 * filtering integration: the parent dashboards.html toolbar
 * passes ?org_filter=/&team_filter=/&time_from=/&time_to= on the iframe URL
 * exactly as it does for view.html; every binding query merges them the same
 * way charts.js _withExtraFilters does. Dashboard-level filters (
 * model, persisted at canvas_config.filters) render as the standard
 * read-only strip via dashboard_filters.js — a value toggle re-resolves
 * every live element in one query_batch (window.onDashboardFilterRequery).
 *
 * published (public) mode: canvas_viewer.html sets
 * window.CVV_PUBLIC_TOKEN before loading this file. The renderer is
 * identical; only the transport changes — the document loads from
 * /public/api/dashboards/<token>, bindings resolve through
 * /public/api/query_batch with the token in the body (the server forces
 * the publish entry's org/team scope; anything client-side is advisory),
 * and the header time-range selector replaces the parent
 * toolbar's URL params as the DASHBOARD_TIME_RANGE source.
 *
 * rich text: text objects may carry ``text.rich`` (structured
 * block/run model, never HTML). cvTextTokens / cvRunEl / cvAppendRuns /
 * cvRenderTextInto / cvApplyTextBoxStyle must stay byte-identical with
 * canvas_editor.js — a formatted text box renders identically in the
 * editor and the published view.
 *
 * shapes (rect / ellipse / line / arrow / callout) and image
 * objects (in-doc data: URI at ``image.src``). cvShapeBorderCss /
 * cvRenderShapeInto / cvRenderImageInto join the byte-identical shared set;
 * images render ONLY through an <img> element, where a crafted SVG's
 * scripts never execute (the server additionally rejects active-content
 * SVG on save).
 *
 * time pill: a text object with ``role: "time_pill"`` always
 * renders the page's CURRENT time range (cvTimeRangeLabel joins the
 * byte-identical shared set) — the parent toolbar's URL params on the
 * authenticated iframe, the selector (or the fixed default) in
 * public mode. Every resolveAllFields pass re-labels the pills.
 */
(function () {
  'use strict';

  // public-token mode (published viewer at /public/report/<t>).
  const PUBLIC_TOKEN = (typeof window !== 'undefined' && window.CVV_PUBLIC_TOKEN) || '';
  // Authenticated page URL is /dashboard-builder/view/<id> → API root is one
  // level up. Public pages address the public_report blueprint absolutely.
  const API_BASE = PUBLIC_TOKEN ? '/public/api/' : '../api/';
  const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const ICON_BASE = document.querySelector('meta[name="cv-icon-base"]')?.content || '';
  const DASHBOARD_ID = document.body.dataset.dashboardId || '';
  const stage = document.getElementById('cvv-stage');

  let DOC = null;

  // ── globals dashboard_filters.js reads at call time. Canvas
  // pages never load charts.js, so the '__ALL__' sentinel and DASHBOARD_BASE
  // are defined here (window properties resolve the same bare references).
  // (The public template does not load dashboard_filters.js — its value
  // endpoints are authenticated — but the globals are harmless there.)
  window.DASHBOARD_FILTER_ALL_DV = '__ALL__';
  window.DASHBOARD_FILTERS_READONLY = true;
  window.DASHBOARD_BASE = PUBLIC_TOKEN ? '/public/' : '../';
  window.DASHBOARD_STATE = { id: DASHBOARD_ID, charts: {}, global_date_range: null, filters: [] };
  // table objects render through charts.js queryAndRender, whose
  // _withExtraFilters injects this token into every /public/api/query body
  // (the same public transport the legacy published viewer uses).
  if (PUBLIC_TOKEN) window.PUBLIC_REPORT_TOKEN = PUBLIC_TOKEN;

  // Org/team scope from the parent dashboards toolbar — same URL params and
  // same merge rules as view.html (skip the org clause when team filters are
  // present: some data views lack organization_title and the AND would 0-match).
  //
  // public mode ignores the URL entirely: org/team scope is forced
  // server-side from the publish entry, and the time window starts at the
  // fixed default (Last 7 days — mirrors viewer.html) whether or not
  // the publisher opted in to showing the selector.
  (function readParentFilterParams() {
    if (PUBLIC_TOKEN) {
      window.DASHBOARD_EXTRA_FILTERS = [];
      window.DASHBOARD_TIME_RANGE = { from: 'now-7d/d', to: 'now' };
      return;
    }
    const params = new URLSearchParams(window.location.search);
    const orgFilter = params.get('org_filter') || '';
    const teamFilters = params.getAll('team_filter');
    const extra = [];
    if (orgFilter && teamFilters.length === 0) {
      extra.push({ field: 'organization_title', op: 'match_phrase', value: orgFilter });
    }
    if (teamFilters.length === 1) {
      extra.push({ field: 'team_title', op: 'match_phrase', value: teamFilters[0] });
    } else if (teamFilters.length > 1) {
      extra.push({ field: 'team_title', op: 'match_phrase_any', value: teamFilters });
    }
    window.DASHBOARD_EXTRA_FILTERS = extra;
    window.DASHBOARD_TIME_RANGE = {
      from: params.get('time_from') || '',
      to: params.get('time_to') || '',
    };
  })();

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  // ══ theme-token bridges. Keep the two functions below
  // byte-identical with canvas_editor.js (same convention as the
  // helpers). Object-level style values resolve through the shared resolver
  // (canvas_theme.js, loaded first on every canvas surface); raw values pass
  // through unchanged either way, so a missing resolver can never crash or
  // restyle a page. ══

  function cvColor(v) {
    return (typeof window !== 'undefined' && window.cvThemeColor) ? window.cvThemeColor(v) : v;
  }

  function cvFontPx(v) {
    return (typeof window !== 'undefined' && window.cvThemeFontSize) ? window.cvThemeFontSize(v) : v;
  }

  // ══ rich-text theme + renderer. Keep the five functions below
  // byte-identical with canvas_editor.js (same convention as
  // formatFieldNumber): editor, preview, and published output must build
  // the same DOM from the same document. ══

  // The text theme. Every rich-text run color/font and the box-level
  // font_family persist as a KEY into this set; rendering looks the key up
  // and silently drops unknown keys, so a crafted document can never
  // inject styling (frontend rule: fonts/colors come from a defined token
  // set — no ad-hoc inline values scattered per object).
  // follow-up: the set is SOURCED from the active page theme
  // (canvas_theme.js), so rich-text runs restyle with the theme. The key
  // set never grows — same 8 colors, 4 fonts — only the VALUES follow the
  // theme; the literal below is the classic look, kept as the fallback
  // when the resolver is absent and per-key when a theme omits a key.
  function cvTextTokens() {
    const fallback = {
      fonts: {
        sans: "-apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
        serif: "Georgia, 'Times New Roman', serif",
        slab: "'Trebuchet MS', Verdana, sans-serif",
        mono: "Consolas, 'Courier New', monospace",
      },
      colors: {
        ink: '#16191f', slate: '#5f6b7a', navy: '#1a2744', blue: '#0972d3',
        green: '#037f0c', amber: '#8a6116', red: '#d91515', white: '#ffffff',
      },
      sizes: [12, 14, 15, 18, 20, 24, 28, 32, 40, 48],
    };
    const t = (typeof window !== 'undefined' && window.cvActiveTheme) ? window.cvActiveTheme() : null;
    if (!t || !t.colors || !t.fonts || !t.fonts.family) return fallback;
    const colors = {};
    Object.keys(fallback.colors).forEach((k) => {
      colors[k] = (typeof t.colors[k] === 'string') ? t.colors[k] : fallback.colors[k];
    });
    const fonts = {};
    Object.keys(fallback.fonts).forEach((k) => {
      fonts[k] = (typeof t.fonts.family[k] === 'string') ? t.fonts.family[k] : fallback.fonts[k];
    });
    return {
      fonts: fonts,
      colors: colors,
      sizes: (Array.isArray(t.fonts.sizes) && t.fonts.sizes.length) ? t.fonts.sizes : fallback.sizes,
    };
  }

  // the live time-pill label. Formats the page-level time range
  // (window.DASHBOARD_TIME_RANGE) exactly the way the Digital Impact
  // Report's date pill does in charts.js (dateLabel): 'All Time' when no
  // range is active, else "January 5, 2026 — July 13, 2026". Joins the
  // byte-identical shared set — a time pill must read the same in the
  // editor, preview, and published output.
  function cvTimeRangeLabel() {
    const tr = (typeof window !== 'undefined' && window.DASHBOARD_TIME_RANGE) || {};
    if (!tr.from) return 'All Time';
    const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
      'August', 'September', 'October', 'November', 'December'];
    const fmtDate = (s) => {
      const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s) : null;
      if (d && !isNaN(d)) return MONTHS[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear();
      // Parse "now-Xd/d" style date math (presets).
      const m = String(s).match(/^now-(\d+)([dMy])(?:\/[dMy])?$/i);
      if (m) {
        const n = parseInt(m[1], 10);
        const u = m[2].toLowerCase();
        const dt = new Date();
        if (u === 'd') dt.setDate(dt.getDate() - n);
        else if (u === 'm') dt.setMonth(dt.getMonth() - n);
        else if (u === 'y') dt.setFullYear(dt.getFullYear() - n);
        return MONTHS[dt.getMonth()] + ' ' + dt.getDate() + ', ' + dt.getFullYear();
      }
      if (/^now$/i.test(s)) {
        const dt = new Date();
        return MONTHS[dt.getMonth()] + ' ' + dt.getDate() + ', ' + dt.getFullYear();
      }
      return s;
    };
    return fmtDate(tr.from) + ' — ' + fmtDate(tr.to || 'now');
  }

  // One rich-text run → a span. textContent only — a run can never inject
  // markup; color/font/size resolve through the token set or not at all.
  function cvRunEl(run) {
    const t = cvTextTokens();
    const span = document.createElement('span');
    span.textContent = (run && typeof run.t === 'string') ? run.t : '';
    if (!run || typeof run !== 'object') return span;
    if (run.bold) span.style.fontWeight = '700';
    if (run.italic) span.style.fontStyle = 'italic';
    if (run.underline) span.style.textDecoration = 'underline';
    if (run.color && t.colors[run.color]) span.style.color = t.colors[run.color];
    if (run.font && t.fonts[run.font]) span.style.fontFamily = t.fonts[run.font];
    const size = Number(run.size);
    if (size && t.sizes.indexOf(size) !== -1) span.style.fontSize = size + 'px';
    return span;
  }

  function cvAppendRuns(parent, runs) {
    (Array.isArray(runs) ? runs : []).forEach((r) => parent.appendChild(cvRunEl(r)));
    // An empty paragraph/list item keeps its line box (like <p><br></p>).
    if (!parent.textContent) parent.appendChild(document.createElement('br'));
  }

  // Render a text object into `node`: rich blocks (p / ul / ol) when
  // text.rich is present, else the plain string. Built entirely with
  // createElement/textContent — no document-controlled markup ever renders.
  function cvRenderTextInto(node, obj) {
    // a time pill's wording is DERIVED: it always shows the
    // page's current time range, never its stored text (which is only the
    // no-range fallback wording).
    if (obj && obj.role === 'time_pill') {
      node.textContent = cvTimeRangeLabel();
      return;
    }
    const rich = (obj.text && Array.isArray(obj.text.rich) && obj.text.rich.length)
      ? obj.text.rich : null;
    if (!rich) {
      node.textContent = (obj.text && obj.text.content) || '';
      return;
    }
    node.textContent = '';
    rich.forEach((block) => {
      if (!block || typeof block !== 'object') return;
      const type = (block.type === 'ul' || block.type === 'ol') ? block.type : 'p';
      const blockEl = document.createElement(type);
      if (['left', 'center', 'right'].indexOf(block.align) !== -1) blockEl.style.textAlign = block.align;
      if (type === 'p') {
        cvAppendRuns(blockEl, block.runs);
      } else {
        (Array.isArray(block.items) ? block.items : []).forEach((item) => {
          const li = document.createElement('li');
          cvAppendRuns(li, item);
          blockEl.appendChild(li);
        });
      }
      node.appendChild(blockEl);
    });
  }

  // Box-level text-box styling. extends the set (size,
  // bold, color, fill, radius, align) with border, padding, opacity, and a
  // theme font_family token.
  function cvApplyTextBoxStyle(node, style) {
    const t = cvTextTokens();
    node.style.fontSize = (cvFontPx(style.font_size) || 15) + 'px';
    if (style.bold) node.style.fontWeight = '700';
    if (style.color) node.style.color = cvColor(style.color);
    if (style.bg_color) node.style.background = cvColor(style.bg_color);
    if (style.radius) node.style.borderRadius = style.radius + 'px';
    if (style.align) node.style.textAlign = style.align;
    if (style.font_family && t.fonts[style.font_family]) node.style.fontFamily = t.fonts[style.font_family];
    const bw = Number(style.border_width);
    if (bw > 0) node.style.border = Math.min(bw, 20) + 'px solid ' + (cvColor(style.border_color) || '#16191f');
    const pad = Number(style.padding);
    if (style.padding !== undefined && style.padding !== null && style.padding !== '' && !isNaN(pad)) {
      node.style.padding = Math.min(Math.max(pad, 0), 100) + 'px';
    }
    const op = Number(style.opacity);
    if (op >= 10 && op < 100) node.style.opacity = String(op / 100);
  }

  // ══ shape + image renderers. Keep the three functions below
  // byte-identical with canvas_editor.js (same convention as the
  // rich-text helpers): editor, preview, and published output must build
  // the same DOM. Everything is createElement + inline styles — a document
  // can never inject markup, and image sources render ONLY through an <img>
  // element (scripts inside a crafted SVG never execute in <img> context). ══

  // Shape border shorthand from style: width (capped), style keyword gated
  // to dashed/dotted (else solid), color. '' when there is no border.
  function cvShapeBorderCss(style) {
    const bw = Number(style.border_width);
    if (!(bw > 0)) return '';
    const bs = (style.border_style === 'dashed' || style.border_style === 'dotted')
      ? style.border_style : 'solid';
    return Math.min(bw, 20) + 'px ' + bs + ' ' + (cvColor(style.border_color) || '#1a2744');
  }

  function cvRenderShapeInto(node, obj) {
    const kind = (obj.shape && obj.shape.kind) || 'rect';
    const style = obj.style || {};
    node.textContent = '';
    const op = Number(style.opacity);
    if (op >= 10 && op < 100) node.style.opacity = String(op / 100);
    const border = cvShapeBorderCss(style);
    const fill = (style.fill_color === undefined || style.fill_color === null || style.fill_color === '')
      ? '#dbe7f5' : cvColor(style.fill_color);
    if (kind === 'line' || kind === 'arrow') {
      // A line is a border-top strip at the frame's vertical center — CSS
      // borders keep thickness and dash patterns exact at any size (an SVG
      // stretched with preserveAspectRatio="none" would distort both);
      // frame.rotation provides angles. The arrow adds a CSS-triangle head
      // sized to the stroke; the shaft stops short of it so a dashed shaft
      // never pokes past the tip.
      const lineW = Math.min(Math.max(Number(style.border_width) || 2, 1), 20);
      const lineColor = cvColor(style.border_color) || '#1a2744';
      const bs = (style.border_style === 'dashed' || style.border_style === 'dotted')
        ? style.border_style : 'solid';
      const head = kind === 'arrow' ? Math.max(10, lineW * 4) : 0;
      const shaft = document.createElement('div');
      shaft.style.position = 'absolute';
      shaft.style.left = '0';
      shaft.style.right = head ? Math.round(head * 0.75) + 'px' : '0';
      shaft.style.top = '50%';
      shaft.style.marginTop = -Math.ceil(lineW / 2) + 'px';
      shaft.style.borderTop = lineW + 'px ' + bs + ' ' + lineColor;
      node.appendChild(shaft);
      if (head) {
        const half = Math.round(head * 0.6);
        const tip = document.createElement('div');
        tip.style.position = 'absolute';
        tip.style.right = '0';
        tip.style.top = '50%';
        tip.style.marginTop = -half + 'px';
        tip.style.width = '0';
        tip.style.height = '0';
        tip.style.borderTop = half + 'px solid transparent';
        tip.style.borderBottom = half + 'px solid transparent';
        tip.style.borderLeft = head + 'px solid ' + lineColor;
        node.appendChild(tip);
      }
      return;
    }
    if (kind === 'callout') {
      // Word/PowerPoint-style speech bubble: rounded body over a tail
      // diamond whose two exposed edges carry the same border. The diamond
      // overlaps the body's bottom border and its fill covers the seam, so
      // the two read as one connected region.
      const tail = 16;
      const body = document.createElement('div');
      body.style.position = 'absolute';
      body.style.left = '0';
      body.style.right = '0';
      body.style.top = '0';
      body.style.bottom = tail + 'px';
      body.style.background = fill;
      if (border) body.style.border = border;
      body.style.borderRadius = ((style.radius === undefined || style.radius === null)
        ? 8 : Number(style.radius) || 0) + 'px';
      node.appendChild(body);
      const tip = document.createElement('div');
      tip.style.position = 'absolute';
      tip.style.left = '28px';
      tip.style.bottom = Math.round(tail / 2) + 'px';
      tip.style.width = tail + 'px';
      tip.style.height = tail + 'px';
      tip.style.transform = 'rotate(45deg)';
      tip.style.background = fill;
      if (border) {
        tip.style.borderRight = border;
        tip.style.borderBottom = border;
      }
      node.appendChild(tip);
      const t = cvTextTokens();
      const text = document.createElement('div');
      text.className = 'cvs-text';
      text.style.position = 'absolute';
      text.style.left = '0';
      text.style.right = '0';
      text.style.top = '0';
      text.style.bottom = tail + 'px';
      text.style.padding = '8px 12px';
      text.style.overflow = 'hidden';
      text.style.display = 'flex';
      text.style.flexDirection = 'column';
      text.style.justifyContent = 'center';
      text.style.whiteSpace = 'pre-wrap';
      text.style.wordBreak = 'break-word';
      text.style.lineHeight = '1.35';
      text.style.fontSize = (cvFontPx(style.font_size) || 14) + 'px';
      text.style.color = cvColor(style.color) || '#16191f';
      text.style.fontFamily = (style.font_family && t.fonts[style.font_family])
        ? t.fonts[style.font_family] : t.fonts.sans;
      if (style.align) text.style.textAlign = style.align;
      cvRenderTextInto(text, obj);
      node.appendChild(text);
      return;
    }
    // rect / ellipse — one filled, bordered box; the radius is what differs.
    const box = document.createElement('div');
    box.style.position = 'absolute';
    box.style.left = '0';
    box.style.right = '0';
    box.style.top = '0';
    box.style.bottom = '0';
    box.style.background = fill;
    if (border) box.style.border = border;
    box.style.borderRadius = kind === 'ellipse' ? '50%' : (Number(style.radius) || 0) + 'px';
    node.appendChild(box);
  }

  function cvRenderImageInto(node, obj) {
    const style = obj.style || {};
    node.textContent = '';
    const src = (obj.image && typeof obj.image.src === 'string') ? obj.image.src : '';
    const img = document.createElement('img');
    // Only a data:image/... URI ever reaches src — the server enforces the
    // same rule on save, so a crafted document degrades to an empty box
    // instead of dereferencing an external or javascript: URL.
    if (src.indexOf('data:image/') === 0) img.src = src;
    img.alt = (obj.image && obj.image.name) || '';
    img.draggable = false;
    img.style.width = '100%';
    img.style.height = '100%';
    img.style.objectFit = (style.fit === 'cover' || style.fit === 'fill') ? style.fit : 'contain';
    const radius = Number(style.radius) || 0;
    if (radius) {
      node.style.borderRadius = radius + 'px';
      img.style.borderRadius = radius + 'px';
    }
    const border = cvShapeBorderCss(style);
    if (border) node.style.border = border;
    const op = Number(style.opacity);
    if (op >= 10 && op < 100) node.style.opacity = String(op / 100);
    node.appendChild(img);
  }

  // follow-up: card-style fields abbreviate like the live report's
  // formatNumber in charts.js (32.6M / 50.2K); table_cell variants keep the
  // full comma number to match the .gt-table look. Keep in sync with the
  // identical helpers in canvas_editor.js.
  function abbrevNumber(n) {
    const abs = Math.abs(n);
    if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (abs >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  // explicit decimals: null means "auto" (per-format default).
  function decimalsOf(style) {
    const d = parseInt(style.decimals, 10);
    if (isNaN(d)) return null;
    return Math.min(Math.max(d, 0), 6);
  }

  // one formatter for every bound value. style.format picks the
  // family (number / percent / currency), style.decimals overrides the
  // family's default precision, style.thousands (default on) controls
  // grouping, style.abbrev (default: on for card variants, off for
  // table_cell — the earlier behavior) collapses ≥1000 to K/M while
  // decimals is auto. Keep in sync with the identical helper in
  // canvas_editor.js.
  function formatFieldNumber(n, obj) {
    const style = obj.style || {};
    const fmt = style.format || 'number';
    const dp = decimalsOf(style);
    const group = style.thousands !== false;
    const opts = (d) => ({ minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: group });
    if (fmt === 'percent') {
      // Computed bindings are ratios (numerator ÷ denominator) — percent is
      // the ×100 human reading of that ratio.
      return (n * 100).toLocaleString(undefined, opts(dp === null ? 1 : dp)) + '%';
    }
    if (fmt === 'currency') {
      // calc_field results are ratios/derived values like
      // computed bindings, so auto currency precision is 2 for both.
      const isDerived = (obj.binding && obj.binding.computed) || obj.object_type === 'calc_field';
      const d = dp === null ? (isDerived ? 2 : 0) : dp;
      return '$' + n.toLocaleString(undefined, opts(d));
    }
    const abbrev = style.abbrev === undefined ? style.variant !== 'table_cell' : !!style.abbrev;
    if (abbrev && dp === null && Math.abs(n) >= 1e3) return abbrevNumber(n);
    if (dp === null) return n.toLocaleString(undefined, { maximumFractionDigits: 2, useGrouping: group });
    return n.toLocaleString(undefined, opts(dp));
  }

  // Viewer degrade rule: unbound and broken bindings both
  // render an em dash — visibly empty, never a stale/baked number.
  // calculated fields degrade the same way: no formula, a bad
  // formula, a missing variable, or division by zero all land on the em
  // dash (never NaN).
  function fieldValueText(obj) {
    if (obj.object_type === 'calc_field') {
      if (!obj.calc || !obj.calc.formula) return '—';
    } else if (!obj.binding) return '—';
    if (obj._error) return '—';
    const v = obj._value;
    if (v === undefined) return '…';
    if (v === null || !isFinite(v)) return '—';
    return formatFieldNumber(Number(v), obj);
  }

  function buildObjectEl(obj, i) {
    const node = el('div');
    const type = obj.object_type || 'unknown';
    node.dataset.objectId = obj.object_id || '';
    const f = obj.frame || {};
    node.style.left = (f.x || 0) + 'px';
    node.style.top = (f.y || 0) + 'px';
    node.style.width = (f.w || 100) + 'px';
    node.style.height = (f.h || 40) + 'px';
    node.style.zIndex = String(i + 1);
    // rotation parity with buildObjectEl in canvas_editor.js.
    const rot = Number(f.rotation) || 0;
    if (rot) node.style.transform = 'rotate(' + rot + 'deg)';
    const style = obj.style || {};
    if (type === 'text') {
      node.className = 'cvv-object cvv-text';
      // box style + content through the shared (editor-parity)
      // renderer; rich documents build p/ul/ol blocks, plain ones a bare
      // text node exactly as before.
      cvApplyTextBoxStyle(node, style);
      cvRenderTextInto(node, obj);
    } else if (type === 'data_field' || type === 'calc_field') {
      // a calculated field renders exactly like a data field;
      // only where its value comes from differs (calc.formula evaluated
      // over calc.variables in resolveAllFields).
      node.className = 'cvv-object cvv-data-field';
      if (style.variant === 'table_cell') node.classList.add('cvv-df-cell');
      if (style.variant === 'plain') node.classList.add('cvv-df-plain');
      if (style.bg_color) node.style.background = cvColor(style.bg_color);
      if (style.border_color) node.style.borderColor = cvColor(style.border_color);
      if (style.align) node.style.textAlign = style.align;
      const value = el('div', 'cvv-df-value');
      value.textContent = fieldValueText(obj);
      if (style.value_color) value.style.color = cvColor(style.value_color);
      if (style.value_font_size) value.style.fontSize = cvFontPx(style.value_font_size) + 'px';
      const label = el('div', 'cvv-df-label');
      label.textContent = obj.label || '';
      if (style.label_bg_color) label.style.background = cvColor(style.label_bg_color);
      if (style.label_color) label.style.color = cvColor(style.label_color);
      if (style.label_font_size) label.style.fontSize = cvFontPx(style.label_font_size) + 'px';
      if (style.label_bold === false) label.style.fontWeight = '500';
      if (style.label_hidden) {
        node.appendChild(value);
      } else if (style.label_position === 'top') {
        node.appendChild(label);
        node.appendChild(value);
      } else {
        node.appendChild(value);
        node.appendChild(label);
      }
    } else if (type === 'icon') {
      node.className = 'cvv-object cvv-icon';
      if (style.bg_color) node.style.background = cvColor(style.bg_color);
      if (style.radius) node.style.borderRadius = style.radius + 'px';
      if (style.padding) node.style.padding = style.padding + 'px';
      const img = el('img');
      const name = (obj.icon && obj.icon.name) || '';
      img.src = ICON_BASE + '/' + encodeURIComponent(name);
      img.alt = name;
      // follow-up: white-ify for badge icons — the same
      // brightness(0) invert(1) treatment _SEC_ICON applies in charts.js.
      if (style.white) img.style.filter = 'brightness(0) invert(1)';
      node.appendChild(img);
    } else if (type === 'shape') {
      // shared (editor-parity) renderer.
      node.className = 'cvv-object cvv-shape';
      cvRenderShapeInto(node, obj);
    } else if (type === 'image') {
      node.className = 'cvv-object cvv-image';
      cvRenderImageInto(node, obj);
    } else if (type === 'chart') {
      // live geo/trend charts through the shared canvas_charts.js
      // renderer (async; queries live data on every render — never baked).
      node.className = 'cvv-object cvv-chart';
      if (window.cvRenderChartInto) window.cvRenderChartInto(node, obj, cvChartCtx());
      else node.textContent = 'Chart';
    } else if (type === 'table') {
      // a legacy table config rendered by the REAL charts.js
      // renderTable through the shared canvas_charts.js glue. Both viewer
      // templates load charts.js; queries ride DASHBOARD_BASE + api/query
      // ('../' authenticated, '/public/' with the token in public mode).
      node.className = 'cvv-object cvv-table';
      if (window.cvRenderTableInto) window.cvRenderTableInto(node, obj);
      else node.textContent = 'Table';
    } else if (type === 'placeholder') {
      node.className = 'cvv-object cvv-placeholder-obj';
      const ph = obj.placeholder || {};
      const label = el('div', 'cvv-ph-label');
      label.textContent = ph.label || 'Unsupported element';
      const reason = el('div', 'cvv-ph-reason');
      reason.textContent = ph.reason || '';
      node.appendChild(label);
      node.appendChild(reason);
    } else {
      node.className = 'cvv-object';
    }
    return node;
  }

  function render() {
    stage.innerHTML = '';
    const page = DOC.page || {};
    // pin the page's theme before anything resolves a token.
    if (window.cvSetActiveTheme) window.cvSetActiveTheme(page.theme);
    stage.style.width = (page.width || 1280) + 'px';
    stage.style.height = (page.height || 720) + 'px';
    const bg = page.background || {};
    if (bg.type === 'image' && bg.value) {
      stage.style.backgroundImage = 'url("' + bg.value + '")';
      stage.style.backgroundSize = 'cover';
      stage.style.backgroundPosition = 'center';
    } else {
      stage.style.background = cvColor(bg.value) || '#ffffff';
    }
    (DOC.objects || []).forEach((obj, i) => stage.appendChild(buildObjectEl(obj, i)));
    fit();
  }

  // Fit the fixed-size page to the container: downscale to fit a narrower
  // pane, but NEVER scale past 1:1 — the report blocks the canvas pages
  // mirror never scale their text, so text-size parity (what the eye
  // compares) requires rendering at design size and centering on wider
  // panes like a document. The seed's page width (canvas_seed._PAGE_W =
  // 880) is chosen so the narrowest common pane still lands ≈1:1.
  //
  // Height: when the scaled page is shorter than the window, the SHEET
  // stretches to the bottom (its background continues; objects stay put)
  // so the page reaches the bottom of the pane like the report blocks do
  // instead of ending on a strip of viewer-gray (user decision 2026-07-10:
  // "page should fill the window").
  function fit() {
    if (!DOC) return;
    const pageW = (DOC.page && DOC.page.width) || 1280;
    const pageH = (DOC.page && DOC.page.height) || 720;
    const scale = Math.min(1, Math.max(0.1, (document.documentElement.clientWidth - 16) / pageW));
    const paneH = Math.max(0, document.documentElement.clientHeight - stage.offsetTop - 8);
    const sheetH = Math.max(pageH, paneH / scale);
    stage.style.transform = 'scale(' + scale + ')';
    stage.style.height = sheetH + 'px';
    stage.style.marginLeft = Math.max(8, (document.documentElement.clientWidth - pageW * scale) / 2) + 'px';
    // stage.offsetTop covers the filter strip when one is rendered.
    document.body.style.height = stage.offsetTop + sheetH * scale + 'px';
  }
  window.addEventListener('resize', fit);

  function updateFieldEls() {
    (DOC.objects || []).forEach((obj) => {
      if (obj.object_type !== 'data_field' && obj.object_type !== 'calc_field') return;
      const node = stage.querySelector('[data-object-id="' + obj.object_id + '"] .cvv-df-value');
      if (node) node.textContent = fieldValueText(obj);
    });
  }

  // re-stamp every time pill's label from the live time range.
  function updateTimePillEls() {
    const label = cvTimeRangeLabel();
    (DOC.objects || []).forEach((obj) => {
      if (obj.object_type !== 'text' || obj.role !== 'time_pill') return;
      const node = stage.querySelector('[data-object-id="' + obj.object_id + '"]');
      if (node) node.textContent = label;
    });
  }

  // transport context for the shared chart renderer
  // (canvas_charts.js): the publish token flips it onto the /public/ routes.
  const MAP_URL = document.querySelector('meta[name="cv-map-url"]')?.content || '';
  function cvChartCtx() {
    return { apiBase: API_BASE, csrf: CSRF, publicToken: PUBLIC_TOKEN, mapUrl: MAP_URL };
  }

  // re-render every live chart object: they query at render time,
  // so filter/time changes re-invoke the renderer.
  function updateChartEls() {
    if (!window.cvRenderChartInto || !DOC) return;
    (DOC.objects || []).forEach((obj) => {
      if (obj.object_type !== 'chart') return;
      const node = stage.querySelector('[data-object-id="' + obj.object_id + '"]');
      if (node) window.cvRenderChartInto(node, obj, cvChartCtx());
    });
  }

  // keep in sync with the identical helpers in canvas_editor.js.
  function _isDtDataView(dv) {
    const s = dv || '';
    return s.includes('disciple-tools') || s.includes('disciple_tools');
  }

  function bindingQuery(binding) {
    const q = {
      chart_type: 'metric',
      data_view: binding.data_view,
      y_fields: [binding.field],
      aggregation: binding.aggregation || 'sum',
      filters: [],
    };
    if (binding.equals_value !== undefined && binding.equals_value !== null) {
      q.equals_value = binding.equals_value;
    }
    // per-binding row filters ({field, value} equality pairs,
    // ANDed) ride as plain eq clauses; the server still scope-checks every
    // query_batch item regardless of what we send.
    (Array.isArray(binding.filters) ? binding.filters : []).forEach((f) => {
      if (f && f.field && f.value !== undefined && f.value !== null && f.value !== '') {
        q.filters.push({ field: f.field, op: 'eq', value: f.value });
      }
    });
    // org/team scope filters from the parent toolbar (viewer only;
    // unset in the editor). The server additionally enforces the caller's
    // allowed scope on every query_batch item regardless of what we send.
    const extra = window.DASHBOARD_EXTRA_FILTERS || [];
    if (extra.length) q.filters = q.filters.concat(extra);
    // dashboard-level filters. An '__ALL__' filter is
    // rebound to this binding's own data view — the
    // server only honors a filter whose data_view matches the query's.
    const dfs = (window.DASHBOARD_STATE && Array.isArray(window.DASHBOARD_STATE.filters))
      ? window.DASHBOARD_STATE.filters : [];
    if (dfs.length && q.data_view) {
      q.dashboard_filters = dfs.map((f) => (
        (f && f.data_view === window.DASHBOARD_FILTER_ALL_DV)
          ? { ...f, data_view: q.data_view }
          : f
      ));
    }
    // global time range from the parent toolbar. kibana_date is the
    // standard date field; DT indices lack it, so they range on date_created_ms
    // (ES date type — date math passes through), mirroring charts.js.
    // a binding carrying a time_override uses its own range
    // instead (preset or custom {from, to}, resolved by the shared
    // cvResolveTimeOverride in canvas_charts.js), ignoring the page's
    // time-range filter entirely.
    const ov = binding.time_override;
    const tr = ov ? (window.cvResolveTimeOverride ? window.cvResolveTimeOverride(ov) : {}) : (window.DASHBOARD_TIME_RANGE || {});
    if ((tr.from || tr.to) && q.data_view) {
      q.date_range = {
        field: _isDtDataView(q.data_view) ? 'date_created_ms' : 'kibana_date',
        from: tr.from || null,
        to: tr.to || null,
      };
    }
    return q;
  }

  // the distinct data views the page's live bindings span
  // (computed bindings contribute their numerator/denominator views). Feeds
  // dashboard_filters.js's "All Data Views" intersection via the
  // dashboardFilterExtraDataViews hook.
  function bindingDataViews() {
    const out = new Set();
    const add = (b) => { if (b && b.data_view) out.add(b.data_view); };
    ((DOC && DOC.objects) || []).forEach((obj) => {
      // a calculated field spans every variable's data view.
      if (obj.object_type === 'calc_field') {
        const vars = (obj.calc && obj.calc.variables) || {};
        Object.keys(vars).forEach((n) => add(vars[n]));
        return;
      }
      if (obj.object_type !== 'data_field' || !obj.binding) return;
      if (obj.binding.computed) {
        add(obj.binding.numerator);
        add(obj.binding.denominator);
      } else {
        add(obj.binding);
      }
    });
    return Array.from(out);
  }

  async function resolveAllFields() {
    // every filter/time-range change funnels through here, so
    // time pills re-label and live charts re-query in the same beat the
    // data fields re-resolve. tables re-query the same way.
    updateTimePillEls();
    updateChartEls();
    if (window.cvUpdateTableEls && DOC) window.cvUpdateTableEls(DOC, stage);
    const tasks = [];
    const queries = [];
    (DOC.objects || []).forEach((obj) => {
      // calculated fields: one metric query per variable binding
      // in the same batch, evaluated client-side below (shared
      // cvParseFormula/cvEvalFormula from canvas_charts.js).
      if (obj.object_type === 'calc_field') {
        delete obj._error;
        obj._value = undefined;
        const calc = obj.calc || {};
        const names = Object.keys(calc.variables || {});
        if (!calc.formula || !names.length) return;
        tasks.push({ obj, start: queries.length, calcNames: names });
        names.forEach((n) => queries.push(bindingQuery(calc.variables[n] || {})));
        return;
      }
      if (obj.object_type !== 'data_field' || !obj.binding) return;
      if (obj.binding.computed) {
        tasks.push({ obj, start: queries.length, computed: true });
        queries.push(bindingQuery(obj.binding.numerator || {}), bindingQuery(obj.binding.denominator || {}));
      } else {
        tasks.push({ obj, start: queries.length, computed: false });
        queries.push(bindingQuery(obj.binding));
      }
    });
    if (!queries.length) return;
    let results = [];
    try {
      // public mode authenticates by token in the body; the
      // /public/ path is CSRF-exempt (auth.py path bypass), so no header.
      const body = PUBLIC_TOKEN ? { token: PUBLIC_TOKEN, queries } : { queries };
      const res = await fetch(API_BASE + 'query_batch', {
        method: 'POST',
        // X-CSRF-Token is load-bearing on the authenticated page: that POST
        // is CSRF-checked and this page does not ship app.js's
        // token-injecting fetch wrapper.
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      results = Array.isArray(data.results) ? data.results : [];
    } catch (err) {
      // every task lands in the em-dash state below
    }
    tasks.forEach((t) => {
      const val = (offset) => {
        const r = results[t.start + offset];
        if (!r || r.success === false) return null;
        return r.value ?? r.doc_count ?? 0;
      };
      if (t.calcNames) {
        // a failed/missing variable stays out of the value map,
        // so cvEvalFormula returns null and the field shows the em dash —
        // same contract as division by zero (never NaN, never a crash).
        const parsed = window.cvParseFormula
          ? window.cvParseFormula((t.obj.calc || {}).formula || '')
          : { ok: false };
        if (!parsed.ok) {
          t.obj._error = 'invalid formula';
          return;
        }
        const values = {};
        t.calcNames.forEach((n, idx) => {
          const v = val(idx);
          if (v !== null) values[n] = Number(v);
        });
        t.obj._value = window.cvEvalFormula(parsed.tree, values);
        return;
      }
      if (t.computed) {
        const num = val(0);
        const den = val(1);
        if (num === null || den === null) t.obj._error = 'query failed';
        else t.obj._value = den > 0 ? num / den : null;
      } else {
        const v = val(0);
        if (v === null) t.obj._error = 'query failed';
        else t.obj._value = v;
      }
    });
    updateFieldEls();
  }

  function contentReady() {
    try {
      // Target the iframe's own origin: the only consumer is dashboards.html,
      // served by the same app as this viewer, so parent origin == ours. A
      // wildcard would deliver the (harmless) ready-ping to any embedder, and
      // Semgrep blocks it (wildcard-postmessage-configuration).
      window.parent.postMessage({ type: 'echarts-content-ready' }, window.location.origin);
    } catch (err) { /* best-effort reveal handshake */ }
  }

  // header time-range selector on the published page (
  // opt-in; markup lives in canvas_viewer.html and is absent both when the
  // publisher keeps the fixed window and on the authenticated iframe page,
  // where the parent toolbar owns the range). Mirrors viewer.html's
  // wireTimeRangeControls, including the custom-range guards (:
  // future dates capped, from<=to, no min/max rewrites mid-keystroke).
  function wireTimeRangeControls() {
    const preset = document.getElementById('cvv-time-preset');
    if (!preset) return;
    const custom = document.getElementById('cvv-custom-range');
    const fromEl = document.getElementById('cvv-time-from');
    const toEl = document.getElementById('cvv-time-to');
    const applyBtn = document.getElementById('cvv-apply-custom');

    function todayValue() {
      const d = new Date();
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      return y + '-' + m + '-' + day;
    }
    if (fromEl) fromEl.max = todayValue();
    if (toEl) toEl.max = todayValue();

    function validateRange() {
      if (!fromEl || !toEl) return true;
      const f = fromEl.value;
      const t = toEl.value;
      const today = todayValue();
      const invalid = (f && f > today) || (t && t > today) || (f && t && f > t);
      const blocked = invalid || !f || !t;
      if (applyBtn) applyBtn.disabled = blocked;
      return !invalid;
    }
    if (fromEl) {
      fromEl.addEventListener('input', validateRange);
      fromEl.addEventListener('change', validateRange);
    }
    if (toEl) {
      toEl.addEventListener('input', validateRange);
      toEl.addEventListener('change', validateRange);
    }
    validateRange();
    window.addEventListener('pageshow', validateRange);

    preset.addEventListener('change', () => {
      if (preset.value === 'custom') {
        if (custom) custom.classList.add('visible');
        validateRange();
        return;
      }
      if (custom) custom.classList.remove('visible');
      window.DASHBOARD_TIME_RANGE = preset.value === ''
        ? { from: '', to: '' }
        : { from: preset.value, to: 'now' };
      resolveAllFields();
    });

    if (applyBtn) {
      applyBtn.addEventListener('click', () => {
        if (!validateRange()) return;
        if (!fromEl.value || !toEl.value) return;
        window.DASHBOARD_TIME_RANGE = { from: fromEl.value, to: toEl.value };
        resolveAllFields();
      });
    }
  }

  async function boot() {
    try {
      const docUrl = PUBLIC_TOKEN
        ? API_BASE + 'dashboards/' + encodeURIComponent(PUBLIC_TOKEN)
        : API_BASE + 'dashboards/' + encodeURIComponent(DASHBOARD_ID);
      const res = await fetch(docUrl);
      const data = await res.json();
      if (data.error || data.dashboard_type !== 'canvas') {
        stage.innerHTML = '';
        const err = el('div', 'cvv-error');
        err.textContent = 'Could not load this canvas page' + (data.error ? ': ' + data.error : '.');
        stage.appendChild(err);
        contentReady();
        return;
      }
      DOC = data.canvas_config || { page: {}, objects: [] };
      // dashboard-level filters. DASHBOARD_STATE.filters is what
      // dashboard_filters.js edits in place and bindingQuery reads; the
      // server surfaces canvas_config.filters as top-level `filters`. In
      // public mode the strip never renders — the author's saved
      // selections still ride every binding query as dashboard_filters.
      window.DASHBOARD_STATE.id = data.id || DASHBOARD_ID;
      window.DASHBOARD_STATE.filters = Array.isArray(data.filters) ? data.filters : [];
      window.dashboardFilterExtraDataViews = bindingDataViews;
      window.onDashboardFilterRequery = () => { resolveAllFields(); };
      if (typeof hydrateViewerFilterSelections === 'function') hydrateViewerFilterSelections();
      if (typeof renderDashboardFilterStrip === 'function') renderDashboardFilterStrip();
      wireTimeRangeControls();
      render();
      contentReady();
      resolveAllFields();
    } catch (err) {
      const box = el('div', 'cvv-error');
      box.textContent = 'Could not load this canvas page: ' + err.message;
      stage.appendChild(box);
      contentReady();
    }
  }

  boot();
})();
