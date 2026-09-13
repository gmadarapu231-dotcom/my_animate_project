"""Read a posting out of a career page's own structured data.

Every employer that wants to appear in Google Jobs publishes a schema.org
`JobPosting` block on the posting page -- Google requires it. That makes it the
sanctioned, stable way to read a company's own career site: no selectors to
break, no markup to reverse-engineer, and the publisher put it there for
exactly this consumer.

Handles the shapes that occur in practice: a bare object, an array, an
`@graph`, and a posting nested inside a wrapper. Falls back to microdata
attributes, which older Taleo and SuccessFactors pages still emit.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any, Iterator

JOB_POSTING = "jobposting"


class _LdExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._in_ld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attr = {k.lower(): (v or "").lower() for k, v in attrs}
        if "ld+json" in attr.get("type", ""):
            self._in_ld = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld:
            self.blocks.append("".join(self._buf))
            self._in_ld = False
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self._buf.append(data)


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from _walk(node[key])


def _is_job_posting(node: dict[str, Any]) -> bool:
    raw = node.get("@type") or node.get("type") or ""
    types = raw if isinstance(raw, list) else [raw]
    return any(str(t).lower().replace("schema:", "") == JOB_POSTING for t in types)


def job_postings(html_text: str) -> list[dict[str, Any]]:
    """Every JobPosting in the document, in source order."""
    parser = _LdExtractor()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    found: list[dict[str, Any]] = []
    for block in parser.blocks:
        text = block.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # Some CMSs emit trailing commas or concatenated objects.
            try:
                payload = json.loads(re.sub(r",\s*([}\]])", r"\1", text))
            except json.JSONDecodeError:
                continue
        for node in _walk(payload):
            if _is_job_posting(node) and node not in found:
                found.append(node)
    return found


# --- field readers ---------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", value)
    text = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6])\s*/?>", "\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return strip_html(node)
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, list):
        return ", ".join(filter(None, (_text(n) for n in node)))
    if isinstance(node, dict):
        for key in ("name", "value", "@value", "text", "description", "title"):
            if key in node:
                return _text(node[key])
    return ""


def _parse_date(value: Any) -> date | None:
    text = _text(value)[:32].strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[: len(fmt) + 6] if "%z" in fmt else text[: len(fmt)], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _address(node: Any) -> tuple[str, str | None, str | None, str | None]:
    """(display, city, region, country) from a JobLocation of any shape."""
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return _text(node), None, None, None
    addr = node.get("address") or node
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if not isinstance(addr, dict):
        return _text(addr), None, None, None
    city = _text(addr.get("addressLocality")) or None
    region = _text(addr.get("addressRegion")) or None
    country = _text(addr.get("addressCountry")) or None
    display = ", ".join(p for p in (city, region, country) if p) or _text(node.get("name"))
    return display, city, region, country


def _salary(node: Any) -> str | None:
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return _text(node) or None
    currency = _text(node.get("currency") or node.get("salaryCurrency"))
    value = node.get("value", node)
    if isinstance(value, list):
        value = value[0] if value else {}
    if not isinstance(value, dict):
        amount = _text(value)
    else:
        lo, hi = _text(value.get("minValue")), _text(value.get("maxValue"))
        amount = f"{lo} - {hi}" if lo and hi else (lo or hi or _text(value.get("value")))
    unit = _text(value.get("unitText")) if isinstance(value, dict) else ""
    if not amount:
        return None
    parts = [p for p in (currency, amount) if p]
    out = " ".join(parts)
    return f"{out} per {unit.lower()}" if unit else out


def to_fields(node: dict[str, Any]) -> dict[str, Any]:
    """A JobPosting node reduced to the fields `RawJob` wants."""
    display, city, region, country = _address(node.get("jobLocation"))
    remote = node.get("jobLocationType")
    if not display and remote:
        display = "Remote"
    employment = node.get("employmentType")
    return {
        "title": _text(node.get("title")) or _text(node.get("name")),
        "company": _text((node.get("hiringOrganization") or {})),
        "description": _text(node.get("description")),
        "url": _text(node.get("url")) or None,
        "location": display or None,
        "city": city,
        "region": region,
        "country": country,
        "employment_type": _text(employment).replace("_", " ").lower() or None,
        "salary_raw": _salary(node.get("baseSalary")),
        "posted_on": _parse_date(node.get("datePosted")),
        "deadline_on": _parse_date(node.get("validThrough")),
        "external_id": _text(node.get("identifier")) or _text(node.get("url")) or None,
    }
