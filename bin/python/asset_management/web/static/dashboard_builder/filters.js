/**
 * filters.js — Data filter panel logic for the Dashboard Builder
 * Each chart has its own filter list stored in DASHBOARD_STATE.charts[chartId].filters
 */

// First Look templates bind a chart to the consumer's asset via a
// per-chart filter whose value is a placeholder (__FIRST_LOOK_ASSET_PLACEHOLDER__),
// swapped for the real asset id at install time. The editor shows these as
// ordinary editable rows (no special lock — keep the filter,
// drop the lock; whether the filter belongs in templates at all is its own
// follow-up). The _isPlaceholderFilter detector + FIRST_LOOK_PLACEHOLDER
// constant live in charts.js (loads first; the only script the viewer loads) so
// renderGroupedTable can drop a still-unresolved placeholder from its per-cell
// queries — a raw placeholder would match nothing useful in a live query.

// ══════════════════════════════════════════════════════════════
// Operator definitions by field type
// ══════════════════════════════════════════════════════════════

const OPERATORS_BY_TYPE = {
  numeric: [
    { value: 'eq',      label: 'is' },
    { value: 'neq',     label: 'is not' },
    { value: 'gt',      label: '>' },
    { value: 'gte',     label: '>=' },
    { value: 'lt',      label: '<' },
    { value: 'lte',     label: '<=' },
    { value: 'between', label: 'between' },
  ],
  keyword: [
    { value: 'eq',  label: 'is' },
    { value: 'neq', label: 'is not' },
    { value: 'in',  label: 'is one of' },
  ],
  date: [
    { value: 'eq',      label: 'is' },
    { value: 'gt',      label: 'after' },
    { value: 'lt',      label: 'before' },
    { value: 'between', label: 'between' },
  ],
};

function getOperatorsForType(fieldType) {
  return OPERATORS_BY_TYPE[fieldType] || OPERATORS_BY_TYPE.keyword;
}

function getFieldType(fieldName, fields) {
  // fields are the chart's data-view fields ([{name,type}]),
  // resolved by renderFilterRows via _loadTableFields. (The old global
  // CURRENT_FIELDS was never assigned anywhere — referencing it threw, which
  // is part of why the orphaned editor never worked even before the container
  // went missing.) Default keyword when the field/type is unknown.
  const list = fields || _filterFieldsByChart[SELECTED_CHART_ID] || [];
  const field = list.find(f => f.name === fieldName);
  return field ? field.type : 'keyword';
}

// ══════════════════════════════════════════════════════════════
// breakdown-record opt-in (Google Ads / YouTube)
// ══════════════════════════════════════════════════════════════

// Sources whose importers write breakdown docs alongside the primary totals.
// Mirrors _BREAKDOWN_RECORD_TYPES in es_query_builder.py — the server excludes
// these record_types by default; this checkbox lets the user opt back in.
const BREAKDOWN_RECORD_SOURCES = ['google_ads', 'youtube_analytics'];

// JS mirror of source_from_data_view() in es_query_builder.py: strip a leading
// "*_" / "*" wildcard, then take the segment after the last "__".
function _sourceFromDataView(dataView) {
  let dv = dataView || '';
  if (dv.startsWith('*_')) dv = dv.slice(2);
  else if (dv.startsWith('*')) dv = dv.slice(1);
  const parts = dv.split('__');
  return parts[parts.length - 1];
}

function _isBreakdownRecordSource(dataView) {
  return BREAKDOWN_RECORD_SOURCES.includes(_sourceFromDataView(dataView));
}

// Show/hide + hydrate the "Include breakdown records" checkbox for the chart.
// Only visible on guarded sources; reflects config.include_breakdown_records.
function _syncBreakdownRecordsToggle(config) {
  const group = document.getElementById('breakdown-records-group');
  const box = document.getElementById('include-breakdown-records');
  if (!group || !box) return;
  const dataView = config.data_view
    || (config.data_sources && config.data_sources[0] && config.data_sources[0].data_view)
    || '';
  if (_isBreakdownRecordSource(dataView)) {
    group.style.display = '';
    box.checked = !!config.include_breakdown_records;
  } else {
    group.style.display = 'none';
  }
}

