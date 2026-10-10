/**
 * dashboard_filters.js — Dashboard-level filter controls.
 *
 * Distinct from filters.js (per-chart developer filters: field + operator
 * + value, builder-only). This module manages dashboard-wide value pickers
 * that render at the top of the dashboard area in BOTH the builder canvas
 * (index.html) and the read-only viewer (view.html).
 *
 * State lives at DASHBOARD_STATE.filters as an array of:
 *   { id, title, data_view, field, field_type, selected_values: [...] }
 *
 * Selection changes do not save — same dirty-flag UX as the rest of the
 * builder. The viewer side calls every handler the same way the builder
 * does; READ-ONLY mode is governed by window.DASHBOARD_FILTERS_READONLY,
 * which view.html will set in Phase 21. In the builder it's undefined →
 * editor + strip both render.
 */

// ══════════════════════════════════════════════════════════════
// Page-local cache for fetched value lists. Mirrors the server-side
// 60s cache so we don't re-fetch on every popover open.
//   key: `${dv}::${field}::${asset_type}::${team_filter_joined}::${org_filter}`
//   value: [{label, value}]
// ══════════════════════════════════════════════════════════════
const DASHBOARD_FILTER_VALUES_CACHE = {};

// Track which filter's popover is currently open so a click-elsewhere
// handler can close it. Null when nothing is open.
let _DASHBOARD_OPEN_FILTER_IDX = null;

// ══════════════════════════════════════════════════════════════
// "All Data Views" support.
//
// A filter whose data_view is the '__ALL__' sentinel (DASHBOARD_FILTER_ALL_DV,
// defined in charts.js) carries a shared keyword field present on EVERY data
// view in the dashboard, and is rebound per-chart at query time
// (_withExtraFilters). The editor offers only fields in the intersection of
// every data view's keyword fields; the value popover reads values from the
// first data view that carries the field.
// ══════════════════════════════════════════════════════════════

// Cache of keyword field-name sets per data view (name -> field meta), and the
// computed intersection. Cleared implicitly per page load.
const _DASHBOARD_DV_FIELDS_CACHE = {};
let _DASHBOARD_ALL_VIEWS_FIELDS = null;

// The distinct data views this dashboard spans: every chart's own data_view,
// PLUS the canva Summary Report's hardcoded data views when a canva chart is
// present (a canva chart config has no top-level data_view — its 8 DVs live in
// renderCanvaChart, exposed as CANVA_SUMMARY_DVS in charts.js).
function _dashboardDataViews() {
  const out = new Set();
  if (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE.charts) {
    Object.keys(DASHBOARD_STATE.charts).forEach(cid => {
      const cfg = DASHBOARD_STATE.charts[cid];
      if (!cfg) return;
      if (cfg.data_view) out.add(cfg.data_view);
      if (cfg.chart_type === 'canva' && typeof CANVA_SUMMARY_DVS !== 'undefined') {
        CANVA_SUMMARY_DVS.forEach(dv => out.add(dv));
      }
    });
  }
  // canvas pages carry no DASHBOARD_STATE.charts; their live
  // elements are data-field bindings. canvas_editor.js / canvas_view.js set
  // this hook to enumerate the distinct binding data views so the
  // "All Data Views" intersection works there too. Undefined on classic pages.
  if (typeof window !== 'undefined' && typeof window.dashboardFilterExtraDataViews === 'function') {
    (window.dashboardFilterExtraDataViews() || []).forEach(dv => dv && out.add(dv));
  }
  return Array.from(out);
}

// Set of keyword-usable BASE field names for one data view (cached).
//
// a field is keyword-usable here if EITHER it is mapped as a bare
// `keyword` OR it has a `.keyword` sub-field (text+keyword). The same field
// drifts between those two shapes across data views — and even across indices
// within ONE wildcard — so the /fields endpoint reports e.g. `team_title`
// (keyword) on one index and `team_title` (text) + `team_title.keyword`
// (keyword) on another. We normalize BOTH to the base name `team_title`, so the
// cross-view intersection isn't silently emptied by the .keyword drift. The
// dashboard-filter query path already expands `.keyword` filters to match both
// mapping shapes server-side (_terms_variants /), so a base-name filter
// resolves correctly regardless of how any single index maps it.
async function _dashboardKeywordFieldNames(dataView) {
  if (_DASHBOARD_DV_FIELDS_CACHE[dataView]) return _DASHBOARD_DV_FIELDS_CACHE[dataView];
  const names = new Set();
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/data-views/' + encodeURIComponent(dataView) + '/fields');
    const data = await res.json();
    if (data && data.success && Array.isArray(data.fields)) {
      const all = data.fields;
      const byName = new Set(all.map(f => f.name));
      all.forEach(f => {
        if (f.type === 'keyword') {
          // Bare keyword field, or the `.keyword` sub-field of a text field —
          // either way the usable base name is the field minus a .keyword tail.
          names.add(f.name.replace(/\.keyword$/, ''));
        } else if (byName.has(f.name + '.keyword')) {
          // A text field that has a .keyword sub-field is keyword-usable too.
          names.add(f.name);
        }
      });
    }
  } catch (e) { /* leave empty — degrades gracefully */ }
  _DASHBOARD_DV_FIELDS_CACHE[dataView] = names;
  return names;
}

// Intersection of keyword-usable base field names across every dashboard DV.
async function _dashboardAllViewsFields() {
  if (_DASHBOARD_ALL_VIEWS_FIELDS) return _DASHBOARD_ALL_VIEWS_FIELDS;
  const dvs = _dashboardDataViews();
  const perDv = await Promise.all(dvs.map(dv => _dashboardKeywordFieldNames(dv)));
  let intersection = null;
  perDv.forEach(names => {
    intersection = (intersection === null)
      ? new Set(names)
      : new Set([...intersection].filter(n => names.has(n)));
  });
  _DASHBOARD_ALL_VIEWS_FIELDS = Array.from(intersection || []).sort();
  return _DASHBOARD_ALL_VIEWS_FIELDS;
}

