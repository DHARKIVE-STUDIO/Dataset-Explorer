"""Reads stored CSVs into DataFrames, with a small in-process LRU cache.

The cache is per process. Run one worker, or each worker keeps its own copy.
"""

import csv
import io
import threading
from collections import OrderedDict
from pathlib import Path

import pandas as pd

import config
from errors import ApiError
from profiling import correlations, profile_dataframe

_cache: "OrderedDict[str, dict]" = OrderedDict()
_lock = threading.Lock()


def decode_bytes(raw: bytes, preferred: str | None = None) -> tuple[str, str]:
    candidates = ([preferred] if preferred else []) + ["utf-8-sig", "cp1252", "latin-1"]
    for enc in candidates:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    # latin-1 maps every byte, so this line is only reached if the list above changes.
    raise ApiError(422, "unreadable_encoding", "Could not decode the file's text.")


def sniff_delimiter(text: str) -> str:
    try:
        return csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_csv(text: str, delimiter: str) -> pd.DataFrame:
    try:
        return pd.read_csv(io.StringIO(text), sep=delimiter, low_memory=False)
    except pd.errors.EmptyDataError:
        raise ApiError(422, "no_header", "The file is empty or has no header row.")
    except pd.errors.ParserError as exc:
        raise ApiError(
            422, "malformed_csv",
            "Some rows have a different number of fields than the header row.",
            {"detail": str(exc), "hint": "Check for unquoted commas inside values."},
        )
    except ValueError as exc:
        raise ApiError(422, "unparseable", f"The file could not be parsed: {exc}")


def _load_frame(meta: dict) -> pd.DataFrame:
    with _lock:
        if meta["id"] in _cache:
            _cache.move_to_end(meta["id"])
            return _cache[meta["id"]]["df"]

    path = Path(meta["file_path"])
    if not path.exists():
        raise ApiError(410, "file_missing",
                       "The stored file for this dataset is gone. Delete it and upload again.")
    text, _ = decode_bytes(path.read_bytes(), meta["encoding"])
    df = parse_csv(text, meta["delimiter"])

    with _lock:
        _cache[meta["id"]] = {"df": df, "analysis": None}
        while len(_cache) > config.CACHE_SIZE:
            _cache.popitem(last=False)
    return df


def get_analysis(meta: dict) -> tuple[pd.DataFrame, dict]:
    """Return (dataframe, analysis) where analysis has profile, types and correlations."""
    df = _load_frame(meta)
    with _lock:
        entry = _cache.get(meta["id"])
        if entry is not None and entry["analysis"] is not None:
            return df, entry["analysis"]

    profile = profile_dataframe(df)
    types = {c["name"]: c["type"] for c in profile["columns"]}
    analysis = {"profile": profile, "types": types, "correlations": correlations(df, types)}
    with _lock:
        if meta["id"] in _cache:
            _cache[meta["id"]]["analysis"] = analysis
    return df, analysis


def forget(dataset_id: str) -> None:
    with _lock:
        _cache.pop(dataset_id, None)
