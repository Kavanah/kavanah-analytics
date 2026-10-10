"""
ECharts Transformer
Converts raw Elasticsearch responses + chart configs into ECharts option objects.
"""

from .es_query_builder import _equals_col_agg_key, is_ratio_field, table_metric_agg_keys


def _safe_agg_key(field_name):
    """Convert a field name to a safe ES aggregation key (matches es_query_builder)."""
    return field_name.replace(".", "__").replace("-", "_")


def _extract_scalar_leaves(value):
    """Flatten a top_hits ``_source`` value to its scalar string leaves.

    A ``text`` column on a NESTED field (e.g. ``ig_media.ig_media_caption``)
    can't be projected flatly by ES — ``_source`` includes the whole nested
    parent, so the value arrives as a list of dicts like
    ``[{"ig_media_caption": "🚀 ..."}]``. Naively ``str()``-ing that leaks a
    Python repr into the cell. This walks lists/dicts and returns the scalar
    leaves in document order, so the caller can join the real text instead.

    Scalars (str/int/float/bool) come back as a single-element list.
    """
    leaves = []

    def _walk(v):
        if isinstance(v, dict):
            for item in v.values():
                _walk(item)
        elif isinstance(v, list):
            for item in v:
                _walk(item)
        elif v is not None:
            leaves.append(v)

    _walk(value)
    return leaves


def _get_agg_value(bucket, agg_name="metric_value"):
    """Extract numeric value from a metric aggregation bucket.

    Handles standard metric aggregations (value key), filter aggregations
    (doc_count key for 'equals'), and top_hits aggregations (text aggregation).
    """
    agg = bucket.get(agg_name, {})
    # top_hits aggregation (used by 'text') — return the raw field value as a string
    if "hits" in agg:
        hits = agg["hits"].get("hits", [])
        if hits:
            src = hits[0].get("_source", {})
            # The requested field may be nested (e.g. ig_media.ig_media_caption),
            # in which case _source carries the whole nested parent. Flatten to
            # scalar leaves and join them so the cell shows the real text rather
            # than a "[{'field': ...}]" Python repr. Flat scalar fields collapse
            # to a single leaf — identical to the old str(values[0]) behavior.
            leaves = _extract_scalar_leaves(src)
            return " ".join(str(leaf) for leaf in leaves) if leaves else ""
        return ""
    # Filter aggregations (used by 'equals') return doc_count instead of value
    if "doc_count" in agg:
        return agg.get("doc_count", 0) or 0
    return agg.get("value", 0) or 0


def _format_value(v):
    """Round floats to 2 decimal places for display."""
    if isinstance(v, float):
        return round(v, 2)
    return v