function onIncludeBreakdownRecordsChange(checked) {
  const chartId = SELECTED_CHART_ID;
  if (!chartId) return;
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  config.include_breakdown_records = !!checked;
  applyFilters();  // re-query so the chart reflects the change immediately
}

// ══════════════════════════════════════════════════════════════
// Render filter rows for a chart
// ══════════════════════════════════════════════════════════════

// Per-chart cache of the resolved data-view fields, so the row change handlers
// (which run after render) can look up a field's type without re-fetching.
const _filterFieldsByChart = {};

function renderFilterRows(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  const container = document.getElementById('filters-list');
  if (!container) return;

  // surface the breakdown-records opt-in for guarded sources.
  _syncBreakdownRecordsToggle(config);

  const filters = config.filters || [];

  // Resolve the chart's data-view fields so the field/operator selectors are
  // populated. _loadTableFields is cached, so repeated selects are cheap.
  const dataView = config.data_view || (config.data_sources && config.data_sources[0] && config.data_sources[0].data_view) || '';
  const loader = (typeof _loadTableFields === 'function' && dataView)
    ? _loadTableFields(dataView)
    : Promise.resolve([]);

  loader.then(fields => {
    _filterFieldsByChart[chartId] = fields || [];
    // Re-check the container: the user may have selected another chart while
    // the fetch was in flight.
    const c = document.getElementById('filters-list');
    if (!c || SELECTED_CHART_ID !== chartId) return;
    c.innerHTML = '';
    filters.forEach((filter, idx) => {
      c.appendChild(buildFilterRow(chartId, idx, filter, fields || []));
    });
  });
}

function buildFilterRow(chartId, idx, filter, fields) {
  const row = document.createElement('div');
  row.className = 'db-filter-row';
  row.id = `filter-row-${chartId}-${idx}`;

  // Field selector
  const fieldList = fields || [];
  const fieldSel = document.createElement('select');
  fieldSel.innerHTML = '<option value="">— field —</option>';
  fieldList.forEach(f => {
    const opt = document.createElement('option');
    opt.value = f.name;
    opt.textContent = f.name;
    if (f.name === filter.field) opt.selected = true;
    fieldSel.appendChild(opt);
  });
  // Preserve a saved field that isn't in the current data view's field list
  // (e.g. data view changed) so the row still shows what's stored.
  if (filter.field && !fieldList.some(f => f.name === filter.field)) {
    const opt = document.createElement('option');
    opt.value = filter.field;
    opt.textContent = `${filter.field} (saved)`;
    opt.selected = true;
    fieldSel.appendChild(opt);
  }

  const fieldType = filter.field ? getFieldType(filter.field, fieldList) : 'keyword';
  const operators = getOperatorsForType(fieldType);

  // Operator selector
  const opSel = document.createElement('select');
  operators.forEach(op => {
    const opt = document.createElement('option');
    opt.value = op.value;
    opt.textContent = op.label;
    if (op.value === filter.op) opt.selected = true;
    opSel.appendChild(opt);
  });

  // Value inputs
  const valueContainer = document.createElement('span');
  valueContainer.style.flex = '1';
  renderValueInputs(valueContainer, filter.op || 'eq', filter.value, fieldType);

  // Remove button
  const rmBtn = document.createElement('button');
  rmBtn.className = 'btn-rm-filter';
  rmBtn.textContent = '✕';
  rmBtn.title = 'Remove filter';
  rmBtn.addEventListener('click', () => removeFilterRow(chartId, idx));

  // Re-render operator options when field changes
  fieldSel.addEventListener('change', () => {
    const newType = getFieldType(fieldSel.value, fieldList);
    const newOps = getOperatorsForType(newType);
    opSel.innerHTML = '';
    newOps.forEach(op => {
      const opt = document.createElement('option');
      opt.value = op.value;
      opt.textContent = op.label;
      opSel.appendChild(opt);
    });
    renderValueInputs(valueContainer, newOps[0].value, null, newType);
    updateFilterState(chartId, idx, fieldSel, opSel, valueContainer);
    // No applyFilters() here — the second fieldSel 'change' listener below
    // requeries; calling it in both would fire two queries per field change.
  });

  // Re-render value inputs when operator changes
  opSel.addEventListener('change', () => {
    const fType = getFieldType(fieldSel.value, fieldList);
    renderValueInputs(valueContainer, opSel.value, null, fType);
    updateFilterState(chartId, idx, fieldSel, opSel, valueContainer);
    applyFilters();
  });

  // Update state on value input — requery live so the chart reflects the
  // filter as the user types, matching the rest of the panel.
  valueContainer.addEventListener('input', () => {
    updateFilterState(chartId, idx, fieldSel, opSel, valueContainer);
    applyFilters();
  });
  fieldSel.addEventListener('change', () => {
    updateFilterState(chartId, idx, fieldSel, opSel, valueContainer);
    applyFilters();
  });

  row.append(fieldSel, opSel, valueContainer, rmBtn);
  return row;
}

