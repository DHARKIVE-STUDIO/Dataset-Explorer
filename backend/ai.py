"""AI-written insights, with a deterministic rule-based fallback.

What gets sent to the model: column names, types, missing/unique counts, numeric
min/max/mean/median, outlier counts, quality issue codes and correlation values.
Raw rows and categorical values are NOT sent.

Every result carries generated_by ("ai" or "rules") so the UI can label it.
"""

import json
import re
from datetime import datetime, timezone

import httpx

import config
from charts import AGGS, KINDS
from storage import get_insight, save_insight

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MAX_PROMPT_COLUMNS = 60
MAX_PROMPT_ISSUES = 30


class AIUnavailable(Exception):
    """The AI path could not produce a usable result. The message is shown to the user."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rule_based(profile: dict, corrs: list[dict]) -> dict:
    cols = profile["columns"]
    issues = profile["issues"]
    numeric = [c["name"] for c in cols if c["type"] == "numeric"]
    dates = [c["name"] for c in cols if c["type"] == "datetime"]
    cats = [c for c in cols if c["type"] in ("categorical", "boolean")]

    patterns = []
    for issue in issues:
        if issue["severity"] in ("high", "medium"):
            prefix = f"{issue['column']}: " if issue["column"] else ""
            patterns.append(prefix + issue["message"])
    for p in corrs[:5]:
        if abs(p["r"]) >= 0.7:
            direction = "positively" if p["r"] > 0 else "negatively"
            patterns.append(f"{p['x']} and {p['y']} are strongly {direction} correlated (r = {p['r']}).")
    for c in cols:
        if c["type"] == "numeric" and c.get("outliers", 0) > 0.05 * profile["row_count"]:
            patterns.append(f"{c['name']} has many outliers ({c['outliers']} values outside 1.5x the IQR).")

    charts: list[dict] = []

    def add(kind, x, y, agg, reason):
        key = (kind, x, y, agg)
        if all((c["kind"], c["x"], c["y"], c["agg"]) != key for c in charts):
            charts.append({"kind": kind, "x": x, "y": y, "agg": agg, "reason": reason})

    if dates and numeric:
        add("line", dates[0], numeric[0], "mean", "Shows how the first numeric column changes over time.")
    for p in corrs[:2]:
        if abs(p["r"]) >= 0.5:
            add("scatter", p["x"], p["y"], "count", f"Correlation r = {p['r']}.")
    for c in cats:
        if 2 <= c["unique"] <= 15:
            add("bar", c["name"], None, "count", f"{c['unique']} categories, counted.")
    for n in numeric[:2]:
        add("histogram", n, None, "count", "Distribution of values.")

    summary = (
        f"{profile['row_count']:,} rows and {profile['column_count']} columns: "
        f"{len(numeric)} numeric, {len(cats)} categorical, {len(dates)} date. "
        f"{sum(i['severity'] == 'high' for i in issues)} high and "
        f"{sum(i['severity'] == 'medium' for i in issues)} medium severity quality issues."
    )
    return {
        "generated_by": "rules",
        "model": None,
        "summary": summary,
        "patterns": patterns[:8] or ["No notable patterns from the rule checks."],
        "suggested_charts": charts[:4],
        "fallback_reason": None,
        "created_at": _now(),
    }


PROMPT = """You are reviewing a statistical profile of a CSV dataset. You do not see the rows.

Return ONLY a JSON object with this shape:
{"summary": "2-3 plain sentences", "patterns": ["up to 5 short observations"], "suggested_charts": [{"kind": "histogram|bar|line|scatter", "x": "column name", "y": "column name or null", "agg": "count|sum|mean|median|min|max", "reason": "short reason"}]}

Rules:
- Only use column names that appear in the profile, spelled exactly.
- "line" needs a date or numeric x and a numeric y. "scatter" needs numeric x and y. "histogram" needs numeric x.
- Only claim what the profile supports. Say so when something is uncertain.
- At most 4 suggested charts. No prose outside the JSON.

