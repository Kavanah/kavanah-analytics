/**
 * Canvas Builder editor foundation.
 *
 * The editor shell everything else plugs into: blank page, page size +
 * orientation, background (solid color / image), margin guides, rulers,
 * guides, grid, zoom, edit/preview modes, and save/load/rename/delete of
 * `dashboard_type: "canvas"` documents through the existing /api/dashboards
 * routes (workspace stamping + visibility/mutate-scope gates come with them).
 *
 * Architecture per the private docs (2026-07-07):
 *   - absolute-positioned HTML/CSS on a fixed-dimension page div
 *   - CSS transform: scale() zoom; pointer math divided by scale
 *   - canvas_config schema v1; z-order = objects[] array order
 *   - editor chrome CSS-gated on body.mode-edit / .cv-chrome — the page DOM
 *     is identical in edit and view modes, so chrome cannot leak into
 * published output
 *
 * adds seeding a page from an existing report (Summary Report /
 * Facebook / Google Digital Impact) plus the minimal object layer its
 * acceptance criteria require: type-aware rendering (text / data_field /
 * icon / placeholder), live data resolution through api/query_batch,
 * select/move/resize/delete/nudge/dblclick-edit (ported from the
 * prototype), section add/remove, re-binding, and icon insertion from the
 * library.
 *
 * completes the core-manipulation story on top of that layer:
 * z-order controls (bring forward / send backward / to front / to back over
 * the objects[] array order that IS the z-order), per-object lock (a locked
 * object can be selected but not moved, resized, edited, re-layered, or
 * deleted until unlocked), and rotation (frame.rotation in degrees around
 * the object center, rendered as a CSS transform in editor AND viewer —
 * keep canvas_view.js in sync; drag the top handle, Shift snaps to 15°).
 *
 * adds the multi-object tools: group/ungroup (group_id — selecting
 * any member selects the whole group, which then moves, resizes, locks, and
 * deletes as one unit; editing an individual member means ungrouping first),
 * group-aware align/distribute (a group counts as ONE unit so alignment can
 * never mangle its internal layout), and a persistent smart-guides toggle
 * (page.smart_snap — grid snap keeps its own checkbox; Alt still drags free).
 * group_id is an editor concept only — the viewer ignores it.
 *
 * filtering integration: dashboard-level filters (the
 * model, persisted at canvas_config.filters) are edited through the SAME
 * dashboard_filters.js strip + modal the Dashboard Builder uses.
 * DASHBOARD_STATE.filters is bridged to DOC.filters (same array reference)
 * so edits ride the document's save/undo/redo; every binding query attaches
 * them as ``dashboard_filters`` and a filter change re-resolves all live
 * elements in one query_batch (window.onDashboardFilterRequery).
 *
 * data-field formatting: number format (number / percent /
 * currency), decimals, thousands separators, K/M abbreviation, value font
 * size, value color, and alignment, all persisted under the object's
 * ``style`` so they survive save/reload like every other style key. The
 * value itself stays a LIVE binding (query_batch on every render — nothing
 * baked); a broken binding renders a red badge + tooltip in the editor and
 * degrades to an em dash in preview/published output. Also under this
 * ticket: "+ Section" inserts just the titled header band (it used to add
 * an unbound data field too and select IT, which read as "+ Section added
 * a data field"), and the panel labels ``role: "section_header"`` objects
 * "Section" instead of "Text".
 *
 * rich text. A text object may carry ``text.rich``: a structured
 * block/run model (blocks: p / ul / ol with per-block align; runs:
 * bold / italic / underline / color / size / font), NEVER persisted HTML.
 * Fonts and colors are KEYS into the cvTextTokens() theme set (frontend
 * rule: no ad-hoc inline values) — rendering looks keys up and silently
 * drops unknown ones, so a crafted document cannot inject styling, and the
 * renderer builds DOM with createElement/textContent only, so it cannot
 * inject markup. Editing stays the in-place contenteditable (dblclick) with
 * a floating toolbar; on commit the browser's DOM is parsed back through a
 * whitelist into the model and ``text.content`` is refreshed as the plain
 * mirror (old docs and the seeder keep working untouched; a box with no
 * formatting stays plain — no rich blob). Box-level formatting (border,
 * padding, opacity, theme font) rides ``style`` like everything else.
 * cvTextTokens / cvRunEl / cvAppendRuns / cvRenderTextInto /
 * cvApplyTextBoxStyle must stay byte-identical with canvas_view.js —
 * editor, preview, and published output render the same DOM.
 *
 * images and shapes. Shape objects (object_type "shape",
 * shape.kind: rect / ellipse / line / arrow / callout) render as pure
 * CSS-styled DOM (no SVG engine — the decision) with fill, border
 * color/width/style, opacity, and corner radius under ``style``; a callout
 * carries ``text`` and edits through the SAME dblclick rich-text session as
 * a text box. Image objects (object_type "image") store their pixels as a
 * data: URI at ``image.src`` INSIDE canvas_config (the private docs
 * 2026-07-13 — same posture as the page background), capped
 * per-image and per-document; SVG uploads are scanned for active content
 * here AND server-side, and every surface renders images through an <img>
 * element only, where scripts never execute. cvShapeBorderCss /
 * cvRenderShapeInto / cvRenderImageInto join the byte-identical shared set.
 *
 * three additions. (1) Time pill: a text object with
 * ``role: "time_pill"`` always renders the page's CURRENT time range (the
 * Digital Impact Report's date-pill wording — cvTimeRangeLabel joins the
 * byte-identical shared set); its stored text is only the no-range
 * fallback, and dblclick editing is blocked because the wording is
 * derived. (2) Live filters: the editor grows the Dashboard Builder's
 * org/team/time-range toolbar — preview-only state that fills the same
 * DASHBOARD_EXTRA_FILTERS / DASHBOARD_TIME_RANGE globals bindingQuery has
 * read and re-resolves everything in one batch; nothing is
 * persisted. (3) The seed dialog shows the Choose-a-Template description
 * line under each report name.
 */
