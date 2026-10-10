/**
 * canvas theme-token resolution (the ONE shared JS point).
 *
 * Loaded by all three canvas surfaces (canvas.html editor, canvas_view.html
 * iframe viewer, public_report/canvas_viewer.html) BEFORE canvas_charts.js /
 * canvas_editor.js / canvas_view.js — like canvas_charts.js, this is a
 * single shared file, NOT a byte-identical duplicated block.
 *
 * Token DATA is never defined here: the templates inject
 * window.CANVAS_THEME_TOKENS from canvas_theme.py (tokens_json), so Python
 * and JS resolve from the same single source. This extends the
 * cvTextTokens rich-text convention to object-level styling.
 *
 * Back-compat contract: a raw value ('#1a2744', 15) passes
 * through unchanged — only a string that exactly matches a token key
 * resolves. If the injection is ever absent, every resolver degrades to raw
 * passthrough, so a page can never crash or restyle on a missing theme.
 *
 * The active theme comes from canvas_config.page.theme: the renderers call
 * cvSetActiveTheme(DOC.page.theme) at the top of each render pass; unknown
 * or missing keys fall back to 'classic' (a doc saved with a theme a later
 * deploy removed must still render).
 */
(function () {
  'use strict';

  var DEFAULT_KEY = 'classic';
  var active = null;

  function themes() {
    return (typeof window !== 'undefined' && window.CANVAS_THEME_TOKENS) || null;
  }

  function themeFor(ref) {
    var all = themes();
    if (!all) return null;
    if (typeof ref === 'string' && all[ref]) return all[ref];
    return all[DEFAULT_KEY] || null;
  }

  window.cvSetActiveTheme = function (ref) {
    active = themeFor(ref);
  };

  window.cvActiveTheme = function () {
    return active || themeFor(DEFAULT_KEY);
  };

  // Color token key → concrete color; anything else raw. The typeof check on
  // the map value skips non-color entries (colors.series is an array).
  window.cvThemeColor = function (v) {
    var t = window.cvActiveTheme();
    if (t && typeof v === 'string' && t.colors && typeof t.colors[v] === 'string') return t.colors[v];
    return v;
  };

  // fonts.scale token key → concrete px number; numbers and unknown strings
  // pass through raw.
  window.cvThemeFontSize = function (v) {
    var t = window.cvActiveTheme();
    if (t && typeof v === 'string' && t.fonts && t.fonts.scale
        && typeof t.fonts.scale[v] === 'number') return t.fonts.scale[v];
    return v;
  };

  // The theme's chart-series palette (canvas_charts.js fallback colors).
  window.cvThemeSeriesColors = function () {
    var t = window.cvActiveTheme();
    var s = t && t.colors && t.colors.series;
    return Array.isArray(s) ? s : [];
  };
})();
