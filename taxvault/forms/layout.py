"""Reading a W-2 by where things sit on the page, not by what order they fall out.

A W-2 is a grid. Each box has a printed label in its top-left corner and its
figure underneath. Flattening a PDF to text throws that grid away: the labels
and figures survive, but their association is reduced to "roughly adjacent",
and on any form where the extractor walks columns in a different order that
association is simply wrong. The failure is quiet and expensive -- Box 16 and
Box 17 land far apart on the page, so a flat read misses the state figures and
the client is shown a state tax of zero.

So this module keeps the coordinates. `words_from_pdf` records every text run
with its position; `find_box_value` then answers a geometric question -- what
amount sits inside this label's box -- rather than a textual one.

Where the geometry is unreadable (a scan, an unusual producer), the caller
falls back to the flat parse. This is an improvement on that path, never a
replacement for it.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from taxvault.money import ZERO, money

#: An amount with cents or a thousands separator. A bare integer is form
#: furniture -- a box number, a ZIP, a year -- not a figure.
AMOUNT = re.compile(r"^\$?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})$")
#: Tokens that are never part of a name.
_NOT_NAME = re.compile(r"\d|^(?:suite|apt|apartment|unit|ste|floor|fl|po box)\b", re.I)


@dataclass
class Word:
    text: str
    x: float
    y: float
    page: int = 0

    @property
    def amount(self) -> Decimal | None:
        cleaned = self.text.strip().replace("$", "").replace(",", "")
        if AMOUNT.match(self.text.strip()):
            return money(cleaned)
        return None


@dataclass
class Page:
    number: int
    words: list[Word] = field(default_factory=list)
    height: float = 792.0


def words_from_pdf(blob: bytes) -> list[Page]:
    """Every text run on every page, with its position.

    pypdf reports a transformation matrix per run; its last two entries are the
    translation, which is where on the page the run was drawn.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(blob))
    pages: list[Page] = []
    for index, source in enumerate(reader.pages):
        page = Page(number=index)
        try:
            box = source.mediabox
            page.height = float(box.height)
        except Exception:
            pass

        def visitor(text, cm, tm, font_dict, font_size, _page=page, _index=index):
            content = (text or "").strip()
            if not content:
                return
            try:
                x, y = float(tm[4]), float(tm[5])
            except Exception:
                return
            _page.words.append(Word(text=content, x=x, y=y, page=_index))

        try:
            source.extract_text(visitor_text=visitor)
        except Exception:
            continue
        pages.append(page)
    return pages


def split_runs(words: Iterable[Word]) -> list[Word]:
    """A run may hold several tokens; positions are interpolated across it.

    Interpolation is approximate -- character widths vary -- but it only has to
    be good enough to keep tokens inside the box they were drawn in, and box
    widths are tens of points.
    """
    out: list[Word] = []
    for word in words:
        parts = word.text.split()
        if len(parts) <= 1:
            out.append(word)
            continue
        # Rough advance per character at a typical form font size.
        span = max(len(word.text), 1)
        cursor = 0
        for part in parts:
            offset = (cursor / span) * (span * 4.6)
            out.append(Word(text=part, x=word.x + offset, y=word.y, page=word.page))
            cursor += len(part) + 1
    return out


def lines_of(page: Page, tolerance: float = 3.0) -> list[list[Word]]:
    """Group words into visual lines, each sorted left to right."""
    rows: list[list[Word]] = []
    for word in sorted(page.words, key=lambda w: (-w.y, w.x)):
        for row in rows:
            if abs(row[0].y - word.y) <= tolerance:
                row.append(word)
                break
        else:
            rows.append([word])
    return [sorted(row, key=lambda w: w.x) for row in rows]


def find_label(page: Page, patterns: Iterable[str]) -> Word | None:
    """Locate a printed box label, anchored where the label itself begins.

    Matching is done across the whole line, because a label like "Wages, tips,
    other compensation" is usually several runs. But the anchor must be the
    word the match *starts* at, not the first word on the line: a W-2 puts
    several boxes side by side, so one line carries "a Employee's social
    security number" and "1 Wages, tips, other compensation" together. Taking
    the start of the line would anchor Box 1 to Box a and read the wrong cell —
    which is exactly how Boxes 3 through 6 all came back with the same figure.
    """
    for row in lines_of(page):
        # Character offset of each word within the joined line, so a regex
        # match can be mapped back to the word it began in.
        offsets: list[int] = []
        cursor = 0
        for word in row:
            offsets.append(cursor)
            cursor += len(word.text) + 1
        joined = " ".join(w.text for w in row).lower()

        for pattern in patterns:
            match = re.search(pattern, joined)
            if match is None:
                continue
            start = match.start()
            anchor = row[0]
            for word, offset in zip(row, offsets):
                if offset <= start:
                    anchor = word
                else:
                    break
            return anchor
    return None


