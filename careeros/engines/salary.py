"""Multi-country salary parsing and normalisation.

The rule everywhere in the product: **normalise internally, display the
original**. `18 LPA CTC` and `an 18 LPA base` are different offers, so the raw
string and the detected components are preserved alongside the normalised
annual figure -- the UI shows both.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from careeros.config import CountryPack, countries
from careeros.engines.textutil import normalize

# Number plus an optional magnitude suffix: "140k", "1.2m", "25,00,000".
_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([km])?\b")
_SUFFIX_SCALE = {"k": 1_000.0, "m": 1_000_000.0}

# Tokens that contain digits but are never compensation. Stripped before the
# number scan so "$65/hr W2" does not parse as a 2-65 range.
_NOISE_TOKENS = re.compile(
    r"\b(?:w-?2|c2c|c2h|corp2corp|401\s?\(?k\)?|24/7|1099|s-?corp|h-?1b|eeo|w-?9)\b"
)
_CURRENCY_SYMBOLS = {
    "$": "USD", "us$": "USD", "usd": "USD",
    "₹": "INR", "rs.": "INR", "rs": "INR", "inr": "INR",
    "£": "GBP", "gbp": "GBP",
    "€": "EUR", "eur": "EUR",
    "c$": "CAD", "cad": "CAD",
    "a$": "AUD", "aud": "AUD",
    "s$": "SGD", "sgd": "SGD",
}
_COMPONENT_WORDS = {
    "ctc": ["ctc", "cost to company"],
    "base": ["base", "fixed", "basic"],
    "variable": ["variable", "performance pay"],
    "bonus": ["bonus", "annual bonus", "signing bonus"],
    "esop": ["esop", "equity", "stock", "rsu"],
    "retention": ["retention"],
}


@dataclass
class SalaryParse:
    """Both views of a compensation string."""

    original: str | None = None
    currency: str | None = None
    period: str | None = None            # the period as written (year/month/hour)
    format_id: str | None = None         # e.g. 'lpa', 'hourly'
    amount_min: float | None = None      # as written, in the source unit
    amount_max: float | None = None
    annual_min: float | None = None      # normalised to an annual figure
    annual_max: float | None = None
    components: list[str] = field(default_factory=list)
    confidence: float = 0.0
    note: str | None = None

    @property
    def annual_mid(self) -> float | None:
        vals = [v for v in (self.annual_min, self.annual_max) if v is not None]
        return sum(vals) / len(vals) if vals else None

    def display(self, symbol: str = "") -> str:
        """Original first -- that is what the employer actually wrote."""
        if self.original:
            base = self.original.strip()
        elif self.annual_min:
            base = f"{symbol}{self.annual_min:,.0f}"
        else:
            return "Not disclosed"
        if self.annual_min and self.format_id not in {"annual", None}:
            lo = f"{symbol}{self.annual_min:,.0f}"
            hi = f"{symbol}{self.annual_max:,.0f}" if self.annual_max else None
            norm = f"{lo}-{hi}" if hi and hi != lo else lo
            return f"{base}  (~{norm}/yr)"
        return base

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["annual_mid"] = self.annual_mid
        return d


class SalaryNormalizer:
    """Parses compensation text using a country pack's `salary.formats` table."""

    def __init__(self, pack: CountryPack) -> None:
        self.pack = pack
        cfg = pack.salary or {}
        self.formats: list[dict[str, Any]] = cfg.get("formats", [])
        self.hours_per_year: float = float(cfg.get("hours_per_year", 2080))
        self.months_per_year: float = float(cfg.get("months_per_year", 12))

    # -- helpers -----------------------------------------------------------
    def _detect_currency(self, text: str) -> str | None:
        for token, code in sorted(_CURRENCY_SYMBOLS.items(), key=lambda kv: -len(kv[0])):
            if token in text:
                return code
        return None

    def _detect_format(self, text: str) -> dict[str, Any] | None:
        best: tuple[int, dict[str, Any]] | None = None
        for fmt in self.formats:
            for pattern in fmt.get("patterns", []):
                p = normalize(pattern)
                if p and p in text:
                    cand = (len(p), fmt)
                    if best is None or cand[0] > best[0]:
                        best = cand
        return best[1] if best else None

    def _detect_components(self, text: str) -> list[str]:
        allowed = set(self.pack.salary.get("components", list(_COMPONENT_WORDS)))
        out = []
        for comp, words in _COMPONENT_WORDS.items():
            if comp in allowed and any(w in text for w in words):
                out.append(comp)
        return out

    def _to_annual(self, value: float, period: str) -> float:
        if period == "hour":
            return value * self.hours_per_year
        if period == "month":
            return value * self.months_per_year
        return value

    # -- public ------------------------------------------------------------
    def parse(self, raw: str | None) -> SalaryParse:
        if not raw or not str(raw).strip():
            return SalaryParse(note="not disclosed")

        original = str(raw).strip()
        text = normalize(original)
        result = SalaryParse(original=original)

        result.currency = self._detect_currency(text) or self.pack.currency
        result.components = self._detect_components(text)

        fmt = self._detect_format(text)
        multiplier = float(fmt.get("multiplier", 1)) if fmt else 1.0
        period = fmt.get("period") if fmt else self.pack.raw.get("locale", {}).get(
            "default_salary_period", "year"
        )
        result.format_id = fmt.get("id") if fmt else None
        result.period = period

        scan = _NOISE_TOKENS.sub(" ", text)
        numbers: list[float] = []
        for m in _NUMBER.finditer(scan):
            value = float(m.group(1).replace(",", ""))
            if m.group(2):
                value *= _SUFFIX_SCALE[m.group(2)]
                result.note = result.note or "magnitude suffix applied"
            if value > 0:
                numbers.append(value)
        if not numbers:
            result.note = "no numeric amount found"
            return result

        # A bare number with no format hint: infer the unit from magnitude so
        # "12 LPA" and "1200000" both land sensibly.
        had_suffix = bool(re.search(r"\d\s*[km]\b", _NOISE_TOKENS.sub(" ", text)))
        if fmt is None and self.pack.code == "IN" and max(numbers) < 200 and not had_suffix:
            multiplier, period, result.format_id = 100000.0, "year", "lpa"
            result.period = "year"
            result.note = "unit inferred from magnitude (assumed LPA)"
        elif fmt is None and max(numbers) <= 400 and self.pack.code == "US" and not had_suffix:
            multiplier, period, result.format_id = 1.0, "hour", "hourly"
            result.period = "hour"
            result.note = "unit inferred from magnitude (assumed hourly)"

        scaled = sorted(n * multiplier for n in numbers)
        # Keep only figures within one order of magnitude of the largest: a
        # stray "3" from "3 days onsite" must not become the bottom of a range.
        top = scaled[-1]
        scaled = [n for n in scaled if n >= top * 0.2] or [top]

        result.amount_min = min(numbers)
        result.amount_max = max(numbers) if len(set(numbers)) > 1 else None
        result.annual_min = self._to_annual(scaled[0], period)
        result.annual_max = self._to_annual(scaled[-1], period) if len(scaled) > 1 else None

        result.confidence = 0.9 if fmt else (0.6 if result.note else 0.45)
        return result


def parse_salary(raw: str | None, country: str = "US") -> SalaryParse:
    """Convenience wrapper used by the ingest pipeline."""
    pack = countries().get(country) or countries().require("US")
    return SalaryNormalizer(pack).parse(raw)
