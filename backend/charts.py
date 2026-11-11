"""Chart data builders. Each function returns plain lists ready for Chart.js."""

import numpy as np
import pandas as pd

from errors import ApiError
from profiling import to_datetime

KINDS = {"histogram", "bar", "line", "scatter"}
AGGS = {"count", "sum", "mean", "median", "min", "max"}
SCALES = {"auto", "shared", "dual", "indexed"}
MAX_SERIES = 6
MAX_BARS = 30
MAX_LINE_POINTS = 1000
MAX_SCATTER_POINTS = 2000
SHARED_RATIO_LIMIT = 10  # metrics whose sizes differ by more than this get their own axis or indexing


def _require_column(df: pd.DataFrame, name: str | None) -> None:
    if name is None or name not in df.columns:
        raise ApiError(400, "unknown_column", f"There is no column named '{name}'.")


def _need_type(types: dict, col: str, allowed: set, kind: str) -> None:
    if types.get(col) not in allowed:
        raise ApiError(
            400, "wrong_column_type",
            f"A {kind} chart needs a {' or '.join(sorted(allowed))} column. "
            f"'{col}' is {types.get(col)}.",
        )


def _num(v):
    return None if v is None or pd.isna(v) else round(float(v), 6)


def _fmt(v) -> str:
    return f"{float(v):.4g}"


def build_chart(df: pd.DataFrame, types: dict[str, str], kind: str, x: str,
                ys: list[str] | None = None, agg: str = "count", scale: str = "auto",
                bins: int = 20) -> dict:
    ys = [y for y in (ys or []) if y]
    if kind not in KINDS:
        raise ApiError(400, "unknown_chart_kind", f"Chart kind '{kind}' is not supported.",
                       {"supported": sorted(KINDS)})
    if agg not in AGGS:
        raise ApiError(400, "unknown_aggregation", f"Aggregation '{agg}' is not supported.",
                       {"supported": sorted(AGGS)})
    if scale not in SCALES:
        raise ApiError(400, "unknown_scale", f"Scale '{scale}' is not supported.",
                       {"supported": sorted(SCALES)})
    _require_column(df, x)
    for y in ys:
        _require_column(df, y)

    if kind == "histogram":
        return _histogram(df, types, x, bins)
    if kind == "scatter":
        return _scatter(df, types, x, ys)
    return _series(df, types, kind, x, ys, agg, scale)


def _histogram(df, types, x, bins):
    _need_type(types, x, {"numeric"}, "histogram")
    num = pd.to_numeric(df[x], errors="coerce").dropna()
    if num.empty:
        raise ApiError(422, "no_plottable_values", f"'{x}' has no numeric values to plot.")
    counts, edges = np.histogram(num, bins=max(2, min(int(bins), 100)))
    return {
        "kind": "histogram", "x": x, "ys": [], "agg": "count",
        "scale": "shared", "scale_ratio": None, "note": None,
        "labels": [f"{_fmt(edges[i])} to {_fmt(edges[i + 1])}" for i in range(len(counts))],
        "values": [int(c) for c in counts], "truncated": False,
    }


def _scatter(df, types, x, ys):
    if len(ys) != 1:
        raise ApiError(400, "scatter_needs_one_y", "A scatter chart needs exactly one Y column.")
    y = ys[0]
    _need_type(types, x, {"numeric"}, "scatter")
    _need_type(types, y, {"numeric"}, "scatter")
    frame = pd.DataFrame({
        "x": pd.to_numeric(df[x], errors="coerce"),
        "y": pd.to_numeric(df[y], errors="coerce"),
    }).dropna()
    if frame.empty:
        raise ApiError(422, "no_plottable_values",
                       f"'{x}' and '{y}' have no rows where both are numeric.")
    total = len(frame)
    if total > MAX_SCATTER_POINTS:
        frame = frame.sample(MAX_SCATTER_POINTS, random_state=0)
    return {
        "kind": "scatter", "x": x, "ys": [y], "agg": None,
        "scale": "shared", "scale_ratio": None, "note": None,
        "points": [{"x": _num(a), "y": _num(b)} for a, b in zip(frame["x"], frame["y"])],
        "truncated": total > MAX_SCATTER_POINTS,
    }


