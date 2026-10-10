/**
 * Canvas Builder spike — THROWAWAY prototype. Not product code.
 *
 * Proves the architecture chosen for the epic (see
 * the private docs, 2026-07-07):
 *   - absolute-positioned HTML/CSS rendering on a fixed-dimension page div
 *   - CSS transform: scale() zoom, with pointer math divided by scale
 *   - canvas document schema v1 (page + objects[], z-order = array order)
 *   - data fields bound as mini metric chart-configs resolved through the
 *     existing POST /api/query path (live values, server-side scope)
 *   - identical DOM for edit and published-view modes; editor chrome is
 *     CSS-gated on body.mode-edit
 *
 * The document lives in memory only. Persistence is.
 */
(function () {
  'use strict';

  // Relative to /dashboard-builder/canvas-prototype → /dashboard-builder/api/…
  const API_BASE = 'api/';
  const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';

  // ── Canvas document (schema v1 — the shape will persist) ──
  const CANVAS_DOC = {
    schema_version: 1,
    page: {
      width: 1280,
      height: 720,
      orientation: 'landscape',
      background: { type: 'color', value: '#ffffff' },
      grid: { size: 20, show: false, snap: false },
    },
    // z-order = array order, bottom → top. Groups (group_id) are.
    objects: [
      {
        object_id: 'obj-seed-text',
        object_type: 'text',
        frame: { x: 80, y: 60, w: 420, h: 60, rotation: 0 },
        locked: false,
        group_id: null,
        style: { font_size: 15 },
        text: { content: 'Double-click to edit me. Drag to move. Corner handle resizes.' },
      },
      {
        object_id: 'obj-seed-field',
        object_type: 'data_field',
        frame: { x: 80, y: 160, w: 260, h: 110, rotation: 0 },
        locked: false,
        group_id: null,
        style: {},
        binding: null, // {data_view, field, aggregation, filters:[], format:{}}
      },
    ],
  };

  let mode = 'edit'; // 'edit' | 'view'
  let scale = 1;
  let selectedId = null;
  let objSeq = 0;

  const pageEl = document.getElementById('cp-page');
  const scalerEl = document.getElementById('cp-page-scaler');
  const selToolbar = document.getElementById('cp-sel-toolbar');
  const configEl = document.getElementById('cp-config');

  // ── Doc helpers ──
  const getObj = (id) => CANVAS_DOC.objects.find((o) => o.object_id === id);
  const objIndex = (id) => CANVAS_DOC.objects.findIndex((o) => o.object_id === id);

  function newId() {
    objSeq += 1;
    return 'obj-' + objSeq + '-' + Math.random().toString(36).slice(2, 8);
  }

  // ── Page + object rendering (shared by edit and view modes) ──
  function renderPage() {
    pageEl.style.width = CANVAS_DOC.page.width + 'px';
    pageEl.style.height = CANVAS_DOC.page.height + 'px';
    if (CANVAS_DOC.page.background.type === 'color') {
      pageEl.style.background = CANVAS_DOC.page.background.value;
    }
    // Scale the page itself from its top-left corner; the scaler is a plain
    // box sized to the scaled footprint so the workarea scrollbars cover the
    // full page at any zoom (no unreachable left edge above 100%).
    pageEl.style.transform = 'scale(' + scale + ')';
    scalerEl.style.width = CANVAS_DOC.page.width * scale + 'px';
    scalerEl.style.height = CANVAS_DOC.page.height * scale + 'px';
  }

  function applyFrame(el, obj, zIndex) {
    el.style.left = obj.frame.x + 'px';
    el.style.top = obj.frame.y + 'px';
    el.style.width = obj.frame.w + 'px';
    el.style.height = obj.frame.h + 'px';
    el.style.zIndex = String(zIndex + 1);
  }

  function renderObjects() {
    pageEl.innerHTML = '';
    CANVAS_DOC.objects.forEach((obj, i) => {
      const el = document.createElement('div');
      el.className = 'cp-object cp-' + obj.object_type.replace('_', '-');
      el.dataset.objectId = obj.object_id;
      applyFrame(el, obj, i);

      if (obj.object_type === 'text') {
        el.textContent = obj.text.content;
        el.style.fontSize = (obj.style.font_size || 15) + 'px';
      } else if (obj.object_type === 'data_field') {
        renderDataFieldBody(el, obj);
      }

      if (obj.object_id === selectedId && mode === 'edit') el.classList.add('selected');

      const handle = document.createElement('div');
      handle.className = 'cp-resize-handle';
      el.appendChild(handle);

      pageEl.appendChild(el);
    });
    positionSelToolbar();
  }

  function renderDataFieldBody(el, obj) {
    el.classList.remove('cp-df-unbound', 'cp-df-error');
    let valueHtml;
    let labelText = '';
    if (!obj.binding) {
      el.classList.add('cp-df-unbound');
      valueHtml = 'Unbound data field — select it and bind a source';
    } else if (obj._error) {
      el.classList.add('cp-df-error');
      valueHtml = 'Binding failed: ' + obj._error;
      labelText = obj.binding.aggregation + ' of ' + obj.binding.field;
    } else {
      valueHtml = obj._value !== undefined ? String(obj._value) : 'Loading…';
      labelText = obj.binding.aggregation + ' of ' + obj.binding.field + ' (' + obj.binding.data_view + ')';
    }
    const valueEl = document.createElement('div');
    valueEl.className = 'cp-df-value';
    valueEl.textContent = valueHtml;
    const labelEl = document.createElement('div');
    labelEl.className = 'cp-df-label';
    labelEl.textContent = labelText;
    el.textContent = '';
    el.appendChild(valueEl);
    el.appendChild(labelEl);
  }

  // ── Live data resolution (the epic's core differentiator) ──
  async function resolveDataField(obj) {
    if (!obj.binding) return;
    delete obj._error;
    obj._value = undefined;
    renderObjects();
    try {
      const res = await fetch(API_BASE + 'query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
        body: JSON.stringify({
          chart_type: 'metric',
          data_view: obj.binding.data_view,
          y_fields: [obj.binding.field],
          aggregation: obj.binding.aggregation,
          filters: [],
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        obj._error = err.error || ('HTTP ' + res.status);
      } else {
        const data = await res.json();
        obj._value = data.value !== undefined && data.value !== null ? data.value : '—';
      }
    } catch (e) {
      obj._error = 'network error';
    }
    renderObjects();
  }

  function resolveAllDataFields() {
    CANVAS_DOC.objects.filter((o) => o.object_type === 'data_field').forEach(resolveDataField);
  }

  // ── Selection ──
  // Updates classes in place — NEVER re-renders the object DOM. A re-render
  // on click replaces the node between the two clicks of a double-click, so
  // the browser never fires dblclick and text editing becomes unreachable.
  function select(id) {
    selectedId = id;
    pageEl.querySelectorAll('.cp-object.selected').forEach((el) => el.classList.remove('selected'));
    if (id) {
      const el = pageEl.querySelector('[data-object-id="' + id + '"]');
      if (el) el.classList.add('selected');
    }
    positionSelToolbar();
    syncConfigPanel();
  }

  function positionSelToolbar() {
    const obj = selectedId && getObj(selectedId);
    if (!obj || mode !== 'edit') {
      selToolbar.classList.remove('visible');
      return;
    }
    const el = pageEl.querySelector('[data-object-id="' + selectedId + '"]');
    if (!el) { selToolbar.classList.remove('visible'); return; }
    const r = el.getBoundingClientRect();
    selToolbar.style.left = r.left + 'px';
    selToolbar.style.top = Math.max(4, r.top - 32) + 'px';
    // Live layer readout — z-order feedback even when objects don't overlap.
    document.getElementById('cp-z-indicator').textContent =
      'Layer ' + (objIndex(selectedId) + 1) + '/' + CANVAS_DOC.objects.length;
    // Edit button only makes sense for text objects.
    document.getElementById('cp-edit-text').style.display =
      obj.object_type === 'text' ? '' : 'none';
    selToolbar.classList.add('visible');
  }

  function syncConfigPanel() {
    const obj = selectedId && getObj(selectedId);
    if (obj && obj.object_type === 'data_field' && mode === 'edit') {
      configEl.classList.add('visible');
      if (obj.binding) {
        const dvSel = document.getElementById('cp-cfg-dv');
        dvSel.value = obj.binding.data_view;
        loadFields(obj.binding.data_view).then(() => {
          document.getElementById('cp-cfg-field').value = obj.binding.field;
        });
        document.getElementById('cp-cfg-agg').value = obj.binding.aggregation;
      }
    } else {
      configEl.classList.remove('visible');
    }
  }

  // ── Pointer interactions: move + resize (edit mode only) ──
  // All pointer deltas are divided by `scale` so dragging stays 1:1 with the
  // page's coordinate space at any zoom — the core math this spike proves.
  let drag = null; // {kind:'move'|'resize', id, startX, startY, frame0}

  pageEl.addEventListener('pointerdown', (e) => {
    if (mode !== 'edit') return;
    const objEl = e.target.closest('.cp-object');
    if (!objEl) { select(null); return; }
    if (objEl.classList.contains('editing')) return; // text being edited
    const id = objEl.dataset.objectId;
    const obj = getObj(id);
    select(id);
    if (obj.locked) return;
    const kind = e.target.classList.contains('cp-resize-handle') ? 'resize' : 'move';
    drag = { kind, id, startX: e.clientX, startY: e.clientY, frame0: { ...obj.frame } };
    // Capture on the OBJECT, not pageEl: pointer capture retargets every
    // subsequent pointer/mouse event — including the derived click/dblclick —
    // to the capture element. Capturing on pageEl made dblclick fire with
    // e.target === pageEl, so the text-edit delegate never matched.
    // (pointermove/pointerup still bubble up to the pageEl listeners.)
    objEl.setPointerCapture(e.pointerId);
    // No preventDefault: user-select:none already suppresses text selection,
    // and canceling pointerdown can suppress the derived dblclick that text
    // editing depends on.
  });

  pageEl.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const obj = getObj(drag.id);
    if (!obj) return;
    const dx = (e.clientX - drag.startX) / scale;
    const dy = (e.clientY - drag.startY) / scale;
    if (drag.kind === 'move') {
      obj.frame.x = Math.min(Math.max(0, drag.frame0.x + dx), CANVAS_DOC.page.width - obj.frame.w);
      obj.frame.y = Math.min(Math.max(0, drag.frame0.y + dy), CANVAS_DOC.page.height - obj.frame.h);
    } else {
      obj.frame.w = Math.min(Math.max(40, drag.frame0.w + dx), CANVAS_DOC.page.width - obj.frame.x);
      obj.frame.h = Math.min(Math.max(24, drag.frame0.h + dy), CANVAS_DOC.page.height - obj.frame.y);
    }
    const el = pageEl.querySelector('[data-object-id="' + drag.id + '"]');
    if (el) applyFrame(el, obj, objIndex(drag.id));
    positionSelToolbar();
  });

  pageEl.addEventListener('pointerup', () => { drag = null; });
  pageEl.addEventListener('pointercancel', () => { drag = null; });

  // ── Text editing (double-click or toolbar Edit button, edit mode only) ──
  function beginTextEdit(objEl) {
    const obj = objEl && getObj(objEl.dataset.objectId);
    if (mode !== 'edit' || !obj || obj.object_type !== 'text' || obj.locked) return;
    if (objEl.classList.contains('editing')) return;
    objEl.classList.add('editing');
    objEl.contentEditable = 'true';
    objEl.focus();
    const commit = () => {
      objEl.contentEditable = 'false';
      objEl.classList.remove('editing');
      obj.text.content = objEl.textContent; // plain text; rich text is
      renderObjects();
    };
    objEl.addEventListener('blur', commit, { once: true });
  }

  pageEl.addEventListener('dblclick', (e) => {
    beginTextEdit(e.target.closest('.cp-object.cp-text'));
  });

  // ── Keyboard: arrow nudge + delete (edit mode only) ──
  document.addEventListener('keydown', (e) => {
    if (mode !== 'edit' || !selectedId) return;
    if (document.activeElement && document.activeElement.isContentEditable) return;
    const obj = getObj(selectedId);
    if (!obj || obj.locked) return;
    const step = e.shiftKey ? 10 : 1;
    const nudges = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (nudges[e.key]) {
      obj.frame.x = Math.min(Math.max(0, obj.frame.x + nudges[e.key][0]), CANVAS_DOC.page.width - obj.frame.w);
      obj.frame.y = Math.min(Math.max(0, obj.frame.y + nudges[e.key][1]), CANVAS_DOC.page.height - obj.frame.h);
      renderObjects();
      e.preventDefault();
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      deleteSelected();
      e.preventDefault();
    }
  });

  // ── Selection toolbar actions ──
  function deleteSelected() {
    const i = objIndex(selectedId);
    if (i >= 0) CANVAS_DOC.objects.splice(i, 1);
    renderObjects();
    select(null);
  }
  document.getElementById('cp-delete').addEventListener('click', deleteSelected);
  document.getElementById('cp-edit-text').addEventListener('click', () => {
    if (!selectedId) return;
    beginTextEdit(pageEl.querySelector('[data-object-id="' + selectedId + '"]'));
  });
  document.getElementById('cp-z-fwd').addEventListener('click', () => {
    const i = objIndex(selectedId);
    if (i >= 0 && i < CANVAS_DOC.objects.length - 1) {
      const [o] = CANVAS_DOC.objects.splice(i, 1);
      CANVAS_DOC.objects.splice(i + 1, 0, o);
      renderObjects();
    }
  });
  document.getElementById('cp-z-back').addEventListener('click', () => {
    const i = objIndex(selectedId);
    if (i > 0) {
      const [o] = CANVAS_DOC.objects.splice(i, 1);
      CANVAS_DOC.objects.splice(i - 1, 0, o);
      renderObjects();
    }
  });

  // ── Topbar: add objects ──
  document.getElementById('cp-add-text').addEventListener('click', () => {
    const id = newId();
    CANVAS_DOC.objects.push({
      object_id: id, object_type: 'text',
      frame: { x: 120, y: 120, w: 240, h: 50, rotation: 0 },
      locked: false, group_id: null,
      style: { font_size: 15 }, text: { content: 'New text box' },
    });
    renderObjects();
    select(id);
  });
  document.getElementById('cp-add-field').addEventListener('click', () => {
    const id = newId();
    CANVAS_DOC.objects.push({
      object_id: id, object_type: 'data_field',
      frame: { x: 160, y: 300, w: 260, h: 110, rotation: 0 },
      locked: false, group_id: null,
      style: {}, binding: null,
    });
    renderObjects();
    select(id);
  });

  // ── Topbar: zoom ──
  document.getElementById('cp-zoom').addEventListener('change', (e) => {
    scale = parseFloat(e.target.value) || 1;
    renderPage();
    renderObjects();
  });

  // ── Topbar: edit / published-view mode toggle ──
  document.getElementById('cp-mode-toggle').addEventListener('click', (e) => {
    mode = mode === 'edit' ? 'view' : 'edit';
    document.body.classList.toggle('mode-edit', mode === 'edit');
    document.body.classList.toggle('mode-view', mode === 'view');
    e.target.textContent = mode === 'edit' ? 'View published' : 'Back to editor';
    select(null);
    // Entering the published view re-resolves every data field against live
    // data — nothing is baked; this is the liveness guarantee.
    if (mode === 'view') resolveAllDataFields();
  });

  // ── Doc JSON modal ──
  document.getElementById('cp-show-json').addEventListener('click', () => {
    const clean = JSON.parse(JSON.stringify(CANVAS_DOC, (k, v) => (k.startsWith('_') ? undefined : v)));
    document.getElementById('cp-json-pre').textContent = JSON.stringify(clean, null, 2);
    document.getElementById('cp-json-modal').classList.add('visible');
  });
  document.getElementById('cp-json-close').addEventListener('click', () => {
    document.getElementById('cp-json-modal').classList.remove('visible');
  });

  // ── Data-field binding config panel ──
  async function loadDataViews() {
    const dvSel = document.getElementById('cp-cfg-dv');
    try {
      const res = await fetch(API_BASE + 'data-views');
      const data = await res.json();
      const views = data.data_views || [];
      dvSel.innerHTML = '<option value="">Choose…</option>' +
        views.map((v) => '<option value="' + v.id + '">' + v.label + '</option>').join('');
    } catch (e) {
      dvSel.innerHTML = '<option value="">Failed to load data views</option>';
    }
  }

  async function loadFields(dataView) {
    const fieldSel = document.getElementById('cp-cfg-field');
    if (!dataView) {
      fieldSel.innerHTML = '<option value="">Pick a data view first</option>';
      return;
    }
    fieldSel.innerHTML = '<option value="">Loading…</option>';
    try {
      const res = await fetch(API_BASE + 'data-views/' + encodeURIComponent(dataView) + '/fields');
      const data = await res.json();
      const numeric = (data.fields || []).filter((f) => f.type === 'numeric');
      fieldSel.innerHTML = numeric.map((f) => '<option value="' + f.name + '">' + f.name + '</option>').join('') ||
        '<option value="">No numeric fields</option>';
    } catch (e) {
      fieldSel.innerHTML = '<option value="">Failed to load fields</option>';
    }
  }

  document.getElementById('cp-cfg-dv').addEventListener('change', (e) => loadFields(e.target.value));
  document.getElementById('cp-cfg-apply').addEventListener('click', () => {
    const obj = selectedId && getObj(selectedId);
    if (!obj || obj.object_type !== 'data_field') return;
    const dataView = document.getElementById('cp-cfg-dv').value;
    const field = document.getElementById('cp-cfg-field').value;
    if (!dataView || !field) return;
    obj.binding = {
      data_view: dataView,
      field: field,
      aggregation: document.getElementById('cp-cfg-agg').value,
      filters: [],
      format: {},
    };
    resolveDataField(obj);
  });

  // ── Boot ──
  renderPage();
  renderObjects();
  loadDataViews();
})();
