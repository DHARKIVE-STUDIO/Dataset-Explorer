"""Column typing, data quality checks, correlations and summary statistics."""

from __future__ import annotations

import warnings

import pandas as pd

SAMPLE_SIZE = 5000
NUMERIC_MIN_SHARE = 0.95
MIXED_MIN_SHARE = 0.60
DATE_MIN_SHARE = 0.90
CATEGORY_MAX_UNIQUE = 50
BOOL_TOKENS = {"true", "false", "yes", "no"}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def to_datetime(values):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.to_datetime(values, errors="coerce")


def _r(x):
    if x is None or pd.isna(x):
        return None
    return round(float(x), 6)


def detect_type(series: pd.Series) -> tuple[str, float]:
    """Return (column type, share of non-null values that parse as numbers)."""
    if pd.api.types.is_bool_dtype(series):
        return "boolean", 1.0
    if pd.api.types.is_numeric_dtype(series):
        return "numeric", 1.0
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime", 1.0

    non_null = series.dropna()
    if non_null.empty:
        return "empty", 0.0

    sample = non_null.head(SAMPLE_SIZE).astype(str).str.strip()
    lowered = sample.str.lower()
    if lowered.nunique() <= 2 and lowered.isin(BOOL_TOKENS).all():
        return "boolean", 1.0

    numeric_share = float(pd.to_numeric(sample, errors="coerce").notna().mean())
    if numeric_share >= NUMERIC_MIN_SHARE:
        return "numeric", numeric_share
    if to_datetime(sample).notna().mean() >= DATE_MIN_SHARE:
        return "datetime", numeric_share

    distinct = sample.nunique()
    if distinct <= min(CATEGORY_MAX_UNIQUE, max(2, len(sample) // 5)):
        return "categorical", numeric_share
    return "text", numeric_share


def _iqr_outliers(num: pd.Series) -> int:
    if len(num) < 8:
        return 0
    q1, q3 = num.quantile([0.25, 0.75])
    iqr = q3 - q1
    if iqr == 0:
        return 0
    return int(((num < q1 - 1.5 * iqr) | (num > q3 + 1.5 * iqr)).sum())


def _column_issues(name: str, info: dict, numeric_share: float, n: int) -> list[dict]:
    out: list[dict] = []

    def add(severity, code, message):
        out.append({"column": name, "severity": severity, "code": code, "message": message})

    if name.startswith("Unnamed:"):
        add("medium", "unnamed_column", "This column has no header name.")
    if info["missing"] == n:
        add("high", "empty_column", "Every value in this column is missing.")
    elif info["missing_pct"] >= 50:
        add("high", "mostly_missing", f"{info['missing_pct']}% of values are missing.")
    elif info["missing_pct"] >= 20:
        add("medium", "many_missing", f"{info['missing_pct']}% of values are missing.")
    elif info["missing"] > 0:
        add("low", "some_missing", f"{info['missing']} values are missing.")
    if info["unique"] == 1 and info["missing"] < n:
        add("low", "constant_column", "Only one distinct value. Probably not useful for analysis.")
    if info["type"] in ("text", "categorical") and MIXED_MIN_SHARE <= numeric_share < NUMERIC_MIN_SHARE:
        add("medium", "mixed_types",
            f"About {round(numeric_share * 100)}% of values are numbers and the rest are not. "
            "Check for stray text or units.")
    if info.get("whitespace_padded"):
        add("low", "whitespace_padding",
            f"{info['whitespace_padded']} values have leading or trailing spaces.")
    if info.get("outliers", 0) > 0.01 * n:
        add("low", "outliers", f"{info['outliers']} values sit outside 1.5x the IQR.")
    return out


def profile_dataframe(df: pd.DataFrame) -> dict:
    n = len(df)
    columns: list[dict] = []
    issues: list[dict] = []

    for col in df.columns:
        name = str(col)
        series = df[col]
        ctype, numeric_share = detect_type(series)
        missing = int(series.isna().sum())
        info = {
            "name": name,
            "type": ctype,
            "missing": missing,
            "missing_pct": round(100 * missing / n, 2) if n else 0.0,
            "unique": int(series.nunique(dropna=True)),
        }
        if ctype == "numeric":
            num = pd.to_numeric(series, errors="coerce").dropna()
            if not num.empty:
                info.update(min=_r(num.min()), max=_r(num.max()),
                            mean=_r(num.mean()), median=_r(num.median()))
            info["outliers"] = _iqr_outliers(num)
        elif ctype == "datetime":
            dt = to_datetime(series).dropna()
            if not dt.empty:
                info.update(min=dt.min().isoformat(), max=dt.max().isoformat())
        if ctype in ("text", "categorical"):
            text = series.dropna().astype(str)
            info["whitespace_padded"] = int((text != text.str.strip()).sum())

        columns.append(info)
        issues.extend(_column_issues(name, info, numeric_share, n))

    dup = int(df.duplicated().sum())
    if dup:
        issues.append({
            "column": None, "severity": "medium", "code": "duplicate_rows",
            "message": f"{dup} rows are exact duplicates of an earlier row.",
        })
    issues.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])

    return {
        "row_count": n,
        "column_count": len(columns),
        "duplicate_rows": dup,
        "columns": columns,
        "issues": issues,
    }


def correlations(df: pd.DataFrame, types: dict[str, str], max_cols: int = 15) -> list[dict]:
    cols = [c for c, t in types.items() if t == "numeric"][:max_cols]
    if len(cols) < 2:
        return []
    numeric = df[cols].apply(lambda s: pd.to_numeric(s, errors="coerce"))
    corr = numeric.corr(method="pearson", min_periods=10)
    pairs = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            r = corr.loc[a, b]
            if pd.notna(r):
                pairs.append({"x": a, "y": b, "r": round(float(r), 3)})
    pairs.sort(key=lambda p: -abs(p["r"]))
    return pairs[:20]


def column_stats(df: pd.DataFrame, name: str, ctype: str) -> dict:
    series = df[name]
    missing = int(series.isna().sum())

    if ctype == "numeric":
        num = pd.to_numeric(series, errors="coerce").dropna()
        if num.empty:
            return {"count": 0, "missing": missing}
        q1, med, q3 = num.quantile([0.25, 0.5, 0.75])
        return {
            "count": int(num.size), "missing": missing,
            "mean": _r(num.mean()),
            "std": _r(num.std()) if num.size > 1 else 0.0,
            "min": _r(num.min()), "p25": _r(q1), "median": _r(med), "p75": _r(q3),
            "max": _r(num.max()),
            "skew": _r(num.skew()) if num.size > 2 else None,
        }

    if ctype == "datetime":
        dt = to_datetime(series).dropna()
        if dt.empty:
            return {"count": 0, "missing": missing}
        return {
            "count": int(dt.size), "missing": missing,
            "earliest": dt.min().isoformat(), "latest": dt.max().isoformat(),
            "span_days": int((dt.max() - dt.min()).days),
        }

    values = series.dropna().astype(str)
    top = values.value_counts().head(10)
    return {
        "count": int(values.size), "missing": missing,
        "unique": int(series.nunique()),
        "top": [{"value": str(k), "count": int(v)} for k, v in top.items()],
    }