def _resolve_scale(table: pd.DataFrame, kind: str, requested: str):
    """Return (scale, note, ratio). Ratio is the largest mean size divided by the smallest."""
    names = list(table.columns)
    if requested == "dual" and len(names) != 2:
        raise ApiError(400, "bad_scale", "Two axes need exactly two Y columns.")
    if requested == "indexed" and kind != "line":
        raise ApiError(400, "bad_scale", "Indexed scale only works on line charts.")
    if len(names) < 2:
        return "shared", None, 1.0

    means = [float(table[n].abs().mean()) for n in names]
    positive = [m for m in means if pd.notna(m) and m > 0]
    ratio = round(max(positive) / min(positive), 1) if len(positive) >= 2 else 1.0

    note = None
    if requested == "auto":
        if ratio <= SHARED_RATIO_LIMIT:
            scale = "shared"
        elif len(names) == 2:
            scale = "dual"
            note = "These two metrics differ a lot in size, so each one has its own axis."
        elif kind == "line":
            scale = "indexed"
            note = "These metrics differ a lot in size. Each starts at 100 so you can compare how they change."
        else:
            scale = "shared"
            note = ("These metrics differ a lot in size, so the smaller ones may look flat. "
                    "Pick two metrics to get separate axes.")
    else:
        scale = requested

    if scale == "indexed" and note is None:
        note = "Each series starts at 100, so the chart shows growth, not raw values."
    return scale, note, ratio


def _series(df, types, kind, x, ys, agg, scale):
    if agg == "count":
        ys = []
    elif not ys:
        raise ApiError(400, "missing_column", f"'{agg}' needs at least one Y column.")
    if len(ys) > MAX_SERIES:
        raise ApiError(400, "too_many_series", f"Pick at most {MAX_SERIES} Y columns.")

    if kind == "line":
        if types.get(x) == "datetime":
            key = to_datetime(df[x])
        elif types.get(x) == "numeric":
            key = pd.to_numeric(df[x], errors="coerce")
        else:
            raise ApiError(400, "wrong_column_type",
                           f"A line chart needs a date or numeric x column. '{x}' is {types.get(x)}.")
    else:
        key = df[x].map(lambda v: str(v) if pd.notna(v) else None)

    frame = pd.DataFrame({"__key__": key})
    for y in ys:
        _need_type(types, y, {"numeric"}, kind)
        frame[y] = pd.to_numeric(df[y], errors="coerce")
    frame = frame.dropna(subset=["__key__"])

    if agg == "count":
        table = pd.DataFrame({"count": frame.groupby("__key__").size()})
    else:
        table = pd.DataFrame({
            y: frame.dropna(subset=[y]).groupby("__key__")[y].agg(agg) for y in ys
        })
    if table.empty:
        raise ApiError(422, "no_plottable_values", "No rows have values to plot for these columns.")

    if kind == "bar":
        table = table.sort_values(table.columns[0], ascending=False)
        truncated = len(table) > MAX_BARS
        table = table.head(MAX_BARS)
        labels = [str(k) for k in table.index]
    else:
        table = table.sort_index()
        truncated = len(table) > MAX_LINE_POINTS
        if truncated:
            idx = np.linspace(0, len(table) - 1, MAX_LINE_POINTS).round().astype(int)
            table = table.iloc[idx]
        labels = [k.isoformat() if isinstance(k, pd.Timestamp) else _fmt(k) for k in table.index]

    resolved, note, ratio = _resolve_scale(table, kind, scale)

    series = []
    for i, name in enumerate(table.columns):
        col = table[name]
        if resolved == "indexed":
            non_null = col.dropna()
            base = float(non_null.iloc[0]) if not non_null.empty else 0.0
            col = col / base * 100 if base else col * np.nan
        series.append({
            "name": name,
            "axis": "right" if resolved == "dual" and i == 1 else "left",
            "values": [_num(v) for v in col],
        })

    return {
        "kind": kind, "x": x, "ys": [] if agg == "count" else list(table.columns),
        "agg": agg, "scale": resolved, "scale_ratio": ratio, "note": note,
        "labels": labels, "series": series, "truncated": truncated,
    }


def build_heatmap(df: pd.DataFrame, types: dict[str, str], columns: list[str] | None = None,
                  max_cols: int = 15) -> dict:
    if columns:
        for name in columns:
            _require_column(df, name)
            if types.get(name) != "numeric":
                raise ApiError(400, "wrong_column_type",
                               f"'{name}' is {types.get(name)}. Heatmaps only use numeric columns.")
        cols = list(dict.fromkeys(columns))
        if len(cols) > 30:
            raise ApiError(400, "too_many_columns", "Pick at most 30 columns for a heatmap.")
        truncated = False
    else:
        cols = [n for n, t in types.items() if t == "numeric"]
        truncated = len(cols) > max_cols
        cols = cols[:max_cols]
    if len(cols) < 2:
        raise ApiError(422, "not_enough_numeric_columns",
                       "A correlation heatmap needs at least two numeric columns. Pick at least two.")
    numeric = df[cols].apply(lambda s: pd.to_numeric(s, errors="coerce"))
    corr = numeric.corr(method="pearson", min_periods=10)
    matrix = [[_num(corr.loc[a, b]) for b in cols] for a in cols]
    return {"kind": "heatmap", "columns": cols, "matrix": matrix, "truncated": truncated}