// Phase 3 — team filtering moved to the dashboard viewer's top-bar
// multi-select (checkboxes + Select all/Clear all). Stop OFFERING team fields in
// the builder filter's field picker so no NEW builder-level team filter gets
// authored — the top-bar control is now the one place teams are filtered.
// Existing builder team filters keep working (the query engine still applies
// them, and this only hides them from the NEW-filter field list — a saved
// team_title filter re-selects fine because populate*() force-adds the saved
// field below). Base-name match so the .keyword drift (team_title vs
// team_title.keyword) is covered; team_esid alias retired too.
const _RETIRED_BUILDER_FILTER_FIELDS = new Set(['team_title', 'team_esid']);
function _isRetiredBuilderFilterField(name) {
  return _RETIRED_BUILDER_FILTER_FIELDS.has(String(name || '').replace(/\.keyword$/, ''));
}

// the data views whose values a filter's popover should union.
//
// A single-DV fetch can't match a multi-DV table's rows: the table's row set
// is the UNION of every column DV's group-by keys, while the popover listed
// one DV's values (for '__ALL__', just the FIRST dashboard DV carrying the
// field). Candidates:
//   * '__ALL__' filter — every dashboard DV;
//   * specific-DV filter — its own DV, plus every DV of any table whose
//     group-by field (x_field) is this filter's field and whose DV set
// includes the filter's DV (mirrors the server's row-key
//     rebind in _run_table_multi_dv_query).
// Candidates that don't carry the field are dropped (fields endpoint,
// cached); if field metadata failed for every candidate, fetch them all
// anyway so the popover degrades like the earlier path did.
async function _filterValueDvs(filter) {
  const base = f => String(f || '').replace(/\.keyword$/, '');
  const isAll = filter.data_view === DASHBOARD_FILTER_ALL_DV;
  const out = new Set(isAll ? _dashboardDataViews() : [filter.data_view]);

  if (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE.charts) {
    Object.values(DASHBOARD_STATE.charts).forEach(cfg => {
      if (!cfg || cfg.chart_type !== 'table' || !cfg.x_field) return;
      if (base(cfg.x_field) !== base(filter.field)) return;
      const chartDvs = new Set(cfg.data_view ? [cfg.data_view] : []);
      (cfg.table_columns || []).forEach(col => {
        if (col && col.data_view) chartDvs.add(col.data_view);
        Object.values((col && col.formula_field_views) || {}).forEach(dv => dv && chartDvs.add(dv));
      });
      if (isAll || chartDvs.has(filter.data_view)) chartDvs.forEach(dv => out.add(dv));
    });
  }

  const dvs = Array.from(out).filter(Boolean);
  const carries = await Promise.all(
    dvs.map(async dv => (await _dashboardKeywordFieldNames(dv)).has(base(filter.field)))
  );
  const kept = dvs.filter((dv, i) => carries[i]);
  return kept.length ? kept : dvs;
}

// ══════════════════════════════════════════════════════════════
// Helpers — DOM-safe element builders (no innerHTML on user input).
// ══════════════════════════════════════════════════════════════

function _clearChildren(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function _option(value, text, selected) {
  const opt = document.createElement('option');
  opt.value = value;
  opt.textContent = text;
  if (selected) opt.selected = true;
  return opt;
}

// ══════════════════════════════════════════════════════════════
// Modal — dashboard filter editor.
// Phase 20 follow-up: lives in #dashboard-filters-modal (a dedicated
// overlay) so chart-level Edit / Properties work in the right panel
// is never displaced by filter editing. The right-panel
// #dashboard-props-panel is back to plain Properties — no tab strip.
// ══════════════════════════════════════════════════════════════

function openDashboardFiltersModal() {
  const overlay = document.getElementById('dashboard-filters-modal');
  if (!overlay) return;
  overlay.style.display = 'flex';
  renderDashboardFiltersEditor();
  // the modal also hosts the author-curated Date basis choices.
  if (typeof renderDateBasisChoicesEditor === 'function') renderDateBasisChoicesEditor();
}

function closeDashboardFiltersModal() {
  const overlay = document.getElementById('dashboard-filters-modal');
  if (!overlay) return;
  overlay.style.display = 'none';
}

// ══════════════════════════════════════════════════════════════
// Editor — picker dropdown + action buttons + edit pane.
// mirrors the table-columns editor pattern (Title dropdown
// to switch between filters; ↑ ↓ + − action row beneath). Avoids the
// scrolling stack of full rows the original modal showed and keeps the
// modal compact when there are many filters.
// ══════════════════════════════════════════════════════════════

function _ensureDashboardFiltersArray() {
  if (typeof DASHBOARD_STATE === 'undefined') return [];
  if (!Array.isArray(DASHBOARD_STATE.filters)) DASHBOARD_STATE.filters = [];
  return DASHBOARD_STATE.filters;
}

// Tracks which filter is currently being edited inside the modal.
// Re-renders pin selection so a re-render after a title edit (or a
// move) doesn't snap back to the first filter.
let _DASHBOARD_FILTER_EDIT_IDX = -1;

// render generation token. The editor is re-entrant (the picker
// dropdown, data-view change, add/remove/move all re-render) and the field
// options load via an un-awaited fetch. A render that supersedes an in-flight
// field-options fetch bumps this token; the stale fetch checks it after its
// await and bails rather than writing options into a select that now belongs
// to a different render (which dropped the saved field to the placeholder).
let _DASHBOARD_FILTER_RENDER_GEN = 0;

async function renderDashboardFiltersEditor() {
  const list = document.getElementById('dashboard-filters-list');
  if (!list) return;
  // bump the generation so any field-options fetch still in flight
  // from a prior render knows it's stale and won't clobber this render's select.
  const myGen = ++_DASHBOARD_FILTER_RENDER_GEN;
  const filters = _ensureDashboardFiltersArray();

  // Fetch the data-view list once; reused across every filter row.
  let dataViews = [];
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/data-views');
    const data = await res.json();
    if (data.success) dataViews = data.data_views || [];
  } catch (e) { /* keep empty — UI degrades gracefully */ }

  // Clamp the edit-target into range. addDashboardFilter() sets it to
  // the new filter's index; removeDashboardFilter() handles its own
  // clamp. Default to the first filter on initial open.
  let editIdx = _DASHBOARD_FILTER_EDIT_IDX;
  if (editIdx < 0 || editIdx >= filters.length) {
    editIdx = filters.length > 0 ? 0 : -1;
    _DASHBOARD_FILTER_EDIT_IDX = editIdx;
  }

  _clearChildren(list);

  // Picker row — Title dropdown listing every filter by its title (or
  // a placeholder when blank). Clicking a label switches the edit pane.
  const pickerRow = document.createElement('div');
  pickerRow.className = 'tc-col-picker-row';
  const pickerSel = document.createElement('select');
  pickerSel.id = 'dashboard-filter-picker';
  if (filters.length === 0) {
    pickerSel.appendChild(_option('-1', '— No filters —', true));
  } else {
    filters.forEach((f, i) => {
      const label = f.title || `(unnamed filter ${i + 1})`;
      pickerSel.appendChild(_option(String(i), label, i === editIdx));
    });
  }
  pickerSel.addEventListener('change', () => {
    selectDashboardFilter(parseInt(pickerSel.value, 10));
  });
  pickerRow.appendChild(pickerSel);
  list.appendChild(pickerRow);

  // Action row — ↑ ↓ + − to mirror the table-columns editor.
  const actionRow = document.createElement('div');
  actionRow.className = 'tc-col-picker-actions';

  const upBtn = document.createElement('button');
  upBtn.type = 'button';
  upBtn.className = 'db-btn db-btn-sm';
  upBtn.title = 'Move up';
  upBtn.textContent = '↑';
  upBtn.disabled = editIdx <= 0;
  upBtn.addEventListener('click', () => moveDashboardFilter(editIdx, -1));

  const downBtn = document.createElement('button');
  downBtn.type = 'button';
  downBtn.className = 'db-btn db-btn-sm';
  downBtn.title = 'Move down';
  downBtn.textContent = '↓';
  downBtn.disabled = editIdx < 0 || editIdx >= filters.length - 1;
  downBtn.addEventListener('click', () => moveDashboardFilter(editIdx, 1));

  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'db-btn db-btn-sm';
  addBtn.title = 'Add filter';
  addBtn.textContent = '+';
  addBtn.addEventListener('click', () => addDashboardFilter());

  const rmBtn = document.createElement('button');
  rmBtn.type = 'button';
  rmBtn.className = 'db-btn db-btn-sm';
  rmBtn.title = 'Remove filter';
  rmBtn.textContent = '−';
  rmBtn.disabled = editIdx < 0;
  rmBtn.addEventListener('click', () => removeDashboardFilter(editIdx));

  actionRow.append(upBtn, downBtn, addBtn, rmBtn);
  list.appendChild(actionRow);

  // Edit pane for the currently-selected filter. Empty state when the
  // dashboard has no filters yet.
  if (editIdx < 0) {
    const hint = document.createElement('p');
    hint.className = 'db-hint';
    hint.style.cssText = 'margin:8px 0 0;color:#888;font-size:12px;';
    hint.textContent = 'No filters yet — click + above to add one.';
    list.appendChild(hint);
    return;
  }

  const pane = _buildFilterEditPane(editIdx, filters[editIdx], dataViews);
  list.appendChild(pane);
  // Field-list population needs the field-select to be in the document
  // (getElementById can't find a detached child) — populate after the
  // append above. await it and pass the render generation so a
  // superseding render abandons this fetch instead of leaving the saved
  // field showing as the placeholder.
  if (filters[editIdx].data_view) {
    await populateFilterFieldOptions(editIdx, filters[editIdx].data_view, filters[editIdx].field, myGen);
  }
}