def transform(es_response, config):
    """
    Transform an ES response dict + chart config into an ECharts-compatible option dict.

    Returns different shapes depending on chart_type:
      - bar / line  →  standard ECharts option with xAxis/yAxis/series
      - pie         →  ECharts option with polar series
      - table       →  { chart_type: 'table', columns: [...], rows: [[...], ...] }
      - metric      →  { chart_type: 'metric', label, value, aggregation }

    Accepts y_fields (list) for multi-series bar/line/table.
    Legacy y_field (single string) is normalised to y_fields automatically.
    """
    chart_type = config.get("chart_type", "bar")

    x_field = config.get("x_field", "")
    aggregation = config.get("aggregation", "sum")
    breakdown = config.get("breakdown")

    # Normalise y_fields
    y_fields_raw = config.get("y_fields") or []
    if not y_fields_raw and config.get("y_field"):
        y_fields_raw = [config["y_field"]]
    y_fields = [yf for yf in y_fields_raw if yf]
    y_field = y_fields[0] if y_fields else config.get("y_field", "")

    aggs = es_response.get("aggregations", {})

    # ── Metric ────────────────────────────────────────────
    if chart_type == "metric":
        if breakdown and "breakdown_buckets" in aggs:
            buckets = aggs["breakdown_buckets"].get("buckets", [])
            return {
                "chart_type": "metric",
                "label": breakdown,
                "breakdowns": [{"label": str(b["key"]), "value": b["doc_count"]} for b in buckets if b.get("key")],
                "aggregation": "count",
            }
        agg_key = f"metric_{_safe_agg_key(y_field)}" if y_field else "metric_value"
        # ratio fields wrap the sum/sum/bucket_script triplet
        # in a `filters` (plural) agg because ES requires a multi-bucket
        # parent for bucket_script and rejects both top-level placement
        # and a single-bucket `filter`. The response carries the divided
        # value under aggs["{agg_key}_scope"]["buckets"]["all"][agg_key].
        if y_field and is_ratio_field(y_field):
            scope_bucket = (aggs.get(f"{agg_key}_scope") or {}).get("buckets", {}).get("all") or {}
            value = _get_agg_value(scope_bucket, agg_key)
        else:
            value = _get_agg_value(aggs, agg_key)
        return {
            "chart_type": "metric",
            "label": y_field,
            "value": _format_value(value),
            "aggregation": aggregation,
        }

    # ── Table ─────────────────────────────────────────────
    if chart_type == "table":
        buckets = aggs.get("table_buckets", {}).get("buckets", [])
        x_field_2 = config.get("x_field_2")

        # Support table_columns: derive y_fields and per-column decimal places.
        # Phase 18 — calculation columns synthesize a __calc_<ts>
        # ``field`` so they appear in y_fields / columns; we resolve their
        # values at row-build time via the formula evaluator instead of
        # reading a sub-agg.
        table_columns = config.get("table_columns")
        if table_columns:
            y_fields = [col["field"] for col in table_columns if col.get("field")]
        # Build per-field decimal map
        col_decimals = {}
        if table_columns:
            for col in table_columns:
                if col.get("field"):
                    col_decimals[col["field"]] = col.get("decimals", 2)

        # Pre-parse formulas for any calculation columns so we don't
        # re-parse on every row. parse_formula returns a tree we can
        # feed into evaluate(). Bad formulas (or missing formula_fields)
        # silently produce None values — the renderer shows "—" rather
        # than crashing the response. Parser errors are surfaced in
        # the editor at type time.
        from .dashboard_formula import parse_formula, evaluate as _eval_formula

        calc_trees: dict[str, object] = {}
        if table_columns:
            for col in table_columns:
                if col.get("aggregation") == "calculation":
                    parsed = parse_formula(col.get("formula") or "")
                    if parsed.get("ok"):
                        calc_trees[col["field"]] = parsed["tree"]
        # calc columns the dedup router served from one window
        # (dedup_routing.stitch_table_responses): their operands ride under
        # window_metric_<f>, never mixed with the daily metric_<f> values.
        window_calcs = set(es_response.get("dedup_window_calcs") or [])

        def _round_col(v, field):
            if not isinstance(v, float):
                return v
            d = col_decimals.get(field, 2)
            return round(v, d)

        # When x_field is absent (auto-promoted text column), derive the key from
        # the first bucket key in the composite result.
        effective_x = x_field
        if not effective_x and buckets:
            first_key = buckets[0].get("key", {})
            effective_x = list(first_key.keys())[0] if isinstance(first_key, dict) else None

        # Strip .keyword suffix for display and matching
        effective_x_base = effective_x[:-8] if (effective_x and effective_x.endswith(".keyword")) else effective_x
        label_cols = [effective_x_base] if effective_x_base else []
        if x_field_2:
            label_cols.append(x_field_2)
        # per-column (display_name, source_field, agg_key) specs.
        # Equals columns are keyed by field + equals-value digest (mirroring
        # es_query_builder._equals_col_agg_key) so several equals columns on
        # the SAME field each read their own filter agg and render as their
        # own column; their display name carries the same suffix so the
        # frontend's per-index label/format maps stay collision-free.
        col_specs = []
        if table_columns:
            # per-(field, aggregation) agg keys so two columns on one
            # field but different aggregations don't collapse (both reading the
            # first column's value). display_name stays the field for a single
            # aggregation (unchanged); a disambiguated field carries the agg in
            # its display suffix so the frontend's per-column label/format maps
            # stay collision-free — same shape as the equals suffix.
            dup_agg_keys = table_metric_agg_keys(table_columns)
            for col in table_columns:
                fname = col.get("field")
                if not fname:
                    continue
                if col.get("aggregation") == "equals" and col.get("equals_value") is not None:
                    agg_key = _equals_col_agg_key(fname, col["equals_value"])
                    col_specs.append((agg_key[len("metric_") :], fname, agg_key))
                else:
                    agg_key = dup_agg_keys.get((fname, col.get("aggregation")), f"metric_{_safe_agg_key(fname)}")
                    display = fname if agg_key == f"metric_{_safe_agg_key(fname)}" else agg_key[len("metric_") :]
                    col_specs.append((display, fname, agg_key))
        else:
            col_specs = [(yf, yf, f"metric_{_safe_agg_key(yf)}") for yf in y_fields]
        # Remove auto-promoted text column — it's now the row key
        display_specs = [s for s in col_specs if s[1] != effective_x_base]
        display_y = [s[0] for s in display_specs]
        columns = label_cols + display_y if display_y else label_cols
        # R5 (UI) — a field → output-column-index map so a bucket's
        # per-cell dedup_empty {field: reason} can be projected onto the row's
        # actual column position the renderer paints. Built from display_specs:
        # output column for display field j is len(label_cols)+j.
        n_label_cols = len(label_cols)
        col_index_by_field = {spec[1]: n_label_cols + j for j, spec in enumerate(display_specs)}
        rows = []
        # R5 (UI) — per-row {output-col-index: reason} for explained-empty
        # dedup cells. Parallel to `rows`; the renderer reads it to show the reason
        # copy on a blank dedup cell instead of a bare blank. Empty dict on a row
        # with no empty dedup cell (the common case).
        dedup_empty_cells = []
        for bucket in buckets:
            key = bucket.get("key", {})
            row = []
            if effective_x_base:
                x_val = key.get(effective_x, key.get(effective_x_base, "")) if isinstance(key, dict) else key
                row.append(x_val)
            if x_field_2:
                # the composite source may have been renamed by
                # _terms_field (strips a redundant.keyword; the text
                # auto-suffix adds one), so the raw config name alone can miss
                # and the sub-row column rendered blank. Mirror the x_field
                # lookup above: stored name, then base, then .keyword variant.
                if isinstance(key, dict):
                    x2_base = x_field_2[:-8] if x_field_2.endswith(".keyword") else x_field_2
                    x2_val = key.get(x_field_2, key.get(x2_base, key.get(f"{x2_base}.keyword", "")))
                    # missing_bucket emits a null key for docs that
                    # lack the sub-row field; render it as an explicit bucket
                    # label rather than a blank cell.
                    row.append("(none)" if x2_val is None else x2_val)
                else:
                    row.append("")
            # Pre-extract this bucket's per-field metric values once, so
            # multiple calc columns referencing the same field don't pay
            # repeated _get_agg_value lookups.
            bucket_values: dict[str, float] = {}
            window_values: dict[str, float] = {}
            if calc_trees:
                for col in table_columns or []:
                    if col.get("aggregation") == "calculation":
                        is_window = col.get("field") in window_calcs
                        target = window_values if is_window else bucket_values
                        for fname in col.get("formula_fields") or []:
                            if fname in target:
                                continue
                            if is_window:
                                wkey = f"window_metric_{_safe_agg_key(fname)}"
                                # Absent = this row has no window doc: leave the
                                # operand out so evaluate() returns None ("—").
                                if wkey not in bucket:
                                    continue
                                v = _get_agg_value(bucket, wkey)
                            else:
                                v = _get_agg_value(bucket, f"metric_{_safe_agg_key(fname)}")
                            if isinstance(v, (int, float)):
                                target[fname] = v
            for _disp, yf, agg_key in display_specs:
                if yf in calc_trees:
                    val = _eval_formula(calc_trees[yf], window_values if yf in window_calcs else bucket_values)
                    row.append(val)  # None on div-by-zero / missing field
                    continue
                row.append(_round_col(_get_agg_value(bucket, agg_key), yf))
            rows.append(row)

            # R5 (UI) — project this bucket's per-cell dedup_empty
            # {field: reason} onto the row's output-column indices.
            cell_reasons = bucket.get("dedup_empty")
            row_empty = {}
            if isinstance(cell_reasons, dict):
                for fld, reason in cell_reasons.items():
                    ci = col_index_by_field.get(fld)
                    if ci is not None:
                        row_empty[ci] = reason
            dedup_empty_cells.append(row_empty)

        result = {
            "chart_type": "table",
            "columns": columns,
            "rows": rows,
        }

        # server-computed grand totals, aligned positionally with
        # ``columns`` (None = blank cell). Ratio columns divide whole-scope
        # sums (the ``table_totals`` sibling agg build_query emits when the
        # totals box is on); an agg key ABSENT from the totals bucket means the
        # column has no honest whole-scope total (non-additive window metrics,
        # text columns) and stays None. Calculation columns evaluate their
        # formula over the whole-scope sums — for ratio-shaped formulas that is
        # exactly ratio-of-totals. The renderer prefers this row over its
        # legacy client-side sum, which mis-totaled ratio columns.
        if config.get("include_totals_row") is True:
            totals_bucket = ((aggs.get("table_totals") or {}).get("buckets") or {}).get("all") or {}
            totals_values: dict[str, float] = {}
            if calc_trees and totals_bucket:
                for col in table_columns or []:
                    if col.get("aggregation") == "calculation":
                        for fname in col.get("formula_fields") or []:
                            tkey = f"metric_{_safe_agg_key(fname)}"
                            if fname in totals_values or tkey not in totals_bucket:
                                continue
                            v = _get_agg_value(totals_bucket, tkey)
                            if isinstance(v, (int, float)) and not isinstance(v, bool):
                                totals_values[fname] = v
            totals = [None] * len(label_cols)
            for _disp, yf, agg_key in display_specs:
                if yf in calc_trees:
                    totals.append(_eval_formula(calc_trees[yf], totals_values))
                    continue
                if agg_key not in totals_bucket:
                    totals.append(None)
                    continue
                v = _get_agg_value(totals_bucket, agg_key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    totals.append(_round_col(v, yf))
                else:
                    totals.append(None)
            result["totals"] = totals
        # R5 (UI) — carry the dedup metadata the routed read path
        # (dedup_routing.stitch_table_responses) attached to the ES response so
        # the client can render the snapped-window label + explained-empty cells.
        # Absent on the unrouted/flag-off path → renderTable behaves as today.
        dedup_window = es_response.get("dedup_window")
        if dedup_window:
            result["dedup_window"] = dedup_window
        dedup_empty_col = es_response.get("dedup_empty")
        if dedup_empty_col:
            result["dedup_empty"] = dedup_empty_col
        if any(dedup_empty_cells):
            result["dedup_empty_cells"] = dedup_empty_cells
        return result

    # ── Pie / Donut ───────────────────────────────────────
    # Donut is a pie with a hollow center: an inner+outer radius pair instead
    # of a single radius. Identical data path otherwise.
    if chart_type in ("pie", "donut"):
        buckets = aggs.get("x_buckets", {}).get("buckets", [])
        agg_key = f"metric_{_safe_agg_key(y_field)}" if y_field else "metric_value"
        series_data = [
            {
                "name": str(b.get("key", "")),
                "value": _format_value(_get_agg_value(b, agg_key)),
            }
            for b in buckets
        ]
        radius = ["45%", "70%"] if chart_type == "donut" else "50%"
        return {
            "title": {"text": ""},
            "tooltip": {"trigger": "item"},
            "legend": {"data": [d["name"] for d in series_data]},
            "series": [
                {
                    "name": y_field,
                    "type": "pie",
                    "radius": radius,
                    "data": series_data,
                    "avoidLabelOverlap": True,
                    # show the slice name AND its percentage
                    # ({d} is ECharts' computed percent), matching GA4's
                    # "iOS 69.6% / Android 30.4%" donut labels.
                    "label": {
                        "show": True,
                        "formatter": "{b}: {d}%",
                    },
                    "labelLine": {"smooth": True},
                    "emphasis": {
                        "itemStyle": {
                            "shadowBlur": 10,
                            "shadowOffsetX": 0,
                            "shadowColor": "rgba(0,0,0,0.5)",
                        }
                    },
                }
            ],
        }

    # ── Bar / Line ────────────────────────────────────────
    buckets = aggs.get("x_buckets", {}).get("buckets", [])

    if breakdown:
        # Breakdown creates series by breakdown value — uses primary y_field
        agg_key = f"metric_{_safe_agg_key(y_field)}" if y_field else "metric_value"
        breakdown_keys = []
        for bucket in buckets:
            for bb in bucket.get("breakdown_buckets", {}).get("buckets", []):
                bk = str(bb.get("key", ""))
                if bk not in breakdown_keys:
                    breakdown_keys.append(bk)

        x_data = [str(b.get("key_as_string", b.get("key", ""))) for b in buckets]
        series = []
        for bk in breakdown_keys:
            values = []
            for bucket in buckets:
                match = next(
                    (bb for bb in bucket.get("breakdown_buckets", {}).get("buckets", []) if str(bb.get("key", "")) == bk),
                    None,
                )
                values.append(_format_value(_get_agg_value(match, agg_key)) if match else 0)
            series.append(
                {
                    "name": bk,
                    "type": chart_type,
                    "data": values,
                }
            )
        legend_data = breakdown_keys
    else:
        y_field_labels = config.get("y_field_labels", {})
        x_data = [str(b.get("key_as_string", b.get("key", ""))) for b in buckets]
        series = []
        for yf in y_fields:
            agg_key = f"metric_{_safe_agg_key(yf)}"
            values = [_format_value(_get_agg_value(b, agg_key)) for b in buckets]
            series.append({"name": y_field_labels.get(yf, yf), "type": chart_type, "data": values})
        legend_data = [y_field_labels.get(yf, yf) for yf in y_fields]

    return {
        "title": {"text": ""},
        "tooltip": {"trigger": "axis"},
        "legend": {"data": legend_data},
        "xAxis": {"type": "category", "data": x_data, "axisLabel": {"rotate": 30}},
        "yAxis": {"type": "value"},
        "series": series,
    }
