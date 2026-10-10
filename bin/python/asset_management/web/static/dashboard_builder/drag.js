/**
 * drag.js — Drag-and-drop logic for the Dashboard Builder
 *
 * Handles:
 *   1. Dragging chart-type tiles onto the canvas to create new slots
 *   2. SortableJS: reorder chart slots on the canvas
 *
 * Field selection is handled entirely via the right panel dropdowns.
 */

let _sortableInstance = null;

// ══════════════════════════════════════════════════════════════
// Chart type tiles → canvas (create new slot)
// ══════════════════════════════════════════════════════════════

function initChartTypeDrag() {
  // Chart types now use a dropdown + button, so tile drag is no longer needed.
  // Keep this function as a no-op so existing callers don't break.
}

// ══════════════════════════════════════════════════════════════
// SortableJS — reorder chart slots on the grid
// ══════════════════════════════════════════════════════════════

function initSortable() {
  const grid = document.getElementById('chart-grid');
  if (!grid || typeof Sortable === 'undefined') return;

  if (_sortableInstance) {
    _sortableInstance.destroy();
  }

  _sortableInstance = Sortable.create(grid, {
    animation: 150,
    // Always-visible per-slot grip (added by _addDragHandle). Was
    // .chart-slot-header, but that's hidden when hide_title is set
    // (the default), so title-hidden charts couldn't be dragged.
    handle: '.chart-drag-handle',
    ghostClass: 'sortable-ghost',
    onEnd: () => {
      // Rebuild DASHBOARD_STATE.charts in the new visual order so Save persists it
      const orderedIds = Array.from(grid.querySelectorAll('.chart-slot'))
        .map(slot => slot.id.replace('slot-', ''));
      const snapshot = { ...DASHBOARD_STATE.charts };
      Object.keys(DASHBOARD_STATE.charts).forEach(k => delete DASHBOARD_STATE.charts[k]);
      orderedIds.forEach(id => { if (snapshot[id]) DASHBOARD_STATE.charts[id] = snapshot[id]; });
      if (typeof _markDirty === 'function') _markDirty();
    },
  });
}

// ══════════════════════════════════════════════════════════════
// Bootstrap on load
// ══════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  initChartTypeDrag();
});