function _buildFilterEditPane(idx, filter, dataViews) {
  const pane = document.createElement('div');
  pane.className = 'db-dashboard-filter-row';
  pane.id = `dashboard-filter-row-${idx}`;

  // Title input
  const titleGroup = document.createElement('div');
  titleGroup.className = 'db-form-group';
  const titleLbl = document.createElement('label');
  titleLbl.textContent = 'Title';
  const titleIn = document.createElement('input');
  titleIn.type = 'text';
  // follow-up: cap the filter title so it fits the strip button / pill
  // without truncation.
  titleIn.maxLength = 30;
  titleIn.placeholder = 'e.g. Country';
  titleIn.value = filter.title || '';
  titleIn.addEventListener('input', () => {
    onFilterTitleChange(idx, titleIn.value);
    // Refresh the picker label live so the dropdown matches what the
    // user is typing — matches _refreshTableColPickerLabel UX.
    const pickerSel = document.getElementById('dashboard-filter-picker');
    if (pickerSel && pickerSel.options[idx]) {
      pickerSel.options[idx].textContent = titleIn.value || `(unnamed filter ${idx + 1})`;
    }
  });
  titleGroup.append(titleLbl, titleIn);

  // Data view select
  const dvGroup = document.createElement('div');
  dvGroup.className = 'db-form-group';
  const dvLbl = document.createElement('label');
  dvLbl.textContent = 'Data view';
  const dvSel = document.createElement('select');
  dvSel.appendChild(_option('', '— Select data view —', false));
  // "All Data Views" applies a shared (cross-DV) keyword field to
  // every chart on the dashboard. Listed first so multi-source reports (the
  // Summary Report spans 8 data views) can filter all tiles with one filter.
  dvSel.appendChild(_option(DASHBOARD_FILTER_ALL_DV, 'All Data Views', filter.data_view === DASHBOARD_FILTER_ALL_DV));
  dataViews.forEach(dv => {
    dvSel.appendChild(_option(dv.id, dv.label, dv.id === filter.data_view));
  });
  dvSel.addEventListener('change', () => onFilterDvChange(idx, dvSel.value));
  dvGroup.append(dvLbl, dvSel);

  // Field select — populated lazily based on data view
  const fieldGroup = document.createElement('div');
  fieldGroup.className = 'db-form-group';
  const fieldLbl = document.createElement('label');
  fieldLbl.textContent = 'Field (keyword only)';
  const fieldSel = document.createElement('select');
  fieldSel.id = `dashboard-filter-field-${idx}`;
  const initialPlaceholder = filter.data_view ? 'Loading…' : '— Select data view first —';
  fieldSel.appendChild(_option('', initialPlaceholder, false));
  fieldSel.addEventListener('change', () => {
    const opt = fieldSel.options[fieldSel.selectedIndex];
    onFilterFieldChange(
      idx,
      fieldSel.value,
      opt ? opt.dataset.fieldType : '',
      opt ? opt.dataset.assetType : '',
    );
  });
  fieldGroup.append(fieldLbl, fieldSel);

  // Phase 3 — deprecation hint for an existing builder team filter.
  // Team filtering moved to the viewer's top-bar multi-select; a builder-level
  // team filter still works but is no longer the recommended place. Shown only
  // when THIS filter is already on a retired team field (new filters can't pick
  // one — the field picker no longer offers it).
  if (_isRetiredBuilderFilterField(filter.field)) {
    const hint = document.createElement('div');
    hint.className = 'db-field-hint db-field-hint-deprecated';
    hint.textContent = 'Team filtering has moved to the dashboard’s top-bar '
      + 'Team selector. This builder-level team filter still applies, but new '
      + 'team filters should use the top-bar multi-select instead.';
    fieldGroup.appendChild(hint);
  }

  pane.append(titleGroup, dvGroup, fieldGroup);
  return pane;
}

