"""Configuration registry: country packs and taxonomy, loaded from YAML.

The registry is the seam that makes the system domain- and country-agnostic.
Adding a country = dropping a YAML file into `config/countries/`.
Adding a career domain = adding an entry to `config/taxonomy/domains.yaml`
(or letting the classifier infer one at runtime).
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).parent
COUNTRIES_DIR = CONFIG_DIR / "countries"
TAXONOMY_DIR = CONFIG_DIR / "taxonomy"


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Country packs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuthStatus:
    id: str
    label: str
    needs_sponsorship_now: bool
    needs_sponsorship_future: bool
    satisfies: tuple[str, ...]
    aliases: tuple[str, ...]
    transfer_required: bool = False


@dataclass
class CountryPack:
    code: str
    name: str
    aliases: tuple[str, ...]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    # -- locale ------------------------------------------------------------
    @property
    def currency(self) -> str:
        return self.raw.get("locale", {}).get("currency", "USD")

    @property
    def currency_symbol(self) -> str:
        return self.raw.get("locale", {}).get("currency_symbol", "$")

    @property
    def date_format(self) -> str:
        return self.raw.get("locale", {}).get("date_format", "%Y-%m-%d")

    @property
    def terminology(self) -> dict[str, str]:
        return self.raw.get("locale", {}).get("terminology", {})

    def format_location(self, city: str | None, region: str | None) -> str:
        tmpl = self.raw.get("locale", {}).get("location_format", "{city}, {region}")
        parts = tmpl.format(city=city or "", region=region or "")
        return ", ".join(p.strip() for p in parts.split(",") if p.strip())

    # -- work authorization -------------------------------------------------
    @functools.cached_property
    def statuses(self) -> dict[str, AuthStatus]:
        out: dict[str, AuthStatus] = {}
        for s in self.raw.get("work_authorization", {}).get("statuses", []):
            out[s["id"]] = AuthStatus(
                id=s["id"],
                label=s["label"],
                needs_sponsorship_now=bool(s.get("needs_sponsorship_now", True)),
                needs_sponsorship_future=bool(s.get("needs_sponsorship_future", True)),
                satisfies=tuple(s.get("satisfies", []) or []),
                aliases=tuple(s.get("aliases", []) or []),
                transfer_required=bool(s.get("transfer_required", False)),
            )
        return out

    def status(self, status_id: str) -> AuthStatus:
        """Resolve a status id or alias; unknown ids fall back to `unknown`."""
        if status_id in self.statuses:
            return self.statuses[status_id]
        needle = (status_id or "").strip().lower()
        for st in self.statuses.values():
            if needle == st.label.lower() or needle in st.aliases:
                return st
        return self.statuses.get(
            "unknown",
            AuthStatus("unknown", "Work authorization unknown", True, True, (), ()),
        )

    @property
    def jd_signals(self) -> dict[str, Any]:
        return self.raw.get("work_authorization", {}).get("jd_signals", {})

    @property
    def employment_types(self) -> list[dict[str, Any]]:
        return self.raw.get("work_authorization", {}).get("employment_types", [])

    @property
    def salary(self) -> dict[str, Any]:
        return self.raw.get("salary", {})

    @property
    def job_boards(self) -> list[dict[str, Any]]:
        return self.raw.get("job_boards", [])

    @property
    def reminders(self) -> list[str]:
        return self.raw.get("application_rules", {}).get("reminders", [])


class CountryRegistry:
    """All country packs found on disk, addressable by code, name or alias."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory or os.getenv("CAREEROS_COUNTRIES_DIR", COUNTRIES_DIR))
        self._packs: dict[str, CountryPack] = {}
        self._index: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self._packs.clear()
        self._index.clear()
        for path in sorted(self.directory.glob("*.yaml")):
            data = _read_yaml(path)
            code = data["country_code"].upper()
            pack = CountryPack(
                code=code,
                name=data["country_name"],
                aliases=tuple(a.lower() for a in data.get("aliases", [])),
                raw=data,
            )
            self._packs[code] = pack
            self._index[code.lower()] = code
            self._index[pack.name.lower()] = code
            for alias in pack.aliases:
                self._index[alias] = code

    @property
    def codes(self) -> list[str]:
        return sorted(self._packs)

    def all(self) -> list[CountryPack]:
        return [self._packs[c] for c in self.codes]

    def get(self, key: str | None) -> CountryPack | None:
        if not key:
            return None
        code = self._index.get(str(key).strip().lower())
        return self._packs.get(code) if code else None

    def require(self, key: str | None) -> CountryPack:
        pack = self.get(key)
        if pack is None:
            raise KeyError(
                f"No country pack for {key!r}. Add config/countries/<code>.yaml "
                f"- known: {', '.join(self.codes)}"
            )
        return pack

    def detect(self, text: str, default: str = "US") -> CountryPack:
        """Infer the country from free text (a location string or a whole JD)."""
        low = f" {(text or '').lower()} "
        best: tuple[int, str] | None = None
        for needle, code in self._index.items():
            if len(needle) < 3:
                continue
            if f" {needle} " in low or f" {needle}," in low or f",{needle} " in low:
                cand = (len(needle), code)
                if best is None or cand > best:
                    best = cand
        if best:
            return self._packs[best[1]]
        return self.require(default)


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SkillNode:
    id: str
    label: str
    aliases: tuple[str, ...]
    domains: tuple[str, ...]
    related: tuple[str, ...]
    broader: tuple[str, ...]
    narrower: tuple[str, ...]