Profile:
{profile}
"""


def _prompt_profile(profile: dict, corrs: list[dict]) -> str:
    keep = ("name", "type", "missing_pct", "unique", "min", "max", "mean", "median", "outliers")
    columns = [{k: c[k] for k in keep if k in c} for c in profile["columns"][:MAX_PROMPT_COLUMNS]]
    issues = [{"column": i["column"], "code": i["code"], "severity": i["severity"]}
              for i in profile["issues"][:MAX_PROMPT_ISSUES]]
    payload = {
        "row_count": profile["row_count"],
        "column_count": profile["column_count"],
        "columns": columns,
        "quality_issues": issues,
        "strongest_correlations": corrs[:10],
    }
    return PROMPT.replace("{profile}", json.dumps(payload, separators=(",", ":")))


def _call_model(prompt: str) -> str:
    if not config.ANTHROPIC_API_KEY:
        raise AIUnavailable("ANTHROPIC_API_KEY is not set on the server.")
    try:
        resp = httpx.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.AI_MODEL,
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=config.AI_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise AIUnavailable(f"Could not reach the AI service ({exc.__class__.__name__}).")
    if resp.status_code != 200:
        raise AIUnavailable(f"The AI service returned HTTP {resp.status_code}.")
    body = resp.json()
    return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise AIUnavailable("The AI response did not contain JSON.")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise AIUnavailable("The AI response was not valid JSON.")
    if not isinstance(data, dict):
        raise AIUnavailable("The AI response had an unexpected shape.")
    return data


def _validate(data: dict, profile: dict) -> dict:
    """Drop anything the model invented: unknown columns, unsupported kinds, type mismatches."""
    types = {c["name"]: c["type"] for c in profile["columns"]}
    summary = str(data.get("summary", "")).strip()[:800]
    if not summary:
        raise AIUnavailable("The AI response had no summary.")
    patterns = [str(p)[:300] for p in data.get("patterns", []) if isinstance(p, str)][:6]

    charts = []
    for s in data.get("suggested_charts") or []:
        if not isinstance(s, dict):
            continue
        kind, x, y = s.get("kind"), s.get("x"), s.get("y")
        agg = s.get("agg") or "count"
        if kind not in KINDS or agg not in AGGS or x not in types:
            continue
        if y is not None and y not in types:
            continue
        ok = {
            "histogram": types[x] == "numeric",
            "bar": agg == "count" or (y is not None and types.get(y) == "numeric"),
            "line": y is not None and types.get(y) == "numeric" and types[x] in ("datetime", "numeric"),
            "scatter": y is not None and types.get(y) == "numeric" and types[x] == "numeric",
        }[kind]
        if not ok:
            continue
        charts.append({"kind": kind, "x": x, "y": y if kind != "histogram" else None,
                       "agg": agg, "reason": str(s.get("reason", ""))[:200]})

    return {
        "generated_by": "ai",
        "model": config.AI_MODEL,
        "summary": summary,
        "patterns": patterns or ["The model listed no specific patterns."],
        "suggested_charts": charts[:4],
        "fallback_reason": None,
        "created_at": _now(),
    }


def ai_insights(profile: dict, corrs: list[dict]) -> dict:
    text = _call_model(_prompt_profile(profile, corrs))
    return _validate(_parse_json(text), profile)


def get_insights(meta: dict, profile: dict, corrs: list[dict], refresh: bool = False) -> dict:
    """AI results are cached in SQLite. Fallbacks are not cached, so a later request can retry AI."""
    if not refresh:
        cached = get_insight(meta["id"])
        if cached:
            return cached
    try:
        result = ai_insights(profile, corrs)
    except AIUnavailable as exc:
        base = rule_based(profile, corrs)
        base["fallback_reason"] = str(exc)
        return base
    save_insight(meta["id"], result)
    return result
