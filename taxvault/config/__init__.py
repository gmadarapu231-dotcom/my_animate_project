"""Loading the tax parameter files.

Rates live in YAML, not in Python, for one reason: they change every year and
sometimes mid-year. A new tax year is a new file, reviewed against the IRS
revenue procedure, with no code to re-test.
"""

from __future__ import annotations

import functools
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from taxvault.money import Bracket, brackets_from, money, pct

CONFIG_DIR = Path(__file__).resolve().parent
FEDERAL_DIR = CONFIG_DIR / "federal"
STATES_DIR = CONFIG_DIR / "states"


class UnsupportedTaxYear(LookupError):
    """No parameter file exists for that year."""


#: Parameter files in FEDERAL_DIR that are not a tax year.
SHARED_FILE = "shared"


def supported_years() -> list[int]:
    """Every tax year with a parameter file, oldest first.

    `shared.yaml` -- and anything else not named for a year -- is skipped
    rather than crashing the whole package on `int("shared")`.
    """
    years = []
    for path in FEDERAL_DIR.glob("*.yaml"):
        if path.stem == SHARED_FILE:
            continue
        try:
            years.append(int(path.stem))
        except ValueError:  # pragma: no cover - a stray file in the directory
            continue
    return sorted(years)


def latest_year() -> int:
    years = supported_years()
    if not years:  # pragma: no cover - the package ships with parameter files
        raise UnsupportedTaxYear("no federal parameter files are installed")
    return years[-1]


@functools.lru_cache(maxsize=None)
def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """`over` wins, key by key, recursing into nested mappings.

    Only mappings merge. A list in `over` REPLACES the list in `base` rather
    than extending it, because a bracket schedule or a table of exceptions is
    a whole statement about a year -- half a new one and half an old one would
    be a schedule that never existed.
    """
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class FederalParams:
    """One tax year of federal law, with bracket schedules pre-built."""

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.year: int = int(raw["tax_year"])
        self.source: str = raw.get("source", "")
        self.due_date: str = raw.get("due_date", "")
        self.extended_due_date: str = raw.get("extended_due_date", "")
        self.statuses: list[str] = list(raw["filing_statuses"])
        self._brackets = {k: brackets_from(v) for k, v in raw["brackets"].items()}

    def brackets(self, status: str) -> list[Bracket]:
        return self._brackets[self.normalise_status(status)]

    def normalise_status(self, status: str) -> str:
        key = (status or "single").strip().lower()
        aliases = {
            "mfj": "married_jointly", "married": "married_jointly",
            "married_filing_jointly": "married_jointly",
            "mfs": "married_separately",
            "married_filing_separately": "married_separately",
            "hoh": "head_of_household", "qss": "qualifying_surviving_spouse",
            "widow": "qualifying_surviving_spouse",
        }
        key = aliases.get(key, key)
        if key not in self.statuses:
            raise ValueError(f"{status!r} is not a filing status")
        return key

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for step in path:
            if not isinstance(node, dict) or step not in node:
                return default
            node = node[step]
        return node

    def amount(self, *path: str, default: object = 0) -> Decimal:
        return money(self.get(*path, default=default))

    def rate(self, *path: str, default: object = 0) -> Decimal:
        return pct(self.get(*path, default=default))

    def by_status(self, *path: str, status: str, default: object = 0) -> Decimal:
        """A figure that varies by filing status, e.g. a phase-out threshold.

        Where a table lists only `single` and `married_jointly` -- which is how
        the education credits and the OBBBA deductions are written -- separate
        and head-of-household filers use the single figure, which is the rule
        in the statute rather than a guess.
        """
        table = self.get(*path, default={}) or {}
        key = self.normalise_status(status)
        if key in table:
            return money(table[key])
        if key in ("qualifying_surviving_spouse", "married_separately") and "married_jointly" in table:
            fallback = table["married_jointly"]
            return money(fallback) / 2 if key == "married_separately" else money(fallback)
        return money(table.get("single", default))