def find_box_value(
    page: Page, label: Word, *,
    width: float = 118.0, depth: float = 46.0, allow_right: bool = True,
) -> Decimal | None:
    """The amount drawn inside this label's box.

    A W-2 box is about 115 points wide and 46 deep, with the label at the top
    and the figure below it. So: look for money below the label and within its
    column, nearest first. `allow_right` also accepts a figure level with the
    label, which is how some producers lay out the narrower boxes.
    """
    candidates: list[tuple[float, Decimal]] = []
    for word in page.words:
        value = word.amount
        if value is None:
            continue
        dx = word.x - label.x
        dy = label.y - word.y          # positive means below the label
        if not (-6 <= dx <= width):
            continue
        if 0 < dy <= depth:
            candidates.append((dy, value))
        elif allow_right and abs(dy) <= 3 and dx > 8:
            candidates.append((0.5, value))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def find_text_block(
    page: Page, label: Word, *, width: float = 300.0, depth: float = 90.0,
    max_lines: int = 4,
) -> list[str]:
    """The lines of text drawn inside a label's box, top to bottom.

    Used for the name and address blocks, where what matters is the text rather
    than a figure, and where the first line is the name.
    """
    inside: list[Word] = []
    for word in page.words:
        dx = word.x - label.x
        dy = label.y - word.y
        if -6 <= dx <= width and 4 < dy <= depth:
            inside.append(word)

    rows: list[list[Word]] = []
    for word in sorted(inside, key=lambda w: (-w.y, w.x)):
        for row in rows:
            if abs(row[0].y - word.y) <= 3.0:
                row.append(word)
                break
        else:
            rows.append([word])

    out: list[str] = []
    for row in rows[:max_lines]:
        text = " ".join(w.text for w in sorted(row, key=lambda w: w.x)).strip()
        if text:
            out.append(re.sub(r"\s+", " ", text))
    return out


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------
#: Initialisms that stay upper-case in a company name.
_ACRONYMS = {
    "LLC", "L.L.C.", "LLP", "LP", "PLLC", "PC", "PA", "USA", "US", "NA", "PLC",
    "AG", "SA", "BV", "NV", "AB", "AS", "II", "III", "IV", "HR", "IT", "IBM",
    "AT&T", "UPS", "CVS", "KFC", "H&R", "3M", "JP", "TD", "BP", "GE", "HP",
}
#: Company suffixes that are conventionally written in title case, not shouted.
#: "ACME INC." is "Acme Inc.", but "NORTHWIND LLC" stays "Northwind LLC".
_TITLE_SUFFIXES = {"INC", "CORP", "CO", "LTD", "COMPANY", "GMBH", "HOLDINGS", "GROUP"}
#: Particles that stay lower-case inside a name.
_PARTICLES = {"of", "and", "the", "for", "de", "la", "del", "van", "von", "der", "da"}


def _cap_token(token: str) -> str:
    """Capitalise one token, keeping acronyms, hyphens and apostrophes intact."""
    bare = token.strip(".,")
    if bare.upper() in _ACRONYMS:
        return bare.upper() + token[len(bare):]
    if bare.upper() in _TITLE_SUFFIXES:
        return bare.capitalize() + token[len(bare):]
    if len(bare) <= 1:
        return token.upper()
    if any(ch.isdigit() for ch in bare):
        return token.upper()
    # A hyphenated surname capitalises on both sides: SANTOS-RIVERA -> Santos-Rivera.
    if "-" in bare:
        return "-".join(_cap_token(part) for part in token.split("-"))
    if "'" in bare and len(bare.split("'")[0]) <= 2:
        head, _, tail = token.partition("'")
        return f"{head.capitalize()}'{tail.capitalize()}"
    if bare[:2].lower() in ("mc", "o'") and len(bare) > 3:
        return bare[:2].capitalize() + bare[2:].capitalize()
    return token.capitalize()


def tidy_name(raw: str) -> str:
    """Turn a shouted form entry into a name a person would recognise.

    `str.title()` is the obvious approach and it is wrong: it renders
    "NORTHWIND LOGISTICS LLC" as "Northwind Logistics Llc" and
    "SANTOS-RIVERA" as "Santos-Rivera" only by accident. Company suffixes and
    initialisms have to survive, and particles have to stay small.
    """
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return ""
    # Already mixed case: the person typed it, so leave it alone.
    if text != text.upper() and text != text.lower():
        return text

    tokens = text.split()
    out: list[str] = []
    for index, token in enumerate(tokens):
        if index and token.lower() in _PARTICLES:
            out.append(token.lower())
        else:
            out.append(_cap_token(token))
    return " ".join(out)


def looks_like_name(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 2 or len(stripped) > 70:
        return False
    return not _NOT_NAME.search(stripped)
