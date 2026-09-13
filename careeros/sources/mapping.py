"""The declarative field mapper.

Providers describe where each `RawJob` field lives in their payload, so a new
board is a YAML entry rather than a parser. A mapping value is one of:

    "company.display_name"                     dotted path; digits index lists
    {path: created, transform: date}           read, then convert
    {first: [share_link, apply_options.0.link]}  first path that has a value
    {template: "{min} - {max} {cur}", require: [min]}  compose a string
    "{token}"                                  a run-time variable, not a path

Transforms are deliberately few: the pipeline already owns salary parsing,
country resolution and employment-type detection, so a connector's only job is
to hand over the text it was given.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from careeros.sources.jsonld import strip_html

_VAR = re.compile(r"^\{([a-z_]+)\}$")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def resolve_path(payload: Any, path: str) -> Any:
    """`a.b.0.c` walks dicts and lists alike. Missing anything yields None."""
    if path in ("", "{root}"):
        return payload
    node: Any = payload
    for part in path.split("."):
        if node is None:
            return None
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, (list, tuple)):
            if not part.isdigit():
                return None
            index = int(part)
            node = node[index] if -len(node) <= index < len(node) else None
        else:
            return None
    return node


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        parts = [as_text(v) for v in value]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    if isinstance(value, dict):
        for key in ("name", "label", "value", "display_name", "text", "title"):
            if key in value:
                return as_text(value[key])
    return None


# --- transforms ------------------------------------------------------------
_REL = re.compile(
    r"(?i)\b(\d+)\+?\s*(minute|min|hour|hr|day|week|month|year)s?\s*ago\b|\b(today|just posted|yesterday)\b"
)
_UNIT_DAYS = {"minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = as_text(value)
    if not text:
        return None
    text = text.strip()
    if text.isdigit() and len(text) in (10, 13):
        return _from_epoch(text)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text[:24].strip(), fmt).date()
        except ValueError:
            continue
    return _relative_date(text)


def _from_epoch(value: Any) -> date | None:
    text = as_text(value)
    if not text or not text.strip().lstrip("-").isdigit():
        return None
    number = int(text.strip())
    if abs(number) > 10**11:  # milliseconds
        number //= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def _relative_date(value: Any, today: date | None = None) -> date | None:
    """"3 days ago", "just posted" -- what Google Jobs actually returns."""
    text = as_text(value)
    if not text:
        return None
    now = today or date.today()
    match = _REL.search(text)
    if not match:
        return None
    if match.group(3):
        word = match.group(3).lower()
        return now - timedelta(days=1) if word == "yesterday" else now
    count, unit = int(match.group(1)), match.group(2).lower()
    return now - timedelta(days=count * _UNIT_DAYS.get(unit, 1))


TRANSFORMS = {
    "date": _to_date,
    "epoch": _from_epoch,
    "relative_date": _relative_date,
    "html": lambda v: strip_html(as_text(v) or "") or None,
    "text": lambda v: as_text(v),
    "int": lambda v: int(as_text(v)) if (as_text(v) or "").lstrip("-").isdigit() else None,
    "lower": lambda v: (as_text(v) or "").lower() or None,
}


def apply_rule(payload: Any, rule: Any, variables: dict[str, Any] | None = None) -> Any:
    """Evaluate one mapping rule against one item of a provider's payload."""
    vars_ = variables or {}

    if rule is None:
        return None

    if isinstance(rule, str):
        var = _VAR.match(rule)
        if var:
            return vars_.get(var.group(1))
        return as_text(resolve_path(payload, rule))

    if not isinstance(rule, dict):
        return rule

    if "first" in rule:
        for path in rule["first"]:
            value = apply_rule(payload, path, vars_)
            if value not in (None, ""):
                return value
        return None

    if "template" in rule:
        required = rule.get("require") or []
        for path in required:
            if as_text(resolve_path(payload, path)) in (None, ""):
                return None
        def fill(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in vars_:
                return str(vars_[key])
            return as_text(resolve_path(payload, key)) or ""
        text = _PLACEHOLDER.sub(fill, rule["template"])
        text = re.sub(r"\s{2,}", " ", text).strip(" ,-")
        return text or None

    path = rule.get("path", "")
    raw = resolve_path(payload, path) if path else payload
    name = rule.get("transform")
    if name:
        fn = TRANSFORMS.get(name)
        if fn is None:
            raise ValueError(f"unknown transform {name!r}")
        return fn(raw)
    return as_text(raw)


def map_item(payload: Any, mapping: dict[str, Any], variables: dict[str, Any] | None = None) -> dict[str, Any]:
    return {field: apply_rule(payload, rule, variables) for field, rule in mapping.items()}