function renderValueInputs(container, op, currentValue, fieldType) {
  container.innerHTML = '';
  const inputType = fieldType === 'numeric' ? 'number' : 'text';

  if (op === 'between') {
    const lo = document.createElement('input');
    lo.type = inputType;
    lo.placeholder = 'from';
    lo.style.flex = '1';
    const hi = document.createElement('input');
    hi.type = inputType;
    hi.placeholder = 'to';
    hi.style.flex = '1';
    if (Array.isArray(currentValue)) {
      lo.value = currentValue[0] ?? '';
      hi.value = currentValue[1] ?? '';
    }
    container.append(lo, hi);
  } else if (op === 'in') {
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.placeholder = 'val1, val2, …';
    inp.style.flex = '1';
    if (Array.isArray(currentValue)) inp.value = currentValue.join(', ');
    else if (currentValue) inp.value = currentValue;
    container.appendChild(inp);
  } else {
    const inp = document.createElement('input');
    inp.type = inputType;
    inp.placeholder = 'value';
    inp.style.flex = '1';
    if (currentValue !== null && currentValue !== undefined) inp.value = currentValue;
    container.appendChild(inp);
  }
}

function collectFilterValue(valueContainer, op, fieldType) {
  const inputs = valueContainer.querySelectorAll('input');
  if (op === 'between') {
    const lo = fieldType === 'numeric' ? parseFloat(inputs[0].value) : inputs[0].value;
    const hi = fieldType === 'numeric' ? parseFloat(inputs[1].value) : inputs[1].value;
    return [lo, hi];
  } else if (op === 'in') {
    return inputs[0].value.split(',').map(v => v.trim()).filter(Boolean);
  } else {
    const raw = inputs[0] ? inputs[0].value : '';
    return fieldType === 'numeric' ? parseFloat(raw) : raw;
  }
}

function updateFilterState(chartId, idx, fieldSel, opSel, valueContainer) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  if (!config.filters[idx]) config.filters[idx] = {};
  const fieldType = getFieldType(fieldSel.value, _filterFieldsByChart[chartId]);
  config.filters[idx].field = fieldSel.value;
  config.filters[idx].op = opSel.value;
  config.filters[idx].value = collectFilterValue(valueContainer, opSel.value, fieldType);
}

// ══════════════════════════════════════════════════════════════
// Add / remove filter rows
// ══════════════════════════════════════════════════════════════

function addFilterRow() {
  const chartId = SELECTED_CHART_ID;
  if (!chartId) return;
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  config.filters = config.filters || [];
  config.filters.push({ field: '', op: 'eq', value: '' });
  renderFilterRows(chartId);
}

function removeFilterRow(chartId, idx) {
  const config = DASHBOARD_STATE.charts[chartId];
  if (!config) return;
  config.filters.splice(idx, 1);
  renderFilterRows(chartId);
  applyFilters();  // reflect the removal in the chart immediately
}

// ══════════════════════════════════════════════════════════════
// Apply filters — re-query the selected chart
// ══════════════════════════════════════════════════════════════

function applyFilters() {
  const chartId = SELECTED_CHART_ID;
  if (!chartId) return;
  if (typeof queryAndRender === 'function') queryAndRender(chartId);
}

function collectFilters(chartId) {
  const config = DASHBOARD_STATE.charts[chartId];
  return (config && config.filters) ? config.filters.filter(f => f.field && f.op) : [];
}
