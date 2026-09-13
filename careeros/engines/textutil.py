"""Shared text primitives.

Deliberately dependency-free and deterministic: every engine can run with no
API key, and the LLM layer refines these results rather than replacing them.
That keeps the system testable and keeps a bad network day from stopping the
daily run.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_WS = re.compile(r"\s+")
_PUNCT_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ", "•": " ",
}


def normalize(text: str | None) -> str:
    """Lowercase, fold unicode punctuation, collapse whitespace."""
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    for src, dst in _PUNCT_MAP.items():
        out = out.replace(src, dst)
    out = out.lower()
    return _WS.sub(" ", out).strip()


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+|(?:^|\n)\s*[-*•]\s*", text or "")
    return [p.strip() for p in parts if p and p.strip()]


@dataclass(frozen=True)
class PhraseHit:
    """Where a phrase was found -- carried into the UI as the *source* of a verdict."""

    phrase: str
    start: int
    end: int
    excerpt: str


def find_phrase(haystack_norm: str, phrase: str, window: int = 90) -> PhraseHit | None:
    """Locate `phrase` in already-normalised text, returning a quotable excerpt."""
    needle = normalize(phrase)
    if not needle:
        return None
    idx = haystack_norm.find(needle)
    if idx < 0:
        return None
    lo = max(0, idx - window // 2)
    hi = min(len(haystack_norm), idx + len(needle) + window // 2)
    excerpt = haystack_norm[lo:hi].strip()
    if lo > 0:
        excerpt = "..." + excerpt
    if hi < len(haystack_norm):
        excerpt = excerpt + "..."
    return PhraseHit(phrase=needle, start=idx, end=idx + len(needle), excerpt=excerpt)


def contains_phrase(haystack_norm: str, phrase: str) -> bool:
    return find_phrase(haystack_norm, phrase) is not None


_WORD = re.compile(r"[a-z0-9][a-z0-9+#./&-]*")
_STOPWORDS = frozenset(
    """a an the and or of to in for with on at by from as is are be been being will would should could
    we our you your they their this that these those it its has have had do does did not no yes if then
    than but so such can may must about into over under more most other some any each every per
    including include includes etc via using use used across within while during also new role position
    job candidate candidates ideal strong excellent good great ability able work working works team teams
    years year experience experienced required require requires requirements responsibilities skills
    plus preferred must-have nice opportunity company client customers business who what when where how
    """.split()
)


def tokens(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in _STOPWORDS and len(t) > 1]


def ngrams(words: list[str], n: int) -> list[str]:
    return [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]


def candidate_phrases(text: str, max_n: int = 3) -> list[str]:
    """Unigrams .. trigrams with stopwords stripped -- the ATS keyword pool.

    Phrases are generated per line: an n-gram must never straddle a line break,
    or a bulleted JD yields nonsense like "access management bachelor".
    """
    out: list[str] = []
    for line in re.split(r"[\n;]+", text or ""):
        words = tokens(line)
        if not words:
            continue
        for n in range(1, max_n + 1):
            for gram in ngrams(words, n):
                parts = gram.split()
                if parts[0] in _STOPWORDS or parts[-1] in _STOPWORDS:
                    continue
                if all(p in _STOPWORDS for p in parts):
                    continue
                if len(gram) < 3:
                    continue
                out.append(gram)
    return out


# ---------------------------------------------------------------------------
# Requirement extraction
# ---------------------------------------------------------------------------
_YEARS_PATTERNS = [
    re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to)\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)"),
    re.compile(r"(?:minimum|min|at least|atleast)\s*(?:of\s*)?(\d{1,2})\s*\+?\s*(?:years|yrs|year)"),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years|yrs|year)"),
    re.compile(r"(\d{1,2})\s*(?:years|yrs)\s*(?:of\s*)?(?:relevant\s*|hands-on\s*|professional\s*)?experience"),
]


def extract_years_experience(text: str) -> float | None:
    """Smallest credible minimum stated in the text (a range's lower bound)."""
    norm = normalize(text)
    found: list[float] = []
    for pat in _YEARS_PATTERNS:
        for m in pat.finditer(norm):
            try:
                found.append(float(m.group(1)))
            except (TypeError, ValueError):
                continue
    credible = [v for v in found if 0 < v <= 30]
    return min(credible) if credible else None


_EDU_LEVELS = [
    ("phd", ["phd", "ph.d", "doctorate", "doctoral"]),
    ("masters", ["master's", "masters", "master of", "m.s.", "ms in", "mba", "m.tech", "mtech", "postgraduate"]),
    ("bachelors", ["bachelor's", "bachelors", "bachelor of", "b.s.", "bs in", "b.tech", "btech", "b.e.", "undergraduate degree", "4-year degree"]),
    ("associates", ["associate's degree", "associates degree", "diploma"]),
    ("highschool", ["high school", "ged", "secondary school"]),
]
EDU_RANK = {"highschool": 1, "associates": 2, "bachelors": 3, "masters": 4, "phd": 5}


def extract_education_requirement(text: str) -> str | None:
    norm = normalize(text)
    for level, needles in _EDU_LEVELS:
        if any(n in norm for n in needles):
            return level
    if "degree" in norm:
        return "bachelors"
    return None


_SECTION_HEADS = {
    "required": [
        "required skills", "requirements", "required qualifications", "must have",
        "must-have", "minimum qualifications", "basic qualifications", "what you need",
        "required experience", "mandatory skills", "key skills", "essential",
    ],
    "preferred": [
        "preferred", "preferred qualifications", "nice to have", "nice-to-have",
        "bonus points", "desired skills", "good to have", "plus", "a plus",
        "preferred skills", "additional qualifications",
    ],
    "responsibilities": [
        "responsibilities", "what you'll do", "what you will do", "the role",
        "duties", "day to day", "day-to-day", "key responsibilities",
    ],
}


def split_requirement_sections(description: str) -> dict[str, str]:
    """Split a JD into required / preferred / responsibilities / other blocks.

    Heading detection is intentionally forgiving: JDs are written by humans and
    a missed heading only costs a little precision -- everything unattributed
    lands in `other`, which still feeds keyword extraction.
    """
    lines = (description or "").split("\n")
    buckets: dict[str, list[str]] = {k: [] for k in ("required", "preferred", "responsibilities", "other")}
    current = "other"
    for line in lines:
        probe = normalize(line).strip(" :*-•")
        matched = None
        if probe and len(probe) <= 70:
            for bucket, heads in _SECTION_HEADS.items():
                if any(probe == h or probe.startswith(h) or probe.endswith(h) for h in heads):
                    matched = bucket
                    break
        if matched:
            current = matched
            continue
        buckets[current].append(line)
    return {k: "\n".join(v).strip() for k, v in buckets.items()}


def bulletize(text: str) -> list[str]:
    """Pull individual requirement lines out of a JD block."""
    out: list[str] = []
    for raw in (text or "").split("\n"):
        line = raw.strip().lstrip("-*•●▪‣ ").strip()
        if len(line) < 4:
            continue
        out.append(line)
    return out