class Taxonomy:
    """Career domains, seniority ladder, work arrangements and the skill graph."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory or os.getenv("CAREEROS_TAXONOMY_DIR", TAXONOMY_DIR))
        self.reload()

    def reload(self) -> None:
        self._domains_raw = _read_yaml(self.directory / "domains.yaml")
        self._skills_raw = _read_yaml(self.directory / "skills.yaml")

        self.domains: list[dict[str, Any]] = self._domains_raw.get("domains", [])
        self.domain_by_id = {d["id"]: d for d in self.domains}
        self.confidence_floor: float = float(self._domains_raw.get("confidence_floor", 0.18))
        self.seniority_levels: list[dict[str, Any]] = self._domains_raw.get("seniority", {}).get("levels", [])
        self.default_seniority: str = self._domains_raw.get("seniority", {}).get("default", "mid")
        self.work_arrangements: list[dict[str, Any]] = self._domains_raw.get("work_arrangement", [])

        self.weights: dict[str, float] = self._skills_raw.get("weights", {})
        self.skills: dict[str, SkillNode] = {}
        self._alias_index: dict[str, str] = {}
        for s in self._skills_raw.get("skills", []):
            node = SkillNode(
                id=s["id"],
                label=s["label"],
                aliases=tuple(a.lower() for a in s.get("aliases", []) or []),
                domains=tuple(s.get("domains", []) or []),
                related=tuple(s.get("related", []) or []),
                broader=tuple(s.get("broader", []) or []),
                narrower=tuple(s.get("narrower", []) or []),
            )
            self.skills[node.id] = node
            self._alias_index[node.id.replace("_", " ")] = node.id
            self._alias_index[node.label.lower()] = node.id
            for alias in node.aliases:
                self._alias_index[alias] = node.id

    # -- skill graph --------------------------------------------------------
    @property
    def alias_index(self) -> dict[str, str]:
        """Surface form -> canonical skill id. Longest aliases first when scanning."""
        return self._alias_index

    def add_learned_skill(self, skill_id: str, label: str, aliases: list[str], related: list[str]) -> SkillNode:
        """Register a skill discovered at runtime (persisted via `LearnedSkill`)."""
        node = SkillNode(
            id=skill_id,
            label=label,
            aliases=tuple(a.lower() for a in aliases),
            domains=(),
            related=tuple(related),
            broader=(),
            narrower=(),
        )
        self.skills[skill_id] = node
        self._alias_index[label.lower()] = skill_id
        for alias in node.aliases:
            self._alias_index[alias] = skill_id
        return node

    def neighbours(self, skill_id: str) -> dict[str, tuple[str, ...]]:
        node = self.skills.get(skill_id)
        if not node:
            return {"related": (), "broader": (), "narrower": ()}
        # Edges are declared one-way in YAML; close them both ways here so the
        # matcher sees a symmetric graph.
        related = set(node.related)
        broader = set(node.broader)
        narrower = set(node.narrower)
        for other in self.skills.values():
            if skill_id in other.related:
                related.add(other.id)
            if skill_id in other.broader:      # other is narrower than us
                narrower.add(other.id)
            if skill_id in other.narrower:     # other is broader than us
                broader.add(other.id)
        return {
            "related": tuple(sorted(related)),
            "broader": tuple(sorted(broader)),
            "narrower": tuple(sorted(narrower)),
        }


# ---------------------------------------------------------------------------
# Process-wide singletons (cheap: a few small YAML files)
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def countries() -> CountryRegistry:
    return CountryRegistry()


@functools.lru_cache(maxsize=1)
def taxonomy() -> Taxonomy:
    return Taxonomy()


def reload_all() -> None:
    countries.cache_clear()
    taxonomy.cache_clear()


__all__ = [
    "AuthStatus",
    "CountryPack",
    "CountryRegistry",
    "SkillNode",
    "Taxonomy",
    "countries",
    "taxonomy",
    "reload_all",
]
