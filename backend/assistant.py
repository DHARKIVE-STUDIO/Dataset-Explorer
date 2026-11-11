"""Chart assistant: turns a plain-English question into a chart spec.

AI path: Claude gets the dataset profile and the question, and returns an answer plus a chart spec.
Rule path: keyword matching on the question and the column types. Used when AI is unavailable.

Every chart spec, from either path, is checked against the real columns before it is returned.
"""

import json
import re
from datetime import datetime, timezone

import config
from ai import AIUnavailable, _call_model, _parse_json
from charts import AGGS, KINDS, MAX_SERIES

MAX_PROFILE_COLUMNS = 60
MAX_PROFILE_ISSUES = 30

ASSISTANT_PROMPT = """You help someone choose a chart for a CSV dataset. You see a statistical profile of the file, not the rows.

Dataset profile:
{profile}

The user asks:
{question}

Return ONLY a JSON object:
{"answer": "2-4 plain sentences: which chart to use, why, and anything to watch out for", "chart": {"kind": "bar|line|histogram|scatter|heatmap", "x": "column name", "ys": ["column name"], "agg": "count|sum|mean|median|min|max"}}

Use "chart": null only if no chart fits.

Rules:
- Use column names exactly as they appear in the profile.
- line: x is a datetime or numeric column, ys are numeric columns.
- bar: x is any category column. ys are numeric columns, unless agg is count, then ys is [].
- histogram: x is numeric, ys is [].
- scatter: x is numeric, ys has exactly one numeric column.
- heatmap: correlations between every pair of numeric columns. Set x to null and ys to [].
- Use several ys only when the user wants metrics compared. At most 6.
- Answer the question that was asked, not the most common chart.
"""

TREND_WORDS = ("over time", "trend", "monthly", "weekly", "daily", "per month", "per day",
               "by month", "timeline", "growth", "over the")
COMPARE_WORDS = ("compare", "comparison", " by ", " per ", "each", "across", "breakdown",
                 "split", "which", " vs", "versus")
DIST_WORDS = ("distribut", "spread", "histogram", "frequency", "typical", "range of")
HEATMAP_WORDS = ("heatmap", "heat map", "correlation", "correlate", "matrix")
RELATE_WORDS = ("relationship", "correlat", "against", "related", "depend", " vs", "versus",
                "affect", "impact")