// Switch which filter is being edited. Re-renders the editor; the
// picker dropdown and action buttons update via _DASHBOARD_FILTER_EDIT_IDX.
function selectDashboardFilter(idx) {
  const filters = _ensureDashboardFiltersArray();
  if (idx < 0 || idx >= filters.length) return;
  _DASHBOARD_FILTER_EDIT_IDX = idx;
  renderDashboardFiltersEditor();
}

// Move a filter up (delta=-1) or down (delta=1). Re-renders the
// editor + the strip so the visible order updates immediately.
function moveDashboardFilter(idx, delta) {
  const filters = _ensureDashboardFiltersArray();
  const target = idx + delta;
  if (idx < 0 || idx >= filters.length) return;
  if (target < 0 || target >= filters.length) return;
  const moved = filters.splice(idx, 1)[0];
  filters.splice(target, 0, moved);
  _DASHBOARD_FILTER_EDIT_IDX = target;
  _markDashboardFiltersDirty();
  renderDashboardFiltersEditor();
  renderDashboardFilterStrip();
}

async function populateFilterFieldOptions(idx, dataView, currentField, renderGen) {
  // if a newer render has started since this call was scheduled, the
  // field select we'd write to belongs to a stale render. Bail before touching
  // the DOM so we don't blank out the current render's saved-field selection.
  const _stale = () => renderGen !== undefined && renderGen !== _DASHBOARD_FILTER_RENDER_GEN;
  if (_stale()) return;

  const initial = document.getElementById(`dashboard-filter-field-${idx}`);
  if (!initial) return;
  _clearChildren(initial);
  initial.appendChild(_option('', 'Loading…', false));

  // "All Data Views": field options are the keyword-field
  // intersection across every dashboard data view, not one DV's mapping.
  if (dataView === DASHBOARD_FILTER_ALL_DV) {
    const names = await _dashboardAllViewsFields();
    if (_stale()) return;
    const sel = document.getElementById(`dashboard-filter-field-${idx}`);
    if (!sel) return;
    _clearChildren(sel);
    sel.appendChild(_option('', names.length ? '— Select field —' : '— No shared fields —', false));
    names.forEach(name => {
      // Phase 3 — don't offer team fields for NEW filters (top-bar
      // multi-select owns teams now). Keep a field that's ALREADY saved on this
      // filter so an existing builder team filter still re-selects + edits.
      if (_isRetiredBuilderFilterField(name) && name !== currentField) return;
      const opt = _option(name, name, name === currentField);
      opt.dataset.fieldType = 'keyword';
      sel.appendChild(opt);
    });
    if (currentField && names.includes(currentField)) sel.value = currentField;
    return;
  }

  let data;
  try {
    const res = await fetch(DASHBOARD_BASE + 'api/data-views/' + encodeURIComponent(dataView) + '/fields');
    data = await res.json();
  } catch (e) {
    if (_stale()) return;
    // Re-look-up after the await: the editor list may have been re-rendered
    // while the fetch was in flight (a row added/removed), in which case the
    // captured element is detached. Mutating the live element keeps the UI
    // consistent with current state.
    const errSel = document.getElementById(`dashboard-filter-field-${idx}`);
    if (!errSel) return;
    _clearChildren(errSel);
    errSel.appendChild(_option('', '— Error loading fields —', false));
    return;
  }
  // a newer render superseded us while the fetch was in flight; its
  // own populateFilterFieldOptions owns the select now. Abandon quietly.
  if (_stale()) return;
  // Same re-look-up after the fetch await, for the success path.
  const fieldSel = document.getElementById(`dashboard-filter-field-${idx}`);
  if (!fieldSel) return;
  _clearChildren(fieldSel);
  fieldSel.appendChild(_option('', '— Select field —', false));
  if (data.success && data.fields) {
    data.fields.forEach(f => {
      // Keyword-only this round (decision in plan). Other types skipped.
      if (f.type !== 'keyword') return;
      // Phase 3 — retire team fields from NEW builder filters (top-bar
      // multi-select owns teams); keep an already-saved team field so an
      // existing builder team filter still re-selects.
      if (_isRetiredBuilderFilterField(f.name) && f.name !== currentField) return;
      const opt = _option(f.name, f.name, f.name === currentField);
      opt.dataset.fieldType = f.type;
      // server stamps asset_type on fields it knows are
      // backed by an asset index. The change handler reads this off
      // the option's dataset to flip value_source on the filter.
      if (f.asset_type) opt.dataset.assetType = f.asset_type;
      fieldSel.appendChild(opt);
    });
  }

  // re-assert the saved field onto the select after the options
  // exist. Programmatic selection via the `selected` flag above is correct,
  // but if `currentField` was set on the option list we set .value here too
  // so the control's displayed value matches state even across browsers that
  // don't honor a late `selected` flag. The saved field lives in
  // DASHBOARD_STATE regardless of whether a change event ever fired, so this
  // is the authoritative re-bind — fixing the "reverts to unselected" symptom.
  if (currentField) {
    const hasOption = Array.from(fieldSel.options).some(o => o.value === currentField);
    if (hasOption) {
      fieldSel.value = currentField;
    }
  }

  // heal existing saved filters whose currentField is
  // asset-backed but whose value_source is absent (e.g. saved before
  // the asset-backed UI shipped, or saved against an older
  // (data_view, field) key that no longer matches the registry).
  // Without this, reopening the editor wouldn't re-stamp value_source
  // because no field-change event fires when the field is already
  // populated from the saved doc.
  if (currentField) {
    const filters = _ensureDashboardFiltersArray();
    const filter = filters[idx];
    if (filter) {
      const matched = (data.fields || []).find(f => f.name === currentField);
      const serverAssetType = matched && matched.asset_type;
      if (serverAssetType) {
        if (!filter.value_source || filter.value_source.kind !== 'asset' ||
            filter.value_source.asset_type !== serverAssetType) {
          filter.value_source = { kind: 'asset', asset_type: serverAssetType };
          // No _markDashboardFiltersDirty() here — this is a heal-on-load,
          // not a user edit.
        }
      } else if (filter.value_source) {
        // Server no longer flags this field as asset-backed but the
        // saved filter has a value_source — drop it so the popover
        // doesn't try to fetch from a now-unconfigured asset type.
        delete filter.value_source;
      }
    }
  }
}

