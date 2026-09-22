"""Reading a résumé into *proposals*, never into verified facts.

This is the one place in the system where career facts are inferred rather
than asserted by the user, so the output is deliberately second-class: every
item comes back `unverified`, and `resume_intake.commit` refuses to promote
anything the user has not confirmed. The parser proposes; the person decides.
That keeps the invariant the whole résumé layer rests on -- a generated résumé
can only cite evidence the user has verified -- while still saving them from
typing their own history in by hand.

Parsing is deterministic and structural: section headings, date ranges,
employer/title lines, bullet shapes. Skills come from the existing skill graph
rather than a new word list, so a résumé in any field is read with the same
machinery as a posting in that field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from careeros.engines.skills import default_scanner
from careeros.enums import EvidenceKind

# ---------------------------------------------------------------------------
# Section detection
#
# Headings are matched by shape (short line, no sentence punctuation, often
# upper-case) and then by keyword, so an unusual heading still splits the
# document even when its wording is not in the list.
# ---------------------------------------------------------------------------
SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "profile", "objective", "about", "overview"),
    "experience": (
        "experience", "employment", "work history", "professional experience",
        "career history", "positions", "professional background",
    ),
    "skills": ("skills", "technical skills", "technologies", "competencies", "expertise"),
    "education": ("education", "academic", "qualifications", "degrees"),
    "certifications": ("certification", "certifications", "licences", "licenses", "credentials"),
    "projects": ("projects", "selected projects", "key projects"),
    "awards": ("awards", "honours", "honors", "achievements", "accomplishments"),
    "publications": ("publications", "papers", "patents"),
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

#: "Mar 2021", "03/2021", "2021-03", "2021"
_DATE = rf"(?:(?:{_MONTH_RE})[a-z]*\.?\s+\d{{4}}|\d{{1,2}}[/-]\d{{4}}|\d{{4}}[-/]\d{{1,2}}|\d{{4}})"
_PRESENT = r"(?:present|current|now|to\s*date|ongoing)"
_RANGE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:-|–|—|to|until|through)\s*(?P<end>{_DATE}|{_PRESENT})",
    re.IGNORECASE,
)

_BULLET = re.compile(r"^\s*(?:[•·▪◦‣∙*+·]|[-–—](?!\s*\d{4})|\d+[.)])\s+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_URL = re.compile(r"(?:https?://|www\.)[^\s,;]+", re.IGNORECASE)
_LINKEDIN = re.compile(r"linkedin\.com/in/[\w-]+", re.IGNORECASE)
_GITHUB = re.compile(r"github\.com/[\w-]+", re.IGNORECASE)

#: A metric worth keeping: a number with a unit or a percent, not a year.
_METRIC = re.compile(
    r"(?<![\w/])(\d[\d,.]*)\s*(%|percent|k\b|m\b|bn\b|million|billion|"
    r"users?|customers?|sites?|servers?|nodes?|devices?|clients?|"
    r"hours?|days?|weeks?|months?|tickets?|records?|tps\b|qps\b|rps\b|"
    r"gbps\b|mbps\b|tb\b|gb\b|pb\b)",
    re.IGNORECASE,
)
_YEAR_ONLY = re.compile(r"^(19|20)\d{2}$")

#: Separators used between a company and a title on one line. A comma needs no
#: space before it ("Acme, Network Engineer, Dallas, TX"), but a dash does, or
#: every hyphenated job title would come apart.
_SPLIT = re.compile(r"(?:\s*,\s+|\s+(?:[|·•–—]|-{1,2}|\bat\b)\s+)", re.IGNORECASE)

#: A line that is nothing but a date range -- résumés very often put the dates
#: on their own line under the employer.
_DATE_ONLY = re.compile(
    rf"^\s*\(?\s*{_DATE}\s*(?:-|–|—|to|until|through)\s*(?:{_DATE}|{_PRESENT})\s*\)?\s*$",
    re.IGNORECASE,
)

_SENIORITY = (
    "intern", "junior", "associate", "senior", "sr", "staff", "principal", "lead",
    "head", "director", "manager", "vp", "chief", "architect", "consultant",
)
_TITLE_NOUNS = (
    "engineer", "developer", "analyst", "scientist", "administrator", "architect",
    "consultant", "manager", "specialist", "designer", "researcher", "technician",
    "accountant", "nurse", "physician", "therapist", "teacher", "recruiter",
    "coordinator", "director", "officer", "lead", "advisor", "auditor", "planner",
    "strategist", "buyer", "controller", "paralegal", "pharmacist", "veterinarian",
    "practitioner", "supervisor", "executive", "representative", "associate",
)


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip().rstrip(":")
    if not stripped or len(stripped) > 48:
        return False
    if stripped.endswith((".", ",", ";")):
        return False
    words = stripped.split()
    if len(words) > 5:
        return False
    letters = [c for c in stripped if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return True
    return stripped.istitle() or stripped.islower()


def section_of(line: str) -> str | None:
    """The canonical section a heading line names, if it names one."""
    if not _looks_like_heading(line):
        return None
    lowered = line.strip().rstrip(":").lower()
    for name, keywords in SECTION_KEYWORDS.items():
        for keyword in keywords:
            if lowered == keyword or lowered.startswith(keyword) or keyword in lowered:
                return name
    return None


def split_sections(text: str) -> dict[str, list[str]]:
    """Group lines under the section heading above them.

    Everything before the first recognised heading goes to `header`, which is
    where the name, email and links live.
    """
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.split("\n"):
        name = section_of(line)
        if name:
            current = name
            sections.setdefault(current, [])
            continue
        if line.strip():
            sections.setdefault(current, []).append(line.rstrip())
    return sections


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def parse_month(token: str, *, end: bool = False) -> date | None:
    """A résumé date token as a real date. Missing pieces resolve sensibly."""
    token = token.strip().rstrip(".").lower()
    if not token:
        return None
    if re.fullmatch(_PRESENT, token, re.IGNORECASE):
        return None  # open-ended: the caller treats None-with-present as current

    month_name = re.match(rf"({_MONTH_RE})[a-z]*\.?\s+(\d{{4}})", token)
    if month_name:
        return date(int(month_name.group(2)), _MONTHS[month_name.group(1)], 1)

    slashed = re.fullmatch(r"(\d{1,2})[/-](\d{4})", token)
    if slashed:
        month = min(max(int(slashed.group(1)), 1), 12)
        return date(int(slashed.group(2)), month, 1)

    iso = re.fullmatch(r"(\d{4})[-/](\d{1,2})", token)
    if iso:
        month = min(max(int(iso.group(2)), 1), 12)
        return date(int(iso.group(1)), month, 1)

    year = re.fullmatch(r"(\d{4})", token)
    if year:
        value = int(year.group(1))
        if 1950 <= value <= date.today().year + 1:
            return date(value, 12 if end else 1, 1)
    return None


def find_range(line: str) -> tuple[date | None, date | None, bool] | None:
    """(start, end, is_current) for the first date range on the line."""
    match = _RANGE.search(line)
    if not match:
        return None
    start = parse_month(match.group("start"))
    end_token = match.group("end")
    current = bool(re.fullmatch(_PRESENT, end_token.strip(), re.IGNORECASE))
    end = None if current else parse_month(end_token, end=True)
    if start is None and end is None:
        return None
    return start, end, current


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------
@dataclass
class EmployerProposal:
    name: str
    title: str | None = None
    location: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    current: bool = False
    raw_line: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "location": self.location,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "current": self.current,
            "raw_line": self.raw_line,
        }


@dataclass
class EvidenceProposal:
    """A candidate fact. Unverified by construction -- see the module docstring."""

    text: str
    kind: str = EvidenceKind.RESPONSIBILITY.value
    employer_name: str | None = None
    role_title: str | None = None
    skills: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    start_date: date | None = None
    end_date: date | None = None
    confidence: float = 0.5
    source_line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "employer_name": self.employer_name,
            "role_title": self.role_title,
            "skills": self.skills,
            "technologies": self.technologies,
            "metrics": self.metrics,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "confidence": round(self.confidence, 2),
            "verification": "unverified",
        }


@dataclass
class ParsedResume:
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    links: dict[str, str] = field(default_factory=dict)
    location: str | None = None
    current_title: str | None = None
    titles: list[str] = field(default_factory=list)
    employers: list[EmployerProposal] = field(default_factory=list)
    evidence: list[EvidenceProposal] = field(default_factory=list)
    skills: dict[str, float] = field(default_factory=dict)
    certifications: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    summary: str | None = None
    total_experience_years: float = 0.0
    sections_found: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "email": self.email,
            "phone": self.phone,
            "links": self.links,
            "location": self.location,
            "current_title": self.current_title,
            "titles": self.titles,
            "total_experience_years": round(self.total_experience_years, 1),
            "employers": [e.to_dict() for e in self.employers],
            "evidence": [e.to_dict() for e in self.evidence],
            "skills": {k: round(v, 2) for k, v in self.skills.items()},
            "certifications": self.certifications,
            "education": self.education,
            "summary": self.summary,
            "sections_found": self.sections_found,
            "warnings": self.warnings,
            "counts": {
                "employers": len(self.employers),
                "evidence": len(self.evidence),
                "skills": len(self.skills),
            },
        }


# ---------------------------------------------------------------------------
def _looks_like_title(text: str) -> bool:
    lowered = text.lower()
    return any(noun in lowered for noun in _TITLE_NOUNS) or any(
        lowered.startswith(word + " ") for word in _SENIORITY
    )


def _clean(fragment: str) -> str:
    return re.sub(r"\s{2,}", " ", fragment.strip(" \t·•|–—,-")).strip()





def _take_location(line: str) -> tuple[str, str | None]:
    """Pull the location out of a header line before anything else is split.

    Order matters: a standalone "Remote" is unambiguous, "Austin, TX" nearly
    so, and "City, Region" only when it ends the line. Splitting on commas
    first and reassembling afterwards cannot work -- it cannot tell
    "Austin, TX" from "Orrin Systems, Network Engineer".
    """
    for pattern in (
        r"(?:(?<=^)|(?<=[,|·•–—\-]\s))(remote|hybrid|on-?site|work from home)(?=\s*(?:[,|·•–—]|$))",
        r"\b([A-Z][\w.'’]+(?: [A-Z][\w.'’]+){0,2},\s*[A-Z]{2})\b(?!\w)",
        r"([A-Z][\w.'’]+(?: [A-Z][\w.'’]+){0,2},\s*[A-Z][a-z]+(?: [A-Z][a-z]+){0,2})\s*$",
    ):
        match = re.search(pattern, line, re.IGNORECASE if "remote" in pattern else 0)
        if not match:
            continue
        found = match.group(1)
        if _looks_like_title(found):
            continue
        return line[: match.start(1)] + " " + line[match.end(1) :], _clean(found)
    return line, None


def _split_employer_line(line: str) -> tuple[str | None, str | None, str | None]:
    """(employer, title, location) from a header line of an experience entry.

    Résumés write this a dozen ways -- "Acme | Senior Engineer | Austin, TX",
    "Senior Engineer, Acme Corp", "Acme Corp — Senior Engineer". The location
    is removed first, the title is then identified by its own vocabulary, and
    whatever remains is the employer -- so the order on the page does not
    matter.
    """
    without_dates = _RANGE.sub("", line)
    without_dates = re.sub(r"\(\s*\)", "", without_dates)
    # Removing the dates can leave a dangling separator, and the "City, Region"
    # pattern anchors to end-of-line.
    without_dates = without_dates.strip().strip(" \t|·•–—,-").strip()
    without_location, location = _take_location(without_dates)

    parts = [_clean(p) for p in _SPLIT.split(without_location) if _clean(p)]
    if not parts:
        return None, None, location

    title = next((p for p in parts if _looks_like_title(p)), None)
    remaining = [p for p in parts if p is not title]
    employer = remaining[0] if remaining else None
    return employer, title, location


def _metrics_of(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for value, unit in _METRIC.findall(text):
        cleaned = value.replace(",", "").rstrip(".")
        if _YEAR_ONLY.match(cleaned):
            continue
        try:
            number = float(cleaned)
        except ValueError:
            continue
        key = unit.lower().rstrip(".").rstrip("s") or "value"
        key = {"percent": "%"}.get(key, key)
        out.setdefault(key, number if number % 1 else int(number))
    return out


def _years_between(spans: Iterable[tuple[date | None, date | None]]) -> float:
    """Total months covered by the spans, unioned so overlaps are not double-counted."""
    today = date.today()
    intervals: list[tuple[int, int]] = []
    for start, end in spans:
        if start is None:
            continue
        finish = end or today
        if finish < start:
            continue
        intervals.append((start.year * 12 + start.month, finish.year * 12 + finish.month))
    if not intervals:
        return 0.0
    intervals.sort()
    months = 0
    cur_start, cur_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            months += cur_end - cur_start
            cur_start, cur_end = start, end
    months += cur_end - cur_start
    return round(months / 12.0, 1)


def parse_resume(text: str) -> ParsedResume:
    """Structure a résumé's text. Deterministic; no model involved."""
    parsed = ParsedResume()
    sections = split_sections(text)
    parsed.sections_found = [name for name in sections if name != "header" and sections[name]]

    # -- header ------------------------------------------------------------
    header = sections.get("header", [])
    blob = "\n".join(header)
    if (found := _EMAIL.search(blob)):
        parsed.email = found.group(0)
    if (found := _PHONE.search(_EMAIL.sub("", blob))):
        candidate = found.group(0).strip()
        if sum(c.isdigit() for c in candidate) >= 9:
            parsed.phone = candidate
    if (found := _LINKEDIN.search(blob)):
        parsed.links["linkedin"] = found.group(0)
    if (found := _GITHUB.search(blob)):
        parsed.links["github"] = found.group(0)
    for url in _URL.findall(blob):
        if "linkedin.com" in url.lower() or "github.com" in url.lower():
            continue
        parsed.links.setdefault("website", url)
        break

    for line in header[:6]:
        candidate = _clean(_EMAIL.sub("", _URL.sub("", _PHONE.sub("", line))))
        if not candidate or len(candidate) > 60:
            continue
        words = candidate.split()
        if parsed.full_name is None and 1 < len(words) <= 5 and not _looks_like_title(candidate):
            if all(w[0].isupper() or w.isupper() for w in words if w[:1].isalpha()):
                parsed.full_name = candidate
                continue
        if parsed.current_title is None and _looks_like_title(candidate):
            parsed.current_title = candidate

    # -- summary -----------------------------------------------------------
    if sections.get("summary"):
        parsed.summary = " ".join(sections["summary"])[:1200]

    # -- experience --------------------------------------------------------
    experience = sections.get("experience", []) + sections.get("projects", [])
    current_employer: EmployerProposal | None = None

    for line_number, line in enumerate(experience, start=1):
        stripped = line.strip()
        if not stripped:
            continue

        is_bullet = bool(_BULLET.match(line))

        # A line that is only a date range belongs to the entry above it --
        # the commonest résumé layout puts the dates on their own line.
        if not is_bullet and _DATE_ONLY.match(stripped):
            span = find_range(stripped)
            if span and current_employer and current_employer.start_date is None:
                current_employer.start_date, current_employer.end_date, current_employer.current = span
                current_employer.raw_line = f"{current_employer.raw_line} / {stripped}"
                # Bullets already attached to this employer predate the dates.
                for proposal in parsed.evidence:
                    if proposal.employer_name == current_employer.name and proposal.start_date is None:
                        proposal.start_date = current_employer.start_date
                        proposal.end_date = current_employer.end_date
            continue

        span = find_range(stripped)

        # A header line: has a date range, or reads like "Company — Title".
        if not is_bullet and (span or _looks_like_title(stripped)):
            employer, title, location = _split_employer_line(stripped)
            if employer or title:
                start, end, is_current = span or (None, None, False)
                current_employer = EmployerProposal(
                    name=employer or (title or stripped)[:120],
                    title=title,
                    location=location,
                    start_date=start,
                    end_date=end,
                    current=is_current,
                    raw_line=stripped,
                )
                parsed.employers.append(current_employer)
                if title and title not in parsed.titles:
                    parsed.titles.append(title)
                continue

        body = _BULLET.sub("", stripped).strip()
        if len(body) < 25:
            continue

        metrics = _metrics_of(body)
        kind = EvidenceKind.ACHIEVEMENT.value if metrics else EvidenceKind.RESPONSIBILITY.value
        parsed.evidence.append(
            EvidenceProposal(
                text=body,
                kind=kind,
                employer_name=current_employer.name if current_employer else None,
                role_title=current_employer.title if current_employer else None,
                metrics=metrics,
                start_date=current_employer.start_date if current_employer else None,
                end_date=current_employer.end_date if current_employer else None,
                # A bullet under a known employer with a measurable outcome is
                # the strongest shape a résumé line comes in.
                confidence=0.75 if (is_bullet and current_employer and metrics)
                else 0.6 if (is_bullet and current_employer)
                else 0.45,
                source_line=line_number,
            )
        )

    # -- skills, from the same graph the classifier uses -------------------
    scanner = default_scanner()
    hits = scanner.scan(text)
    # Weight by how often the résumé mentions it, capped: a skill named six
    # times is a real one, but ten mentions is not ten times the evidence.
    for skill_id, hit in hits.items():
        parsed.skills[skill_id] = min(1.0, 0.4 + 0.15 * hit.count)

    skills_text = "\n".join(sections.get("skills", []))
    if skills_text:
        for skill_id in scanner.scan(skills_text):
            # Named in a skills section: the user claims it explicitly.
            parsed.skills[skill_id] = 1.0

    for proposal in parsed.evidence:
        found = scanner.scan(proposal.text)
        proposal.skills = sorted(found)
        proposal.technologies = sorted(
            {surface for hit in found.values() for surface in hit.surfaces}
        )

    # -- certifications / education ---------------------------------------
    parsed.certifications = [
        _clean(_BULLET.sub("", line)) for line in sections.get("certifications", [])
        if len(_clean(line)) > 3
    ][:24]
    parsed.education = [
        _clean(_BULLET.sub("", line)) for line in sections.get("education", [])
        if len(_clean(line)) > 3
    ][:12]

    # -- derived -----------------------------------------------------------
    for proposal in parsed.evidence:
        if proposal.start_date is not None:
            continue
        owner = next((e for e in parsed.employers if e.name == proposal.employer_name), None)
        if owner:
            proposal.start_date, proposal.end_date = owner.start_date, owner.end_date

    parsed.total_experience_years = _years_between(
        (e.start_date, e.end_date if not e.current else None) for e in parsed.employers
    )
    if parsed.current_title is None:
        current = next((e for e in parsed.employers if e.current and e.title), None)
        parsed.current_title = current.title if current else (parsed.titles[0] if parsed.titles else None)

    # -- warnings: say what was not found rather than pretend --------------
    if not parsed.employers:
        parsed.warnings.append(
            "No employment entries found. Check that the experience section has a "
            "heading, and that each role names a company and a date range."
        )
    if not parsed.evidence:
        parsed.warnings.append(
            "No bullet points found, so there is nothing to build résumé claims from yet."
        )
    if not parsed.skills:
        parsed.warnings.append("No known skills matched. The skill graph may not cover this field yet.")
    if not parsed.email:
        parsed.warnings.append("No email address found in the header.")
    undated = sum(1 for e in parsed.employers if e.start_date is None)
    if undated:
        parsed.warnings.append(
            f"{undated} role(s) have no readable date range, so total experience is understated."
        )
    return parsed