(function () {
  'use strict';

  // Relative to /dashboard-builder/canvas → /dashboard-builder/api/…
  const API_BASE = 'api/';
  const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const RULER_PX = 24;
  // Client-side mirrors of the server caps in routes.py (_validate_canvas_payload).
  const PAGE_MIN = 200, PAGE_MAX = 10000;
  const MAX_BG_DATA_URI = 1000000;
  const MAX_GUIDES_PER_AXIS = 50;
  const MAX_TEXT_LEN = 5000; // mirrors _MAX_CANVAS_TEXT_LEN server-side
  const MAX_OBJECTS = 200; // mirrors _MAX_CANVAS_OBJECTS server-side
  const MAX_RICH_BLOCKS = 200; // mirrors _MAX_CANVAS_RICH_BLOCKS server-side
  // image objects (all three mirror the server caps in routes.py).
  const MAX_IMAGE_DATA_URI = 1000000; // mirrors _MAX_CANVAS_IMAGE_VALUE
  const MAX_TOTAL_IMAGE_CHARS = 6000000; // mirrors _MAX_CANVAS_TOTAL_IMAGE_CHARS
  const IMAGE_MIME_RE = /^data:image\/(png|jpeg|gif|webp|svg\+xml)[;,]/;

  function blankDoc() {
    return {
      schema_version: 1,
      page: {
        width: 1280,
        height: 720,
        orientation: 'landscape',
        background: { type: 'color', value: '#ffffff' },
        grid: { size: 20, show: false, snap: false },
        margins: { top: 48, right: 48, bottom: 48, left: 48, show: false },
        rulers: { show: true },
        guides: { h: [], v: [], show: true },
        smart_snap: true,
        // page-level theme reference (canvas_theme.js resolves
        // token-keyed style values against it; 'classic' is the pre-theme
        // look, so defaulting it changes nothing visually).
        theme: 'classic',
      },
      objects: [],
      // dashboard-level filters: [{id, title,
      // data_view, field, field_type, selected_values}]. Sibling of objects
      // inside canvas_config, mirroring echarts_config.filters.
      filters: [],
    };
  }

  // Deep-default merge so a doc saved by an older/newer schema (or a minimal
  // crafted one) opens with every page key present. Arrays and scalars are
  // taken from the source when the type matches; unknown extra keys survive.
  function normalizeDoc(cfg) {
    const base = blankDoc();
    const src = (cfg && typeof cfg === 'object') ? cfg : {};
    // JSON.parse keeps "__proto__" as an own key; recursing into
    // dst["__proto__"] would write onto Object.prototype for whoever opens a
    // crafted doc. These keys are never part of the canvas schema.
    const UNSAFE_KEYS = ['__proto__', 'constructor', 'prototype'];
    const merge = (dst, from) => {
      Object.keys(from || {}).forEach((k) => {
        if (UNSAFE_KEYS.includes(k)) return;
        const v = from[k];
        if (v && typeof v === 'object' && !Array.isArray(v)
            && dst[k] && typeof dst[k] === 'object' && !Array.isArray(dst[k])) {
          merge(dst[k], v);
        } else if (v !== undefined) {
          dst[k] = v;
        }
      });
    };
    merge(base, src);
    base.page.width = clampInt(base.page.width, PAGE_MIN, PAGE_MAX, 1280);
    base.page.height = clampInt(base.page.height, PAGE_MIN, PAGE_MAX, 720);
    if (base.page.orientation !== 'portrait') base.page.orientation = 'landscape';
    // the theme ref must be a non-empty string key; anything else
    // (crafted doc, pre-theme doc merged over the default) lands on classic.
    if (typeof base.page.theme !== 'string' || !base.page.theme) base.page.theme = 'classic';
    if (!Array.isArray(base.page.guides.h)) base.page.guides.h = [];
    if (!Array.isArray(base.page.guides.v)) base.page.guides.v = [];
    if (!Array.isArray(base.objects)) base.objects = [];
    if (!Array.isArray(base.filters)) base.filters = [];
    // every object needs a stable id for selection/manipulation;
    // docs saved before object editing existed may lack them.
    let fixSeq = 0;
    base.objects.forEach((o) => {
      if (o && typeof o === 'object' && !o.object_id) o.object_id = 'obj-fix-' + (++fixSeq);
    });
    return base;
  }

  function clampInt(v, lo, hi, fallback) {
    const n = parseInt(v, 10);
    if (isNaN(n)) return fallback;
    return Math.min(Math.max(n, lo), hi);
  }

  // ── State ──
  let DOC = blankDoc();
  let docId = null;
  let mode = 'edit'; // 'edit' | 'view'
  let scale = 1;
  let dirty = false;

  // ── Elements ──
  const $ = (id) => document.getElementById(id);
  const workarea = $('cv-workarea');
  const scaler = $('cv-scaler');
  const wrap = $('cv-page-wrap');
  const pageEl = $('cv-page');
  const rulerTop = $('cv-ruler-top');
  const rulerLeft = $('cv-ruler-left');
  const rulerCorner = $('cv-ruler-corner');
  const titleInput = $('cv-doc-title');

  // ── Toast ──
  let toastTimer = null;
  function toast(msg, isError) {
    const el = $('cv-toast');
    el.textContent = msg;
    el.classList.toggle('error', !!isError);
    el.classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('visible'), isError ? 5000 : 2600);
  }

  function markDirty() {
    dirty = true;
    // History: coalesce rapid mutation streams (drags,
    // color scrubs) into one undo step recorded shortly after they settle.
    if (historyMuted) return;
    clearTimeout(historyTimer);
    historyTimer = setTimeout(recordHistory, 350);
    updateHistoryButtons();
  }

  // ── History: undo/redo over whole-document snapshots ──
  let undoStack = [];
  let redoStack = [];
  let historyTimer = null;
  let historyMuted = false;

  function historySnapshot() {
    // Runtime-only keys (_value/_error) are excluded so a live-data refresh
    // never registers as an undoable edit.
    return JSON.stringify({ doc: DOC, title: titleInput.value }, (k, v) => (k.startsWith('_') ? undefined : v));
  }

  function recordHistory() {
    clearTimeout(historyTimer);
    historyTimer = null;
    const snap = historySnapshot();
    if (snap === undoStack[undoStack.length - 1]) { updateHistoryButtons(); return; }
    undoStack.push(snap);
    if (undoStack.length > 60) undoStack.shift();
    redoStack.length = 0;
    updateHistoryButtons();
  }

  // A fresh document (blank, opened, or seeded) starts its own history.
  function resetHistory() {
    clearTimeout(historyTimer);
    historyTimer = null;
    undoStack = [historySnapshot()];
    redoStack = [];
    updateHistoryButtons();
  }

  function applyHistorySnapshot(snap) {
    historyMuted = true;
    try {
      const state = JSON.parse(snap);
      DOC = normalizeDoc(state.doc);
      titleInput.value = state.title || 'Untitled Canvas';
      dirty = true;
      select(null);
      syncFilterBridge(); // filters are part of the snapshot
      syncPanel();
      renderAll();
      resolveAllFields();
    } finally {
      historyMuted = false;
    }
    updateHistoryButtons();
  }

  function undo() {
    if (historyTimer) recordHistory(); // flush the in-flight change first
    if (undoStack.length < 2) return;
    redoStack.push(undoStack.pop());
    applyHistorySnapshot(undoStack[undoStack.length - 1]);
  }

  function redo() {
    if (!redoStack.length) return;
    const snap = redoStack.pop();
    undoStack.push(snap);
    applyHistorySnapshot(snap);
  }

  function updateHistoryButtons() {
    $('cv-undo').disabled = undoStack.length < 2 && !historyTimer;
    $('cv-redo').disabled = !redoStack.length;
  }

  document.addEventListener('keydown', (e) => {
    if (mode !== 'edit') return;
    if (!(e.ctrlKey || e.metaKey)) return;
    const ae = document.activeElement;
    if (ae && (ae.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName))) return;
    const key = e.key.toLowerCase();
    if (key === 'z' && !e.shiftKey) {
      undo();
      e.preventDefault();
    } else if (key === 'y' || (key === 'z' && e.shiftKey)) {
      redo();
      e.preventDefault();
    } else if (key === 'c') {
      if (copySelection()) e.preventDefault();
    } else if (key === 'v') {
      if (objectClipboard.length) {
        pasteClipboard(null);
        e.preventDefault();
      }
    } else if (key === 'd') {
      if (selectedIds.size) {
        duplicateSelection();
        e.preventDefault();
      }
    } else if (key === 'g') {
      if (selectedIds.size) {
        if (e.shiftKey) ungroupSelection();
        else groupSelection();
        e.preventDefault();
      }
    }
  });

  // ── Rendering ──
  function renderAll() {
    // pin the page's theme before anything resolves a token.
    if (window.cvSetActiveTheme) window.cvSetActiveTheme(DOC.page.theme);
    renderLayout();
    renderBackground();
    renderPageChildren();
    renderRulerLabels();
  }

  function renderLayout() {
    const rulersOn = mode === 'edit' && DOC.page.rulers.show;
    const R = rulersOn ? RULER_PX : 0;
    const w = DOC.page.width, h = DOC.page.height;

    wrap.style.width = (w + R) + 'px';
    wrap.style.height = (h + R) + 'px';
    wrap.style.transform = 'scale(' + scale + ')';
    // The scaler is a plain box sized to the scaled footprint so the workarea
    // scrollbars cover the full page at any zoom.
    scaler.style.width = (w + R) * scale + 'px';
    scaler.style.height = (h + R) * scale + 'px';

    pageEl.style.left = R + 'px';
    pageEl.style.top = R + 'px';
    pageEl.style.width = w + 'px';
    pageEl.style.height = h + 'px';

    rulerTop.style.display = rulersOn ? '' : 'none';
    rulerLeft.style.display = rulersOn ? '' : 'none';
    rulerCorner.style.display = rulersOn ? '' : 'none';
    rulerTop.style.left = R + 'px';
    rulerTop.style.width = w + 'px';
    rulerTop.style.height = R + 'px';
    rulerLeft.style.top = R + 'px';
    rulerLeft.style.height = h + 'px';
    rulerLeft.style.width = R + 'px';
    rulerCorner.style.width = R + 'px';
    rulerCorner.style.height = R + 'px';
  }

  function renderBackground() {
    const bg = DOC.page.background;
    pageEl.style.backgroundColor = '#ffffff';
    pageEl.style.backgroundImage = '';
    if (bg.type === 'color' && bg.value) {
      pageEl.style.backgroundColor = cvColor(bg.value);
    } else if (bg.type === 'image' && bg.value) {
      pageEl.style.backgroundImage = 'url("' + bg.value + '")';
      pageEl.style.backgroundSize = 'cover';
      pageEl.style.backgroundPosition = 'center';
      pageEl.style.backgroundRepeat = 'no-repeat';
    }
  }

  // Grid, margin guides, guides, and objects all live inside the page div.
  // Chrome overlays carry .cv-chrome (hidden outright in view mode) and are
  // additionally skipped here when their show flag is off.
  function renderPageChildren() {
    pageEl.innerHTML = '';

    // Objects first (z 1..n); chrome overlays sit above at z 9000+.
    DOC.objects.forEach((obj, i) => pageEl.appendChild(buildObjectEl(obj, i)));

    if (DOC.page.grid.show) {
      const grid = document.createElement('div');
      grid.className = 'cv-grid cv-chrome';
      const s = clampInt(DOC.page.grid.size, 4, 500, 20);
      grid.style.backgroundImage =
        'repeating-linear-gradient(to right, rgba(9,114,211,.16) 0 1px, transparent 1px ' + s + 'px), ' +
        'repeating-linear-gradient(to bottom, rgba(9,114,211,.16) 0 1px, transparent 1px ' + s + 'px)';
      pageEl.appendChild(grid);
    }

    if (DOC.page.margins.show) {
      const m = DOC.page.margins;
      const mg = document.createElement('div');
      mg.className = 'cv-margins cv-chrome';
      mg.style.left = (m.left || 0) + 'px';
      mg.style.top = (m.top || 0) + 'px';
      mg.style.right = (m.right || 0) + 'px';
      mg.style.bottom = (m.bottom || 0) + 'px';
      pageEl.appendChild(mg);
    }

    if (DOC.page.guides.show) {
      DOC.page.guides.v.forEach((x, i) => pageEl.appendChild(makeGuide('v', x, i)));
      DOC.page.guides.h.forEach((y, i) => pageEl.appendChild(makeGuide('h', y, i)));
    }

    // the wipe above removed the group bbox — restore it for the
    // current selection (no-op when the selection isn't one whole group).
    syncGroupBox();
  }

  function makeGuide(axis, pos, index) {
    const g = document.createElement('div');
    g.className = 'cv-guide cv-guide-' + axis + ' cv-chrome';
    g.dataset.axis = axis;
    g.dataset.index = String(index);
    if (axis === 'v') g.style.left = pos + 'px';
    else g.style.top = pos + 'px';
    g.title = 'Drag to move, double-click to remove';
    return g;
  }

  function renderRulerLabels() {
    [rulerTop, rulerLeft].forEach((r) => {
      r.querySelectorAll('.cv-ruler-label').forEach((el) => el.remove());
    });
    for (let x = 0; x <= DOC.page.width; x += 100) {
      const s = document.createElement('span');
      s.className = 'cv-ruler-label';
      s.style.left = (x + 2) + 'px';
      s.textContent = String(x);
      rulerTop.appendChild(s);
    }
    for (let y = 0; y <= DOC.page.height; y += 100) {
      const s = document.createElement('span');
      s.className = 'cv-ruler-label';
      s.style.top = (y + 1) + 'px';
      s.textContent = String(y);
      rulerLeft.appendChild(s);
    }
  }

  // ── Guides: add from rulers, drag to move, double-click to remove ──
  // All pointer coordinates are divided by `scale` so guide positions live in
  // page coordinate space at any zoom.
  function pagePoint(e) {
    const r = pageEl.getBoundingClientRect();
    return { x: (e.clientX - r.left) / scale, y: (e.clientY - r.top) / scale };
  }

  rulerTop.addEventListener('click', (e) => {
    if (mode !== 'edit') return;
    if (DOC.page.guides.v.length >= MAX_GUIDES_PER_AXIS) { toast('Guide limit reached.', true); return; }
    const x = Math.round(pagePoint(e).x);
    if (x < 0 || x > DOC.page.width) return;
    DOC.page.guides.show = true;
    DOC.page.guides.v.push(x);
    syncPanel();
    markDirty();
    renderPageChildren();
  });

  rulerLeft.addEventListener('click', (e) => {
    if (mode !== 'edit') return;
    if (DOC.page.guides.h.length >= MAX_GUIDES_PER_AXIS) { toast('Guide limit reached.', true); return; }
    const y = Math.round(pagePoint(e).y);
    if (y < 0 || y > DOC.page.height) return;
    DOC.page.guides.show = true;
    DOC.page.guides.h.push(y);
    syncPanel();
    markDirty();
    renderPageChildren();
  });

  let guideDrag = null; // {axis, index, moved}
  pageEl.addEventListener('pointerdown', (e) => {
    if (mode !== 'edit') return;
    const g = e.target.closest('.cv-guide');
    if (!g) return;
    guideDrag = { axis: g.dataset.axis, index: parseInt(g.dataset.index, 10), moved: false };
    g.setPointerCapture(e.pointerId);
  });
  pageEl.addEventListener('pointermove', (e) => {
    if (!guideDrag) return;
    const p = pagePoint(e);
    const list = guideDrag.axis === 'v' ? DOC.page.guides.v : DOC.page.guides.h;
    const max = guideDrag.axis === 'v' ? DOC.page.width : DOC.page.height;
    const pos = Math.round(Math.min(Math.max(guideDrag.axis === 'v' ? p.x : p.y, 0), max));
    if (list[guideDrag.index] !== pos) {
      list[guideDrag.index] = pos;
      guideDrag.moved = true;
      markDirty();
      const el = pageEl.querySelector('.cv-guide-' + guideDrag.axis + '[data-index="' + guideDrag.index + '"]');
      if (el) el.style[guideDrag.axis === 'v' ? 'left' : 'top'] = pos + 'px';
    }
  });
  const endGuideDrag = () => { guideDrag = null; };
  pageEl.addEventListener('pointerup', endGuideDrag);
  pageEl.addEventListener('pointercancel', endGuideDrag);
  pageEl.addEventListener('dblclick', (e) => {
    if (mode !== 'edit') return;
    const g = e.target.closest('.cv-guide');
    if (!g) return;
    const list = g.dataset.axis === 'v' ? DOC.page.guides.v : DOC.page.guides.h;
    list.splice(parseInt(g.dataset.index, 10), 1);
    markDirty();
    renderPageChildren();
  });

  // ══ object layer: rendering, selection, manipulation, live data ══
  //
  // Seeded objects are ordinary canvas objects. The manipulation slice here
  // (select/move/resize/delete/nudge/dblclick text edit) is the minimal set
  // acceptance criteria require, ported from the
  // prototype; extends it (multi-select, z-order UI, lock UI,
  // rotation) rather than re-implementing.

  const ICON_BASE = document.querySelector('meta[name="cv-icon-base"]')?.content || '';
  // Selection is a SET (marquee / shift-click multi-select); selectedId is
  // the derived single selection (null when 0 or 2+) — the object panel and
  // resize handle only apply to a single selection.
  let selectedIds = new Set();
  let selectedId = null;
  let insertSeq = 0;

  const getObj = (id) => DOC.objects.find((o) => o.object_id === id);
  const objIndex = (id) => DOC.objects.findIndex((o) => o.object_id === id);

  function newObjectId() {
    insertSeq += 1;
    return 'obj-' + Date.now().toString(36) + '-' + insertSeq;
  }

  function newSectionId() {
    let n = 0;
    DOC.objects.forEach((o) => {
      const m = /^sec-(\d+)$/.exec(o.section_id || '');
      if (m) n = Math.max(n, parseInt(m[1], 10));
    });
    return 'sec-' + (n + 1);
  }

  // ── Groups: a group = the objects sharing a group_id ──
  function newGroupId() {
    let n = 0;
    DOC.objects.forEach((o) => {
      const m = /^grp-(\d+)$/.exec(o.group_id || '');
      if (m) n = Math.max(n, parseInt(m[1], 10));
    });
    return 'grp-' + (n + 1);
  }

  function groupMembers(gid) {
    return DOC.objects.filter((o) => o.group_id === gid);
  }

  // Selecting any member selects the whole group — every selection setter
  // funnels through applySelection, which expands here.
  function expandToGroups(ids) {
    const gids = new Set();
    ids.forEach((id) => {
      const o = getObj(id);
      if (o && o.group_id) gids.add(o.group_id);
    });
    if (!gids.size) return ids;
    const out = new Set(ids);
    DOC.objects.forEach((o) => {
      if (o.group_id && gids.has(o.group_id)) out.add(o.object_id);
    });
    return out;
  }

  // The gid when the selection is exactly one whole group, else null.
  function selectionSingleGroupId() {
    if (selectedIds.size < 2) return null;
    let gid = null;
    for (const id of selectedIds) {
      const o = getObj(id);
      if (!o || !o.group_id) return null;
      if (gid === null) gid = o.group_id;
      else if (o.group_id !== gid) return null;
    }
    return gid;
  }

  // follow-up: card-style fields abbreviate like the live report's
  // formatNumber in charts.js (32.6M / 50.2K); table_cell variants keep the
  // full comma number to match the .gt-table look. Keep in sync with the
  // identical helpers in canvas_view.js.
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
  // canvas_view.js.
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

  function fieldValueText(obj) {
    // calculated fields: "configured" means a formula is present
    // (variables bind inside calc.variables, not obj.binding); after that
    // the same ladder applies. A failed evaluation (missing variable,
    // division by zero) is _value === null → the em-dash placeholder, never
    // NaN.
    if (obj.object_type === 'calc_field') {
      if (!obj.calc || !obj.calc.formula) return 'No formula';
    } else if (!obj.binding) {
      return 'Not bound';
    }
    if (obj._error) return '—';
    const v = obj._value;
    if (v === undefined) return '…';
    if (v === null || !isFinite(v)) return '—';
    return formatFieldNumber(Number(v), obj);
  }

  // ══ theme-token bridges. Keep the two functions below
  // byte-identical with canvas_view.js (same convention as the
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
  // byte-identical with canvas_view.js (same convention as
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
  // byte-identical with canvas_view.js (same convention as the
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

  function buildObjectEl(obj, i) {
    const el = document.createElement('div');
    const type = obj.object_type || 'unknown';
    el.dataset.objectId = obj.object_id || '';
    const f = obj.frame || {};
    el.style.left = (f.x || 0) + 'px';
    el.style.top = (f.y || 0) + 'px';
    el.style.width = (f.w || 100) + 'px';
    el.style.height = (f.h || 40) + 'px';
    el.style.zIndex = String(i + 1);
    // rotation in degrees around the object center. Number guards
    // a crafted doc — an invalid value renders unrotated, never broken CSS.
    const rot = Number(f.rotation) || 0;
    if (rot) el.style.transform = 'rotate(' + rot + 'deg)';
    const style = obj.style || {};
    if (type === 'text') {
      el.className = 'cv-object cv-text';
      // box style + content through the shared (viewer-parity)
      // renderer; rich documents build p/ul/ol blocks, plain ones a bare
      // text node exactly as before.
      cvApplyTextBoxStyle(el, style);
      cvRenderTextInto(el, obj);
    } else if (type === 'data_field' || type === 'calc_field') {
      // a calculated field renders exactly like a data field
      // (label + live value + the same style keys); only where its value
      // comes from differs (calc.formula over calc.variables).
      el.className = 'cv-object cv-data-field';
      // Variants: 'table_cell' mirrors the
      // grouped_table look (colored header strip over a white value cell);
      // 'plain' is a chrome-free metric for styled card layouts (canva).
      const isCell = style.variant === 'table_cell';
      if (isCell) el.classList.add('cv-df-cell');
      if (style.variant === 'plain') el.classList.add('cv-df-plain');
      const unconfigured = type === 'calc_field' ? !(obj.calc && obj.calc.formula) : !obj.binding;
      if (unconfigured) el.classList.add('cv-df-unbound');
      if (obj._error) {
        el.classList.add('cv-df-error');
        // a broken binding must be diagnosable, not just red.
        el.title = type === 'calc_field'
          ? 'Calculated field problem — ' + obj._error + '. Select the field and re-apply its calculation.'
          : 'Broken binding — the bound data view/field did not resolve. Select the field and re-apply its binding.';
      }
      if (style.bg_color) el.style.background = cvColor(style.bg_color);
      if (style.border_color) el.style.borderColor = cvColor(style.border_color);
      if (style.align) el.style.textAlign = style.align;
      const value = document.createElement('div');
      value.className = 'cv-df-value';
      value.textContent = fieldValueText(obj);
      if (style.value_color) value.style.color = cvColor(style.value_color);
      if (style.value_font_size) value.style.fontSize = cvFontPx(style.value_font_size) + 'px';
      const label = document.createElement('div');
      label.className = 'cv-df-label';
      label.textContent = obj.label || '';
      if (style.label_bg_color) label.style.background = cvColor(style.label_bg_color);
      if (style.label_color) label.style.color = cvColor(style.label_color);
      if (style.label_font_size) label.style.fontSize = cvFontPx(style.label_font_size) + 'px';
      if (style.label_bold === false) label.style.fontWeight = '500';
      if (style.label_hidden) {
        el.appendChild(value);
      } else if (style.label_position === 'top') {
        el.appendChild(label);
        el.appendChild(value);
      } else {
        el.appendChild(value);
        el.appendChild(label);
      }
    } else if (type === 'icon') {
      el.className = 'cv-object cv-icon';
      // Badge styling (canva section badges): filled circle behind the SVG.
      if (style.bg_color) el.style.background = cvColor(style.bg_color);
      if (style.radius) el.style.borderRadius = style.radius + 'px';
      if (style.padding) el.style.padding = style.padding + 'px';
      const img = document.createElement('img');
      const name = (obj.icon && obj.icon.name) || '';
      img.src = ICON_BASE + '/' + encodeURIComponent(name);
      img.alt = name;
      img.draggable = false;
      // follow-up: white-ify for badge icons — the same
      // brightness(0) invert(1) treatment _SEC_ICON applies in charts.js.
      if (style.white) img.style.filter = 'brightness(0) invert(1)';
      el.appendChild(img);
    } else if (type === 'shape') {
      // shared (viewer-parity) renderer; a callout's text edits
      // through the same dblclick session as a text box.
      el.className = 'cv-object cv-shape';
      cvRenderShapeInto(el, obj);
    } else if (type === 'image') {
      el.className = 'cv-object cv-image';
      cvRenderImageInto(el, obj);
    } else if (type === 'chart') {
      // live geo/trend charts through the shared canvas_charts.js
      // renderer (async; queries live data on every render — never baked).
      el.className = 'cv-object cv-chart';
      if (window.cvRenderChartInto) window.cvRenderChartInto(el, obj, cvChartCtx());
      else el.textContent = 'Chart';
    } else if (type === 'table') {
      // a legacy table config rendered by the REAL charts.js
      // renderTable through the shared canvas_charts.js glue (live query on
      // every render — never baked).
      el.className = 'cv-object cv-table';
      if (window.cvRenderTableInto) window.cvRenderTableInto(el, obj);
      else el.textContent = 'Table';
    } else if (type === 'placeholder') {
      el.className = 'cv-object cv-placeholder-obj';
      const ph = obj.placeholder || {};
      const label = document.createElement('div');
      label.className = 'cv-ph-label';
      label.textContent = ph.label || 'Unsupported element';
      const reason = document.createElement('div');
      reason.className = 'cv-ph-reason';
      reason.textContent = ph.reason || 'This element could not be converted.';
      el.appendChild(label);
      el.appendChild(reason);
    } else {
      el.className = 'cv-object cv-placeholder';
      el.textContent = type;
    }
    if (obj.locked) el.classList.add('cv-locked');
    if (mode === 'edit' && selectedIds.has(obj.object_id)) {
      el.classList.add('selected');
      if (selectedIds.size === 1 && !obj.locked) {
        el.appendChild(makeResizeHandle());
        // The rotate handle only shows while rotate mode is active (panel
        // "Rotate" toggle) — always-on circles read as visual noise.
        if (rotateMode) el.appendChild(makeRotateHandle());
      }
    }
    return el;
  }

  function makeResizeHandle() {
    const handle = document.createElement('div');
    handle.className = 'cv-resize-handle cv-chrome';
    return handle;
  }

  function makeRotateHandle() {
    // Inset at top-center (like the resize handle) — .cv-object has
    // overflow:hidden, so an outside handle would be clipped.
    const handle = document.createElement('div');
    handle.className = 'cv-rotate-handle cv-chrome';
    handle.title = 'Drag to rotate — hold Shift for 15° steps';
    return handle;
  }

  // In-place class updates only — a re-render on click would replace the node
  // between the two clicks of a double-click and kill text editing (
  // prototype lesson).
  // Rotate mode: the rotate handle renders only while
  // this is on, toggled from the panel's Rotate button. Any selection change
  // exits it.
  let rotateMode = false;

  function setRotateMode(on) {
    rotateMode = !!on;
    const btn = $('cv-obj-rotate-toggle');
    if (btn) btn.classList.toggle('active', rotateMode);
    const el = selectedId && pageEl.querySelector('[data-object-id="' + selectedId + '"]');
    if (el) {
      const h = el.querySelector('.cv-rotate-handle');
      if (h) h.remove();
      const o = getObj(selectedId);
      if (rotateMode && o && !o.locked) el.appendChild(makeRotateHandle());
    }
  }

  function applySelection() {
    selectedIds = expandToGroups(selectedIds);
    selectedId = selectedIds.size === 1 ? selectedIds.values().next().value : null;
    pageEl.querySelectorAll('.cv-object.selected').forEach((el) => {
      el.classList.remove('selected');
      el.querySelectorAll('.cv-resize-handle, .cv-rotate-handle').forEach((h) => h.remove());
    });
    selectedIds.forEach((id) => {
      const el = pageEl.querySelector('[data-object-id="' + id + '"]');
      if (!el) return;
      el.classList.add('selected');
      el.querySelectorAll('.cv-resize-handle, .cv-rotate-handle').forEach((h) => h.remove());
      const o = getObj(id);
      if (selectedIds.size === 1 && o && !o.locked) {
        el.appendChild(makeResizeHandle());
        if (rotateMode) el.appendChild(makeRotateHandle());
      }
    });
    syncGroupBox();
    syncObjectPanel();
  }

  function select(id) {
    rotateMode = false;
    selectedIds = id ? new Set([id]) : new Set();
    applySelection();
  }

  function selectMany(ids) {
    rotateMode = false;
    selectedIds = new Set(ids);
    applySelection();
  }

  function toggleSelect(id) {
    rotateMode = false;
    // Shift-click on a grouped object adds/removes its WHOLE group.
    const o = getObj(id);
    const ids = o && o.group_id ? groupMembers(o.group_id).map((m) => m.object_id) : [id];
    const allIn = ids.every((i) => selectedIds.has(i));
    ids.forEach((i) => (allIn ? selectedIds.delete(i) : selectedIds.add(i)));
    applySelection();
  }

  // ── Group bounding box: when the selection is one whole group,
  // a dashed bbox overlay carries the group's single resize handle — dragging
  // it scales every member's frame proportionally (rotations unchanged).
  let groupBoxEl = null;

  function syncGroupBox() {
    if (groupBoxEl && !groupBoxEl.isConnected) groupBoxEl = null;
    const gid = mode === 'edit' ? selectionSingleGroupId() : null;
    const members = gid ? groupMembers(gid).filter((o) => o.frame) : [];
    if (members.length < 2) {
      if (groupBoxEl) { groupBoxEl.remove(); groupBoxEl = null; }
      return;
    }
    const x = Math.min(...members.map((o) => o.frame.x));
    const y = Math.min(...members.map((o) => o.frame.y));
    const r = Math.max(...members.map((o) => o.frame.x + (o.frame.w || 0)));
    const b = Math.max(...members.map((o) => o.frame.y + (o.frame.h || 0)));
    if (!groupBoxEl) {
      groupBoxEl = document.createElement('div');
      groupBoxEl.className = 'cv-group-box cv-chrome';
      pageEl.appendChild(groupBoxEl);
    }
    groupBoxEl.style.left = x + 'px';
    groupBoxEl.style.top = y + 'px';
    groupBoxEl.style.width = (r - x) + 'px';
    groupBoxEl.style.height = (b - y) + 'px';
    const resizable = members.every((o) => !o.locked);
    let handle = groupBoxEl.querySelector('.cv-group-resize-handle');
    if (resizable && !handle) {
      handle = document.createElement('div');
      handle.className = 'cv-group-resize-handle';
      handle.title = 'Drag to resize the whole group';
      groupBoxEl.appendChild(handle);
    } else if (!resizable && handle) {
      handle.remove();
    }
  }

  function groupSelection() {
    if (selectedIds.size < 2) {
      toast('Select two or more objects to group.', true);
      return;
    }
    if (selectionSingleGroupId()) return; // already exactly one group
    const objs = [...selectedIds].map(getObj).filter(Boolean);
    // Grouping rewrites group_id — an edit, so lock blocks it.
    if (objs.some((o) => o.locked)) {
      toast('Locked — unlock the object(s) before grouping.', true);
      return;
    }
    const gid = newGroupId();
    objs.forEach((o) => { o.group_id = gid; });
    markDirty();
    applySelection();
    toast('Grouped ' + objs.length + ' objects.');
  }

  function ungroupSelection() {
    const gids = new Set();
    selectedIds.forEach((id) => {
      const o = getObj(id);
      if (o && o.group_id) gids.add(o.group_id);
    });
    if (!gids.size) return;
    const members = DOC.objects.filter((o) => gids.has(o.group_id));
    if (members.some((o) => o.locked)) {
      toast('Locked — unlock the group before ungrouping.', true);
      return;
    }
    members.forEach((o) => { o.group_id = null; });
    markDirty();
    applySelection();
    toast('Ungrouped.');
  }

  function updateFieldEls() {
    DOC.objects.forEach((obj) => {
      if (obj.object_type !== 'data_field' && obj.object_type !== 'calc_field') return;
      const el = pageEl.querySelector('[data-object-id="' + obj.object_id + '"]');
      if (!el) return;
      const unconfigured = obj.object_type === 'calc_field'
        ? !(obj.calc && obj.calc.formula) : !obj.binding;
      el.classList.toggle('cv-df-unbound', unconfigured);
      el.classList.toggle('cv-df-error', !!obj._error);
      el.title = obj._error
        ? (obj.object_type === 'calc_field'
          ? 'Calculated field problem — ' + obj._error + '. Select the field and re-apply its calculation.'
          : 'Broken binding — the bound data view/field did not resolve. Select the field and re-apply its binding.')
        : '';
      const value = el.querySelector('.cv-df-value');
      if (value) value.textContent = fieldValueText(obj);
      const label = el.querySelector('.cv-df-label');
      if (label) label.textContent = obj.label || '';
    });
  }

  // transport context for the shared chart renderer
  // (canvas_charts.js). The editor is always the authenticated surface.
  const MAP_URL = document.querySelector('meta[name="cv-map-url"]')?.content || '';
  function cvChartCtx() {
    return { apiBase: API_BASE, csrf: CSRF, publicToken: '', mapUrl: MAP_URL };
  }

  // re-render every live chart object (geo map / trends): they
  // query at render time, so filter/time changes re-invoke the renderer
  // (which preserves .cv-chrome selection handles when clearing the node).
  function updateChartEls() {
    if (!window.cvRenderChartInto) return;
    DOC.objects.forEach((obj) => {
      if (obj.object_type !== 'chart') return;
      const el = pageEl.querySelector('[data-object-id="' + obj.object_id + '"]');
      if (el) window.cvRenderChartInto(el, obj, cvChartCtx());
    });
  }

  // re-stamp every time pill's label from the live time range.
  // Chrome children (selection handles) survive: only the text is replaced.
  function updateTimePillEls() {
    const label = cvTimeRangeLabel();
    DOC.objects.forEach((obj) => {
      if (obj.object_type !== 'text' || obj.role !== 'time_pill') return;
      const el = pageEl.querySelector('[data-object-id="' + obj.object_id + '"]');
      if (!el) return;
      const chrome = Array.from(el.querySelectorAll(':scope > .cv-chrome'));
      el.textContent = label;
      chrome.forEach((h) => el.appendChild(h));
    });
  }

  // ── Live data resolution ──

  // keep in sync with the identical helpers in canvas_view.js.
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
    // org/team scope filters. The viewer fills
    // DASHBOARD_EXTRA_FILTERS from the parent toolbar's URL params; the
    // editor from its own live-filter toolbar. The server
    // enforces the caller's allowed scope on every query_batch item
    // regardless.
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
    // global time range (filled by the same two sources as the
    // extra filters above). a binding carrying a time_override
    // uses its own range instead (preset or custom {from, to}, resolved by
    // the shared cvResolveTimeOverride in canvas_charts.js), ignoring the
    // page's time-range filter entirely.
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

  let resolveGen = 0;
  async function resolveAllFields() {
    const gen = ++resolveGen;
    // every filter/time-range change funnels through here, so
    // time pills re-label and live charts re-query in the same beat the
    // data fields re-resolve. tables re-query the same way.
    updateTimePillEls();
    updateChartEls();
    if (window.cvUpdateTableEls) window.cvUpdateTableEls(DOC, pageEl);
    const tasks = [];
    const queries = [];
    DOC.objects.forEach((obj) => {
      // calculated fields: one metric query per variable binding
      // in the SAME batch as everything else (a page
      // resolves in one query_batch round-trip), evaluated client-side over
      // the results below.
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
      if (obj.object_type !== 'data_field') return;
      delete obj._error;
      obj._value = undefined;
      if (!obj.binding) return;
      if (obj.binding.computed) {
        tasks.push({ obj, start: queries.length, computed: true });
        queries.push(bindingQuery(obj.binding.numerator || {}), bindingQuery(obj.binding.denominator || {}));
      } else {
        tasks.push({ obj, start: queries.length, computed: false });
        queries.push(bindingQuery(obj.binding));
      }
    });
    updateFieldEls();
    if (!queries.length) return;
    let results = [];
    try {
      const res = await fetch(API_BASE + 'query_batch', {
        method: 'POST', headers: jsonHeaders(), body: JSON.stringify({ queries }),
      });
      const data = await res.json();
      results = Array.isArray(data.results) ? data.results : [];
    } catch (err) {
      // results stays empty — every task lands in the error state below.
    }
    if (gen !== resolveGen) return; // a newer resolution superseded this one
    tasks.forEach((t) => {
      const val = (offset) => {
        const r = results[t.start + offset];
        if (!r || r.success === false) return null;
        return r.value ?? r.doc_count ?? 0;
      };
      if (t.calcNames) {
        // evaluate the formula over the variables that resolved.
        // A failed/missing variable simply stays out of the value map, so
        // cvEvalFormula returns null and the field shows the placeholder —
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

  // ── dashboard-level filter bridge (dashboard_filters.js) ──
  // The shared strip/modal edits DASHBOARD_STATE.filters IN PLACE; the bridge
  // points it at DOC.filters (same array reference) so filter edits ride the
  // document's save payload and undo/redo history. Globals below are what
  // dashboard_filters.js resolves at call time (canvas never loads charts.js).
  window.DASHBOARD_FILTER_ALL_DV = '__ALL__';
  window.DASHBOARD_BASE = ''; // dashboard_filters.js fetches DASHBOARD_BASE + 'api/…'
  // the canvas page now loads builder.js (table config panel),
  // whose lexical `const DASHBOARD_STATE` is what bare references in
  // charts.js/dashboard_filters.js resolve to. Reuse that object as the one
  // shared store; only create our own when builder.js is absent.
  window.DASHBOARD_STATE = (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE)
    ? DASHBOARD_STATE
    : { id: null, charts: {}, global_date_range: null, filters: [] };
  window._markDirty = markDirty; // dirty-flag bridge for _markDashboardFiltersDirty
  window.onDashboardFilterRequery = () => { resolveAllFields(); };
  window.dashboardFilterExtraDataViews = bindingDataViews;

  // The distinct data views the page's live bindings span (computed bindings
  // contribute their numerator/denominator views) — feeds the "All Data
  // Views" field intersection in dashboard_filters.js.
  function bindingDataViews() {
    const out = new Set();
    const add = (b) => { if (b && b.data_view) out.add(b.data_view); };
    DOC.objects.forEach((obj) => {
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

  // Re-point the bridge after every DOC replacement (open, new, seed,
  // undo/redo) — the strip re-render keeps the visible buttons in step.
  function syncFilterBridge() {
    if (!Array.isArray(DOC.filters)) DOC.filters = [];
    window.DASHBOARD_STATE.filters = DOC.filters;
    window.DASHBOARD_STATE.id = docId;
    if (typeof renderDashboardFilterStrip === 'function') renderDashboardFilterStrip();
  }

  // ══ live org/team/time filters (the Dashboard Builder's topbar
  // filters). These are PREVIEW filters: they set the same
  // DASHBOARD_EXTRA_FILTERS / DASHBOARD_TIME_RANGE globals bindingQuery
  // already reads (the viewer fills them from the parent toolbar's URL
  // params), then re-resolve every live element in one query_batch. Nothing
  // here is persisted with the document. Unlike the builder, the time range
  // defaults to "All time" — the earlier canvas behavior. ══

  // App-root endpoint: the canvas page
  // lives at /dashboard-builder/canvas, so ../api/ is the app root.
  const FILTER_OPTIONS_URL = '../api/dashboards/filter-options';

  // Same merge rules as the builder's _applyBuilderOrgTeamFilters: a team
  // pins team_title; an org with "All Teams" expands to its teams as an OR
  // (some data views lack organization_title — filtering on it directly
  // would 0-match them), falling back to organization_title only when the
  // team list hasn't loaded.
  function applyCanvasOrgTeamFilters() {
    const orgSelect = $('cv-filter-org');
    const teamSelect = $('cv-filter-team');
    const orgVal = orgSelect ? orgSelect.value : '';
    const teamVal = teamSelect ? teamSelect.value : '';
    const filters = [];
    if (teamVal) {
      filters.push({ field: 'team_title', op: 'match_phrase', value: teamVal });
    } else if (orgVal && teamSelect) {
      const availableTeams = Array.from(teamSelect.options)
        .filter((opt) => opt.value !== '')
        .map((opt) => opt.value);
      if (availableTeams.length > 0) {
        filters.push({ field: 'team_title', op: 'in', value: availableTeams });
      } else {
        filters.push({ field: 'organization_title', op: 'match_phrase', value: orgVal });
      }
    }
    window.DASHBOARD_EXTRA_FILTERS = filters.length ? filters : undefined;
    resolveAllFields();
  }

  async function loadCanvasFilterOptions(orgFilter) {
    const orgSelect = $('cv-filter-org');
    const teamSelect = $('cv-filter-team');
    const prevOrg = orgSelect.value;
    const prevTeam = teamSelect.value;
    try {
      let url = FILTER_OPTIONS_URL;
      if (orgFilter) url += '?org=' + encodeURIComponent(orgFilter);
      const res = await fetch(url);
      const data = await res.json();
      if (!data.success) return;
      orgSelect.innerHTML = '<option value="">All Organizations</option>';
      (data.organizations || []).forEach((org) => {
        const opt = document.createElement('option');
        opt.value = org.title;
        opt.textContent = org.title;
        orgSelect.appendChild(opt);
      });
      if (prevOrg && Array.prototype.some.call(orgSelect.options, (o) => o.value === prevOrg)) {
        orgSelect.value = prevOrg;
      }
      teamSelect.innerHTML = '<option value="">All Teams</option>';
      (data.teams || []).forEach((team) => {
        const opt = document.createElement('option');
        opt.value = team.title;
        opt.textContent = team.title;
        teamSelect.appendChild(opt);
      });
      if (prevTeam && Array.prototype.some.call(teamSelect.options, (o) => o.value === prevTeam)) {
        teamSelect.value = prevTeam;
      }
    } catch (err) {
      // fail-soft: the selects keep their "All …" defaults
    }
  }

  function applyCanvasTimeRange(from, to) {
    window.DASHBOARD_TIME_RANGE = from ? { from: from, to: to || 'now' } : {};
    resolveAllFields();
  }

  // calendar-anchored presets (This/Last Month, This/
  // Last Quarter). The server date-math can't express them, so the option
  // value is a cal:* sentinel the client converts to an explicit ISO
  // from/to window. Mirrors _calendarPresetRange in dashboards.html; keep
  // in sync with TIME_PRESETS in time_preset_service.py.
  function calendarPresetRange(sentinel) {
    const d = new Date();
    const ref = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
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

  function initCanvasLiveFilters() {
    const orgSelect = $('cv-filter-org');
    const teamSelect = $('cv-filter-team');
    const preset = $('cv-time-preset');
    const custom = $('cv-custom-date-range');
    const fromEl = $('cv-time-from');
    const toEl = $('cv-time-to');
    const applyBtn = $('cv-time-apply');

    orgSelect.addEventListener('change', async () => {
      // Re-scope the team list to the selected org before applying.
      teamSelect.value = '';
      await loadCanvasFilterOptions(orgSelect.value || undefined);
      applyCanvasOrgTeamFilters();
    });
    teamSelect.addEventListener('change', applyCanvasOrgTeamFilters);
    loadCanvasFilterOptions();

    // Custom-range guards (mirrors the builder / rules): no future
    // dates, from <= to, Apply disabled while incomplete or invalid.
    function todayValue() {
      const d = new Date();
      return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
        + '-' + String(d.getDate()).padStart(2, '0');
    }
    fromEl.max = todayValue();
    toEl.max = todayValue();
    function validateRange() {
      const f = fromEl.value;
      const t = toEl.value;
      const today = todayValue();
      const invalid = (f && f > today) || (t && t > today) || (f && t && f > t);
      applyBtn.disabled = invalid || !f || !t;
      return !invalid;
    }
    fromEl.addEventListener('input', validateRange);
    fromEl.addEventListener('change', validateRange);
    toEl.addEventListener('input', validateRange);
    toEl.addEventListener('change', validateRange);
    validateRange();

    preset.addEventListener('change', () => {
      const isCustom = preset.value === 'custom';
      custom.style.display = isCustom ? 'flex' : 'none';
      if (isCustom) {
        // Wait for Apply with a complete, valid range.
        validateRange();
        return;
      }
      const cal = calendarPresetRange(preset.value);
      if (cal) {
        applyCanvasTimeRange(cal.from, cal.to);
        return;
      }
      applyCanvasTimeRange(preset.value, preset.value ? 'now' : '');
    });
    applyBtn.addEventListener('click', () => {
      if (!validateRange() || !fromEl.value || !toEl.value) return;
      applyCanvasTimeRange(fromEl.value, toEl.value);
    });
  }
  initCanvasLiveFilters();

  // ── Move / resize (pointer deltas divided by scale math) ──
  let objDrag = null; // {kind:'move'|'resize', id, startX, startY, frame0}

  let marquee = null; // {x0, y0, el} — rubber-band selection in progress

  pageEl.addEventListener('pointerdown', (e) => {
    if (mode !== 'edit') return;
    if (e.button !== 0) return; // right button opens the context menu instead
    if (e.target.closest('.cv-guide')) return; // guides own their own drag
    if (e.target.classList.contains('cv-group-resize-handle')) {
      // group resize — capture every member's start frame plus the
      // group bbox; the move handler scales members proportionally.
      const gid = selectionSingleGroupId();
      if (!gid) return;
      const frames0 = {};
      groupMembers(gid).forEach((o) => {
        if (o.frame && !o.locked) frames0[o.object_id] = { ...o.frame };
      });
      const fs = Object.values(frames0);
      if (fs.length < 2) return;
      const bx = Math.min(...fs.map((f) => f.x));
      const by = Math.min(...fs.map((f) => f.y));
      const bbox0 = {
        x: bx,
        y: by,
        w: Math.max(...fs.map((f) => f.x + f.w)) - bx,
        h: Math.max(...fs.map((f) => f.y + f.h)) - by,
      };
      objDrag = { kind: 'gresize', id: null, startX: e.clientX, startY: e.clientY, frames0, bbox0 };
      pageEl.setPointerCapture(e.pointerId);
      return;
    }
    const objEl = e.target.closest('.cv-object');
    if (!objEl) {
      // Empty page: start a rubber-band selection. A no-movement release
      // still deselects (handled on pointerup).
      const p = pagePoint(e);
      marquee = { x0: p.x, y0: p.y, el: null };
      pageEl.setPointerCapture(e.pointerId);
      return;
    }
    if (objEl.classList.contains('editing')) return;
    const id = objEl.dataset.objectId;
    const obj = getObj(id);
    if (!obj) return;
    if (e.shiftKey) { toggleSelect(id); return; } // add/remove, no drag
    if (!selectedIds.has(id)) select(id);
    if (obj.locked) return;
    const kind = e.target.classList.contains('cv-resize-handle') ? 'resize'
      : e.target.classList.contains('cv-rotate-handle') ? 'rotate' : 'move';
    if (kind !== 'move' && selectedIds.size !== 1) return;
    // Multi-drag: capture every selected (unlocked) object's start frame so
    // the whole selection moves as one.
    const frames0 = {};
    selectedIds.forEach((sid) => {
      const o = getObj(sid);
      if (o && !o.locked && o.frame) frames0[sid] = { ...o.frame };
    });
    objDrag = { kind, id, startX: e.clientX, startY: e.clientY, frame0: { ...obj.frame }, frames0 };
    // Capture on the OBJECT so the derived dblclick still targets it.
    objEl.setPointerCapture(e.pointerId);
  });

  pageEl.addEventListener('pointermove', (e) => {
    if (!marquee) return;
    const p = pagePoint(e);
    const x = Math.min(marquee.x0, p.x);
    const y = Math.min(marquee.y0, p.y);
    const w = Math.abs(p.x - marquee.x0);
    const h = Math.abs(p.y - marquee.y0);
    if (!marquee.el) {
      if (w < 4 && h < 4) return; // still a click, not a drag
      marquee.el = document.createElement('div');
      marquee.el.className = 'cv-marquee cv-chrome';
      pageEl.appendChild(marquee.el);
    }
    marquee.el.style.left = x + 'px';
    marquee.el.style.top = y + 'px';
    marquee.el.style.width = w + 'px';
    marquee.el.style.height = h + 'px';
  });

  pageEl.addEventListener('pointerup', (e) => {
    if (!marquee) return;
    if (!marquee.el) {
      // Plain click on empty page — deselect.
      marquee = null;
      select(null);
      return;
    }
    const p = pagePoint(e);
    const rx = Math.min(marquee.x0, p.x);
    const ry = Math.min(marquee.y0, p.y);
    const rw = Math.abs(p.x - marquee.x0);
    const rh = Math.abs(p.y - marquee.y0);
    marquee.el.remove();
    marquee = null;
    const hit = DOC.objects.filter((o) => {
      const f = o.frame || {};
      return (f.x || 0) < rx + rw && (f.x || 0) + (f.w || 0) > rx
        && (f.y || 0) < ry + rh && (f.y || 0) + (f.h || 0) > ry;
    }).map((o) => o.object_id);
    selectMany(hit);
  });
  pageEl.addEventListener('pointercancel', () => {
    if (marquee && marquee.el) marquee.el.remove();
    marquee = null;
  });

  // ── Snapping (follow-up; owns the full smart-guide
  // story). While dragging, edges/centers snap to other objects, guides,
  // margins, page edges/center — and the grid when "Snap to grid" is on.
  // Hold Alt to drag free.
  const SNAP_THRESHOLD = 6; // page px

  function snapCandidates(excludeIds) {
    const xs = [0, DOC.page.width / 2, DOC.page.width];
    const ys = [0, DOC.page.height / 2, DOC.page.height];
    const m = DOC.page.margins || {};
    xs.push(m.left || 0, DOC.page.width - (m.right || 0));
    ys.push(m.top || 0, DOC.page.height - (m.bottom || 0));
    (DOC.page.guides.v || []).forEach((v) => xs.push(v));
    (DOC.page.guides.h || []).forEach((v) => ys.push(v));
    DOC.objects.forEach((o) => {
      if (!o.frame || excludeIds.has(o.object_id)) return;
      xs.push(o.frame.x, o.frame.x + (o.frame.w || 0) / 2, o.frame.x + (o.frame.w || 0));
      ys.push(o.frame.y, o.frame.y + (o.frame.h || 0) / 2, o.frame.y + (o.frame.h || 0));
    });
    return { xs, ys };
  }

  function bestSnap(edges, candidates, gridSize) {
    let best = null;
    edges.forEach((edge) => {
      const withGrid = gridSize ? candidates.concat([Math.round(edge / gridSize) * gridSize]) : candidates;
      withGrid.forEach((c) => {
        const delta = c - edge;
        if (Math.abs(delta) <= SNAP_THRESHOLD && (!best || Math.abs(delta) < Math.abs(best.delta))) {
          best = { delta: delta, at: c };
        }
      });
    });
    return best;
  }

  let snapLineV = null;
  let snapLineH = null;
  function updateSnapLines(x, y) {
    if (snapLineV && !snapLineV.isConnected) snapLineV = null;
    if (snapLineH && !snapLineH.isConnected) snapLineH = null;
    if (x == null) {
      if (snapLineV) { snapLineV.remove(); snapLineV = null; }
    } else {
      if (!snapLineV) {
        snapLineV = document.createElement('div');
        snapLineV.className = 'cv-snapline cv-snapline-v cv-chrome';
        pageEl.appendChild(snapLineV);
      }
      snapLineV.style.left = x + 'px';
    }
    if (y == null) {
      if (snapLineH) { snapLineH.remove(); snapLineH = null; }
    } else {
      if (!snapLineH) {
        snapLineH = document.createElement('div');
        snapLineH.className = 'cv-snapline cv-snapline-h cv-chrome';
        pageEl.appendChild(snapLineH);
      }
      snapLineH.style.top = y + 'px';
    }
  }

  pageEl.addEventListener('pointermove', (e) => {
    if (!objDrag) return;
    if (objDrag.kind === 'gresize') {
      // scale every member's frame by the bbox scale factors. The
      // scale floor keeps the smallest member at the per-object minimum; the
      // ceiling keeps the bbox on the page. Snapping is skipped — a scaled
      // group has no single meaningful edge to snap (same call as rotated
      // resize).
      const b0 = objDrag.bbox0;
      const frames = objDrag.frames0;
      const list = Object.keys(frames);
      const dx = (e.clientX - objDrag.startX) / scale;
      const dy = (e.clientY - objDrag.startY) / scale;
      const sxMin = Math.max(...list.map((id) => 24 / Math.max(frames[id].w, 1)));
      const syMin = Math.max(...list.map((id) => 16 / Math.max(frames[id].h, 1)));
      const sx = Math.min(Math.max((b0.w + dx) / b0.w, sxMin), (DOC.page.width - b0.x) / b0.w);
      const sy = Math.min(Math.max((b0.h + dy) / b0.h, syMin), (DOC.page.height - b0.y) / b0.h);
      list.forEach((id) => {
        const o = getObj(id);
        const f0 = frames[id];
        if (!o || !o.frame) return;
        o.frame.x = Math.round(b0.x + (f0.x - b0.x) * sx);
        o.frame.y = Math.round(b0.y + (f0.y - b0.y) * sy);
        o.frame.w = Math.round(f0.w * sx);
        o.frame.h = Math.round(f0.h * sy);
        const n = pageEl.querySelector('[data-object-id="' + id + '"]');
        if (n) {
          n.style.left = o.frame.x + 'px';
          n.style.top = o.frame.y + 'px';
          n.style.width = o.frame.w + 'px';
          n.style.height = o.frame.h + 'px';
        }
      });
      syncGroupBox();
      markDirty();
      return;
    }
    const obj = getObj(objDrag.id);
    if (!obj) { objDrag = null; return; }
    if (objDrag.kind === 'rotate') {
      // Angle from the object center to the pointer; the handle sits at the
      // top of the object, so straight up = 0°. Shift snaps to 15° steps.
      const f = obj.frame;
      const p = pagePoint(e);
      let deg = Math.atan2(p.y - (f.y + f.h / 2), p.x - (f.x + f.w / 2)) * 180 / Math.PI + 90;
      if (e.shiftKey) deg = Math.round(deg / 15) * 15;
      deg = ((Math.round(deg) + 180) % 360 + 360) % 360 - 180; // → [-180, 180)
      if (f.rotation !== deg) {
        f.rotation = deg;
        const n = pageEl.querySelector('[data-object-id="' + objDrag.id + '"]');
        if (n) n.style.transform = deg ? 'rotate(' + deg + 'deg)' : '';
        $('cv-obj-rotation').value = deg;
        markDirty();
      }
      return;
    }
    const dx = (e.clientX - objDrag.startX) / scale;
    const dy = (e.clientY - objDrag.startY) / scale;
    const gridSize = DOC.page.grid.snap ? clampInt(DOC.page.grid.size, 4, 500, 20) : 0;
    const dragSet = new Set(Object.keys(objDrag.frames0 || {}));
    dragSet.add(objDrag.id);
    // page.smart_snap off keeps grid snapping (its own checkbox)
    // but drops the object/guide/margin/page candidates; Alt drops everything.
    const cand = e.altKey ? null
      : (DOC.page.smart_snap === false ? { xs: [], ys: [] } : snapCandidates(dragSet));
    let lineX = null;
    let lineY = null;
    if (objDrag.kind === 'move') {
      // The whole selection moves by one shared delta, clamped so every
      // member stays on the page (relative positions never distort).
      const ids = Object.keys(objDrag.frames0);
      let minDx = -Infinity; let maxDx = Infinity;
      let minDy = -Infinity; let maxDy = Infinity;
      ids.forEach((sid) => {
        const f0 = objDrag.frames0[sid];
        minDx = Math.max(minDx, -f0.x);
        maxDx = Math.min(maxDx, DOC.page.width - f0.w - f0.x);
        minDy = Math.max(minDy, -f0.y);
        maxDy = Math.min(maxDy, DOC.page.height - f0.h - f0.y);
      });
      let dxA = Math.min(Math.max(dx, minDx), maxDx);
      let dyA = Math.min(Math.max(dy, minDy), maxDy);
      if (cand) {
        const f0 = objDrag.frame0;
        const nx = f0.x + dxA;
        const ny = f0.y + dyA;
        const sx = bestSnap([nx, nx + f0.w / 2, nx + f0.w], cand.xs, gridSize);
        if (sx) { dxA = Math.min(Math.max(dxA + sx.delta, minDx), maxDx); lineX = sx.at; }
        const sy = bestSnap([ny, ny + f0.h / 2, ny + f0.h], cand.ys, gridSize);
        if (sy) { dyA = Math.min(Math.max(dyA + sy.delta, minDy), maxDy); lineY = sy.at; }
      }
      ids.forEach((sid) => {
        const o = getObj(sid);
        const f0 = objDrag.frames0[sid];
        if (!o || !f0) return;
        o.frame.x = Math.round(f0.x + dxA);
        o.frame.y = Math.round(f0.y + dyA);
        const n = pageEl.querySelector('[data-object-id="' + sid + '"]');
        if (n) { n.style.left = o.frame.x + 'px'; n.style.top = o.frame.y + 'px'; }
      });
      if (groupBoxEl) syncGroupBox(); // the group bbox rides along
      updateSnapLines(lineX, lineY);
      markDirty();
      return;
    }
    // on a rotated object, project the pointer delta onto the
    // object's local axes so the SE handle grows width/height the way the
    // handle visually moves. Axis-aligned snapping is meaningless on rotated
    // edges, so it only applies at rotation 0.
    const rot = Number(obj.frame.rotation) || 0;
    let rdx = dx;
    let rdy = dy;
    if (rot) {
      const rad = rot * Math.PI / 180;
      rdx = dx * Math.cos(rad) + dy * Math.sin(rad);
      rdy = -dx * Math.sin(rad) + dy * Math.cos(rad);
    }
    let nw = Math.min(Math.max(24, objDrag.frame0.w + rdx), DOC.page.width - obj.frame.x);
    let nh = Math.min(Math.max(16, objDrag.frame0.h + rdy), DOC.page.height - obj.frame.y);
    if (cand && !rot) {
      const sx = bestSnap([obj.frame.x + nw], cand.xs, gridSize);
      if (sx) { nw += sx.delta; lineX = sx.at; }
      const sy = bestSnap([obj.frame.y + nh], cand.ys, gridSize);
      if (sy) { nh += sy.delta; lineY = sy.at; }
    }
    obj.frame.w = Math.round(Math.min(Math.max(24, nw), DOC.page.width - obj.frame.x));
    obj.frame.h = Math.round(Math.min(Math.max(16, nh), DOC.page.height - obj.frame.y));
    updateSnapLines(lineX, lineY);
    markDirty();
    const el = pageEl.querySelector('[data-object-id="' + objDrag.id + '"]');
    if (el) {
      el.style.width = obj.frame.w + 'px';
      el.style.height = obj.frame.h + 'px';
    }
  });

  const endObjDrag = () => {
    objDrag = null;
    updateSnapLines(null, null);
  };
  pageEl.addEventListener('pointerup', endObjDrag);
  pageEl.addEventListener('pointercancel', endObjDrag);

  // ── Align the selection within the page's margin box ──
  // The selection moves AS A GROUP: one shared delta aligns its bounding box
  // to the page box, preserving relative layout. (Per-object alignment
  // stacked every selected item onto the same center line.) A single object
  // degenerates to the same result as aligning it directly.
  function _patchObjectPosition(obj) {
    const el = pageEl.querySelector('[data-object-id="' + obj.object_id + '"]');
    if (el) {
      el.style.left = obj.frame.x + 'px';
      el.style.top = obj.frame.y + 'px';
    }
  }

  // align/distribute operate on UNITS — a whole group is one unit
  // (moved by a shared delta so its internal layout never distorts), an
  // ungrouped object is its own unit. All-solo selections behave exactly as
  // the per-object version did.
  function selectionUnits() {
    const byKey = {};
    const units = [];
    selectedIds.forEach((id) => {
      const o = getObj(id);
      if (!o || !o.frame || o.locked) return;
      const key = o.group_id || ('solo-' + id);
      if (!byKey[key]) {
        byKey[key] = { objs: [] };
        units.push(byKey[key]);
      }
      byKey[key].objs.push(o);
    });
    units.forEach((u) => {
      u.x = Math.min(...u.objs.map((o) => o.frame.x));
      u.y = Math.min(...u.objs.map((o) => o.frame.y));
      u.w = Math.max(...u.objs.map((o) => o.frame.x + o.frame.w)) - u.x;
      u.h = Math.max(...u.objs.map((o) => o.frame.y + o.frame.h)) - u.y;
    });
    return units;
  }

  function _moveUnit(u, dx, dy) {
    // Shared delta, clamped so the unit's bbox stays on the page.
    dx = Math.min(Math.max(dx, -u.x), Math.max(-u.x, DOC.page.width - u.w - u.x));
    dy = Math.min(Math.max(dy, -u.y), Math.max(-u.y, DOC.page.height - u.h - u.y));
    if (!dx && !dy) return;
    u.objs.forEach((o) => {
      o.frame.x = Math.round(o.frame.x + dx);
      o.frame.y = Math.round(o.frame.y + dy);
      _patchObjectPosition(o);
    });
  }

  // PowerPoint semantics:
  //  - single unit (one object OR one whole group): align/center within the
  //    page's margin box
  //  - multiple units, left/right/top/bottom: align to the selection's own
  //    extreme edge (left-most / right-most / top-most / bottom-most unit)
  //  - multiple units, center/middle = DISTRIBUTE: space the units evenly
  //    between the selection's extremes (the outer two stay put)
  function alignSelected(mode2) {
    const units = selectionUnits();
    if (!units.length) return;

    if (units.length === 1) {
      const m = DOC.page.margins || {};
      const left = m.left || 0;
      const top = m.top || 0;
      const right = DOC.page.width - (m.right || 0);
      const bottom = DOC.page.height - (m.bottom || 0);
      const u = units[0];
      let dx = 0;
      let dy = 0;
      if (mode2 === 'left') dx = left - u.x;
      else if (mode2 === 'center') dx = Math.round(left + (right - left - u.w) / 2) - u.x;
      else if (mode2 === 'right') dx = Math.max(0, right - u.w) - u.x;
      else if (mode2 === 'top') dy = top - u.y;
      else if (mode2 === 'middle') dy = Math.round(top + (bottom - top - u.h) / 2) - u.y;
      else if (mode2 === 'bottom') dy = Math.max(0, bottom - u.h) - u.y;
      _moveUnit(u, dx, dy);
      syncGroupBox();
      markDirty();
      return;
    }

    const selLeft = Math.min(...units.map((u) => u.x));
    const selTop = Math.min(...units.map((u) => u.y));
    const selRight = Math.max(...units.map((u) => u.x + u.w));
    const selBottom = Math.max(...units.map((u) => u.y + u.h));

    if (mode2 === 'center' || mode2 === 'middle') {
      // Distribute: sort along the axis, keep the outer units fixed, and
      // make every gap equal (negative gaps = even overlap, like PowerPoint).
      const horizontal = mode2 === 'center';
      const sorted = [...units].sort((a, b) => (horizontal ? a.x - b.x : a.y - b.y));
      if (sorted.length < 3) return; // outer two ARE the extremes — nothing to space
      const span = horizontal ? selRight - selLeft : selBottom - selTop;
      const total = sorted.reduce((s, u) => s + (horizontal ? u.w : u.h), 0);
      const gap = (span - total) / (sorted.length - 1);
      let cursor = horizontal ? selLeft : selTop;
      sorted.forEach((u) => {
        _moveUnit(u, horizontal ? Math.round(cursor) - u.x : 0, horizontal ? 0 : Math.round(cursor) - u.y);
        cursor += (horizontal ? u.w : u.h) + gap;
      });
      syncGroupBox();
      markDirty();
      return;
    }

    units.forEach((u) => {
      let dx = 0;
      let dy = 0;
      if (mode2 === 'left') dx = selLeft - u.x;
      else if (mode2 === 'right') dx = selRight - u.w - u.x;
      else if (mode2 === 'top') dy = selTop - u.y;
      else if (mode2 === 'bottom') dy = selBottom - u.h - u.y;
      _moveUnit(u, dx, dy);
    });
    syncGroupBox();
    markDirty();
  }

  document.querySelectorAll('#cv-align-row button').forEach((btn) => {
    btn.addEventListener('click', () => alignSelected(btn.dataset.align));
  });

  // ── Text editing (double-click, edit mode only) ──

  // follow-up: the MAX_TEXT_LEN cap used to be a silent slice on
  // blur only — typing or pasting into the contenteditable could grow the
  // box unbounded until commit. Enforce it live: truncate as the content
  // changes, keep the caret at the end, and say so once per edit session.
  function enforceTextCap(objEl, state) {
    const t = objEl.textContent || '';
    if (t.length <= MAX_TEXT_LEN) return;
    objEl.textContent = t.slice(0, MAX_TEXT_LEN);
    const range = document.createRange();
    range.selectNodeContents(objEl);
    range.collapse(false);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    if (!state.warned) {
      state.warned = true;
      toast('Text is limited to ' + MAX_TEXT_LEN + ' characters.', true);
    }
  }

  // ── rich-text editing session ──
  // The object edits IN PLACE: buildObjectEl already rendered the model's
  // DOM (spans/blocks for rich, a bare text node for plain), contenteditable
  // turns that same DOM editable, and on commit parseRichFromDom() funnels
  // whatever markup the browser produced back through a whitelist into the
  // structured model. HTML never persists.
  const richToolbar = $('cv-rich-toolbar');
  let richEdit = null; // {objEl, obj, onInput, onBlur}
  let richSavedRange = null; // selection preserved across toolbar <select> focus

  function saveRichSelection() {
    const s = window.getSelection();
    if (s && s.rangeCount) richSavedRange = s.getRangeAt(0).cloneRange();
  }

  function restoreRichSelection() {
    if (!richSavedRange) return;
    const s = window.getSelection();
    s.removeAllRanges();
    s.addRange(richSavedRange);
  }

  function positionRichToolbar(objEl) {
    const r = objEl.getBoundingClientRect();
    richToolbar.classList.add('visible');
    const tb = richToolbar.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left), window.innerWidth - tb.width - 8);
    const top = r.top - tb.height - 8;
    richToolbar.style.left = left + 'px';
    richToolbar.style.top = (top < 8 ? r.bottom + 8 : top) + 'px';
  }

  function syncRichToolbarState() {
    if (!richEdit) return;
    ['bold', 'italic', 'underline'].forEach((cmd) => {
      const btn = richToolbar.querySelector('[data-rt="' + cmd + '"]');
      let on = false;
      try { on = document.queryCommandState(cmd); } catch (err) { /* unsupported */ }
      if (btn) btn.classList.toggle('active', on);
    });
  }

  document.addEventListener('selectionchange', () => {
    if (richEdit) syncRichToolbarState();
  });

  // Convert the <font> elements execCommand leaves behind into the spans
  // the parser reads: fontSize only speaks 1–7 (so the real px rides
  // richPendingSize), fontName only speaks face="…" (we pass the token key).
  let richPendingSize = 0;

  function convertFontTags(objEl, tokens) {
    const changed = [];
    objEl.querySelectorAll('font').forEach((f) => {
      const span = document.createElement('span');
      if (f.getAttribute('size') && richPendingSize) span.style.fontSize = richPendingSize + 'px';
      const face = f.getAttribute('face') || '';
      if (face && tokens.fonts[face]) span.style.fontFamily = tokens.fonts[face];
      if (f.getAttribute('color')) span.style.color = f.getAttribute('color');
      if (f.style && f.style.cssText) span.style.cssText += f.style.cssText;
      while (f.firstChild) span.appendChild(f.firstChild);
      f.replaceWith(span);
      changed.push(span);
    });
    // bug fix: with styleWithCSS on, Chrome's fontSize emits
    // keyword-sized spans (font-size: xxx-large) instead of <font size> —
    // the keyword rendered huge and, being unparseable, was dropped on
    // commit. Normalize any keyword size to the real requested px.
    if (richPendingSize) {
      objEl.querySelectorAll('span').forEach((s) => {
        const fs = (s.style && s.style.fontSize) || '';
        if (fs && !/^\d/.test(fs)) {
          s.style.fontSize = richPendingSize + 'px';
          changed.push(s);
        }
      });
    }
    // Replacing nodes kills the live selection, which made every FOLLOWING
    // toolbar command a no-op. Re-select the converted run(s).
    if (changed.length) {
      const range = document.createRange();
      range.setStartBefore(changed[0]);
      range.setEndAfter(changed[changed.length - 1]);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  }

  function applyRichCommand(action, value) {
    if (!richEdit) return;
    const tokens = cvTextTokens();
    richEdit.objEl.focus();
    restoreRichSelection();
    document.execCommand('styleWithCSS', false, true);
    if (action === 'size') {
      const px = parseInt(value, 10);
      if (!px || tokens.sizes.indexOf(px) === -1) return;
      richPendingSize = px;
      // fontSize must run with styleWithCSS OFF so every browser emits the
      // <font size="7"> marker convertFontTags rewrites (with it on, Chrome
      // emits keyword-sized spans — the "text becomes huge then reverts"
      // bug). Restored right after; the other commands keep CSS output.
      document.execCommand('styleWithCSS', false, false);
      document.execCommand('fontSize', false, '7');
      document.execCommand('styleWithCSS', false, true);
      convertFontTags(richEdit.objEl, tokens);
      richPendingSize = 0;
    } else if (action === 'font') {
      if (!tokens.fonts[value]) return;
      document.execCommand('fontName', false, value);
      convertFontTags(richEdit.objEl, tokens);
    } else if (action === 'color') {
      if (!tokens.colors[value]) return;
      document.execCommand('foreColor', false, tokens.colors[value]);
    } else {
      document.execCommand(action, false, null);
    }
    saveRichSelection();
    syncRichToolbarState();
  }

  // Parse the edited contenteditable DOM back into the rich model. Browsers
  // emit a zoo of markup for the same edit (font tags vs spans, b vs
  // strong, divs vs p) — everything funnels through this whitelist, so only
  // the model's own keys survive. Returns {content, rich} where rich is
  // null when the result carries no formatting (plain docs stay plain).
  function parseRichFromDom(root) {
    const tokens = cvTextTokens();
    const colorKeyByHex = {};
    Object.keys(tokens.colors).forEach((k) => { colorKeyByHex[tokens.colors[k].toLowerCase()] = k; });

    function cssColorToHex(c) {
      const v = (c || '').trim().toLowerCase();
      const m = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(v);
      if (!m) return v;
      return '#' + [m[1], m[2], m[3]].map((x) => (+x).toString(16).padStart(2, '0')).join('');
    }
    function fontTokenOf(cssFamily) {
      const fam = (cssFamily || '').replace(/['"]/g, '').toLowerCase();
      if (!fam) return '';
      if (tokens.fonts[fam]) return fam; // <font face="sans"> carries the bare key
      const keys = Object.keys(tokens.fonts);
      for (let k = 0; k < keys.length; k++) {
        const first = tokens.fonts[keys[k]].split(',')[0].replace(/['"]/g, '').toLowerCase();
        if (fam.indexOf(first) !== -1) return keys[k];
      }
      return '';
    }
    function nearestSize(px) {
      // bug fix: browsers can leave CSS size KEYWORDS in the DOM
      // (Chrome's styleWithCSS fontSize path emitted xxx-large) — map them
      // to px instead of dropping the size on commit.
      const KEYWORD_PX = {
        'xx-small': 10, 'x-small': 10, small: 13, medium: 16,
        large: 18, 'x-large': 24, 'xx-large': 32, 'xxx-large': 48,
      };
      const n = parseFloat(px) || KEYWORD_PX[String(px).trim().toLowerCase()] || 0;
      if (!n) return 0;
      let best = tokens.sizes[0];
      tokens.sizes.forEach((s) => { if (Math.abs(s - n) < Math.abs(best - n)) best = s; });
      return best;
    }
    function inlineFmt(node, fmt) {
      const f = Object.assign({}, fmt);
      const tag = node.tagName;
      if (tag === 'B' || tag === 'STRONG') f.bold = true;
      if (tag === 'I' || tag === 'EM') f.italic = true;
      if (tag === 'U') f.underline = true;
      const st = node.style || {};
      const weight = st.fontWeight || '';
      if (weight === 'bold' || parseInt(weight, 10) >= 600) f.bold = true;
      if (weight === 'normal' || (parseInt(weight, 10) > 0 && parseInt(weight, 10) < 600)) f.bold = false;
      if (st.fontStyle === 'italic') f.italic = true;
      if (st.fontStyle === 'normal') f.italic = false;
      const deco = st.textDecoration || st.textDecorationLine || '';
      if (deco.indexOf('underline') !== -1) f.underline = true;
      if (deco.indexOf('none') !== -1) f.underline = false;
      const color = st.color || (tag === 'FONT' ? node.getAttribute('color') : '');
      if (color) {
        const key = colorKeyByHex[cssColorToHex(color)];
        if (key) f.color = key;
      }
      const family = st.fontFamily || (tag === 'FONT' ? node.getAttribute('face') : '');
      if (family) {
        const fk = fontTokenOf(family);
        if (fk) f.font = fk;
      }
      if (st.fontSize) {
        const s = nearestSize(st.fontSize);
        if (s) f.size = s;
      }
      return f;
    }
    function pushRun(runs, text, fmt) {
      if (!text) return;
      const run = { t: text };
      if (fmt.bold) run.bold = true;
      if (fmt.italic) run.italic = true;
      if (fmt.underline) run.underline = true;
      if (fmt.color) run.color = fmt.color;
      if (fmt.font) run.font = fmt.font;
      if (fmt.size) run.size = fmt.size;
      const prev = runs[runs.length - 1];
      const same = prev && ['bold', 'italic', 'underline', 'color', 'font', 'size']
        .every((k) => prev[k] === run[k]);
      if (same) prev.t += run.t;
      else runs.push(run);
    }
    // Inline content → lines (arrays of runs); <br> starts a new line.
    function collectLines(container, fmt) {
      const lines = [[]];
      (function walk(node, f) {
        node.childNodes.forEach((child) => {
          if (child.nodeType === Node.TEXT_NODE) {
            pushRun(lines[lines.length - 1], child.nodeValue, f);
          } else if (child.nodeType === Node.ELEMENT_NODE) {
            if (child.tagName === 'BR') lines.push([]);
            else walk(child, inlineFmt(child, f));
          }
        });
      })(container, fmt);
      return lines;
    }

    const blocks = [];
    let pendingInline = document.createDocumentFragment();
    function alignOf(node) {
      const a = (node.style && node.style.textAlign) || '';
      return ['left', 'center', 'right'].indexOf(a) !== -1 ? a : '';
    }
    function flushPending() {
      if (!pendingInline.childNodes.length) return;
      collectLines(pendingInline, {}).forEach((runs) => blocks.push({ type: 'p', runs }));
      pendingInline = document.createDocumentFragment();
    }
    function pushParagraphs(node) {
      const align = alignOf(node);
      collectLines(node, {}).forEach((runs) => {
        const block = { type: 'p', runs };
        if (align) block.align = align;
        blocks.push(block);
      });
    }
    Array.prototype.slice.call(root.childNodes).forEach((node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        pendingInline.appendChild(node.cloneNode(true));
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const tag = node.tagName;
      if (tag === 'UL' || tag === 'OL') {
        flushPending();
        const block = { type: tag.toLowerCase(), items: [] };
        const align = alignOf(node);
        if (align) block.align = align;
        node.querySelectorAll(':scope > li').forEach((li) => {
          // Lines inside one <li> (Shift+Enter) fold into one item.
          const runs = [];
          collectLines(li, {}).forEach((line, idx) => {
            if (idx > 0) pushRun(runs, ' ', {});
            line.forEach((r) => runs.push(r));
          });
          block.items.push(runs);
        });
        if (block.items.length) blocks.push(block);
      } else if (tag === 'DIV' || tag === 'P' || /^H[1-6]$/.test(tag) || tag === 'BLOCKQUOTE') {
        flushPending();
        pushParagraphs(node);
      } else {
        pendingInline.appendChild(node.cloneNode(true));
      }
    });
    flushPending();
    function isBlockEmpty(b) {
      if (b.type === 'p') return !b.runs.some((r) => r.t);
      return !b.items.some((item) => item.some((r) => r.t));
    }
    // Trailing empty paragraphs (the <div><br></div> browsers leave after
    // the last Enter) don't persist; interior blank lines do.
    while (blocks.length && isBlockEmpty(blocks[blocks.length - 1])) blocks.pop();
    if (blocks.length > MAX_RICH_BLOCKS) blocks.length = MAX_RICH_BLOCKS;

    function runsText(runs) { return runs.map((r) => r.t).join(''); }
    const content = blocks.map((b) => (
      b.type === 'p' ? runsText(b.runs) : b.items.map(runsText).join('\n')
    )).join('\n').slice(0, MAX_TEXT_LEN);

    const isPlain = blocks.every((b) => (
      b.type === 'p' && !b.align
      && b.runs.every((r) => !r.bold && !r.italic && !r.underline && !r.color && !r.font && !r.size)
    ));
    if (!blocks.length || (isPlain && blocks.length === 1)) {
      return { content, rich: null };
    }
    return { content, rich: blocks };
  }

  function commitTextEdit() {
    if (!richEdit) return;
    const { objEl, hostEl, obj, onInput } = richEdit;
    richEdit = null;
    richSavedRange = null;
    richToolbar.classList.remove('visible');
    objEl.removeEventListener('input', onInput);
    objEl.contentEditable = 'false';
    (hostEl || objEl).classList.remove('editing');
    const parsed = parseRichFromDom(objEl);
    const prev = obj.text || {};
    const changed = parsed.content !== (prev.content || '')
      || JSON.stringify(parsed.rich) !== JSON.stringify(prev.rich || null);
    if (changed) {
      obj.text = obj.text || {};
      obj.text.content = parsed.content;
      if (parsed.rich) obj.text.rich = parsed.rich;
      else delete obj.text.rich;
      markDirty();
    }
    renderPageChildren();
  }

  function beginTextEdit(hostEl) {
    const obj = hostEl && getObj(hostEl.dataset.objectId);
    if (mode !== 'edit' || !obj || obj.locked) return;
    // a time pill's wording is derived from the live time range;
    // hand-editing it would be overwritten on the next filter change.
    if (obj.role === 'time_pill') {
      toast('The time pill always shows the current time range — change it with the Time range filter above.');
      return;
    }
    // a callout shape carries text and edits through this same
    // session — the editable node is its inner .cvs-text (the shape chrome
    // around it stays inert), while a text box edits in place as before.
    const isCallout = obj.object_type === 'shape'
      && obj.shape && obj.shape.kind === 'callout';
    if (obj.object_type !== 'text' && !isCallout) return;
    const objEl = isCallout ? hostEl.querySelector('.cvs-text') : hostEl;
    if (!objEl || hostEl.classList.contains('editing')) return;
    hostEl.classList.add('editing');
    objEl.contentEditable = 'true';
    objEl.focus();
    document.execCommand('styleWithCSS', false, true);
    const capState = { warned: false };
    const onInput = () => enforceTextCap(objEl, capState);
    objEl.addEventListener('input', onInput);
    richEdit = { objEl, hostEl, obj, onInput };
    positionRichToolbar(objEl);
    syncRichToolbarState();
    // Blur commits — unless focus moved into the toolbar (a <select>
    // opening, a swatch click), in which case the session continues and
    // focus comes straight back to the object.
    objEl.addEventListener('blur', function onBlur() {
      setTimeout(() => {
        if (!richEdit || richEdit.objEl !== objEl) { objEl.removeEventListener('blur', onBlur); return; }
        const ae = document.activeElement;
        if (ae === objEl || richToolbar.contains(ae)) return;
        objEl.removeEventListener('blur', onBlur);
        commitTextEdit();
      }, 0);
    });
  }

  // Toolbar wiring. Buttons keep focus in the contenteditable via
  // pointerdown preventDefault; the two <select>s must take focus to open,
  // so they save/restore the selection around it instead.
  richToolbar.querySelectorAll('button[data-rt]').forEach((btn) => {
    btn.addEventListener('pointerdown', (e) => e.preventDefault());
    btn.addEventListener('click', () => applyRichCommand(btn.dataset.rt, btn.dataset.rtValue || null));
  });
  ['cv-rt-font', 'cv-rt-size'].forEach((id) => {
    const sel = $(id);
    sel.addEventListener('pointerdown', saveRichSelection);
    sel.addEventListener('change', () => {
      const action = id === 'cv-rt-font' ? 'font' : 'size';
      const v = sel.value;
      sel.value = '';
      if (v) applyRichCommand(action, v);
    });
  });
  // Swatches and the font/size options are built from the token set — the
  // markup carries only placeholders, so the theme has one source of truth.
  (function buildRichTokenControls() {
    const t = cvTextTokens();
    const holder = $('cv-rt-swatches');
    Object.keys(t.colors).forEach((key) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cv-rt-swatch';
      btn.title = 'Text color: ' + key;
      btn.style.background = t.colors[key];
      btn.addEventListener('pointerdown', (e) => e.preventDefault());
      btn.addEventListener('click', () => applyRichCommand('color', key));
      holder.appendChild(btn);
    });
    const fontSel = $('cv-rt-font');
    Object.keys(t.fonts).forEach((key) => {
      const o = document.createElement('option');
      o.value = key;
      o.textContent = key.charAt(0).toUpperCase() + key.slice(1);
      o.style.fontFamily = t.fonts[key];
      fontSel.appendChild(o);
    });
    const sizeSel = $('cv-rt-size');
    t.sizes.forEach((s) => {
      const o = document.createElement('option');
      o.value = String(s);
      o.textContent = s + ' px';
      sizeSel.appendChild(o);
    });
    const famSel = $('cv-obj-font-family');
    Object.keys(t.fonts).forEach((key) => {
      const o = document.createElement('option');
      o.value = key;
      o.textContent = key.charAt(0).toUpperCase() + key.slice(1);
      famSel.appendChild(o);
    });
  })();

  pageEl.addEventListener('dblclick', (e) => {
    if (mode !== 'edit') return;
    if (e.target.closest('.cv-guide')) return; // guide dblclick = remove guide
    // callouts (.cv-shape) join text boxes — beginTextEdit itself
    // gates to text objects and callout-kind shapes.
    beginTextEdit(e.target.closest('.cv-object.cv-text, .cv-object.cv-shape'));
  });

  // ── Keyboard: arrow nudge + delete (applies to the whole selection) ──
  document.addEventListener('keydown', (e) => {
    if (mode !== 'edit' || !selectedIds.size) return;
    const ae = document.activeElement;
    if (ae && (ae.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName))) return;
    const step = e.shiftKey ? 10 : 1;
    const nudges = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (nudges[e.key]) {
      selectedIds.forEach((id) => {
        const obj = getObj(id);
        if (!obj || obj.locked || !obj.frame) return;
        obj.frame.x = Math.min(Math.max(0, obj.frame.x + nudges[e.key][0]), DOC.page.width - obj.frame.w);
        obj.frame.y = Math.min(Math.max(0, obj.frame.y + nudges[e.key][1]), DOC.page.height - obj.frame.h);
        const el = pageEl.querySelector('[data-object-id="' + id + '"]');
        if (el) { el.style.left = obj.frame.x + 'px'; el.style.top = obj.frame.y + 'px'; }
      });
      syncGroupBox();
      markDirty();
      e.preventDefault();
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      deleteSelectedObject();
      e.preventDefault();
    }
  });

  function deleteSelectedObject() {
    if (!selectedIds.size) return;
    // a locked object can't be deleted — delete is an edit. Locked
    // members of the selection survive (and stay selected) with a heads-up.
    const keep = [...selectedIds].filter((id) => {
      const o = getObj(id);
      return o && o.locked;
    });
    if (keep.length === selectedIds.size) {
      toast('Locked — unlock the object to delete it.', true);
      return;
    }
    const removing = new Set([...selectedIds].filter((id) => !keep.includes(id)));
    DOC.objects = DOC.objects.filter((o) => !removing.has(o.object_id));
    // a group reduced to one member is no longer a group.
    const gidCounts = {};
    DOC.objects.forEach((o) => {
      if (o.group_id) gidCounts[o.group_id] = (gidCounts[o.group_id] || 0) + 1;
    });
    DOC.objects.forEach((o) => {
      if (o.group_id && gidCounts[o.group_id] < 2) o.group_id = null;
    });
    selectedIds = new Set(keep);
    markDirty();
    renderPageChildren();
    applySelection();
    if (keep.length) toast(keep.length + ' locked object(s) kept — unlock to delete.');
  }

  // ── Copy / paste / duplicate ──
  // In-memory clipboard of serialized objects (runtime keys stripped).
  // Pasted/duplicated copies get fresh object_ids AND fresh section_ids
  // (one per source section) so remove-section on a copy never touches
  // the original; copies always paste unlocked.
  let objectClipboard = [];

  function selectionObjectsInZOrder() {
    return DOC.objects.filter((o) => selectedIds.has(o.object_id));
  }

  function copySelection() {
    const objs = selectionObjectsInZOrder();
    if (!objs.length) return false;
    objectClipboard = JSON.parse(JSON.stringify(objs, (k, v) => (k.startsWith('_') ? undefined : v)));
    toast('Copied ' + objectClipboard.length + ' object' + (objectClipboard.length === 1 ? '' : 's') + '.');
    return true;
  }

  function cloneForInsert(list, dx, dy) {
    const cloned = JSON.parse(JSON.stringify(list, (k, v) => (k.startsWith('_') ? undefined : v)));
    const sectionMap = {};
    let sectionBase = parseInt(newSectionId().slice(4), 10);
    // fresh group_ids too — a pasted copy of a group is its own
    // group, never entangled with the original.
    const groupMap = {};
    let groupBase = parseInt(newGroupId().slice(4), 10);
    cloned.forEach((o) => {
      o.object_id = newObjectId();
      o.locked = false;
      if (o.section_id) {
        if (!sectionMap[o.section_id]) {
          sectionMap[o.section_id] = 'sec-' + sectionBase;
          sectionBase += 1;
        }
        o.section_id = sectionMap[o.section_id];
      }
      if (o.group_id) {
        if (!groupMap[o.group_id]) {
          groupMap[o.group_id] = 'grp-' + groupBase;
          groupBase += 1;
        }
        o.group_id = groupMap[o.group_id];
      }
      if (o.frame) {
        o.frame.x = Math.round(Math.min(Math.max(0, (o.frame.x || 0) + dx), Math.max(0, DOC.page.width - (o.frame.w || 0))));
        o.frame.y = Math.round(Math.min(Math.max(0, (o.frame.y || 0) + dy), Math.max(0, DOC.page.height - (o.frame.h || 0))));
      }
    });
    return cloned;
  }

  function insertObjects(list, point) {
    if (!list.length) return;
    if (DOC.objects.length + list.length > MAX_OBJECTS) {
      toast('Too many objects on the page (max ' + MAX_OBJECTS + ').', true);
      return;
    }
    // pasting/duplicating image objects counts against the same
    // in-doc image budget the upload path enforces — fail here with a clear
    // message instead of a 400 on save.
    const addedImg = list.reduce((t, o) => (
      t + ((o.object_type === 'image' && o.image && typeof o.image.src === 'string') ? o.image.src.length : 0)
    ), 0);
    if (addedImg && totalImageChars(addedImg) > MAX_TOTAL_IMAGE_CHARS) {
      toast('The page’s images would total too much data (max ~4.5MB).', true);
      return;
    }
    let dx = 16;
    let dy = 16;
    if (point) {
      const minX = Math.min(...list.map((o) => (o.frame && o.frame.x) || 0));
      const minY = Math.min(...list.map((o) => (o.frame && o.frame.y) || 0));
      dx = Math.round(point.x - minX);
      dy = Math.round(point.y - minY);
    }
    const cloned = cloneForInsert(list, dx, dy);
    DOC.objects.push(...cloned);
    markDirty();
    renderPageChildren();
    selectMany(cloned.map((o) => o.object_id));
    resolveAllFields(); // pasted data fields go live immediately
  }

  function pasteClipboard(point) {
    insertObjects(objectClipboard, point || null);
  }

  function duplicateSelection() {
    insertObjects(selectionObjectsInZOrder(), null);
  }

  // ── Right-click context menu: Copy / Paste / Duplicate / Select section ──
  const ctxMenu = $('cv-ctx-menu');
  let ctxPastePoint = null;

  function hideContextMenu() {
    ctxMenu.classList.remove('visible');
  }

  pageEl.addEventListener('contextmenu', (e) => {
    if (mode !== 'edit') return;
    e.preventDefault();
    const objEl = e.target.closest('.cv-object');
    if (objEl) {
      const id = objEl.dataset.objectId;
      if (id && !selectedIds.has(id)) select(id);
    }
    ctxPastePoint = pagePoint(e);
    const onObject = !!objEl && selectedIds.size > 0;
    $('cv-ctx-copy').disabled = !onObject;
    $('cv-ctx-duplicate').disabled = !onObject;
    $('cv-ctx-paste').disabled = !objectClipboard.length;
    const single = selectedId && getObj(selectedId);
    $('cv-ctx-select-section').style.display = objEl && single && single.section_id ? '' : 'none';
    ctxMenu.style.left = Math.min(e.clientX, window.innerWidth - 190) + 'px';
    ctxMenu.style.top = Math.min(e.clientY, window.innerHeight - 170) + 'px';
    ctxMenu.classList.add('visible');
  });

  document.addEventListener('pointerdown', (e) => {
    if (!e.target.closest('.cv-ctx-menu')) hideContextMenu();
  });
  workarea.addEventListener('scroll', hideContextMenu);

  $('cv-ctx-copy').addEventListener('click', () => {
    hideContextMenu();
    copySelection();
  });
  $('cv-ctx-paste').addEventListener('click', () => {
    hideContextMenu();
    pasteClipboard(ctxPastePoint);
  });
  $('cv-ctx-duplicate').addEventListener('click', () => {
    hideContextMenu();
    duplicateSelection();
  });
  $('cv-ctx-select-section').addEventListener('click', () => {
    hideContextMenu();
    const obj = selectedId && getObj(selectedId);
    if (!obj || !obj.section_id) return;
    selectMany(DOC.objects.filter((o) => o.section_id === obj.section_id).map((o) => o.object_id));
  });

  // ── Z-order: objects[] array order IS the z-order — index 0
  // renders at the back. Reordering re-renders (buildObjectEl re-stamps
  // zIndex from the index) and persists as plain array order. Locked objects
  // keep their layer; a multi-selection reorders as a block, preserving the
  // selection's relative order.
  function reorderSelected(dir) {
    if (!selectedIds.size) return;
    const ids = new Set([...selectedIds].filter((id) => {
      const o = getObj(id);
      return o && !o.locked;
    }));
    if (!ids.size) {
      toast('Locked — unlock the object to change its layer.', true);
      return;
    }
    const objs = DOC.objects;
    if (dir === 'front' || dir === 'back') {
      const picked = objs.filter((o) => ids.has(o.object_id));
      const rest = objs.filter((o) => !ids.has(o.object_id));
      DOC.objects = dir === 'front' ? rest.concat(picked) : picked.concat(rest);
    } else if (dir === 'forward') {
      for (let i = objs.length - 2; i >= 0; i--) {
        if (ids.has(objs[i].object_id) && !ids.has(objs[i + 1].object_id)) {
          const t = objs[i]; objs[i] = objs[i + 1]; objs[i + 1] = t;
        }
      }
    } else if (dir === 'backward') {
      for (let i = 1; i < objs.length; i++) {
        if (ids.has(objs[i].object_id) && !ids.has(objs[i - 1].object_id)) {
          const t = objs[i]; objs[i] = objs[i - 1]; objs[i - 1] = t;
        }
      }
    }
    markDirty();
    renderPageChildren();
  }

  document.querySelectorAll('#cv-layer-row button').forEach((btn) => {
    btn.addEventListener('click', () => reorderSelected(btn.dataset.layer));
  });

  // ── Sections: a section = the objects sharing a section_id ──
  function contentBottom() {
    return DOC.objects.reduce(
      (m, o) => Math.max(m, ((o.frame && o.frame.y) || 0) + ((o.frame && o.frame.h) || 0)),
      (DOC.page.margins && DOC.page.margins.top) || 48
    );
  }

  // bug fix: "+ Section" used to also insert an unbound data field
  // and select IT — the sidebar opened on "Data field", which read as
  // "+ Section added a data field". A new section is now just its titled
  // header band (same shape the seeder emits), selected so the panel's
  // color/fill controls are immediately at hand; metrics come in via
  // "+ Field".
  function addSection() {
    const sid = newSectionId();
    const m = DOC.page.margins || {};
    const x = m.left || 48;
    const w = Math.max(200, DOC.page.width - x - (m.right || 48));
    const y = contentBottom() + 24;
    const needed = y + 40 + ((m.bottom || 48));
    if (needed > DOC.page.height) {
      DOC.page.height = Math.min(needed, PAGE_MAX);
      syncPanel();
      renderAll();
    }
    const header = {
      object_id: newObjectId(), object_type: 'text', role: 'section_header', section_id: sid,
      frame: { x, y, w, h: 40, rotation: 0 }, locked: false, group_id: null,
      style: { font_size: 20, bold: true, bg_color: '#eef3f9' }, text: { content: 'New section' },
    };
    DOC.objects.push(header);
    markDirty();
    renderPageChildren();
    select(header.object_id);
  }

  function removeSection(sid) {
    const members = DOC.objects.filter((o) => o.section_id === sid);
    if (!members.length) return;
    const top = Math.min(...members.map((o) => (o.frame && o.frame.y) || 0));
    const bottom = Math.max(...members.map((o) => ((o.frame && o.frame.y) || 0) + ((o.frame && o.frame.h) || 0)));
    DOC.objects = DOC.objects.filter((o) => o.section_id !== sid);
    const shift = bottom - top + 24;
    DOC.objects.forEach((o) => {
      if (o.frame && (o.frame.y || 0) >= bottom) o.frame.y = Math.max(0, o.frame.y - shift);
    });
    selectedId = null;
    markDirty();
    renderPageChildren();
    syncObjectPanel();
  }

  // ── Selected-object panel ──
  const objPanel = $('cv-object-panel');
  let dataViewsLoaded = false;
  // the raw {id, label} list, kept for the calc-variable rows
  // (each row carries its own data-view dropdown).
  let dataViewOptions = [];

  async function loadDataViews() {
    if (dataViewsLoaded) return;
    const sel = $('cv-obj-dv');
    try {
      const res = await fetch(API_BASE + 'data-views');
      const data = await res.json();
      const views = data.data_views || [];
      dataViewOptions = views.map((v) => ({ id: v.id, label: v.label || v.id }));
      sel.innerHTML = '';
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = 'Choose…';
      sel.appendChild(blank);
      views.forEach((v) => {
        const o = document.createElement('option');
        o.value = v.id;
        o.textContent = v.label || v.id;
        sel.appendChild(o);
      });
      dataViewsLoaded = true;
    } catch (err) {
      sel.innerHTML = '<option value="">Failed to load data views</option>';
    }
  }

  // wrap the field picker with the Dashboard Builder's
  // filter-as-you-type input (_makeSearchableSelect, builder.js — already
  // loaded on this page for the table panel). Idempotent: a re-call after
  // every option refresh re-snapshots the list and clears the filter.
  function makeFieldSelectSearchable(sel) {
    if (typeof _makeSearchableSelect === 'function') _makeSearchableSelect(sel);
  }

  async function loadBindingFields(dataView, selectedField) {
    const sel = $('cv-obj-field');
    if (!dataView) {
      sel.innerHTML = '<option value="">Pick a data view first</option>';
      bindingFieldNames = [];
      makeFieldSelectSearchable(sel);
      return;
    }
    sel.innerHTML = '<option value="">Loading…</option>';
    try {
      const res = await fetch(API_BASE + 'data-views/' + encodeURIComponent(dataView) + '/fields');
      const data = await res.json();
      sel.innerHTML = '';
      // a source whose first import has not run yet has no index,
      // so no fields. Say so instead of leaving an empty select.
      if (data && data.no_data_yet) {
        const o = document.createElement('option');
        o.value = '';
        o.textContent = data.message || 'No data imported yet for this source';
        o.disabled = true;
        o.selected = true;
        sel.appendChild(o);
      }
      bindingFieldNames = (data.fields || []).map((f) => f.name);
      (data.fields || []).forEach((f) => {
        const o = document.createElement('option');
        o.value = f.name;
        o.textContent = f.name;
        sel.appendChild(o);
      });
      if (selectedField) {
        if (!Array.prototype.some.call(sel.options, (o) => o.value === selectedField)) {
          const o = document.createElement('option');
          o.value = selectedField;
          o.textContent = selectedField;
          sel.insertBefore(o, sel.firstChild);
        }
        sel.value = selectedField;
      }
      makeFieldSelectSearchable(sel);
    } catch (err) {
      sel.innerHTML = '<option value="">Failed to load fields</option>';
      bindingFieldNames = [];
      makeFieldSelectSearchable(sel);
    }
  }

  function syncEqualsVisibility() {
    $('cv-obj-equals-field').style.display = $('cv-obj-agg').value === 'equals' ? '' : 'none';
  }

  // ── per-binding row filters (field = value pairs, ANDed) ──
  // Rows are working state in #cv-obj-filters-list; "Apply binding" reads
  // them back into binding.filters. Mirrors routes._MAX_CANVAS_BINDING_FILTERS.
  const MAX_BINDING_FILTERS = 10;
  let bindingFieldNames = []; // field names of the currently picked data view

  function bindingFilterRowEl(field, value) {
    const row = document.createElement('div');
    row.className = 'cv-filter-row';
    const sel = document.createElement('select');
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = 'Field…';
    sel.appendChild(blank);
    bindingFieldNames.forEach((name) => {
      const o = document.createElement('option');
      o.value = name;
      o.textContent = name;
      sel.appendChild(o);
    });
    // A stored field missing from the current list (legacy/off-catalog)
    // stays selectable — same pattern as the binding field picker.
    if (field && !Array.prototype.some.call(sel.options, (o) => o.value === field)) {
      const o = document.createElement('option');
      o.value = field;
      o.textContent = field;
      sel.appendChild(o);
    }
    sel.value = field || '';
    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 200;
    input.placeholder = 'Value';
    input.value = value || '';
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'cv-mini-btn';
    del.textContent = '✕';
    del.title = 'Remove filter';
    del.addEventListener('click', () => row.remove());
    row.appendChild(sel);
    row.appendChild(input);
    row.appendChild(del);
    return row;
  }

  function renderBindingFilters(filters) {
    const list = $('cv-obj-filters-list');
    list.innerHTML = '';
    (filters || []).slice(0, MAX_BINDING_FILTERS).forEach((f) => {
      list.appendChild(bindingFilterRowEl((f && f.field) || '', (f && f.value) || ''));
    });
  }

  function readBindingFilterRows() {
    return Array.from($('cv-obj-filters-list').children).map((row) => ({
      field: row.querySelector('select').value,
      value: row.querySelector('input').value,
    }));
  }

  // ── calculated fields: a formula (the
  // calculation-column grammar, parsed by the shared cvParseFormula in
  // canvas_charts.js) over named variables, each variable a earlier
  // metric binding. Rows are working state in #cv-calc-vars-list; "Apply
  // calculation" validates and writes obj.calc = {formula, variables}. ──
  const MAX_CALC_VARS = 10; // mirrors routes._MAX_CANVAS_CALC_VARIABLES
  // Names are plain identifiers (no dots — dotted names are for the
  // builder's direct field references; here every reference is an alias).
  const CALC_VAR_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
  const calcFieldsCache = {}; // data view -> [field names] for the var rows

  async function calcDvFields(dv) {
    if (!dv) return [];
    if (calcFieldsCache[dv]) return calcFieldsCache[dv];
    try {
      const res = await fetch(API_BASE + 'data-views/' + encodeURIComponent(dv) + '/fields');
      const data = await res.json();
      calcFieldsCache[dv] = (data.fields || []).map((f) => f.name);
    } catch (err) {
      calcFieldsCache[dv] = [];
    }
    return calcFieldsCache[dv];
  }

  function calcVarRowEl(name, binding) {
    const b = binding || {};
    const row = document.createElement('div');
    row.className = 'cv-calc-var-row';
    // Filters on a variable binding aren't editable here (the row is
    // already four controls wide) but a stored set — hand-authored or
    // AI-written — must survive an Apply round-trip, so it rides the row.
    row._cvFilters = Array.isArray(b.filters) ? b.filters : null;
    const nameIn = document.createElement('input');
    nameIn.type = 'text';
    nameIn.className = 'cv-calc-var-name';
    nameIn.maxLength = 30;
    nameIn.placeholder = 'name';
    nameIn.title = 'Variable name the formula references (letters, digits, underscores)';
    nameIn.value = name || '';
    const dvSel = document.createElement('select');
    dvSel.className = 'cv-calc-var-dv';
    const dvBlank = document.createElement('option');
    dvBlank.value = '';
    dvBlank.textContent = 'Data view…';
    dvSel.appendChild(dvBlank);
    dataViewOptions.forEach((v) => {
      const o = document.createElement('option');
      o.value = v.id;
      o.textContent = v.label;
      dvSel.appendChild(o);
    });
    if (b.data_view && !Array.prototype.some.call(dvSel.options, (o) => o.value === b.data_view)) {
      const o = document.createElement('option');
      o.value = b.data_view;
      o.textContent = b.data_view;
      dvSel.appendChild(o);
    }
    dvSel.value = b.data_view || '';
    const fieldSel = document.createElement('select');
    fieldSel.className = 'cv-calc-var-field';
    const aggSel = document.createElement('select');
    aggSel.className = 'cv-calc-var-agg';
    [['sum', 'Sum'], ['avg', 'Average'], ['min', 'Min'], ['max', 'Max'],
      ['cardinality', 'Unique count'], ['equals', 'Count where equals…'],
      ['exists', 'Count where exists']].forEach(([v, label]) => {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = label;
      aggSel.appendChild(o);
    });
    aggSel.value = ['avg', 'min', 'max', 'cardinality', 'equals', 'exists'].includes(b.aggregation) ? b.aggregation : 'sum';
    const eqIn = document.createElement('input');
    eqIn.type = 'text';
    eqIn.className = 'cv-calc-var-eq';
    eqIn.maxLength = 200;
    eqIn.placeholder = 'Equals value';
    eqIn.value = b.equals_value !== undefined && b.equals_value !== null ? String(b.equals_value) : '';
    eqIn.style.display = aggSel.value === 'equals' ? '' : 'none';
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'cv-mini-btn';
    del.textContent = '✕';
    del.title = 'Remove variable';
    del.addEventListener('click', () => row.remove());
    function fillFields(selected) {
      const dv = dvSel.value;
      if (!dv) {
        fieldSel.innerHTML = '<option value="">Pick a data view first</option>';
        return;
      }
      fieldSel.innerHTML = '<option value="">Loading…</option>';
      calcDvFields(dv).then((names) => {
        if (dvSel.value !== dv) return; // superseded by a newer pick
        fieldSel.innerHTML = '';
        names.forEach((n) => {
          const o = document.createElement('option');
          o.value = n;
          o.textContent = n;
          fieldSel.appendChild(o);
        });
        if (!names.length) fieldSel.innerHTML = '<option value="">No fields</option>';
        if (selected) {
          if (!Array.prototype.some.call(fieldSel.options, (o) => o.value === selected)) {
            const o = document.createElement('option');
            o.value = selected;
            o.textContent = selected;
            fieldSel.insertBefore(o, fieldSel.firstChild);
          }
          fieldSel.value = selected;
        }
      });
    }
    dvSel.addEventListener('change', () => fillFields(fieldSel.value));
    aggSel.addEventListener('change', () => {
      eqIn.style.display = aggSel.value === 'equals' ? '' : 'none';
    });
    fillFields(b.field || '');
    row.appendChild(nameIn);
    row.appendChild(dvSel);
    row.appendChild(fieldSel);
    row.appendChild(aggSel);
    row.appendChild(eqIn);
    row.appendChild(del);
    return row;
  }

  function renderCalcVars(variables) {
    const list = $('cv-calc-vars-list');
    list.innerHTML = '';
    Object.keys(variables || {}).slice(0, MAX_CALC_VARS).forEach((n) => {
      list.appendChild(calcVarRowEl(n, variables[n]));
    });
  }

  function readCalcVarRows() {
    return Array.from($('cv-calc-vars-list').children).map((row) => ({
      name: row.querySelector('.cv-calc-var-name').value.trim(),
      data_view: row.querySelector('.cv-calc-var-dv').value,
      field: row.querySelector('.cv-calc-var-field').value,
      aggregation: row.querySelector('.cv-calc-var-agg').value || 'sum',
      equals_value: row.querySelector('.cv-calc-var-eq').value.trim(),
      filters: row._cvFilters,
    }));
  }

  function calcFormulaError(msg) {
    const box = $('cv-calc-formula-err');
    box.textContent = msg || '';
    box.style.display = msg ? '' : 'none';
  }

  function hydrateCalcPanel(obj) {
    const calc = obj.calc || {};
    $('cv-calc-formula').value = calc.formula || '';
    calcFormulaError('');
    loadDataViews().then(() => renderCalcVars(calc.variables || {}));
  }

  // Same two-speed error UX as the builder's formula box
  // (updateTableColumnFormula): typing only CLEARS a stale error on
  // success; leaving the box (or Apply) is when a bad formula lights up —
  // "unexpected end of formula" on every keystroke is noise.
  $('cv-calc-formula').addEventListener('input', (e) => {
    const r = window.cvParseFormula ? window.cvParseFormula(e.target.value) : null;
    if (r && r.ok) calcFormulaError('');
  });
  $('cv-calc-formula').addEventListener('blur', (e) => {
    const v = e.target.value.trim();
    if (!v) {
      calcFormulaError('');
      return;
    }
    const r = window.cvParseFormula ? window.cvParseFormula(v) : null;
    calcFormulaError(r && !r.ok ? r.error : '');
  });

  $('cv-calc-var-add').addEventListener('click', () => {
    const list = $('cv-calc-vars-list');
    if (list.children.length >= MAX_CALC_VARS) {
      toast('At most ' + MAX_CALC_VARS + ' variables per calculated field.', true);
      return;
    }
    loadDataViews().then(() => list.appendChild(calcVarRowEl('', null)));
  });

  $('cv-calc-apply').addEventListener('click', () => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.object_type !== 'calc_field' || obj.locked) return;
    const formula = $('cv-calc-formula').value.trim();
    const parsed = window.cvParseFormula
      ? window.cvParseFormula(formula)
      : { ok: false, error: 'Formula engine unavailable.' };
    if (!parsed.ok) {
      calcFormulaError(parsed.error);
      toast('Fix the formula first: ' + parsed.error, true);
      return;
    }
    calcFormulaError('');
    const variables = {};
    for (const r of readCalcVarRows()) {
      if (!r.name && !r.data_view && !r.field) continue; // fully empty row
      if (!CALC_VAR_NAME_RE.test(r.name)) {
        toast('Variable name "' + r.name + '" must be letters, digits, or underscores (not starting with a digit).', true);
        return;
      }
      if (variables[r.name]) {
        toast('Variable "' + r.name + '" is defined twice.', true);
        return;
      }
      if (!r.data_view || !r.field) {
        toast('Variable "' + r.name + '" needs a data view and a field.', true);
        return;
      }
      const binding = { data_view: r.data_view, field: r.field, aggregation: r.aggregation };
      if (r.aggregation === 'equals') {
        if (!r.equals_value) {
          toast('Variable "' + r.name + '" needs the value to count for the equals aggregation.', true);
          return;
        }
        binding.equals_value = r.equals_value;
      }
      if (Array.isArray(r.filters) && r.filters.length) binding.filters = r.filters;
      variables[r.name] = binding;
    }
    if (!Object.keys(variables).length) {
      toast('Add at least one variable for the formula to reference.', true);
      return;
    }
    // Every referenced name must be defined — an undefined reference would
    // evaluate to the placeholder forever, which reads as "broken", so it
    // is rejected here (and again server-side on save).
    const missing = parsed.fields.filter((f) => !variables[f]);
    if (missing.length) {
      toast('The formula references undefined variable(s): ' + missing.join(', ') + '.', true);
      return;
    }
    obj.calc = { formula: formula, variables: variables };
    markDirty();
    renderPageChildren();
    resolveAllFields();
    toast('Calculation applied — value is live.');
  });

  function syncObjectPanel() {
    if (mode === 'edit' && selectedIds.size > 1) {
      // Multi-select: type/binding editors hide; align, layer + delete apply
      // to all (locked members are skipped by each action itself).
      const gid = selectionSingleGroupId();
      const anyGrouped = [...selectedIds].some((id) => {
        const o = getObj(id);
        return o && o.group_id;
      });
      objPanel.style.display = '';
      $('cv-obj-type').textContent = gid
        ? 'Group (' + selectedIds.size + ' objects)'
        : selectedIds.size + ' objects selected';
      $('cv-obj-field-fields').style.display = 'none';
      $('cv-obj-text-fields').style.display = 'none';
      $('cv-obj-icon-fields').style.display = 'none';
      $('cv-obj-shape-fields').style.display = 'none';
      $('cv-obj-image-fields').style.display = 'none';
      $('cv-obj-table-fields').style.display = 'none';
      if (typeof SELECTED_CHART_ID !== 'undefined') SELECTED_CHART_ID = null;
      $('cv-obj-theme-field').style.display = 'none'; // single-object only
      $('cv-obj-remove-section').style.display = 'none';
      // Group merges the selection into one new group; Ungroup
      // dissolves every group in the selection. A whole-group selection
      // exposes the lock toggle so the group locks/unlocks as one unit.
      $('cv-group-field').style.display = '';
      $('cv-obj-group').disabled = !!gid;
      $('cv-obj-ungroup').disabled = !anyGrouped;
      const groupLocked = gid ? groupMembers(gid).every((o) => o.locked) : false;
      $('cv-obj-lock-field').style.display = gid ? '' : 'none';
      $('cv-obj-locked').checked = groupLocked;
      $('cv-obj-locked-note').style.display = groupLocked ? '' : 'none';
      $('cv-align-field').style.display = '';
      $('cv-layer-field').style.display = '';
      $('cv-rotation-field').style.display = 'none'; // rotate is single-object
      $('cv-obj-delete').disabled = false;
      return;
    }
    const obj = selectedId && getObj(selectedId);
    if (!obj || mode !== 'edit') {
      objPanel.style.display = 'none';
      // builder.js' table-panel handlers key off SELECTED_CHART_ID;
      // clear it whenever no table object is the active selection.
      if (typeof SELECTED_CHART_ID !== 'undefined') SELECTED_CHART_ID = null;
      return;
    }
    objPanel.style.display = '';
    const type = obj.object_type || 'unknown';
    const typeLabels = { data_field: 'Data field', calc_field: 'Calculated field', text: 'Text', icon: 'Icon', image: 'Image', table: 'Table', placeholder: 'Placeholder' };
    // shapes label by kind — the user drew a Rectangle, not a "shape".
    const shapeLabels = { rect: 'Rectangle', ellipse: 'Ellipse', line: 'Line', arrow: 'Arrow', callout: 'Callout' };
    // bug fix: a section header IS a text object under the hood,
    // but the user selected a section — say so. Same for the
    // time pill.
    $('cv-obj-type').textContent = obj.role === 'section_header' ? 'Section'
      : obj.role === 'time_pill' ? 'Time pill'
        : type === 'shape' ? (shapeLabels[(obj.shape || {}).kind] || 'Shape')
          : type === 'chart' ? ((obj.chart || {}).kind === 'geo' ? 'World map' : (obj.chart || {}).kind === 'bar' ? 'Bar chart' : 'Trend chart')
            : (typeLabels[type] || type);
    // lock gates every editor in the panel — a locked object keeps
    // only the Locked toggle (so it can be unlocked) and its type label.
    const locked = !!obj.locked;
    $('cv-obj-lock-field').style.display = '';
    $('cv-obj-locked').checked = locked;
    $('cv-obj-locked-note').style.display = locked ? '' : 'none';
    $('cv-align-field').style.display = locked ? 'none' : '';
    $('cv-layer-field').style.display = locked ? 'none' : '';
    $('cv-rotation-field').style.display = locked ? 'none' : '';
    $('cv-obj-rotation').value = Number((obj.frame || {}).rotation) || 0;
    $('cv-obj-rotate-toggle').classList.toggle('active', rotateMode);
    $('cv-obj-delete').disabled = locked;
    // A single selected object is normally ungrouped (selection expands to
    // the whole group) — a stray one-member group (after deletes) can still
    // offer Ungroup so the leftover group_id is clearable.
    $('cv-group-field').style.display = (obj.group_id && !locked) ? '' : 'none';
    $('cv-obj-group').disabled = true;
    $('cv-obj-ungroup').disabled = !obj.group_id;
    $('cv-obj-remove-section').style.display = (obj.section_id && !locked) ? '' : 'none';
    // per-object theme apply: any unlocked visual object (a
    // placeholder has nothing to restyle). The dropdown defaults to the
    // page's theme when a different object becomes selected, then keeps the
    // user's pick for that object.
    $('cv-obj-theme-field').style.display = (locked || type === 'placeholder') ? 'none' : '';
    if (objThemePanelId !== obj.object_id) {
      objThemePanelId = obj.object_id;
      if (themeLibrary()[DOC.page.theme]) $('cv-obj-theme').value = DOC.page.theme;
    }
    $('cv-obj-icon-fields').style.display = (type === 'icon' && !locked) ? '' : 'none';

    // shape styling (fill hidden for line/arrow, radius only
    // where corners exist, callout hint only on callouts).
    const isShape = type === 'shape' && !locked;
    $('cv-obj-shape-fields').style.display = isShape ? '' : 'none';
    if (isShape) {
      const s = obj.style || {};
      const kind = (obj.shape || {}).kind || 'rect';
      const isLine = kind === 'line' || kind === 'arrow';
      $('cv-obj-shape-fill-row').style.display = isLine ? 'none' : '';
      $('cv-obj-shape-radius-field').style.display = (kind === 'rect' || kind === 'callout') ? '' : 'none';
      $('cv-obj-shape-callout-hint').style.display = kind === 'callout' ? '' : 'none';
      $('cv-obj-shape-fill-on').checked = s.fill_color !== 'transparent';
      // pickers hydrate with the RESOLVED color: an <input
      // type=color> can't display a token key (same for every picker below).
      const fillC = cvColor(s.fill_color) || '';
      $('cv-obj-shape-fill').value = /^#[0-9a-f]{6}$/i.test(fillC) ? fillC : '#dbe7f5';
      $('cv-obj-shape-border-width').value = s.border_width || '';
      $('cv-obj-shape-border-color').value = cvColor(s.border_color) || '#1a2744';
      $('cv-obj-shape-border-style').value = ['dashed', 'dotted'].includes(s.border_style) ? s.border_style : 'solid';
      $('cv-obj-shape-radius').value = (s.radius === undefined || s.radius === null) ? '' : s.radius;
      $('cv-obj-shape-opacity').value = s.opacity || '';
    }

    // image styling.
    const isImage = type === 'image' && !locked;
    $('cv-obj-image-fields').style.display = isImage ? '' : 'none';
    if (isImage) {
      const s = obj.style || {};
      $('cv-obj-image-fit').value = ['cover', 'fill'].includes(s.fit) ? s.fit : 'contain';
      $('cv-obj-image-radius').value = (s.radius === undefined || s.radius === null) ? '' : s.radius;
      $('cv-obj-image-border-width').value = s.border_width || '';
      $('cv-obj-image-border-color').value = cvColor(s.border_color) || '#1a2744';
      $('cv-obj-image-opacity').value = s.opacity || '';
    }

    // table objects: embed the REAL builder.js table config panel.
    // The shared partials in canvas.html carry the same element ids the
    // builder page uses, and every existing handler (updateChartConfig /
    // requeryChart / updateTableColumnProp / onIncludeTotalsRowChange …)
    // keys off SELECTED_CHART_ID + DASHBOARD_STATE.charts — point both at
    // this object and hydrate through populateTablePanel. _markDirty is
    // already bridged to the canvas dirty flag, so table edits
    // mark the canvas document dirty and ride its save.
    const isTable = type === 'table' && !locked;
    $('cv-obj-table-fields').style.display = isTable ? '' : 'none';
    if (typeof SELECTED_CHART_ID !== 'undefined') {
      SELECTED_CHART_ID = isTable ? (obj.object_id || null) : null;
    }
    if (isTable && typeof populateTablePanel === 'function'
        && typeof DASHBOARD_STATE !== 'undefined') {
      if (!obj.table || typeof obj.table !== 'object') obj.table = { chart_type: 'table' };
      DASHBOARD_STATE.charts[obj.object_id] = obj.table;
      // The per-column Data View dropdown reads the lazily-filled
      // _gtDataViews cache; builder-page init pre-warms it but the guarded
      // canvas load does not — warm it before first hydrate.
      if (typeof _ensureGtDataViews === 'function') {
        Promise.resolve(_ensureGtDataViews()).finally(() => {
          populateTablePanel(obj.object_id, obj.table);
        });
      } else {
        populateTablePanel(obj.object_id, obj.table);
      }
    }

    const isText = type === 'text' && !locked;
    $('cv-obj-text-fields').style.display = isText ? '' : 'none';
    if (isText) {
      const s = obj.style || {};
      $('cv-obj-font-size').value = cvFontPx(s.font_size) || 15;
      $('cv-obj-bold').checked = !!s.bold;
      $('cv-obj-color').value = cvColor(s.color) || '#16191f';
      $('cv-obj-bg').value = cvColor(s.bg_color) || '#ffffff';
      $('cv-obj-bg-on').checked = !!s.bg_color;
      // box-level formatting (theme font, border, padding,
      // transparency).
      $('cv-obj-font-family').value = cvTextTokens().fonts[s.font_family] ? s.font_family : '';
      $('cv-obj-border-width').value = s.border_width || '';
      $('cv-obj-border-color').value = cvColor(s.border_color) || '#16191f';
      $('cv-obj-padding').value = (s.padding === undefined || s.padding === null) ? '' : s.padding;
      $('cv-obj-opacity').value = s.opacity || '';
    }

    // calculated fields share the whole data-field panel (label
    // + formatting); only the binding pickers swap for the formula editor.
    const isField = (type === 'data_field' || type === 'calc_field') && !locked;
    const isCalc = type === 'calc_field';
    $('cv-obj-field-fields').style.display = isField ? '' : 'none';
    $('cv-obj-calc-fields').style.display = (isField && isCalc) ? '' : 'none';
    if (isField) {
      $('cv-obj-label').value = obj.label || '';
      // formatting controls (apply to computed fields too; only
      // the binding pickers below are computed-gated).
      const s = obj.style || {};
      $('cv-obj-format').value = ['number', 'percent', 'currency'].includes(s.format) ? s.format : 'number';
      $('cv-obj-decimals').value = decimalsOf(s) === null ? '' : decimalsOf(s);
      $('cv-obj-abbrev').checked = s.abbrev === undefined ? s.variant !== 'table_cell' : !!s.abbrev;
      $('cv-obj-thousands').checked = s.thousands !== false;
      $('cv-obj-value-size').value = cvFontPx(s.value_font_size) || '';
      $('cv-obj-label-size').value = cvFontPx(s.label_font_size) || '';
      $('cv-obj-value-color').value = cvColor(s.value_color) || '#1a2744';
      $('cv-obj-align').value = ['left', 'center', 'right'].includes(s.align) ? s.align : '';
      const computed = !isCalc && !!(obj.binding && obj.binding.computed);
      $('cv-obj-binding-fields').style.display = (isCalc || computed) ? 'none' : '';
      $('cv-obj-computed-note').style.display = computed ? '' : 'none';
      if (isCalc) {
        hydrateCalcPanel(obj);
      } else if (!computed) {
        const b = obj.binding || {};
        loadDataViews().then(() => {
          const dvSel = $('cv-obj-dv');
          if (b.data_view && !Array.prototype.some.call(dvSel.options, (o) => o.value === b.data_view)) {
            const o = document.createElement('option');
            o.value = b.data_view;
            o.textContent = b.data_view;
            dvSel.appendChild(o);
          }
          dvSel.value = b.data_view || '';
          // filter rows render after the field list loads so
          // their field dropdowns carry the data view's fields.
          loadBindingFields(b.data_view || '', b.field || '').then(() => renderBindingFilters(b.filters));
          $('cv-obj-agg').value = b.aggregation || 'sum';
          $('cv-obj-equals').value = b.equals_value !== undefined && b.equals_value !== null ? String(b.equals_value) : '';
          syncEqualsVisibility();
          // hydrate the time-range override (presence = enabled).
          // A stored shape without a preset key is the pre-preset custom
          // {from, to} form and hydrates as Custom range.
          const ov = b.time_override || null;
          $('cv-obj-tr-override').checked = !!ov;
          $('cv-obj-tr-preset').value = ov
            ? (typeof ov.preset === 'string' && ov.preset !== 'custom' ? ov.preset : 'custom')
            : '';
          $('cv-obj-tr-from').value = (ov && ov.from) || '';
          $('cv-obj-tr-to').value = (ov && ov.to) || '';
          syncTrOverrideVisibility();
        });
      }
    }
  }

  $('cv-obj-dv').addEventListener('change', async (e) => {
    // re-render the filter rows after the new field list loads
    // so their dropdowns track the picked data view (values are kept).
    const kept = readBindingFilterRows();
    await loadBindingFields(e.target.value, '');
    renderBindingFilters(kept);
  });
  $('cv-obj-agg').addEventListener('change', syncEqualsVisibility);

  $('cv-obj-filter-add').addEventListener('click', () => {
    const list = $('cv-obj-filters-list');
    if (list.children.length >= MAX_BINDING_FILTERS) {
      toast('At most ' + MAX_BINDING_FILTERS + ' filters per field.', true);
      return;
    }
    list.appendChild(bindingFilterRowEl('', ''));
  });

  // ── per-field time-range override. The checkbox reveals a
  // preset picker (the toolbar's #cv-time-preset option set); only "Custom
  // range…" reveals the fixed from/to inputs (same no-future rule as the
  // toolbar's custom range). The override commits with Apply
  // binding like every other binding edit. ──
  function cvTodayValue() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
      + '-' + String(d.getDate()).padStart(2, '0');
  }
  $('cv-obj-tr-from').max = cvTodayValue();
  $('cv-obj-tr-to').max = cvTodayValue();
  function syncTrOverrideVisibility() {
    const on = $('cv-obj-tr-override').checked;
    $('cv-obj-tr-fields').style.display = on ? 'flex' : 'none';
    $('cv-obj-tr-custom').style.display = (on && $('cv-obj-tr-preset').value === 'custom') ? 'flex' : 'none';
  }
  $('cv-obj-tr-override').addEventListener('change', syncTrOverrideVisibility);
  $('cv-obj-tr-preset').addEventListener('change', syncTrOverrideVisibility);

  // exact rotation entry — mirrors the drag handle.
  $('cv-obj-rotation').addEventListener('change', (e) => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.locked || !obj.frame) return;
    const deg = clampInt(e.target.value, -180, 180, 0);
    e.target.value = deg;
    if (obj.frame.rotation === deg) return;
    obj.frame.rotation = deg;
    markDirty();
    renderPageChildren();
  });

  $('cv-obj-rotate-toggle').addEventListener('click', () => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.locked) return;
    setRotateMode(!rotateMode);
  });

  // per-object lock. The checkbox is the only editor that stays
  // active on a locked object — everything else no-ops until unlocked.
  // on a whole-group selection it locks/unlocks every member —
  // the group pins and releases as one unit.
  $('cv-obj-locked').addEventListener('change', (e) => {
    const gid = selectionSingleGroupId();
    if (gid) {
      groupMembers(gid).forEach((o) => { o.locked = e.target.checked; });
      markDirty();
      renderPageChildren();
      applySelection();
      return;
    }
    const obj = selectedId && getObj(selectedId);
    if (!obj) return;
    obj.locked = e.target.checked;
    markDirty();
    renderPageChildren();
    syncObjectPanel();
  });

  // group / ungroup (Ctrl+G / Ctrl+Shift+G do the same).
  $('cv-obj-group').addEventListener('click', groupSelection);
  $('cv-obj-ungroup').addEventListener('click', ungroupSelection);

  // the binding panel's commit, shared by the Apply-binding
  // button and saveDoc's auto-commit. Reads the panel controls into
  // obj.binding. Returns true when the save/apply may proceed (committed,
  // or nothing to commit), false when the panel state is invalid — a toast
  // explains why, so a blocked save is never silent.
  function commitBindingFromPanel(obj, fromApplyButton) {
    const dataView = $('cv-obj-dv').value;
    const field = $('cv-obj-field').value;
    if (!dataView || !field) {
      // Apply demands a complete pick; a save with nothing picked has
      // nothing to commit and proceeds.
      if (fromApplyButton) {
        toast('Pick a data view and a field first.', true);
        return false;
      }
      return true;
    }
    const agg = $('cv-obj-agg').value || 'sum';
    const binding = { data_view: dataView, field: field, aggregation: agg };
    if (agg === 'equals') {
      const ev = $('cv-obj-equals').value.trim();
      if (!ev) {
        toast('Enter the value to count for the equals aggregation.', true);
        return false;
      }
      binding.equals_value = ev;
    }
    // row filters. A half-filled row refuses the apply rather
    // than silently dropping (an unfiltered number presented as filtered
    // is worse than a rejected click); fully empty rows are ignored.
    const filters = [];
    for (const f of readBindingFilterRows()) {
      const fField = f.field;
      const fValue = f.value.trim();
      if (!fField && !fValue) continue;
      if (!fField || !fValue) {
        toast('Each filter needs both a field and a value.', true);
        return false;
      }
      filters.push({ field: fField, value: fValue });
    }
    if (filters.length) binding.filters = filters;
    // time-range override. A preset commits as {preset}; Custom
    // range requires a complete, ordered, not-in-the-future window (the
    // toolbar's custom-range rules) and commits as {preset, from, to}.
    // Unchecked leaves the key off so the field follows the page's
    // time-range filter again.
    if ($('cv-obj-tr-override').checked) {
      const ovPreset = $('cv-obj-tr-preset').value;
      if (ovPreset === 'custom') {
        const ovFrom = $('cv-obj-tr-from').value;
        const ovTo = $('cv-obj-tr-to').value;
        const today = cvTodayValue();
        if (!ovFrom || !ovTo || ovFrom > ovTo || ovFrom > today || ovTo > today) {
          toast('Override Time Range needs both dates, from ≤ to, not in the future.', true);
          return false;
        }
        binding.time_override = { preset: 'custom', from: ovFrom, to: ovTo };
      } else {
        binding.time_override = { preset: ovPreset };
      }
    }
    // Only touch the object when the panel actually differs — a save with a
    // clean panel must not dirty the doc or re-query every field.
    if (JSON.stringify(obj.binding || null) !== JSON.stringify(binding)) {
      obj.binding = binding;
      markDirty();
      resolveAllFields();
    }
    return true;
  }

  $('cv-obj-apply').addEventListener('click', () => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.object_type !== 'data_field' || obj.locked) return;
    if (commitBindingFromPanel(obj, true)) toast('Binding applied — value is live.');
  });

  $('cv-obj-label').addEventListener('input', (e) => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || (obj.object_type !== 'data_field' && obj.object_type !== 'calc_field') || obj.locked) return;
    obj.label = e.target.value.slice(0, 200);
    markDirty();
    updateFieldEls();
  });

  // formatting editors. Same shape as updateSelectedTextStyle:
  // mutate style, mark dirty, re-render (value text AND inline styles both
  // come from buildObjectEl). calculated fields carry the same
  // style keys, so every formatting control applies to them unchanged.
  function updateSelectedFieldStyle(mutate) {
    const obj = selectedId && getObj(selectedId);
    if (!obj || (obj.object_type !== 'data_field' && obj.object_type !== 'calc_field') || obj.locked) return;
    obj.style = obj.style || {};
    mutate(obj.style);
    markDirty();
    renderPageChildren();
  }
  $('cv-obj-format').addEventListener('change', (e) => updateSelectedFieldStyle((s) => {
    s.format = ['percent', 'currency'].includes(e.target.value) ? e.target.value : 'number';
  }));
  $('cv-obj-decimals').addEventListener('change', (e) => updateSelectedFieldStyle((s) => {
    if (e.target.value.trim() === '') delete s.decimals; // blank = auto
    else s.decimals = clampInt(e.target.value, 0, 6, 0);
  }));
  $('cv-obj-abbrev').addEventListener('change', (e) => updateSelectedFieldStyle((s) => {
    s.abbrev = e.target.checked;
  }));
  $('cv-obj-thousands').addEventListener('change', (e) => updateSelectedFieldStyle((s) => {
    s.thousands = e.target.checked;
  }));
  $('cv-obj-value-size').addEventListener('change', (e) => updateSelectedFieldStyle((s) => {
    if (e.target.value.trim() === '') delete s.value_font_size; // blank = variant default
    else s.value_font_size = clampInt(e.target.value, 6, 200, 26);
  }));
  // the label above the metric. style.label_font_size was already
  // rendered and theme-scaled end-to-end; this exposes it in the panel.
  $('cv-obj-label-size').addEventListener('change', (e) => updateSelectedFieldStyle((s) => {
    if (e.target.value.trim() === '') delete s.label_font_size; // blank = variant default
    else s.label_font_size = clampInt(e.target.value, 6, 200, 12);
  }));
  $('cv-obj-value-color').addEventListener('input', (e) => updateSelectedFieldStyle((s) => {
    s.value_color = e.target.value;
  }));
  $('cv-obj-align').addEventListener('change', (e) => updateSelectedFieldStyle((s) => {
    if (['left', 'center', 'right'].includes(e.target.value)) s.align = e.target.value;
    else delete s.align;
  }));

  function updateSelectedTextStyle(mutate) {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.object_type !== 'text' || obj.locked) return;
    obj.style = obj.style || {};
    mutate(obj.style);
    markDirty();
    renderPageChildren();
  }
  $('cv-obj-font-size').addEventListener('change', (e) => updateSelectedTextStyle((s) => {
    s.font_size = clampInt(e.target.value, 6, 200, s.font_size || 15);
  }));
  $('cv-obj-bold').addEventListener('change', (e) => updateSelectedTextStyle((s) => { s.bold = e.target.checked; }));
  $('cv-obj-color').addEventListener('input', (e) => updateSelectedTextStyle((s) => { s.color = e.target.value; }));
  $('cv-obj-bg-on').addEventListener('change', (e) => updateSelectedTextStyle((s) => {
    if (e.target.checked) s.bg_color = $('cv-obj-bg').value;
    else delete s.bg_color;
  }));
  $('cv-obj-bg').addEventListener('input', (e) => updateSelectedTextStyle((s) => {
    s.bg_color = e.target.value;
    $('cv-obj-bg-on').checked = true;
  }));
  // box-level formatting. Same shape as the rest: mutate style,
  // dirty, re-render. font_family persists as a token KEY (never a CSS
  // string); blank clears back to the default.
  $('cv-obj-font-family').addEventListener('change', (e) => updateSelectedTextStyle((s) => {
    if (cvTextTokens().fonts[e.target.value]) s.font_family = e.target.value;
    else delete s.font_family;
  }));
  $('cv-obj-border-width').addEventListener('change', (e) => updateSelectedTextStyle((s) => {
    if (e.target.value.trim() === '' || parseInt(e.target.value, 10) === 0) delete s.border_width;
    else s.border_width = clampInt(e.target.value, 1, 20, 1);
  }));
  $('cv-obj-border-color').addEventListener('input', (e) => updateSelectedTextStyle((s) => {
    s.border_color = e.target.value;
  }));
  $('cv-obj-padding').addEventListener('change', (e) => updateSelectedTextStyle((s) => {
    if (e.target.value.trim() === '') delete s.padding; // blank = CSS default
    else s.padding = clampInt(e.target.value, 0, 100, 0);
  }));
  $('cv-obj-opacity').addEventListener('change', (e) => updateSelectedTextStyle((s) => {
    const v = clampInt(e.target.value, 10, 100, 100);
    if (e.target.value.trim() === '' || v === 100) delete s.opacity;
    else s.opacity = v;
  }));

  $('cv-obj-delete').addEventListener('click', deleteSelectedObject);

  // follow-up: a computed (calculated) field can be unlinked from
  // its numerator/denominator pair so the user can re-bind it manually.
  // A real formula editor is.
  $('cv-obj-unlink').addEventListener('click', () => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.object_type !== 'data_field' || obj.locked || !(obj.binding && obj.binding.computed)) return;
    if (!window.confirm('Unlink this calculated field? It becomes an unbound data field you can bind to any data view/field.')) return;
    obj.binding = null;
    delete obj._error;
    obj._value = undefined;
    markDirty();
    updateFieldEls();
    syncObjectPanel();
  });
  $('cv-obj-remove-section').addEventListener('click', () => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || !obj.section_id || obj.locked) return;
    // section removal must not delete locked members through the
    // back door — same rule as Delete.
    if (DOC.objects.some((o) => o.section_id === obj.section_id && o.locked)) {
      toast('This section contains locked object(s) — unlock them first.', true);
      return;
    }
    if (!window.confirm('Remove this whole section (all of its elements)?')) return;
    removeSection(obj.section_id);
  });

  // ── Insert: text / data field / icon / section ──
  $('cv-add-text').addEventListener('click', () => {
    const o = {
      object_id: newObjectId(), object_type: 'text',
      frame: { x: 120, y: 120, w: 280, h: 44, rotation: 0 }, locked: false, group_id: null,
      // color 'ink': identical to the CSS default (#16191f) on
      // classic, but resolves light on a dark theme so new text is never
      // invisible against the themed page.
      style: { font_size: 15, color: 'ink' }, text: { content: 'New text' },
    };
    DOC.objects.push(o);
    markDirty();
    renderPageChildren();
    select(o.object_id);
  });

  $('cv-add-field').addEventListener('click', () => {
    const o = {
      object_id: newObjectId(), object_type: 'data_field',
      frame: { x: 140, y: 200, w: 284, h: 96, rotation: 0 }, locked: false, group_id: null,
      label: 'New metric', style: { format: 'number' }, binding: null,
    };
    DOC.objects.push(o);
    markDirty();
    renderPageChildren();
    select(o.object_id);
  });

  // insert a calculated field: a data-field look-alike whose
  // value is a formula over named variable bindings (configured in the
  // panel; the skeleton starts unconfigured like + Field's binding: null).
  $('cv-add-calc').addEventListener('click', () => {
    const o = {
      object_id: newObjectId(), object_type: 'calc_field',
      frame: { x: 140, y: 200, w: 284, h: 96, rotation: 0 }, locked: false, group_id: null,
      label: 'New calculation', style: { format: 'number' }, calc: { formula: '', variables: {} },
    };
    DOC.objects.push(o);
    markDirty();
    renderPageChildren();
    select(o.object_id);
  });

  $('cv-add-section').addEventListener('click', addSection);

  // insert a table object. The payload mirrors the legacy
  // builder's default `table` chart config (addChartToCanvas defaults) so
  // the SAME config drives renderTable and the embedded config panel; the
  // user then configures it through the panel exactly as in the builder.
  $('cv-add-table').addEventListener('click', () => {
    const id = newObjectId();
    const o = {
      object_id: id, object_type: 'table',
      frame: { x: 120, y: 160, w: 560, h: 300, rotation: 0 }, locked: false, group_id: null,
      table: {
        chart_id: id,
        chart_type: 'table',
        data_view: '',
        x_field: null,
        x_field_type: null,
        y_fields: [],
        table_columns: [],
        aggregation: 'sum',
        date_format: '',
        hide_title: true,
        table_font_size: 12,
        cell_gradient_enabled: false,
        cell_gradient_color: '#006d2c',
        filters: [],
        display_toggles: {},
      },
    };
    DOC.objects.push(o);
    markDirty();
    renderPageChildren();
    select(o.object_id);
  });

  // insert a live time pill (the Digital Impact Report's date
  // pill): its wording always mirrors the page's current time range; the
  // style keys are ordinary text-object style, so the panel can restyle it.
  $('cv-add-timepill').addEventListener('click', () => {
    const o = {
      object_id: newObjectId(), object_type: 'text', role: 'time_pill',
      frame: { x: 120, y: 64, w: 230, h: 30, rotation: 0 }, locked: false, group_id: null,
      style: { font_size: 12, align: 'center', color: '#cfd6e4', bg_color: '#2b3a5e', radius: 100 },
      text: { content: 'All Time' },
    };
    DOC.objects.push(o);
    markDirty();
    renderPageChildren();
    select(o.object_id);
  });

  // ── Icon library modal (the existing SVG set — the only icon store) ──
  // Two modes: insert a new icon object (+ Icon), or replace the selected
  // icon object's file (panel "Replace icon…" — follow-up).
  const iconModal = $('cv-icon-modal');
  let iconListLoaded = false;
  let iconPickTargetId = null; // null = insert; object_id = replace that icon

  function pickIcon(name) {
    iconModal.classList.remove('visible');
    if (iconPickTargetId) {
      const target = getObj(iconPickTargetId);
      iconPickTargetId = null;
      if (target && target.object_type === 'icon') {
        target.icon = { name: name };
        markDirty();
        renderPageChildren();
        select(target.object_id);
      }
      return;
    }
    const o = {
      object_id: newObjectId(), object_type: 'icon',
      frame: { x: 160, y: 160, w: 56, h: 56, rotation: 0 }, locked: false, group_id: null,
      icon: { name: name },
    };
    DOC.objects.push(o);
    markDirty();
    renderPageChildren();
    select(o.object_id);
  }

  async function openIconModal() {
    iconModal.classList.add('visible');
    if (iconListLoaded) return;
    const grid = $('cv-icon-grid');
    grid.innerHTML = '<div class="cv-open-empty">Loading…</div>';
    try {
      const res = await fetch(API_BASE + 'canvas-icons');
      const data = await res.json();
      const icons = data.icons || [];
      grid.innerHTML = '';
      icons.forEach((name) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cv-icon-choice';
        btn.title = name;
        const img = document.createElement('img');
        img.src = ICON_BASE + '/' + encodeURIComponent(name);
        img.alt = name;
        btn.appendChild(img);
        btn.addEventListener('click', () => pickIcon(name));
        grid.appendChild(btn);
      });
      if (!icons.length) grid.innerHTML = '<div class="cv-open-empty">No icons in the library.</div>';
      iconListLoaded = true;
    } catch (err) {
      grid.innerHTML = '<div class="cv-open-empty">Could not load icons: ' + err.message + '</div>';
    }
  }

  $('cv-add-icon').addEventListener('click', () => {
    iconPickTargetId = null;
    openIconModal();
  });
  $('cv-obj-change-icon').addEventListener('click', () => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.object_type !== 'icon' || obj.locked) return;
    iconPickTargetId = obj.object_id;
    openIconModal();
  });
  $('cv-icon-close').addEventListener('click', () => {
    iconPickTargetId = null;
    iconModal.classList.remove('visible');
  });
  iconModal.addEventListener('click', (e) => {
    if (e.target === iconModal) {
      iconPickTargetId = null;
      iconModal.classList.remove('visible');
    }
  });

  // ══ insert shapes + images ══

  const shapeMenu = $('cv-shape-menu');
  const SHAPE_DEFAULTS = {
    rect: { w: 200, h: 120, style: { fill_color: '#dbe7f5', border_color: '#1a2744', border_width: 1, radius: 0 } },
    ellipse: { w: 150, h: 150, style: { fill_color: '#dbe7f5', border_color: '#1a2744', border_width: 1 } },
    line: { w: 240, h: 24, style: { border_color: '#1a2744', border_width: 2 } },
    arrow: { w: 240, h: 28, style: { border_color: '#1a2744', border_width: 2 } },
    callout: { w: 240, h: 130, style: { fill_color: '#ffffff', border_color: '#1a2744', border_width: 1, radius: 8, font_size: 14 } },
  };

  function addShape(kind) {
    const d = SHAPE_DEFAULTS[kind];
    if (!d) return;
    if (DOC.objects.length >= MAX_OBJECTS) {
      toast('Too many objects on the page (max ' + MAX_OBJECTS + ').', true);
      return;
    }
    const o = {
      object_id: newObjectId(), object_type: 'shape', shape: { kind: kind },
      frame: { x: 160, y: 160, w: d.w, h: d.h, rotation: 0 }, locked: false, group_id: null,
      style: { ...d.style },
    };
    if (kind === 'callout') o.text = { content: 'Callout text' };
    DOC.objects.push(o);
    markDirty();
    renderPageChildren();
    select(o.object_id);
  }

  $('cv-add-shape').addEventListener('click', (e) => {
    const r = e.target.getBoundingClientRect();
    shapeMenu.style.left = Math.min(r.left, window.innerWidth - 190) + 'px';
    shapeMenu.style.top = (r.bottom + 4) + 'px';
    shapeMenu.classList.toggle('visible');
  });
  shapeMenu.querySelectorAll('button[data-shape]').forEach((btn) => {
    btn.addEventListener('click', () => {
      shapeMenu.classList.remove('visible');
      addShape(btn.dataset.shape);
    });
  });
  document.addEventListener('pointerdown', (e) => {
    if (!e.target.closest('#cv-shape-menu') && !e.target.closest('#cv-add-shape')) {
      shapeMenu.classList.remove('visible');
    }
  });

  // Total in-doc image budget: every image object plus an image background.
  // `extra` is the about-to-be-added (or delta) length being tested.
  function totalImageChars(extra) {
    let total = extra || 0;
    if (DOC.page.background.type === 'image') total += (DOC.page.background.value || '').length;
    DOC.objects.forEach((o) => {
      if (o.object_type === 'image' && o.image && typeof o.image.src === 'string') {
        total += o.image.src.length;
      }
    });
    return total;
  }

  // Mirrors the server's _CANVAS_SVG_FORBIDDEN_RE. <img>-only rendering
  // means scripts never execute anyway; this just fails fast with a clear
  // message instead of a 400 on save.
  function svgLooksActive(text) {
    return /<\s*script|javascript:|<\s*foreignobject|\son[a-z]+\s*=/i.test(text);
  }

  const imageFileInput = $('cv-image-file');
  let imagePickTargetId = null; // null = insert; object_id = replace that image

  $('cv-add-image').addEventListener('click', () => {
    imagePickTargetId = null;
    imageFileInput.click();
  });
  $('cv-obj-change-image').addEventListener('click', () => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.object_type !== 'image' || obj.locked) return;
    imagePickTargetId = obj.object_id;
    imageFileInput.click();
  });

  imageFileInput.addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const uri = String(reader.result || '');
      if (!IMAGE_MIME_RE.test(uri)) {
        toast('Unsupported image type — use PNG, JPG, GIF, WebP, or SVG.', true);
        return;
      }
      if (uri.length > MAX_IMAGE_DATA_URI) {
        toast('Image is too large — please use a file under ~700KB.', true);
        return;
      }
      if (uri.slice(0, 19) === 'data:image/svg+xml;' || uri.slice(0, 19) === 'data:image/svg+xml,') {
        let text = '';
        try {
          const comma = uri.indexOf(',');
          const payload = uri.slice(comma + 1);
          text = uri.slice(0, comma).indexOf(';base64') !== -1 ? atob(payload) : decodeURIComponent(payload);
        } catch (err) { /* undecodable → rejected below */ }
        if (!text || svgLooksActive(text)) {
          toast('This SVG contains scripts or event handlers — not allowed.', true);
          return;
        }
      }
      const target = imagePickTargetId && getObj(imagePickTargetId);
      imagePickTargetId = null;
      const replaced = (target && target.image && typeof target.image.src === 'string')
        ? target.image.src.length : 0;
      if (totalImageChars(uri.length - replaced) > MAX_TOTAL_IMAGE_CHARS) {
        toast('The page’s images would total too much data (max ~4.5MB).', true);
        return;
      }
      // Probe the natural size so a new object starts at the image's own
      // aspect ratio (downscaled to fit) instead of an arbitrary box.
      const probe = new Image();
      probe.onload = () => insertOrReplaceImage(uri, file.name, probe.naturalWidth, probe.naturalHeight, target);
      probe.onerror = () => insertOrReplaceImage(uri, file.name, 0, 0, target);
      probe.src = uri;
    };
    reader.readAsDataURL(file);
  });

  function insertOrReplaceImage(uri, name, nw, nh, target) {
    if (target && target.object_type === 'image') {
      target.image = { src: uri, name: (name || '').slice(0, 200) };
      markDirty();
      renderPageChildren();
      select(target.object_id);
      return;
    }
    if (DOC.objects.length >= MAX_OBJECTS) {
      toast('Too many objects on the page (max ' + MAX_OBJECTS + ').', true);
      return;
    }
    let w = 320;
    let h = 200;
    if (nw > 0 && nh > 0) {
      const s = Math.min(1, 360 / nw, 360 / nh);
      w = Math.max(24, Math.round(nw * s));
      h = Math.max(16, Math.round(nh * s));
    }
    const o = {
      object_id: newObjectId(), object_type: 'image',
      frame: { x: 140, y: 140, w: w, h: h, rotation: 0 }, locked: false, group_id: null,
      image: { src: uri, name: (name || '').slice(0, 200) },
      style: { fit: 'contain' },
    };
    DOC.objects.push(o);
    markDirty();
    renderPageChildren();
    select(o.object_id);
  }

  // ── panel editors: same shape as updateSelectedTextStyle ──
  function updateSelectedShapeStyle(mutate) {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.object_type !== 'shape' || obj.locked) return;
    obj.style = obj.style || {};
    mutate(obj.style);
    markDirty();
    renderPageChildren();
  }

  function updateSelectedImageStyle(mutate) {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.object_type !== 'image' || obj.locked) return;
    obj.style = obj.style || {};
    mutate(obj.style);
    markDirty();
    renderPageChildren();
  }

  $('cv-obj-shape-fill-on').addEventListener('change', (e) => updateSelectedShapeStyle((s) => {
    // "No fill" persists as transparent — undefined means the default fill.
    s.fill_color = e.target.checked ? $('cv-obj-shape-fill').value : 'transparent';
  }));
  $('cv-obj-shape-fill').addEventListener('input', (e) => updateSelectedShapeStyle((s) => {
    s.fill_color = e.target.value;
    $('cv-obj-shape-fill-on').checked = true;
  }));
  $('cv-obj-shape-border-width').addEventListener('change', (e) => updateSelectedShapeStyle((s) => {
    if (e.target.value.trim() === '' || parseInt(e.target.value, 10) === 0) delete s.border_width;
    else s.border_width = clampInt(e.target.value, 1, 20, 1);
  }));
  $('cv-obj-shape-border-color').addEventListener('input', (e) => updateSelectedShapeStyle((s) => {
    s.border_color = e.target.value;
  }));
  $('cv-obj-shape-border-style').addEventListener('change', (e) => updateSelectedShapeStyle((s) => {
    if (e.target.value === 'dashed' || e.target.value === 'dotted') s.border_style = e.target.value;
    else delete s.border_style;
  }));
  $('cv-obj-shape-radius').addEventListener('change', (e) => updateSelectedShapeStyle((s) => {
    if (e.target.value.trim() === '') delete s.radius; // blank = per-kind default
    else s.radius = clampInt(e.target.value, 0, 200, 0);
  }));
  $('cv-obj-shape-opacity').addEventListener('change', (e) => updateSelectedShapeStyle((s) => {
    const v = clampInt(e.target.value, 10, 100, 100);
    if (e.target.value.trim() === '' || v === 100) delete s.opacity;
    else s.opacity = v;
  }));

  $('cv-obj-image-fit').addEventListener('change', (e) => updateSelectedImageStyle((s) => {
    if (e.target.value === 'cover' || e.target.value === 'fill') s.fit = e.target.value;
    else delete s.fit; // contain is the default
  }));
  $('cv-obj-image-radius').addEventListener('change', (e) => updateSelectedImageStyle((s) => {
    if (e.target.value.trim() === '') delete s.radius;
    else s.radius = clampInt(e.target.value, 0, 200, 0);
  }));
  $('cv-obj-image-border-width').addEventListener('change', (e) => updateSelectedImageStyle((s) => {
    if (e.target.value.trim() === '' || parseInt(e.target.value, 10) === 0) delete s.border_width;
    else s.border_width = clampInt(e.target.value, 1, 20, 1);
  }));
  $('cv-obj-image-border-color').addEventListener('input', (e) => updateSelectedImageStyle((s) => {
    s.border_color = e.target.value;
  }));
  $('cv-obj-image-opacity').addEventListener('change', (e) => updateSelectedImageStyle((s) => {
    const v = clampInt(e.target.value, 10, 100, 100);
    if (e.target.value.trim() === '' || v === 100) delete s.opacity;
    else s.opacity = v;
  }));

  // ── Seed from an existing report ──
  const seedModal = $('cv-seed-modal');
  const SEED_TYPE_LABELS = { canva: 'Summary Report', grouped_table: 'Summary table', table: 'Table', report: 'Facebook Impact', google_report: 'Google Impact' };
  // the same description lines the Choose a Template dialog shows
  // under each report name (_HARDCODED_TEMPLATES in builder.js — keep the
  // report / google_report / canva wording in sync); grouped_table has no
  // template row, so it carries its own line.
  const SEED_TYPE_DESCRIPTIONS = {
    report: 'Full-page report with KPI cards, funnel, geographic map, and monthly trends.',
    google_report: 'Full-page report with KPI cards, funnel, geographic map, and monthly trends using Google Ads data.',
    canva: 'Four-section visual summary covering Reach, Engagement, Interaction, and Jesus Journey metrics.',
    grouped_table: 'Summary table of key metrics grouped into sections.',
    table: 'Live data table with configurable columns, sorting, and totals.',
  };

  async function showSeedDialog() {
    const list = $('cv-seed-list');
    list.innerHTML = '<div class="cv-open-empty">Loading…</div>';
    seedModal.classList.add('visible');
    let sources = [];
    try {
      const res = await fetch(API_BASE + 'canvas-seed-sources');
      const data = await res.json();
      if (!data.success) throw new Error(data.error || 'list failed');
      sources = data.sources || [];
    } catch (err) {
      list.innerHTML = '<div class="cv-open-empty">Could not load reports: ' + err.message + '</div>';
      return;
    }
    if (!sources.length) {
      list.innerHTML = '<div class="cv-open-empty">No seedable reports found — a report needs a Summary table or an Impact Report chart.</div>';
      return;
    }
    list.innerHTML = '';
    sources.forEach((s) => {
      const row = document.createElement('div');
      row.className = 'cv-open-row';
      const main = document.createElement('div');
      main.className = 'cv-open-main';
      const title = document.createElement('span');
      title.className = 'cv-open-title';
      title.textContent = s.title;
      main.appendChild(title);
      // the template description line under the report name.
      const descText = (s.chart_types || []).map((t) => SEED_TYPE_DESCRIPTIONS[t]).find(Boolean) || '';
      if (descText) {
        const desc = document.createElement('span');
        desc.className = 'cv-open-desc';
        desc.textContent = descText;
        main.appendChild(desc);
      }
      const kinds = document.createElement('span');
      kinds.className = 'cv-open-date';
      kinds.textContent = (s.chart_types || []).map((t) => SEED_TYPE_LABELS[t] || t).join(', ');
      row.appendChild(main);
      row.appendChild(kinds);
      row.addEventListener('click', () => {
        seedModal.classList.remove('visible');
        seedFromReport(s.id, s.title);
      });
      list.appendChild(row);
    });
  }

  async function seedFromReport(id, fallbackTitle) {
    if (!confirmDiscard()) return;
    try {
      // seed in the page's current theme (the picker choice from
      // "+ Canvas" rides on DOC.page.theme), so themed pages seed themed
      // report content. The server treats unknown keys as classic.
      const seedTheme = (DOC.page && DOC.page.theme) || 'classic';
      // seed-time — forward any team_filter params from the editor's
      // own URL so a team-scoped seed is born without the sections that team
      // does not use (server skips dark Summary sections). No params => the
      // seed renders everything, exactly as before.
      const seedTeams = new URLSearchParams(window.location.search).getAll('team_filter')
        .filter(Boolean)
        .map(t => '&team=' + encodeURIComponent(t))
        .join('');
      const res = await fetch(API_BASE + 'canvas-seed/' + encodeURIComponent(id)
        + '?theme=' + encodeURIComponent(seedTheme) + seedTeams);
      const data = await res.json();
      if (!res.ok || !data.success) {
        toast('Seed failed: ' + (data.error || ('HTTP ' + res.status)), true);
        return;
      }
      // COPY-on-seed (the private docs 2026-07-09): this is an UNSAVED
      // new document — saving writes a new canvas doc and the source report
      // is never modified.
      DOC = normalizeDoc(data.canvas_config);
      docId = null;
      // seeding fills the PAGE, not the name: a title the user
      // already set (typed, or handed over via ?title= from "+ Canvas
      // page") survives; only the default placeholder is replaced with
      // "<source report> — Canvas".
      const curTitle = titleInput.value.trim();
      if (!curTitle || curTitle === 'Untitled Canvas') {
        titleInput.value = ((data.title || fallbackTitle || 'Report') + ' — Canvas').slice(0, 200);
      }
      dirty = true;
      history.replaceState(null, '', 'canvas');
      select(null);
      syncFilterBridge();
      syncPanel();
      renderAll();
      resetHistory();
      resolveAllFields();
      const skipped = data.skipped || [];
      if (skipped.length) {
        toast('Seeded (unsaved copy). ' + skipped.length + ' element(s) could not be converted — see the placeholders on the page.');
      } else {
        toast('Seeded from "' + (data.title || fallbackTitle) + '" — unsaved copy.');
      }
    } catch (err) {
      toast('Seed failed: ' + err.message, true);
    }
  }

  $('cv-seed').addEventListener('click', showSeedDialog);
  $('cv-seed-close').addEventListener('click', () => seedModal.classList.remove('visible'));
  seedModal.addEventListener('click', (e) => {
    if (e.target === seedModal) seedModal.classList.remove('visible');
  });

  // ── Settings panel ──
  function syncPanel() {
    $('cv-page-width').value = DOC.page.width;
    $('cv-page-height').value = DOC.page.height;
    $('cv-orient-landscape').classList.toggle('active', DOC.page.orientation === 'landscape');
    $('cv-orient-portrait').classList.toggle('active', DOC.page.orientation === 'portrait');
    $('cv-bg-type').value = DOC.page.background.type;
    if (DOC.page.background.type === 'color' && DOC.page.background.value) {
      $('cv-bg-color').value = DOC.page.background.value;
    }
    $('cv-bg-color-field').style.display = DOC.page.background.type === 'color' ? '' : 'none';
    $('cv-bg-image-field').style.display = DOC.page.background.type === 'image' ? '' : 'none';
    $('cv-margins-show').checked = !!DOC.page.margins.show;
    $('cv-margin-top').value = DOC.page.margins.top;
    $('cv-margin-right').value = DOC.page.margins.right;
    $('cv-margin-bottom').value = DOC.page.margins.bottom;
    $('cv-margin-left').value = DOC.page.margins.left;
    $('cv-grid-show').checked = !!DOC.page.grid.show;
    $('cv-grid-snap').checked = !!DOC.page.grid.snap;
    $('cv-grid-size').value = DOC.page.grid.size;
    $('cv-rulers-show').checked = !!DOC.page.rulers.show;
    $('cv-guides-show').checked = !!DOC.page.guides.show;
    $('cv-smart-snap').checked = DOC.page.smart_snap !== false;
    // highlight the page's current theme (an unknown saved key
    // renders as classic but highlights nothing — it isn't classic).
    document.querySelectorAll('#cv-theme-row .cv-theme-card').forEach((el) => {
      el.classList.toggle('active', el.dataset.themeKey === DOC.page.theme);
    });
  }

  // resizing the page scales its CONTENT to the new dimensions
  // instead of cropping it: every frame scales by the per-axis factors
  // (the group-resize math applied to the whole page), guides and
  // margins ride their axis, and text-ish font sizes scale by the smaller
  // factor so wording keeps fitting its shrunk box. Rotation is unchanged.
  function scaleDocToPage(w0, h0) {
    const sx = DOC.page.width / w0;
    const sy = DOC.page.height / h0;
    if (!isFinite(sx) || !isFinite(sy) || sx <= 0 || sy <= 0 || (sx === 1 && sy === 1)) return;
    const sf = Math.min(sx, sy);
    const scaleFont = (v) => {
      const n = Number(v);
      return n > 0 ? Math.min(Math.max(Math.round(n * sf), 6), 200) : v;
    };
    DOC.objects.forEach((o) => {
      const f = o.frame;
      if (f && o.object_type === 'icon') {
        // icons scale UNIFORMLY (by the smaller factor) about
        // their center: per-axis scaling squashed the 44×44 badge circles
        // into pills whose contain-fit glyph shrank to the short axis.
        // The center still tracks each axis so layout follows the page.
        const cx = ((f.x || 0) + (f.w || 0) / 2) * sx;
        const cy = ((f.y || 0) + (f.h || 0) / 2) * sy;
        f.w = Math.max(1, Math.round((f.w || 0) * sf));
        f.h = Math.max(1, Math.round((f.h || 0) * sf));
        f.x = Math.round(cx - f.w / 2);
        f.y = Math.round(cy - f.h / 2);
        const st = o.style;
        if (st) {
          if (st.radius) st.radius = Math.max(1, Math.round(st.radius * sf));
          if (st.padding) st.padding = Math.max(0, Math.round(st.padding * sf));
        }
      } else if (f) {
        f.x = Math.round((f.x || 0) * sx);
        f.y = Math.round((f.y || 0) * sy);
        f.w = Math.max(1, Math.round((f.w || 0) * sx));
        f.h = Math.max(1, Math.round((f.h || 0) * sy));
      }
      const s = o.style;
      if (s) {
        ['font_size', 'value_font_size', 'label_font_size'].forEach((k) => {
          if (s[k] !== undefined && s[k] !== null && s[k] !== '') s[k] = scaleFont(s[k]);
        });
      }
    });
    DOC.page.guides.v = DOC.page.guides.v.map((x) => Math.round(x * sx));
    DOC.page.guides.h = DOC.page.guides.h.map((y) => Math.round(y * sy));
    const m = DOC.page.margins;
    m.left = Math.round((m.left || 0) * sx);
    m.right = Math.round((m.right || 0) * sx);
    m.top = Math.round((m.top || 0) * sy);
    m.bottom = Math.round((m.bottom || 0) * sy);
  }

  function onDimChange() {
    const w0 = DOC.page.width;
    const h0 = DOC.page.height;
    DOC.page.width = clampInt($('cv-page-width').value, PAGE_MIN, PAGE_MAX, DOC.page.width);
    DOC.page.height = clampInt($('cv-page-height').value, PAGE_MIN, PAGE_MAX, DOC.page.height);
    // Orientation follows the numbers — a manually typed 720×1280 IS portrait.
    DOC.page.orientation = DOC.page.width >= DOC.page.height ? 'landscape' : 'portrait';
    scaleDocToPage(w0, h0); // content scales with the page
    syncPanel();
    markDirty();
    renderAll();
  }
  $('cv-page-width').addEventListener('change', onDimChange);
  $('cv-page-height').addEventListener('change', onDimChange);

  function setOrientation(o) {
    if (DOC.page.orientation === o) return;
    const wantLandscape = o === 'landscape';
    const isLandscape = DOC.page.width >= DOC.page.height;
    if (wantLandscape !== isLandscape) {
      const w0 = DOC.page.width;
      const h0 = DOC.page.height;
      DOC.page.width = h0;
      DOC.page.height = w0;
      scaleDocToPage(w0, h0); // same scale-to-fit as typed dims
    }
    DOC.page.orientation = o;
    syncPanel();
    markDirty();
    renderAll();
  }
  $('cv-orient-landscape').addEventListener('click', () => setOrientation('landscape'));
  $('cv-orient-portrait').addEventListener('click', () => setOrientation('portrait'));

  $('cv-bg-type').addEventListener('change', (e) => {
    const t = e.target.value === 'image' ? 'image' : 'color';
    DOC.page.background.type = t;
    if (t === 'color' && (DOC.page.background.value || '').startsWith('data:')) {
      DOC.page.background.value = $('cv-bg-color').value || '#ffffff';
    }
    if (t === 'image' && !(DOC.page.background.value || '').startsWith('data:')) {
      DOC.page.background.value = '';
    }
    syncPanel();
    markDirty();
    renderBackground();
  });

  $('cv-bg-color').addEventListener('input', (e) => {
    DOC.page.background.type = 'color';
    DOC.page.background.value = e.target.value;
    markDirty();
    renderBackground();
  });

  $('cv-bg-image').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const uri = String(reader.result || '');
      // Mirrors the server cap (_MAX_CANVAS_BG_VALUE) so an oversized file
      // fails here with a clear message instead of a 400 on save.
      if (uri.length > MAX_BG_DATA_URI) {
        toast('Image is too large — please use a file under ~700KB.', true);
        e.target.value = '';
        return;
      }
      DOC.page.background.type = 'image';
      DOC.page.background.value = uri;
      syncPanel();
      markDirty();
      renderBackground();
    };
    reader.readAsDataURL(file);
  });

  $('cv-bg-image-clear').addEventListener('click', () => {
    DOC.page.background = { type: 'color', value: '#ffffff' };
    $('cv-bg-image').value = '';
    syncPanel();
    markDirty();
    renderBackground();
  });

  function bindMargin(id, key) {
    $(id).addEventListener('change', (e) => {
      DOC.page.margins[key] = clampInt(e.target.value, 0, 2000, DOC.page.margins[key]);
      syncPanel();
      markDirty();
      renderPageChildren();
    });
  }
  bindMargin('cv-margin-top', 'top');
  bindMargin('cv-margin-right', 'right');
  bindMargin('cv-margin-bottom', 'bottom');
  bindMargin('cv-margin-left', 'left');
  $('cv-margins-show').addEventListener('change', (e) => {
    DOC.page.margins.show = e.target.checked;
    markDirty();
    renderPageChildren();
  });

  $('cv-grid-show').addEventListener('change', (e) => {
    DOC.page.grid.show = e.target.checked;
    markDirty();
    renderPageChildren();
  });
  $('cv-grid-size').addEventListener('change', (e) => {
    DOC.page.grid.size = clampInt(e.target.value, 4, 500, DOC.page.grid.size);
    syncPanel();
    markDirty();
    renderPageChildren();
  });
  $('cv-grid-snap').addEventListener('change', (e) => {
    DOC.page.grid.snap = e.target.checked;
    markDirty();
  });

  $('cv-rulers-show').addEventListener('change', (e) => {
    DOC.page.rulers.show = e.target.checked;
    markDirty();
    renderAll();
  });
  $('cv-guides-show').addEventListener('change', (e) => {
    DOC.page.guides.show = e.target.checked;
    markDirty();
    renderPageChildren();
  });
  // persistent smart-guides toggle — off drops snapping to objects,
  // guides, margins, and page edges/center (grid snap keeps its own checkbox).
  $('cv-smart-snap').addEventListener('change', (e) => {
    DOC.page.smart_snap = e.target.checked;
    markDirty();
  });

  $('cv-guides-clear').addEventListener('click', () => {
    if (!DOC.page.guides.h.length && !DOC.page.guides.v.length) return;
    DOC.page.guides.h = [];
    DOC.page.guides.v = [];
    markDirty();
    renderPageChildren();
  });

  // ══ apply a theme to an existing dashboard / object ══
  // Dashboard-level: swap page.theme so every token-KEYED style re-resolves,
  // and RE-KEY raw colors that exactly match the outgoing theme's palette so
  // pre-theme documents follow along (and keep following future switches).
  // Object-level: bake the chosen theme's CONCRETE colors into the object's
  // style — token resolution is page-scoped, so a single object can only
  // diverge with raw values; it stops following the page theme by design.
  // Raw colors matching neither treatment are the user's own picks: kept
  // unless "Restyle everything" is chosen in the confirm dialog (the
  // ticket's explicit-and-predictable contract). Undo works through the
  // ordinary whole-document history snapshot markDirty() records.

  // Object style fields whose string values may be color token keys — keep
  // in sync with COLOR_STYLE_FIELDS in canvas_theme.py.
  const THEME_COLOR_FIELDS = ['bg_color', 'color', 'border_color', 'fill_color', 'value_color', 'label_color', 'label_bg_color'];

  // Reverse-lookup priority: when one raw value matches several tokens of
  // the outgoing theme (classic surface == bg == white == #ffffff), the
  // FIELD decides which token it meant. First match wins; every remaining
  // color token is tried after these, in token order. page_bg is the
  // pseudo-field for the page background.
  const THEME_REKEY_PRIORITY = {
    bg_color: ['surface', 'header_bg', 'pill_bg', 'shape_fill', 'subheader_bg', 'paper', 'bg', 'accent'],
    color: ['ink', 'header_text', 'pill_ink', 'muted', 'accent_text', 'value_ink', 'eyebrow', 'accent_ink'],
    border_color: ['border', 'accent', 'ink'],
    fill_color: ['shape_fill', 'surface', 'accent'],
    value_color: ['value_ink', 'accent_text', 'ink'],
    label_color: ['header_text', 'ink'],
    label_bg_color: ['header_bg', 'accent'],
    page_bg: ['bg', 'paper', 'surface'],
  };

  // "Restyle everything" fallback per object kind: the token the field would
  // carry if the object were inserted fresh today. chart/table are ABSENT on
  // purpose — canvas_charts.js draws their internals in fixed dark inks
  // , so their literal white card must survive every theme
  // operation (the seed-comment contract).
  const THEME_RESTYLE_DEFAULTS = {
    data_field: { bg_color: 'surface', border_color: 'border', label_bg_color: 'header_bg', label_color: 'header_text', value_color: 'value_ink', color: 'ink' },
    text: { color: 'ink', bg_color: 'surface', border_color: 'ink' },
    section_header: { color: 'ink', bg_color: 'shape_fill' },
    time_pill: { color: 'pill_ink', bg_color: 'pill_bg' },
    shape: { fill_color: 'shape_fill', border_color: 'accent', color: 'ink' },
    image: { border_color: 'accent' },
    icon: { bg_color: 'accent' },
  };
  THEME_RESTYLE_DEFAULTS.calc_field = THEME_RESTYLE_DEFAULTS.data_field;

  function themeLibrary() { return window.CANVAS_THEME_TOKENS || {}; }
  function themeByKey(key) {
    const lib = themeLibrary();
    return lib[key] || lib.classic || null;
  }
  function themeKindOf(obj) {
    if (obj.role === 'section_header') return 'section_header';
    if (obj.role === 'time_pill') return 'time_pill';
    return obj.object_type;
  }
  function isThemeColorKey(v) {
    const t = themeByKey('classic'); // every theme exposes the same key set
    return !!(t && typeof v === 'string' && t.colors && typeof t.colors[v] === 'string');
  }
  // The token of `theme` whose color equals `raw` for this field, or null.
  function themeRekey(field, raw, theme) {
    if (!theme || typeof raw !== 'string' || raw.charAt(0) !== '#') return null;
    const colors = theme.colors || {};
    const want = raw.toLowerCase();
    const pri = THEME_REKEY_PRIORITY[field] || [];
    const keys = pri.concat(Object.keys(colors).filter((k) => pri.indexOf(k) === -1));
    for (let i = 0; i < keys.length; i++) {
      const val = colors[keys[i]];
      if (typeof val === 'string' && val.toLowerCase() === want) return keys[i];
    }
    return null;
  }
  function themePaletteIndex(palette, raw) {
    const want = String(raw).toLowerCase();
    for (let i = 0; i < palette.length; i++) {
      if (typeof palette[i] === 'string' && palette[i].toLowerCase() === want) return i;
    }
    return -1;
  }
  // Charts/tables keep their literal card colors (fixed dark
  // chart internals) — their style never participates in theme operations.
  function themeSkipsStyle(obj) { return obj.object_type === 'chart' || obj.object_type === 'table'; }

  // Count raw colors that match neither a token key nor the outgoing theme's
  // palette — the user's own picks the confirm dialog is about.
  function countCustomThemeColors(objects, fromTheme) {
    let n = 0;
    objects.forEach((obj) => {
      if (!obj || typeof obj !== 'object') return;
      const palette = (fromTheme.colors || {}).series || [];
      if (!themeSkipsStyle(obj)) {
        const s = obj.style || {};
        THEME_COLOR_FIELDS.forEach((f) => {
          const v = s[f];
          if (typeof v !== 'string' || !v || v === 'transparent') return;
          // Series-palette values count too — seeds bake them raw into
          // funnel bars and other decorations.
          if (isThemeColorKey(v) || themeRekey(f, v, fromTheme)
              || themePaletteIndex(palette, v) !== -1) return;
          n++;
        });
      }
      if (obj.object_type === 'chart' && obj.chart && Array.isArray(obj.chart.series)) {
        obj.chart.series.forEach((sr) => {
          if (!sr || typeof sr.color !== 'string' || !sr.color) return;
          if (themePaletteIndex(palette, sr.color) === -1) n++;
        });
      }
    });
    return n;
  }

  // Restyle the objects' style/series colors for a theme switch.
  // bake=false (dashboard level): keyed fields stay keys and matching raws
  // are re-keyed. bake=true (object level): every treated field lands as the
  // target theme's concrete color. restyle=true additionally maps custom
  // raws onto the object kind's insert-time default token.
  function applyThemeToObjects(objects, fromTheme, toTheme, opts) {
    const bake = !!(opts && opts.bake);
    const restyle = !!(opts && opts.restyle);
    const toColors = toTheme.colors || {};
    objects.forEach((obj) => {
      if (!obj || typeof obj !== 'object') return;
      if (!themeSkipsStyle(obj)) {
        const s = obj.style || {};
        const defaults = THEME_RESTYLE_DEFAULTS[themeKindOf(obj)] || {};
        const fromPal = (fromTheme.colors || {}).series || [];
        const toPal = toColors.series || [];
        THEME_COLOR_FIELDS.forEach((f) => {
          const v = s[f];
          if (typeof v !== 'string' || !v || v === 'transparent') return;
          let key = isThemeColorKey(v) ? v : themeRekey(f, v, fromTheme);
          if (!key) {
            // A raw value from the outgoing SERIES palette (seeded funnel
            // bars and decorations bake these) has no token
            // key: it value-maps to the same slot of the target palette.
            const palIdx = themePaletteIndex(fromPal, v);
            if (palIdx !== -1 && typeof toPal[palIdx] === 'string') {
              s[f] = toPal[palIdx];
              return;
            }
            if (restyle) key = defaults[f] || null;
          }
          if (!key) return; // the user's own color — kept
          if (bake) {
            if (typeof toColors[key] === 'string') s[f] = toColors[key];
          } else {
            s[f] = key;
          }
        });
      }
      // Trend series colors are always raw hex. Palette colors map by INDEX
      // so a series keeps its slot across themes (the stable
      // series_index lesson); restyle drops custom ones onto the target
      // palette by position.
      if (obj.object_type === 'chart' && obj.chart && Array.isArray(obj.chart.series)) {
        const fromPal = (fromTheme.colors || {}).series || [];
        const toPal = toColors.series || [];
        if (toPal.length) {
          obj.chart.series.forEach((sr, i) => {
            if (!sr || typeof sr.color !== 'string' || !sr.color) return;
            const idx = themePaletteIndex(fromPal, sr.color);
            if (idx !== -1) sr.color = toPal[idx % toPal.length];
            else if (restyle) sr.color = toPal[i % toPal.length];
          });
        }
      }
    });
  }

  function applyThemeToDoc(key, restyle) {
    const fromTheme = themeByKey(DOC.page.theme);
    const toTheme = themeLibrary()[key];
    if (!fromTheme || !toTheme) return;
    applyThemeToObjects(DOC.objects, fromTheme, toTheme, { bake: false, restyle });
    // The page background is raw (a color input can't display a token key —
    // the seed decision): remap it through the token it matches on
    // the outgoing theme; a custom background only moves under restyle.
    const bg = DOC.page.background;
    if (bg && bg.type === 'color' && typeof bg.value === 'string') {
      const bgKey = themeRekey('page_bg', bg.value, fromTheme) || (restyle ? 'bg' : null);
      const toVal = bgKey && (toTheme.colors || {})[bgKey];
      if (typeof toVal === 'string') bg.value = toVal;
    }
    DOC.page.theme = key;
    syncPanel();
    syncObjectPanel(); // color pickers hydrate with resolved values
    markDirty();
    renderAll();
    toast('Theme "' + (toTheme.name || key) + '" applied to the page.');
  }

  // ── Raw-override confirm modal ──
  const themeModal = $('cv-theme-modal');
  let themeModalResolve = null;
  function themeConfirm(customCount) {
    return new Promise((resolve) => {
      themeModalResolve = resolve;
      const plural = customCount !== 1;
      $('cv-theme-modal-msg').textContent =
        customCount + ' color' + (plural ? 's' : '') + ' here ' + (plural ? 'are' : 'is')
        + ' custom — picked by hand, not part of the current theme. Keep '
        + (plural ? 'them' : 'it') + ", or restyle everything with the new theme's colors?";
      themeModal.classList.add('visible');
    });
  }
  function settleThemeModal(choice) {
    themeModal.classList.remove('visible');
    if (themeModalResolve) { themeModalResolve(choice); themeModalResolve = null; }
  }
  $('cv-theme-keep').addEventListener('click', () => settleThemeModal('keep'));
  $('cv-theme-restyle').addEventListener('click', () => settleThemeModal('restyle'));
  $('cv-theme-cancel').addEventListener('click', () => settleThemeModal(null));
  $('cv-theme-modal-close').addEventListener('click', () => settleThemeModal(null));

  // ── Dashboard-level entry: the Page setup theme cards ──
  function requestThemeApply(key) {
    if (mode !== 'edit' || key === DOC.page.theme || !themeLibrary()[key]) return;
    const fromTheme = themeByKey(DOC.page.theme);
    let custom = countCustomThemeColors(DOC.objects, fromTheme);
    const bg = DOC.page.background;
    if (bg && bg.type === 'color' && typeof bg.value === 'string'
        && !themeRekey('page_bg', bg.value, fromTheme)) custom++;
    if (!custom) { applyThemeToDoc(key, false); return; }
    themeConfirm(custom).then((choice) => {
      if (choice) applyThemeToDoc(key, choice === 'restyle');
    });
  }

  const themeRow = $('cv-theme-row');
  (function renderThemeCards() {
    const lib = themeLibrary();
    Object.keys(lib).forEach((key) => {
      const t = lib[key] || {};
      const colors = t.colors || {};
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'cv-theme-card';
      card.dataset.themeKey = key;
      card.title = 'Apply the ' + (t.name || key) + ' theme to this page';
      const strip = document.createElement('span');
      strip.className = 'cv-theme-swatch';
      // Same swatch recipe as the builder's Create-dialog picker.
      [colors.paper, colors.accent, colors.header_bg].concat((colors.series || []).slice(0, 3))
        .filter(Boolean).forEach((c) => {
          const seg = document.createElement('span');
          seg.style.background = c;
          strip.appendChild(seg);
        });
      const name = document.createElement('span');
      name.className = 'cv-theme-card-name';
      name.textContent = t.name || key;
      card.appendChild(strip);
      card.appendChild(name);
      card.addEventListener('click', () => requestThemeApply(key));
      themeRow.appendChild(card);
    });
  })();

  // ── Object-level entry: the Selected-object panel ──
  // The object whose theme dropdown default was last set (syncObjectPanel).
  let objThemePanelId = null;
  const objThemeSelect = $('cv-obj-theme');
  (function populateObjThemeSelect() {
    const lib = themeLibrary();
    Object.keys(lib).forEach((key) => {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = (lib[key] && lib[key].name) || key;
      objThemeSelect.appendChild(opt);
    });
  })();

  $('cv-obj-theme-apply').addEventListener('click', () => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.locked) return;
    const key = objThemeSelect.value;
    const toTheme = themeLibrary()[key];
    if (!toTheme) return;
    // Tokens on this object resolve against the PAGE theme today — that is
    // the palette its raw colors are read out of before baking.
    const fromTheme = themeByKey(DOC.page.theme);
    const run = (restyle) => {
      applyThemeToObjects([obj], fromTheme, toTheme, { bake: true, restyle });
      markDirty();
      renderPageChildren();
      syncObjectPanel();
      toast('Theme "' + (toTheme.name || key) + '" applied to the selected object.');
    };
    const custom = countCustomThemeColors([obj], fromTheme);
    if (!custom) run(false);
    else themeConfirm(custom).then((choice) => { if (choice) run(choice === 'restyle'); });
  });

  titleInput.addEventListener('input', markDirty);

  // ── Zoom + mode ──
  $('cv-zoom').addEventListener('change', (e) => {
    scale = parseFloat(e.target.value) || 1;
    renderLayout();
  });

  $('cv-mode-toggle').addEventListener('click', (e) => {
    mode = mode === 'edit' ? 'view' : 'edit';
    document.body.classList.toggle('mode-edit', mode === 'edit');
    document.body.classList.toggle('mode-view', mode === 'view');
    e.target.textContent = mode === 'edit' ? 'Preview' : 'Back to editor';
    select(null);
    renderAll();
    // Entering preview re-resolves every data field against live data —
    // the liveness guarantee, nothing is baked.
    if (mode === 'view') resolveAllFields();
  });

  // ── Persistence (existing /api/dashboards routes) ──
  function jsonHeaders() {
    return { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF };
  }

  // Menu the document files under: the builder's
  // "+ Canvas page" button passes ?menu=<open dashboard's menu> so a new
  // page lands in an explicit menu instead of the server's fall-back (the
  // workspace's first menu). Loading an existing doc adopts its saved menu.
  let docMenu = (new URLSearchParams(window.location.search).get('menu') || '').slice(0, 200);

  async function saveDoc() {
    // auto-commit pending binding-panel edits on the selected
    // data field: the panel used to commit only via Apply binding, so a
    // Save with un-applied edits silently persisted the stale binding
    // underneath the visible panel state. Invalid panel state (e.g. equals
    // aggregation without a value) blocks the save with the same toast
    // Apply shows. Computed bindings hide the picker, so they never commit
    // from it.
    const selObj = selectedId && getObj(selectedId);
    if (selObj && selObj.object_type === 'data_field' && !selObj.locked && !(selObj.binding && selObj.binding.computed)) {
      if (!commitBindingFromPanel(selObj, false)) return false;
    }
    // Strip runtime-only keys (_value/_error on data fields) — persisted
    // bindings must stay LIVE references, never baked values.
    const cleanConfig = JSON.parse(JSON.stringify(DOC, (k, v) => (k.startsWith('_') ? undefined : v)));
    const payload = {
      dashboard_type: 'canvas',
      title: titleInput.value.trim() || 'Untitled Canvas',
      canvas_config: cleanConfig,
    };
    if (docMenu) payload.menu = docMenu;
    if (docId) payload.id = docId;
    try {
      const res = await fetch(API_BASE + 'dashboards', {
        method: 'POST', headers: jsonHeaders(), body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || !data.success) {
        toast('Save failed: ' + (data.error || ('HTTP ' + res.status)), true);
        return false;
      }
      docId = data.id;
      dirty = false;
      history.replaceState(null, '', 'canvas?id=' + encodeURIComponent(docId));
      window.DASHBOARD_STATE.id = docId; // keep the bridge id current
      toast('Saved.');
      return true;
    } catch (err) {
      toast('Save failed: ' + err.message, true);
      return false;
    }
  }
  $('cv-save').addEventListener('click', saveDoc);
  // the shared table panel's "Refresh & Save" button (and any
  // other builder.js code path) calls the builder's global saveDashboard();
  // on the canvas page that must save the canvas document instead. Function
  // declarations are writable window bindings, so this reassignment is what
  // builder.js' own bare `saveDashboard()` calls resolve to.
  window.saveDashboard = function () { return saveDoc(); };

  async function loadDoc(id) {
    try {
      const res = await fetch(API_BASE + 'dashboards/' + encodeURIComponent(id));
      const data = await res.json();
      if (data.error) { toast('Load failed: ' + data.error, true); return; }
      if (data.dashboard_type !== 'canvas') {
        toast('Not a canvas page — open it in the Dashboard Builder instead.', true);
        return;
      }
      DOC = normalizeDoc(data.canvas_config);
      docId = data.id;
      titleInput.value = data.title || 'Untitled Canvas';
      docMenu = data.menu || docMenu;
      dirty = false;
      history.replaceState(null, '', 'canvas?id=' + encodeURIComponent(docId));
      select(null);
      syncFilterBridge();
      syncPanel();
      renderAll();
      resetHistory();
      resolveAllFields(); // live values on every open — never baked
    } catch (err) {
      toast('Load failed: ' + err.message, true);
    }
  }

  function confirmDiscard() {
    return !dirty || window.confirm('You have unsaved changes. Discard them?');
  }

  function newDoc() {
    if (!confirmDiscard()) return;
    DOC = blankDoc();
    docId = null;
    titleInput.value = 'Untitled Canvas';
    dirty = false;
    history.replaceState(null, '', 'canvas');
    select(null);
    syncFilterBridge();
    syncPanel();
    renderAll();
    resetHistory();
  }
  $('cv-new').addEventListener('click', newDoc);
  $('cv-undo').addEventListener('click', undo);
  $('cv-redo').addEventListener('click', redo);

  // no delete from the Canvas Editor — deleting a canvas page
  // happens only from the Dashboard Builder list (same DELETE route).

  // ── Open dialog ──
  const openModal = $('cv-open-modal');

  async function fetchCanvasList() {
    const res = await fetch(API_BASE + 'dashboards');
    const data = await res.json();
    if (!data.success) throw new Error(data.error || 'list failed');
    return (data.dashboards || []).filter((d) => d.dashboard_type === 'canvas');
  }

  async function showOpenDialog() {
    const list = $('cv-open-list');
    list.innerHTML = '';
    openModal.classList.add('visible');
    let docs;
    try {
      docs = await fetchCanvasList();
    } catch (err) {
      list.innerHTML = '<div class="cv-open-empty">Could not load canvas pages: ' + err.message + '</div>';
      return;
    }
    if (!docs.length) {
      list.innerHTML = '<div class="cv-open-empty">No canvas pages yet — create one and Save.</div>';
      return;
    }
    docs.forEach((d) => {
      const row = document.createElement('div');
      row.className = 'cv-open-row';

      const title = document.createElement('span');
      title.className = 'cv-open-title';
      title.textContent = d.title;
      row.appendChild(title);

      const date = document.createElement('span');
      date.className = 'cv-open-date';
      date.textContent = d.updated_at || '';
      row.appendChild(date);

      const renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.textContent = 'Rename';
      renameBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const name = window.prompt('New name for "' + d.title + '":', d.title);
        if (name === null) return;
        const trimmed = name.trim().slice(0, 200);
        if (!trimmed || trimmed === d.title) return;
        try {
          const res = await fetch(API_BASE + 'dashboards/' + encodeURIComponent(d.id) + '/properties', {
            method: 'PATCH', headers: jsonHeaders(), body: JSON.stringify({ title: trimmed }),
          });
          const data = await res.json();
          if (!res.ok || !data.success) {
            toast('Rename failed: ' + (data.error || ('HTTP ' + res.status)), true);
            return;
          }
          if (d.id === docId) titleInput.value = trimmed;
          showOpenDialog(); // refresh the list in place
        } catch (err) {
          toast('Rename failed: ' + err.message, true);
        }
      });
      row.appendChild(renameBtn);

      row.addEventListener('click', () => {
        if (!confirmDiscard()) return;
        openModal.classList.remove('visible');
        loadDoc(d.id);
      });
      list.appendChild(row);
    });
  }
  $('cv-open').addEventListener('click', showOpenDialog);
  $('cv-open-close').addEventListener('click', () => openModal.classList.remove('visible'));
  openModal.addEventListener('click', (e) => {
    if (e.target === openModal) openModal.classList.remove('visible');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    openModal.classList.remove('visible');
    seedModal.classList.remove('visible');
    iconModal.classList.remove('visible');
    iconPickTargetId = null;
    shapeMenu.classList.remove('visible');
    imagePickTargetId = null;
    hideContextMenu();
  });

  window.addEventListener('beforeunload', (e) => {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = '';
  });

  // ── AI chat panel (foundation: chat Q&A + attachments) ──
  // The panel chrome only exists when the server-side canvas_ai flag is on
  // (canvas.html renders it conditionally), so everything is guarded on the
  // toggle button. Conversation state — including attachment bytes — lives
  // server-side; the browser only holds the conversation id.
  (function initCanvasAiChat() {
    const aiToggle = $('cv-ai-toggle');
    const aiPanel = $('cv-ai-panel');
    if (!aiToggle || !aiPanel) return; // feature flag off

    const aiMessages = $('cv-ai-messages');
    const aiEmpty = $('cv-ai-empty');
    const aiInput = $('cv-ai-input');
    const aiSend = $('cv-ai-send');
    const aiFile = $('cv-ai-file');
    const aiStrip = $('cv-ai-attach-strip');
    const aiComplex = $('cv-ai-complex');
    const aiModel = $('cv-ai-model');
    const aiSpend = $('cv-ai-spend'); // workspace-spend readout

    const AI_MAX_FILE_BYTES = 20 * 1024 * 1024; // mirrors MAX_ATTACHMENT_BYTES server-side
    // v1 formats (mirrors canvas_ai_service): images, PDF, plain text/CSV.
    const AI_MIME_RE = /^(image\/(png|jpeg|gif|webp)|application\/pdf|text\/(plain|csv))$/;
    const AI_TIER_LABELS = { fast: 'Haiku · fast', default: 'Sonnet', complex: 'Opus · complex' };

    let aiConversationId = null;
    let aiPendingFiles = [];
    let aiBusy = false;

    function aiSyncSend() {
      aiSend.disabled = aiBusy || (!aiInput.value.trim() && !aiPendingFiles.length);
    }

    // Assistant replies are markdown-ish; render text safely (DOM text
    // nodes only — an attached document must never inject HTML) with a
    // minimal **bold** pass.
    function aiRenderText(target, text) {
      String(text).split('**').forEach((part, i) => {
        if (i % 2 === 1 && part) {
          const b = document.createElement('strong');
          b.textContent = part;
          target.appendChild(b);
        } else if (part) {
          target.appendChild(document.createTextNode(part));
        }
      });
    }

    function aiAppendMsg(kind, text, fileNames) {
      if (aiEmpty) aiEmpty.style.display = 'none';
      const div = document.createElement('div');
      div.className = 'cv-ai-msg ' + kind;
      aiRenderText(div, text);
      if (fileNames && fileNames.length) {
        const files = document.createElement('span');
        files.className = 'cv-ai-files';
        files.textContent = '\u{1F4CE} ' + fileNames.join(', ');
        div.appendChild(files);
      }
      aiMessages.appendChild(div);
      aiMessages.scrollTop = aiMessages.scrollHeight;
      return div;
    }

    // flagged-field list (validation + AI-reported unsupported
    // elements). DOM text nodes only; issue strings come from the AI turn.
    function aiAppendIssues(target, issues) {
      if (!issues || !issues.length) return;
      const wrap = document.createElement('div');
      wrap.className = 'cv-ai-issues';
      const head = document.createElement('div');
      head.className = 'cv-ai-issues-head';
      head.textContent = '⚠ Not included (' + issues.length + '):';
      wrap.appendChild(head);
      issues.forEach((iss) => {
        const li = document.createElement('div');
        li.className = 'cv-ai-issue';
        li.textContent = (iss.label || 'element') + ' — ' + (iss.reason || 'not supported');
        wrap.appendChild(li);
      });
      const note = document.createElement('div');
      note.className = 'cv-ai-issues-note';
      note.textContent = 'Kavanah Analytics doesn’t support these yet — contact support if you need them.';
      wrap.appendChild(note);
      target.appendChild(wrap);
    }

    // proposal card: title, counts, issues, and the preview
    // button. Preview loads the config into the editor as an UNSAVED new
    // document (exactly the seedFromReport contract) — nothing persists
    // until the user saves through the normal save flow.
    function aiAppendProposal(p) {
      const card = document.createElement('div');
      card.className = 'cv-ai-msg assistant cv-ai-proposal';
      const title = document.createElement('div');
      title.className = 'cv-ai-proposal-title';
      title.textContent = p.title || 'AI Dashboard';
      card.appendChild(title);
      const meta = document.createElement('div');
      meta.className = 'cv-ai-proposal-meta';
      meta.textContent = (p.tile_count || 0) + ' tile(s) · ' + (p.section_count || 0) + ' section(s)';
      card.appendChild(meta);
      if (p.summary) {
        const sum = document.createElement('div');
        sum.className = 'cv-ai-proposal-summary';
        sum.textContent = p.summary;
        card.appendChild(sum);
      }
      aiAppendIssues(card, p.issues);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cv-ai-proposal-preview';
      btn.textContent = 'Preview on canvas';
      btn.addEventListener('click', () => aiApplyProposal(p));
      card.appendChild(btn);
      aiMessages.appendChild(card);
      aiMessages.scrollTop = aiMessages.scrollHeight;
    }

    function aiApplyProposal(p) {
      if (!p || !p.canvas_config) return;
      if (!confirmDiscard()) return;
      DOC = normalizeDoc(p.canvas_config);
      docId = null;
      titleInput.value = (p.title || 'AI Dashboard').slice(0, 200);
      dirty = true;
      history.replaceState(null, '', 'canvas');
      select(null);
      syncFilterBridge();
      syncPanel();
      renderAll();
      resetHistory();
      resolveAllFields();
      toast('AI proposal loaded — unsaved preview. Save to keep it.');
    }

    // modification card: summary, the changes diff (+/−/± per
    // object), issues, and the explicit Apply button. Applying swaps the
    // merged config in as an UNDOABLE unsaved edit on the SAME document —
    // nothing persists until the user saves through the normal save flow.
    const AI_CHANGE_GLYPHS = { add: '+', remove: '−', update: '±', page: '⚙' };

    function aiAppendModification(m, sentDocJson) {
      const card = document.createElement('div');
      card.className = 'cv-ai-msg assistant cv-ai-proposal';
      const title = document.createElement('div');
      title.className = 'cv-ai-proposal-title';
      title.textContent = 'Proposed change' + (m.published_warning ? ' ⚠ published page' : '');
      card.appendChild(title);
      if (m.summary) {
        const sum = document.createElement('div');
        sum.className = 'cv-ai-proposal-summary';
        sum.textContent = m.summary;
        card.appendChild(sum);
      }
      (m.changes || []).forEach((c) => {
        const li = document.createElement('div');
        li.className = 'cv-ai-change ' + (c.op || '');
        li.textContent = (AI_CHANGE_GLYPHS[c.op] || '·') + ' ' + (c.label || c.op);
        card.appendChild(li);
      });
      aiAppendIssues(card, m.issues);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cv-ai-proposal-preview';
      btn.textContent = 'Apply to canvas';
      btn.addEventListener('click', () => aiApplyModification(m, sentDocJson));
      card.appendChild(btn);
      aiMessages.appendChild(card);
      aiMessages.scrollTop = aiMessages.scrollHeight;
    }

    function aiApplyModification(m, sentDocJson) {
      if (!m || !m.canvas_config) return;
      // Staleness guard: the merged config replaces the WHOLE page, so any
      // manual edit made after the turn was sent would be overwritten.
      const nowJson = JSON.stringify(DOC, (k, v) => (k.startsWith('_') ? undefined : v));
      if (sentDocJson && nowJson !== sentDocJson
          && !window.confirm('The page has changed since this was proposed — applying will overwrite those edits. Apply anyway?')) return;
      // a live published page gets an extra confirm.
      if (m.published_warning
          && !window.confirm('This page is published — once you apply and save, viewers will see the change. Continue?')) return;
      DOC = normalizeDoc(m.canvas_config);
      dirty = true;
      select(null);
      syncFilterBridge();
      syncPanel();
      renderAll();
      recordHistory(); // one undo step — Ctrl+Z reverts the AI change
      resolveAllFields();
      toast('AI changes applied — unsaved. Ctrl+Z to revert, Save to keep.');
    }

    function aiRenderChips() {
      aiStrip.innerHTML = '';
      aiPendingFiles.forEach((f, idx) => {
        const chip = document.createElement('span');
        chip.className = 'cv-ai-chip';
        const name = document.createElement('span');
        name.textContent = f.name;
        chip.appendChild(name);
        const x = document.createElement('button');
        x.type = 'button';
        x.textContent = '✕';
        x.title = 'Remove attachment';
        x.addEventListener('click', () => {
          aiPendingFiles.splice(idx, 1);
          aiRenderChips();
          aiSyncSend();
        });
        chip.appendChild(x);
        aiStrip.appendChild(chip);
      });
    }

    async function aiSendTurn() {
      const message = aiInput.value.trim();
      if (aiBusy || (!message && !aiPendingFiles.length)) return;
      aiBusy = true;
      aiSyncSend();
      aiAppendMsg('user', message || '(attached files)', aiPendingFiles.map((f) => f.name));

      const fd = new FormData();
      fd.append('message', message);
      if (aiConversationId) fd.append('conversation_id', aiConversationId);
      if (aiComplex.checked) fd.append('complex_mode', '1');
      // the open page rides with every turn so the AI can modify
      // it. Runtime keys (_value/_error) are stripped exactly like history
      // snapshots; the JSON also anchors the staleness check at apply time.
      const sentDocJson = JSON.stringify(DOC, (k, v) => (k.startsWith('_') ? undefined : v));
      fd.append('dashboard', JSON.stringify({
        dashboard_id: docId, title: titleInput.value, canvas_config: JSON.parse(sentDocJson),
      }));
      aiPendingFiles.forEach((f) => fd.append('attachments', f, f.name));
      aiInput.value = '';
      aiPendingFiles = [];
      aiRenderChips();

      const typing = document.createElement('div');
      typing.className = 'cv-ai-typing';
      typing.textContent = 'Thinking';
      aiMessages.appendChild(typing);
      aiMessages.scrollTop = aiMessages.scrollHeight;

      try {
        const res = await fetch(API_BASE + 'ai/chat', {
          method: 'POST', headers: { 'X-CSRF-Token': CSRF }, body: fd,
        });
        // a proxy-level 502/504 returns an HTML error page, so
        // parsing unconditionally throws "Unexpected token '<'" at the user.
        // Only parse JSON responses; otherwise surface the HTTP status.
        const isJson = (res.headers.get('content-type') || '').includes('application/json');
        const data = isJson ? await res.json() : null;
        typing.remove();
        if (!res.ok || !data || !data.success) {
          aiAppendMsg('error', (data && data.error) || ('The server returned an error (HTTP ' + res.status + ').'));
          return;
        }
        aiConversationId = data.conversation_id || aiConversationId;
        // a tool-only turn (proposal/modification card,
        // no prose) renders just the card; '(empty reply)' only appears
        // when the turn produced nothing at all.
        let replyEl = null;
        if (data.reply || (!data.proposal && !data.modification)) {
          replyEl = aiAppendMsg('assistant', data.reply || '(empty reply)');
        }
        // issues WITHOUT a proposal/modification (every
        // tile or change failed validation) render under the reply; with a
        // card they render inside it instead.
        if (data.issues && replyEl && !data.proposal && !data.modification) aiAppendIssues(replyEl, data.issues);
        if (data.proposal) aiAppendProposal(data.proposal);
        if (data.modification) aiAppendModification(data.modification, sentDocJson);
        const tierLabel = AI_TIER_LABELS[data.tier] || data.tier || '';
        aiModel.textContent = tierLabel;
        aiModel.title = 'Model: ' + ((data.usage || {}).model || 'unknown');
        // refresh the workspace-spend readout with the turn just
        // billed. Absent key = readout unavailable this turn; keep the last
        // known figure rather than blanking it mid-conversation.
        if (data.workspace_spend && data.workspace_spend.label) {
          aiSpend.textContent = data.workspace_spend.label;
          aiSpend.title = data.workspace_spend.title || '';
        }
      } catch (err) {
        typing.remove();
        aiAppendMsg('error', 'Request failed: ' + err.message);
      } finally {
        aiBusy = false;
        aiSyncSend();
        aiInput.focus();
      }
    }

    // the assistant takes the right edge: the Page Setup panel
    // (#cv-panel) collapses while the assistant is open and restores when
    // it closes. Inline '' hands display control back to the stylesheet
    // (which still hides the panel in preview mode).
    const setupPanel = $('cv-panel');
    function setAiPanelOpen(open) {
      aiPanel.style.display = open ? 'flex' : 'none';
      if (setupPanel) setupPanel.style.display = open ? 'none' : '';
      if (open) aiInput.focus();
    }
    aiToggle.addEventListener('click', () => {
      setAiPanelOpen(aiPanel.style.display === 'none');
    });
    $('cv-ai-close').addEventListener('click', () => setAiPanelOpen(false));
    $('cv-ai-new').addEventListener('click', () => {
      aiConversationId = null;
      aiPendingFiles = [];
      aiRenderChips();
      aiMessages.querySelectorAll('.cv-ai-msg, .cv-ai-typing').forEach((n) => n.remove());
      if (aiEmpty) aiEmpty.style.display = '';
      aiModel.textContent = '';
      aiSyncSend();
    });
    $('cv-ai-attach').addEventListener('click', () => aiFile.click());
    aiFile.addEventListener('change', () => {
      Array.from(aiFile.files || []).forEach((f) => {
        // .csv/.txt often arrive with vendor MIME types — fall back to the
        // extension for the text formats; everything else is MIME-matched.
        const extOk = /\.(csv|txt)$/i.test(f.name);
        if (!AI_MIME_RE.test(f.type) && !extOk) {
          toast('"' + f.name + '" is not a supported attachment type (images, PDF, TXT, CSV).', true);
          return;
        }
        if (f.size > AI_MAX_FILE_BYTES) {
          toast('"' + f.name + '" is over the 20 MB attachment limit.', true);
          return;
        }
        aiPendingFiles.push(f);
      });
      aiFile.value = '';
      aiRenderChips();
      aiSyncSend();
    });
    aiInput.addEventListener('input', aiSyncSend);
    aiInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        aiSendTurn();
      }
    });
    aiSend.addEventListener('click', aiSendTurn);
  })();

  // ── Boot: ?id=<dashboard_id> opens an existing document ──
  syncFilterBridge(); // bridge + strip before first paint
  syncPanel();
  renderAll();
  const bootParams = new URLSearchParams(window.location.search);
  const bootId = bootParams.get('id');
  if (!bootId) {
    // follow-up: "+ Canvas page" collects the page name in the
    // builder's Create New Dashboard dialog and hands it over as ?title=
    // (alongside the ?menu= it already sent). Set before resetHistory so
    // the name is part of the undo baseline, not an undoable edit.
    const bootTitle = (bootParams.get('title') || '').trim().slice(0, 200);
    if (bootTitle) titleInput.value = bootTitle;
    // the dialog's theme picker rides along as ?theme=. Apply
    // the ref plus the theme's page background to the blank page (also
    // before resetHistory); unknown keys are ignored and stay classic.
    const bootTheme = (bootParams.get('theme') || '').trim();
    const bootTokens = (window.CANVAS_THEME_TOKENS || {})[bootTheme];
    if (bootTheme && bootTokens) {
      DOC.page.theme = bootTheme;
      const themeBg = (bootTokens.colors || {}).bg;
      if (typeof themeBg === 'string') DOC.page.background = { type: 'color', value: themeBg };
      syncPanel();
      renderAll();
    }
  }
  resetHistory();
  if (bootId) loadDoc(bootId);
})();