function addDashboardFilter() {
  const filters = _ensureDashboardFiltersArray();
  filters.push({
    id: 'f' + Date.now() + '-' + Math.floor(Math.random() * 1000),
    title: '',
    data_view: '',
    field: '',
    field_type: 'keyword',
    selected_values: [],
  });
  // Switch the edit pane to the newly-added filter so the user lands
  // straight on the row they need to fill in.
  _DASHBOARD_FILTER_EDIT_IDX = filters.length - 1;
  _markDashboardFiltersDirty();
  renderDashboardFiltersEditor();
  renderDashboardFilterStrip();
}

function removeDashboardFilter(idx) {
  const filters = _ensureDashboardFiltersArray();
  if (idx < 0 || idx >= filters.length) return;
  const removed = filters.splice(idx, 1)[0];
  // Clamp the edit-target into the new range. Prefer the same index
  // (next filter slides up); fall back to the previous filter when
  // removing the last one; -1 when the list is empty.
  if (filters.length === 0) {
    _DASHBOARD_FILTER_EDIT_IDX = -1;
  } else if (idx >= filters.length) {
    _DASHBOARD_FILTER_EDIT_IDX = filters.length - 1;
  } else {
    _DASHBOARD_FILTER_EDIT_IDX = idx;
  }
  _markDashboardFiltersDirty();
  renderDashboardFiltersEditor();
  renderDashboardFilterStrip();
  // Re-query charts that were under the removed filter's DV — they
  // now show data without that filter clause.
  if (removed && removed.data_view) {
    requeryChartsForDataView(removed.data_view);
  }
}

function onFilterTitleChange(idx, value) {
  const filters = _ensureDashboardFiltersArray();
  if (!filters[idx]) return;
  filters[idx].title = value;
  _markDashboardFiltersDirty();
  renderDashboardFilterStrip();
}

function onFilterDvChange(idx, dv) {
  const filters = _ensureDashboardFiltersArray();
  if (!filters[idx]) return;
  const oldDv = filters[idx].data_view;
  filters[idx].data_view = dv;
  // Changing DV invalidates the field choice and any selected values.
  filters[idx].field = '';
  filters[idx].selected_values = [];
  _markDashboardFiltersDirty();
  renderDashboardFilterStrip();
  // pass the current render generation so this population is
  // abandoned if the editor re-renders before the fetch resolves.
  if (dv) populateFilterFieldOptions(idx, dv, '', _DASHBOARD_FILTER_RENDER_GEN);
  // Charts under either DV may need a re-query: the old DV no longer
  // has this filter applied, the new DV doesn't have a field selected
  // yet so empty-selection => no clause (Phase 19 behavior).
  if (oldDv && oldDv !== dv) requeryChartsForDataView(oldDv);
}

function onFilterFieldChange(idx, field, fieldType, assetType) {
  const filters = _ensureDashboardFiltersArray();
  if (!filters[idx]) return;
  filters[idx].field = field;
  filters[idx].field_type = fieldType || 'keyword';
  filters[idx].selected_values = [];
  // stamp value_source when the picked field is asset-backed.
  // Server-side registry holds the label_field / value_field — JS just
  // records the kind + asset_type so the popover and strip know to
  // hit the asset-values endpoint instead of the field-values endpoint.
  if (assetType) {
    filters[idx].value_source = { kind: 'asset', asset_type: assetType };
  } else {
    delete filters[idx].value_source;
  }
  _markDashboardFiltersDirty();
  renderDashboardFilterStrip();
}

// ══════════════════════════════════════════════════════════════
// Strip — buttons above the chart grid; opens popover with checkbox
// list of values.
// ══════════════════════════════════════════════════════════════

function renderDashboardFilterStrip() {
  const strip = document.getElementById('dashboard-filter-strip');
  if (!strip) return;
  const filters = _ensureDashboardFiltersArray();
  const readonly = !!window.DASHBOARD_FILTERS_READONLY;

  _clearChildren(strip);

  // Read-only viewer with no filters configured → hide the strip entirely.
  // No editor entry button to show, no hint to show — leaving a stranded
  // "No filters yet." bar in the public viewer would confuse end-users.
  if (readonly && filters.length === 0) {
    strip.style.display = 'none';
    return;
  }

  for (let i = 0; i < filters.length; i++) {
    strip.appendChild(_buildFilterStripButton(i, filters[i]));
  }

  // "No filters yet" hint when empty — keeps the strip from looking like
  // a stranded toolbar in the builder. Suppressed in the read-only viewer
  // by the early return above.
  if (filters.length === 0) {
    const hint = document.createElement('span');
    hint.className = 'db-filter-strip-hint';
    hint.textContent = 'No filters yet.';
    strip.appendChild(hint);
  }

  // Always-present entry button in builder mode. Suppressed in the
  // read-only viewer (DASHBOARD_FILTERS_READONLY) so the public side
  // can't open the editor modal.
  if (!readonly) {
    const entryBtn = document.createElement('button');
    entryBtn.type = 'button';
    entryBtn.className = 'db-filter-strip-add';
    entryBtn.id = 'dashboard-filter-strip-add';
    entryBtn.textContent = '+ Filters';
    entryBtn.title = 'Add or edit dashboard filters';
    entryBtn.addEventListener('click', openDashboardFiltersEditor);
    strip.appendChild(entryBtn);
  }

  strip.style.display = '';
}

/**
 * Open the dedicated filters-editor modal. Used by the strip's
 * `+ Filters` button. The right-side panel is left untouched, so the
 * user can keep editing a chart while configuring dashboard filters.
 */
function openDashboardFiltersEditor() {
  openDashboardFiltersModal();
}