AGG_WORDS = (("average", "mean"), ("mean", "mean"), ("median", "median"),
             ("maximum", "max"), ("highest", "max"), ("max", "max"),
             ("minimum", "min"), ("lowest", "min"), ("min", "min"),
             ("total", "sum"), ("sum", "sum"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _profile_json(profile: dict, corrs: list[dict]) -> str:
    keep = ("name", "type", "missing_pct", "unique", "min", "max", "mean", "median", "outliers")
    payload = {
        "row_count": profile["row_count"],
        "column_count": profile["column_count"],
        "columns": [{k: c[k] for k in keep if k in c} for c in profile["columns"][:MAX_PROFILE_COLUMNS]],
        "quality_issues": [{"column": i["column"], "code": i["code"], "severity": i["severity"]}
                           for i in profile["issues"][:MAX_PROFILE_ISSUES]],
        "strongest_correlations": corrs[:10],
    }
    return json.dumps(payload, separators=(",", ":"))


def validate_spec(spec, types: dict[str, str]) -> dict | None:
    """Return a clean chart spec, or None if it doesn't match the real columns and types."""
    if not isinstance(spec, dict):
        return None
    if spec.get("kind") == "heatmap":
        if sum(1 for t in types.values() if t == "numeric") < 2:
            return None
        return {"kind": "heatmap", "x": None, "ys": [], "agg": "count", "scale": "auto"}
    kind, x, agg = spec.get("kind"), spec.get("x"), spec.get("agg") or "count"
    ys = spec.get("ys") or []
    if isinstance(ys, str):
        ys = [ys]
    if not isinstance(ys, list) or len(ys) > MAX_SERIES:
        return None
    if kind not in KINDS or agg not in AGGS or x not in types:
        return None
    if any(not isinstance(y, str) or y not in types for y in ys):
        return None

    numeric_ys = all(types[y] == "numeric" for y in ys)
    xt = types[x]
    if kind == "histogram":
        ok, ys = xt == "numeric", []
    elif kind == "scatter":
        ok = xt == "numeric" and len(ys) == 1 and numeric_ys
    elif kind == "line":
        ok = xt in ("datetime", "numeric") and (agg == "count" or (bool(ys) and numeric_ys))
    else:  # bar
        ok = agg == "count" or (bool(ys) and numeric_ys)
    if not ok:
        return None
    if kind in ("bar", "line") and agg == "count":
        ys = []
    return {"kind": kind, "x": x, "ys": ys, "agg": agg, "scale": "auto"}


def _ask_model(profile: dict, corrs: list[dict], question: str):
    prompt = (ASSISTANT_PROMPT
              .replace("{profile}", _profile_json(profile, corrs))
              .replace("{question}", question))
    data = _parse_json(_call_model(prompt))
    answer = str(data.get("answer", "")).strip()[:700]
    if not answer:
        raise AIUnavailable("The AI response had no answer.")
    types = {c["name"]: c["type"] for c in profile["columns"]}
    return answer, validate_spec(data.get("chart"), types)


def _has(q: str, phrases) -> bool:
    return any(p in q for p in phrases)


def _mention_pos(q: str, name: str):
    best = None
    for variant in {name.lower(), name.lower().replace("_", " ")}:
        m = re.search(r"(?<![a-z0-9])" + re.escape(variant) + r"(?![a-z0-9])", q)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best


def _first(types: dict, wanted: str):
    return next((n for n, t in types.items() if t == wanted), None)


def rule_assistant(question: str, profile: dict) -> dict:
    types = {c["name"]: c["type"] for c in profile["columns"]}
    q = question.lower()

    found = []
    for name in types:
        pos = _mention_pos(q, name)
        if pos is not None:
            found.append((pos, name))
    mentioned = [n for _, n in sorted(found)]
    numeric = [n for n in mentioned if types[n] == "numeric"]
    dates = [n for n in mentioned if types[n] == "datetime"]
    cats = [n for n in mentioned if types[n] in ("categorical", "boolean", "text")]
    agg = next((a for w, a in AGG_WORDS if re.search(r"\b" + w + r"\b", q)), None)

    spec, reason = None, None
    numeric_cols = [n for n, t in types.items() if t == "numeric"]
    if _has(q, HEATMAP_WORDS) and len(numeric_cols) >= 2:
        spec = {"kind": "heatmap", "x": None, "ys": [], "agg": "count"}
        reason = "A correlation heatmap shows how strongly each pair of numeric columns moves together."
    elif _has(q, RELATE_WORDS) and len(numeric) >= 2:
        spec = {"kind": "scatter", "x": numeric[0], "ys": [numeric[1]], "agg": "count"}
        reason = (f"You asked how {numeric[0]} and {numeric[1]} relate, "
                  "so a scatter plot shows each row as one point.")
    elif _has(q, DIST_WORDS) and (numeric or _first(types, "numeric")):
        x = numeric[0] if numeric else _first(types, "numeric")
        spec = {"kind": "histogram", "x": x, "ys": [], "agg": "count"}
        reason = f"You asked about the spread of {x}, so a histogram shows how often each range occurs."
    elif dates or _has(q, TREND_WORDS):
        x = dates[0] if dates else _first(types, "datetime")
        ys = numeric or [n for n in [_first(types, "numeric")] if n]
        if x and ys:
            spec = {"kind": "line", "x": x, "ys": ys, "agg": agg or "sum"}
            reason = f"Time is involved, so a line chart of {', '.join(ys)} over {x} shows the change."

    if spec is None:
        x = cats[0] if cats else (_first(types, "categorical") or _first(types, "boolean"))
        if x and (cats or _has(q, COMPARE_WORDS)):
            ys = numeric
            spec = {"kind": "bar", "x": x, "ys": ys, "agg": (agg or "sum") if ys else "count"}
            measure = f"{spec['agg']} of {', '.join(ys)}" if ys else "row counts"
            reason = f"You want to compare groups, so a bar chart of {measure} by {x} fits."

    if spec is None:
        if _first(types, "categorical"):
            x = _first(types, "categorical")
            spec = {"kind": "bar", "x": x, "ys": [], "agg": "count"}
            reason = f"The question doesn't name a chart type, so here are row counts by {x}."
        elif _first(types, "numeric"):
            x = _first(types, "numeric")
            spec = {"kind": "histogram", "x": x, "ys": [], "agg": "count"}
            reason = f"The question doesn't name a chart type, so here is the distribution of {x}."

    spec = validate_spec(spec, types)
    if spec is None:
        answer = ("I couldn't find a chart that fits these columns. "
                  "Name the columns you care about and I'll try again.")
    else:
        answer = reason
    return {"generated_by": "rules", "model": None, "answer": answer, "chart": spec,
            "fallback_reason": None, "created_at": _now()}


def ask(profile: dict, corrs: list[dict], question: str) -> dict:
    """Answer one question. Assistant answers are not cached, because they depend on the question."""
    try:
        answer, chart = _ask_model(profile, corrs, question)
    except AIUnavailable as exc:
        result = rule_assistant(question, profile)
        result["fallback_reason"] = str(exc)
        return result
    return {"generated_by": "ai", "model": config.AI_MODEL, "answer": answer, "chart": chart,
            "fallback_reason": None, "created_at": _now()}