@functools.lru_cache(maxsize=None)
def federal(year: int | None = None) -> FederalParams:
    target = int(year) if year else latest_year()
    path = FEDERAL_DIR / f"{target}.yaml"
    if not path.exists():
        raise UnsupportedTaxYear(
            f"tax year {target} is not supported; available: {supported_years()}"
        )
    shared = FEDERAL_DIR / f"{SHARED_FILE}.yaml"
    raw = _load(path)
    if shared.exists():
        # The year always wins: `shared.yaml` holds only provisions that are
        # not indexed, so a year overriding one means Congress changed it.
        raw = _merge(_load(shared), raw)
        raw.pop("shared", None)
    return FederalParams(raw)


class StateParams:
    """Every jurisdiction for one tax year, plus the reciprocity table."""

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.year = int(raw["tax_year"])
        self.disclaimer: str = raw.get("disclaimer", "")
        self.states: dict[str, dict[str, Any]] = raw["states"]
        self._reciprocity = raw.get("reciprocity", [])

    def __contains__(self, code: str) -> bool:
        return (code or "").strip().upper() in self.states

    def get(self, code: str) -> dict[str, Any]:
        key = (code or "").strip().upper()
        if key not in self.states:
            raise LookupError(f"{code!r} is not a US state or DC")
        return {"code": key, **self.states[key]}

    def codes(self) -> list[str]:
        return sorted(self.states)

    def no_tax_states(self) -> list[str]:
        return sorted(k for k, v in self.states.items() if v["type"] == "none")

    def taxing_states(self) -> list[str]:
        return sorted(k for k, v in self.states.items() if v["type"] != "none")

    def brackets(self, code: str, status: str) -> list[Bracket]:
        """The schedule for a state and status, applying its `joint` rule."""
        state = self.get(code)
        table = state.get("brackets") or {}
        joint = state.get("joint", "doubled")
        married = status in ("married_jointly", "qualifying_surviving_spouse")

        if married and joint == "table" and "married_jointly" in table:
            return brackets_from(table["married_jointly"])
        single = brackets_from(table["single"])
        if married and joint == "doubled":
            return [
                Bracket(
                    floor=b.floor * 2,
                    ceiling=None if b.ceiling is None else b.ceiling * 2,
                    rate=b.rate,
                )
                for b in single
            ]
        return single

    def reciprocity_for(self, live_in: str, work_in: str) -> bool:
        """True when a resident of `live_in` owes nothing to `work_in`."""
        home, work = (live_in or "").upper(), (work_in or "").upper()
        return any(
            row["work"] == work and home in row.get("live", []) for row in self._reciprocity
        )


@functools.lru_cache(maxsize=None)
def states(year: int | None = None) -> StateParams:
    target = int(year) if year else latest_year()
    path = STATES_DIR / f"{target}.yaml"
    if not path.exists():
        # State law moves slower than the federal indexation; falling back to
        # the newest state file we have beats refusing to produce an estimate,
        # and the response says which year the state data came from.
        available = sorted(int(p.stem) for p in STATES_DIR.glob("*.yaml"))
        if not available:
            raise UnsupportedTaxYear("no state parameter files are installed")
        path = STATES_DIR / f"{available[-1]}.yaml"
    return StateParams(_load(path))


@functools.lru_cache(maxsize=None)
def payments() -> dict[str, Any]:
    """IRS payment, instalment and collection options."""
    return _load(CONFIG_DIR / "payments.yaml")


@functools.lru_cache(maxsize=None)
def fees() -> dict[str, Any]:
    """The practice's own price list. A business decision, not tax law."""
    return _load(CONFIG_DIR / "fees.yaml")


@functools.lru_cache(maxsize=None)
def resources() -> dict[str, Any]:
    """Official IRS and state links, grouped for the reference screen."""
    return _load(CONFIG_DIR / "resources.yaml")