function _buildFilterStripButton(idx, filter) {
  const wrap = document.createElement('div');
  wrap.className = 'db-filter-strip-item';
  wrap.id = `dashboard-filter-pop-wrap-${idx}`;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'db-filter-strip-btn';
  // Button shows only the filter title — selections live in the popover.
  // Avoids stale-summary surface-area when selections change and removes
  // the need to re-render the strip on every checkbox toggle (which used
  // to clear the strip and incidentally close the open popover).
  const titleText = filter.title || '(unnamed)';
  btn.textContent = titleText;
  btn.title = titleText;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (_DASHBOARD_OPEN_FILTER_IDX === idx) {
      closeFilterPopover();
    } else {
      openFilterPopover(idx);
    }
  });
  wrap.appendChild(btn);
  return wrap;
}

async function openFilterPopover(idx) {
  closeFilterPopover();
  const filters = _ensureDashboardFiltersArray();
  if (!filters[idx]) return;
  const wrap = document.getElementById(`dashboard-filter-pop-wrap-${idx}`);
  if (!wrap) return;

  const pop = document.createElement('div');
  pop.className = 'db-filter-popover';
  pop.id = `dashboard-filter-popover-${idx}`;
  pop.addEventListener('click', (e) => e.stopPropagation());

  // Search box
  const searchIn = document.createElement('input');
  searchIn.type = 'text';
  searchIn.placeholder = 'Search…';
  // follow-up: every free-text input carries an explicit length
  // cap (app-wide rule). Client-side filter only — 200 matches
  // the generic label cap.
  searchIn.maxLength = 200;
  searchIn.className = 'db-filter-popover-search';
  pop.appendChild(searchIn);

  // Loading placeholder
  const loading = document.createElement('div');
  loading.className = 'db-filter-popover-loading';
  loading.textContent = 'Loading values…';
  pop.appendChild(loading);

  // List container
  const listEl = document.createElement('div');
  listEl.className = 'db-filter-popover-list';
  listEl.style.display = 'none';
  pop.appendChild(listEl);

  wrap.appendChild(pop);
  _DASHBOARD_OPEN_FILTER_IDX = idx;

  // Click-outside handler
  setTimeout(() => {
    document.addEventListener('click', _outsidePopoverClick, { once: true });
  }, 0);

  // Fetch values
  const filter = filters[idx];
  if (!filter.data_view || !filter.field) {
    loading.textContent = 'Pick a data view and field for this filter first.';
    return;
  }

  const values = await loadFilterValues(filter);
  loading.style.display = 'none';

  // Select All / Clear All bulk controls above the value list. Sit
  // between the search box and the list so they're always visible while
  // scrolling a long list. Inserted before listEl in the DOM order.
  if (values && values.length) {
    const actions = document.createElement('div');
    actions.className = 'db-filter-popover-actions';
    const selectAllBtn = document.createElement('button');
    selectAllBtn.type = 'button';
    selectAllBtn.className = 'db-btn db-btn-sm';
    selectAllBtn.textContent = 'Select All';
    selectAllBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _bulkSetFilterSelection(idx, values.map(v => v.value), listEl);
    });
    const clearAllBtn = document.createElement('button');
    clearAllBtn.type = 'button';
    clearAllBtn.className = 'db-btn db-btn-sm';
    clearAllBtn.textContent = 'Clear All';
    clearAllBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _bulkSetFilterSelection(idx, [], listEl);
    });
    actions.append(selectAllBtn, clearAllBtn);
    pop.insertBefore(actions, listEl);
  }

  listEl.style.display = '';
  _renderFilterValueList(listEl, idx, values, filter);

  searchIn.addEventListener('input', () => {
    const term = searchIn.value.toLowerCase();
    listEl.querySelectorAll('label').forEach(lbl => {
      lbl.style.display = lbl.textContent.toLowerCase().includes(term) ? '' : 'none';
    });
  });
}

// set a filter's entire selection at once (Select All / Clear All),
// re-tick the visible checkboxes, persist, and re-query. Mirrors the per-value
// onFilterValueToggle side-effects but for the whole value set.
function _bulkSetFilterSelection(idx, valueList, listEl) {
  const filters = _ensureDashboardFiltersArray();
  const filter = filters[idx];
  if (!filter) return;
  filter.selected_values = Array.from(new Set(valueList));
  const selected = new Set(filter.selected_values);
  if (listEl) {
    listEl.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.checked = selected.has(cb.value);
    });
  }
  _markDashboardFiltersDirty();
  _persistViewerFilterSelections();
  _requeryChartsForFilter(filter);
}

function _renderFilterValueList(listEl, idx, values, filter) {
  _clearChildren(listEl);
  if (!values || values.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'db-filter-popover-empty';
    empty.textContent = 'No values found.';
    listEl.appendChild(empty);
    return;
  }
  const selected = new Set(filter.selected_values || []);
  // values is always [{label, value}]. Asset-backed filters
  // use distinct label/value (e.g. "Example Agency 1" vs "123456789012345");
  // keyword filters synthesize label === value upstream so the renderer
  // doesn't have to branch.
  values.forEach(pair => {
    const label = pair.label;
    const value = pair.value;
    const lbl = document.createElement('label');
    lbl.className = 'db-filter-popover-item';
    // Hover tooltip shows the full label when the row is clipped by
    // the popover's fixed width (matches the strip-button behaviour).
    lbl.title = label;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    // Checkbox value is the underlying ID — that's what the saved
    // selection and the ES terms clause carry. Display is the label.
    cb.value = value;
    cb.checked = selected.has(value);
    cb.addEventListener('change', () => onFilterValueToggle(idx, value, cb.checked));
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(' ' + label));
    listEl.appendChild(lbl);
  });
}

function closeFilterPopover() {
  if (_DASHBOARD_OPEN_FILTER_IDX === null) return;
  const pop = document.getElementById(`dashboard-filter-popover-${_DASHBOARD_OPEN_FILTER_IDX}`);
  if (pop && pop.parentNode) pop.parentNode.removeChild(pop);
  _DASHBOARD_OPEN_FILTER_IDX = null;
}

function _outsidePopoverClick(_e) {
  closeFilterPopover();
}

/**
 * Read the dashboard's current topbar filter values (team / org) so
 * popover values stay in sync with the same filtering the charts apply.
 * Returns ``{teamFilters: string[], orgFilter: string}``.
 *
 * Builder side reads from #builder-team-filter / #builder-org-filter
 * (single-select dropdowns). Viewer side reads window.DASHBOARD_EXTRA_FILTERS
 * (set by view.html from URL params). Empty values are dropped so the
 * cache key + URL stay tidy.
 */
