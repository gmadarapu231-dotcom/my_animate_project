"""Skill detection over free text, driven by the skill graph.

One compiled alternation matches every alias in the graph. Longest aliases win,
so "identity and access management" is recognised as `iam` rather than
fragmenting into "access" and "management".
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

from careeros.config import Taxonomy, taxonomy as default_taxonomy
from careeros.engines.textutil import normalize


@dataclass
class SkillHit:
    skill_id: str
    label: str
    surfaces: set[str] = field(default_factory=set)
    count: int = 0

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "label": self.label,
            "surfaces": sorted(self.surfaces),
            "count": self.count,
        }


def _boundary(alias: str) -> str:
    """Word-boundary-ish wrapper that tolerates aliases like `c#`, `ci/cd`, `.net`.

    An alias ending in a letter also matches its regular plural, so a resume
    saying "user access reviews" hits the `access certification` node.
    """
    escaped = re.escape(alias)
    prefix = r"(?<![a-z0-9])" if alias[0].isalnum() else ""
    if alias[-1].isalpha():
        suffix = r"(?:es|s)?(?![a-z0-9])"
    elif alias[-1].isdigit():
        suffix = r"(?![a-z0-9])"
    else:
        suffix = ""
    return f"{prefix}{escaped}{suffix}"


class SkillScanner:
    """Finds canonical skills in a blob of text."""

    def __init__(self, tax: Taxonomy | None = None) -> None:
        self.taxonomy = tax or default_taxonomy()
        self._build()

    def _build(self) -> None:
        index = self.taxonomy.alias_index
        # Longest first so the alternation prefers the most specific alias.
        aliases = sorted(index, key=len, reverse=True)
        self._index = dict(index)
        chunk = 400  # keep each compiled pattern to a sane size
        self._patterns = [
            re.compile("|".join(_boundary(a) for a in aliases[i : i + chunk]))
            for i in range(0, len(aliases), chunk)
        ]

    def rebuild(self) -> None:
        """Re-compile after runtime skills are added to the taxonomy."""
        self._build()

    def _lookup(self, surface: str) -> str | None:
        """Map a matched span back to a skill id, undoing plural inflection."""
        skill_id = self._index.get(surface)
        if skill_id:
            return skill_id
        for suffix in ("es", "s"):
            if surface.endswith(suffix):
                skill_id = self._index.get(surface[: -len(suffix)])
                if skill_id:
                    return skill_id
        return None

    def scan(self, text: str) -> dict[str, SkillHit]:
        norm = normalize(text)
        hits: dict[str, SkillHit] = {}
        if not norm:
            return hits
        for pattern in self._patterns:
            for m in pattern.finditer(norm):
                surface = m.group(0)
                skill_id = self._lookup(surface)
                if not skill_id:
                    continue
                node = self.taxonomy.skills.get(skill_id)
                hit = hits.get(skill_id)
                if hit is None:
                    hit = SkillHit(skill_id=skill_id, label=node.label if node else skill_id)
                    hits[skill_id] = hit
                hit.surfaces.add(surface)
                hit.count += 1
        return hits

    def resolve(self, phrase: str) -> str | None:
        """Map a single requirement phrase onto a canonical skill id.

        Tries the whole phrase, then the longest alias contained in it -- a JD
        line like "Strong hands-on SAP GRC Access Control experience" resolves
        to `sap_grc`.
        """
        norm = normalize(phrase)
        if not norm:
            return None
        if norm in self._index:
            return self._index[norm]
        best: tuple[int, str] | None = None
        for skill_id, hit in self.scan(norm).items():
            longest = max((len(s) for s in hit.surfaces), default=0)
            cand = (longest, skill_id)
            if best is None or cand > best:
                best = cand
        return best[1] if best else None


@functools.lru_cache(maxsize=1)
def default_scanner() -> SkillScanner:
    return SkillScanner()
