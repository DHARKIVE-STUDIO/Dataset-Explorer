"""Row filtering, sorting and page serialization. Column names are checked against the
DataFrame and never evaluated as code."""

import json
import math

import numpy as np
import pandas as pd

from errors import ApiError
from profiling import to_datetime

OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "contains", "is_null", "not_null"}
RANGE_OPS = {"gt", "gte", "lt", "lte"}
NULL_OPS = {"is_null", "not_null"}
MAX_FILTERS = 20


def parse_filters(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(400, "invalid_filters", "filters must be a JSON array.")
    if not isinstance(data, list) or len(data) > MAX_FILTERS:
        raise ApiError(400, "invalid_filters",
                       f"filters must be an array with at most {MAX_FILTERS} items.")
    if not all(isinstance(f, dict) for f in data):
        raise ApiError(400, "invalid_filters", "Each filter must be an object.")
    return data


def _as_float(value, col):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_value",
                       f"'{value}' is not a number, but '{col}' is a numeric column.")


def _as_timestamp(value, col):
    try:
        ts = pd.Timestamp(str(value))
    except (ValueError, TypeError):
        ts = pd.NaT
    if pd.isna(ts):
        raise ApiError(400, "bad_value", f"'{value}' is not a date, but '{col}' is a date column.")
    return ts


def _compare(left, op, right):
    if op == "eq":
        return left == right
    if op == "neq":
        return (left != right) & left.notna()
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    return left <= right


def _clause(series: pd.Series, ctype: str | None, op: str, value, col: str) -> pd.Series:
    if op == "is_null":
        return series.isna()
    if op == "not_null":
        return series.notna()
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise ApiError(400, "missing_value", f"The '{op}' filter on '{col}' needs a value.")

    if op == "contains":
        return series.astype(str).str.contains(str(value), case=False, regex=False, na=False)

    if ctype == "numeric":
        return _compare(pd.to_numeric(series, errors="coerce"), op, _as_float(value, col))

    if ctype == "datetime":
        return _compare(to_datetime(series), op, _as_timestamp(value, col))

    if op in RANGE_OPS:
        raise ApiError(400, "unsupported_operator",
                       f"'{op}' only works on numeric or date columns. '{col}' is {ctype}.")

    left = series.map(lambda v: str(v).strip().lower() if pd.notna(v) else None)
    return _compare(left, op, str(value).strip().lower())


def apply_filters(df: pd.DataFrame, types: dict[str, str], filters: list[dict]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for f in filters:
        col, op, value = f.get("column"), f.get("op"), f.get("value")
        if col not in df.columns:
            raise ApiError(400, "unknown_column", f"There is no column named '{col}'.")
        if op not in OPS:
            raise ApiError(400, "unknown_operator", f"Operator '{op}' is not supported.",
                           {"supported": sorted(OPS)})
        mask &= _clause(df[col], types.get(str(col)), op, value, col)
    return df[mask]


def sort_frame(df: pd.DataFrame, types: dict[str, str], column: str, direction: str):
    if column not in df.columns:
        raise ApiError(400, "unknown_column", f"There is no column named '{column}'.")
    ctype = types.get(column)
    if ctype == "numeric":
        key = lambda s: pd.to_numeric(s, errors="coerce")
    elif ctype == "datetime":
        key = lambda s: to_datetime(s)
    else:
        key = lambda s: s.map(lambda v: str(v).lower() if pd.notna(v) else None)
    return df.sort_values(by=column, ascending=direction != "desc", key=key, kind="mergesort")


def _json_value(v):
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return float(v) if math.isfinite(float(v)) else None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return str(v)


def to_rows(page: pd.DataFrame) -> list[list]:
    cleaned = page.astype(object).where(page.notna(), None)
    return [[_json_value(v) for v in row] for row in cleaned.itertuples(index=False, name=None)]