function _currentTopbarFilters() {
  const teamFilters = [];
  let orgFilter = '';

  // Builder: read the topbar selects directly.
  const teamEl = document.getElementById('builder-team-filter');
  if (teamEl && teamEl.value) teamFilters.push(teamEl.value);
  const orgEl = document.getElementById('builder-org-filter');
  if (orgEl && orgEl.value) orgFilter = orgEl.value;

  // Viewer: window.DASHBOARD_EXTRA_FILTERS is the canonical source on
  // the read-only side (URL-derived team_title / organization_title).
  if (typeof window !== 'undefined' && Array.isArray(window.DASHBOARD_EXTRA_FILTERS)) {
    window.DASHBOARD_EXTRA_FILTERS.forEach(f => {
      if (!f) return;
      if (f.field === 'team_title' && f.value) {
        // value can be a string or an array (match_phrase_any path).
        if (Array.isArray(f.value)) f.value.forEach(v => v && teamFilters.push(v));
        else teamFilters.push(f.value);
      } else if (f.field === 'organization_title' && f.value && !orgFilter) {
        orgFilter = Array.isArray(f.value) ? (f.value[0] || '') : f.value;
      }
    });
  }
  return { teamFilters, orgFilter };
}

/**
 * Fetch value-picker entries for a filter.
 * Always returns [{label, value}].
 *
 * Phase 24 — single-endpoint design:
 * /api/data-views/<dv>/field/<field>/values returns either bare strings
 * (keyword filter) or [{label, value}] (asset-backed). Caller passes
 * asset_type when the filter has value_source.kind === 'asset' and the
 * server enriches each ID with its title from the asset index.
 *
 * Topbar team/org filters are appended as query params so the popover
 * lists only values that exist in the data the charts are showing.
 */
async function loadFilterValues(filter) {
  if (!filter || !filter.data_view || !filter.field) return [];

  const vs = filter.value_source;
  const assetType = (vs && vs.kind === 'asset' && vs.asset_type) ? vs.asset_type : '';
  const { teamFilters, orgFilter } = _currentTopbarFilters();
  // narrow the value list to the viewer's time range (the server
  // applies it on kibana_date only where that field exists). Same source the
  // chart queries read in _withExtraFilters.
  const timeRange = (typeof window !== 'undefined' && window.DASHBOARD_TIME_RANGE) || {};

  // union values across every data view the filter constrains
  // (was: one representative DV), so the list matches a multi-DV table's rows.
  const fetchDvs = await _filterValueDvs(filter);
  if (!fetchDvs.length) return [];

  const cacheKey = [
    filter.data_view,
    fetchDvs.join('|'),
    filter.field,
    assetType,
    [...teamFilters].sort().join(','),
    orgFilter,
    timeRange.from || '',
    timeRange.to || '',
  ].join('::');
  if (DASHBOARD_FILTER_VALUES_CACHE[cacheKey]) {
    return DASHBOARD_FILTER_VALUES_CACHE[cacheKey];
  }

  const params = new URLSearchParams();
  if (assetType) params.append('asset_type', assetType);
  teamFilters.forEach(t => params.append('team_filter', t));
  if (orgFilter) params.append('org_filter', orgFilter);
  if (timeRange.from) params.append('from', timeRange.from);
  if (timeRange.to) params.append('to', timeRange.to);
  const qs = params.toString();

  const perDv = await Promise.all(fetchDvs.map(async dv => {
    try {
      const url = DASHBOARD_BASE + 'api/data-views/' +
                  encodeURIComponent(dv) +
                  '/field/' + encodeURIComponent(filter.field) + '/values' +
                  (qs ? '?' + qs : '');
      const res = await fetch(url);
      const data = await res.json();
      return (data && data.success) ? (data.values || []) : [];
    } catch (e) {
      return [];
    }
  }));

  // Server returns bare strings without asset_type, [{label, value}] with.
  // Normalize to [{label, value}], dedup by value across DVs (first label
  // wins), and re-sort — each DV's list is alphabetical but the union isn't.
  const byValue = new Map();
  perDv.flat().forEach(v => {
    const pair = (v && typeof v === 'object' && 'label' in v && 'value' in v)
      ? v
      : { label: v, value: v };
    if (!byValue.has(pair.value)) byValue.set(pair.value, pair);
  });
  const pairs = Array.from(byValue.values()).sort((a, b) =>
    String(a.label).toLowerCase().localeCompare(String(b.label).toLowerCase()));

  DASHBOARD_FILTER_VALUES_CACHE[cacheKey] = pairs;
  return pairs;
}

function onFilterValueToggle(idx, value, checked) {
  const filters = _ensureDashboardFiltersArray();
  const filter = filters[idx];
  if (!filter) return;
  if (!Array.isArray(filter.selected_values)) filter.selected_values = [];
  const set = new Set(filter.selected_values);
  if (checked) set.add(value); else set.delete(value);
  filter.selected_values = Array.from(set);
  _markDashboardFiltersDirty();
  // persist the viewer's selection per device (no-op in builder).
  _persistViewerFilterSelections();
  // No strip re-render here. The strip button is title-only, so the
  // visible state doesn't depend on selected_values. Re-rendering
  // would clear the strip's children, detaching the open popover.
  // Re-query the charts this filter affects.
  _requeryChartsForFilter(filter);
}

// ══════════════════════════════════════════════════════════════
// Re-query — selection changes apply immediately to every chart on
// the matching DV. Mismatched-DV charts are not touched (decision 6).
//
// a canva chart (the Summary Report) has no top-level data_view (its
// data views live in its renderer), so a per-DV match never targets it; it is
// always re-queried. An "All Data Views" filter re-queries every chart.
// ══════════════════════════════════════════════════════════════

function requeryChartsForDataView(dataView) {
  // canvas pages have no per-chart queryAndRender; they re-resolve
  // every live element in ONE query_batch via this hook (canvas_editor.js /
  // canvas_view.js set it to resolveAllFields). Undefined on classic pages.
  if (typeof window !== 'undefined' && typeof window.onDashboardFilterRequery === 'function') {
    window.onDashboardFilterRequery(dataView);
  }
  if (typeof DASHBOARD_STATE === 'undefined' || !DASHBOARD_STATE.charts) return;
  if (!dataView) return;
  if (typeof queryAndRender !== 'function') return;
  Object.keys(DASHBOARD_STATE.charts).forEach(chartId => {
    const cfg = DASHBOARD_STATE.charts[chartId];
    if (!cfg) return;
    // Canva charts span many DVs internally — always refresh them.
    if (cfg.data_view === dataView || cfg.chart_type === 'canva') {
      queryAndRender(chartId);
    }
  });
}

// Re-query every chart on the dashboard (used by "All Data Views" filters).
function requeryAllCharts() {
  // same canvas hook as requeryChartsForDataView above.
  if (typeof window !== 'undefined' && typeof window.onDashboardFilterRequery === 'function') {
    window.onDashboardFilterRequery(null);
  }
  if (typeof DASHBOARD_STATE === 'undefined' || !DASHBOARD_STATE.charts) return;
  if (typeof queryAndRender !== 'function') return;
  Object.keys(DASHBOARD_STATE.charts).forEach(chartId => queryAndRender(chartId));
}

// Route a filter's selection change to the right re-query: an "All Data Views"
// filter touches everything; a specific-DV filter touches its DV (+ canva).
function _requeryChartsForFilter(filter) {
  if (filter && filter.data_view === DASHBOARD_FILTER_ALL_DV) {
    requeryAllCharts();
  } else if (filter) {
    requeryChartsForDataView(filter.data_view);
  }
}

// ══════════════════════════════════════════════════════════════
// Dirty-flag bridge — wraps _markDirty so the dashboards-view, which
// doesn't define _markDirty, gets a no-op (live-only viewer behavior).
// Same pattern Phase 17 used for column resize / sort menu.
// ══════════════════════════════════════════════════════════════

function _markDashboardFiltersDirty() {
  if (typeof _markDirty === 'function') _markDirty();
}

// ══════════════════════════════════════════════════════════════
// viewer-only per-device persistence of filter SELECTIONS.
//
// The builder saves the filter config (including selected_values) into the
// dashboard doc server-side. The read-only VIEWER lets an end-user toggle
// selections, but those toggles are ephemeral — a reload reverts to the
// author's saved defaults. We persist the viewer's selections to localStorage,
// keyed by dashboard id + filter id, and restore them on load. Builder side is
// untouched (gated on DASHBOARD_FILTERS_READONLY) so the two never double-write.
// ══════════════════════════════════════════════════════════════

const _DASHBOARD_FILTER_SEL_LS_KEY = 'dashboardFilterSel:v1';

function _viewerPersistEnabled() {
  return typeof window !== 'undefined' && !!window.DASHBOARD_FILTERS_READONLY;
}

function _dashboardIdForPersistence() {
  if (typeof DASHBOARD_STATE !== 'undefined' && DASHBOARD_STATE && DASHBOARD_STATE.id) {
    return String(DASHBOARD_STATE.id);
  }
  return '';
}

function _persistViewerFilterSelections() {
  if (!_viewerPersistEnabled()) return;
  const dashId = _dashboardIdForPersistence();
  if (!dashId) return;
  const filters = _ensureDashboardFiltersArray();
  try {
    const all = JSON.parse(localStorage.getItem(_DASHBOARD_FILTER_SEL_LS_KEY) || '{}');
    const map = (all && typeof all === 'object' && !Array.isArray(all)) ? all : {};
    const byFilter = {};
    filters.forEach(f => {
      if (f && f.id) byFilter[f.id] = Array.isArray(f.selected_values) ? f.selected_values : [];
    });
    map[dashId] = byFilter;
    localStorage.setItem(_DASHBOARD_FILTER_SEL_LS_KEY, JSON.stringify(map));
  } catch (e) { /* storage disabled/full — non-fatal */ }
}

// Apply persisted selections onto the current filters (by id). Call after
// DASHBOARD_STATE.filters is hydrated in the viewer, before the first render.
function hydrateViewerFilterSelections() {
  if (!_viewerPersistEnabled()) return;
  const dashId = _dashboardIdForPersistence();
  if (!dashId) return;
  let saved;
  try {
    const all = JSON.parse(localStorage.getItem(_DASHBOARD_FILTER_SEL_LS_KEY) || '{}');
    saved = (all && typeof all === 'object') ? all[dashId] : null;
  } catch (e) {
    saved = null;
  }
  if (!saved || typeof saved !== 'object') return;
  const filters = _ensureDashboardFiltersArray();
  filters.forEach(f => {
    if (f && f.id && Array.isArray(saved[f.id])) {
      f.selected_values = saved[f.id].slice();
    }
  });
}

// ══════════════════════════════════════════════════════════════
// Topbar-change listener — when the user changes
// the dashboard topbar's team/org filter, the popover values list
// must refresh too so it stays in sync with the chart contents.
// Hooks into the existing #builder-team-filter / #builder-org-filter
// selects on the builder side. Idempotent — repeat calls (e.g. from
// renderDashboardFilterStrip) are safe via a one-shot flag.
// ══════════════════════════════════════════════════════════════

let _DASHBOARD_TOPBAR_LISTENERS_INSTALLED = false;

function _installTopbarFilterListeners() {
  if (_DASHBOARD_TOPBAR_LISTENERS_INSTALLED) return;
  const team = document.getElementById('builder-team-filter');
  const org = document.getElementById('builder-org-filter');
  if (!team && !org) return;  // viewer side — selects don't exist
  const onChange = () => {
    // Topbar narrows the popover values list, but strip buttons are
    // title-only so the strip itself doesn't need a re-render. Closing
    // the popover forces a re-fetch when reopened under the new topbar
    // values (cache miss on the new key).
    closeFilterPopover();
  };
  if (team) team.addEventListener('change', onChange);
  if (org) org.addEventListener('change', onChange);
  _DASHBOARD_TOPBAR_LISTENERS_INSTALLED = true;
}

// Kick off listener installation when the DOM is ready. The script
// loads after #db-page is in the document so we can wire immediately
// without a load handler — but guard against being run before the
// selects exist (e.g. in tests or unusual mount orders).
if (typeof document !== 'undefined' && document.readyState !== 'loading') {
  _installTopbarFilterListeners();
} else if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', _installTopbarFilterListeners);
}
