"""Reading a W-2 from a PDF's geometry.

Every box on the form is found by its printed label, and its figure is then
taken from inside that box. That is the whole idea, and it is what makes the
result trustworthy: Box 16 and Box 17 sit far apart in a flattened text dump
but adjacent on the page, so reading the page gets them right where reading
the dump gets them wrong.

`read_w2_layout` returns the form, a confidence, and the notes. Confidence is
the fraction of the boxes that matter which were actually located -- it is not
a guess about correctness, it is a count, and the UI uses it to decide whether
to route the document to review.
"""

from __future__ import annotations

import io
import re
from decimal import Decimal
from typing import Any

from taxvault.forms.layout import (
    Page,
    Word,
    find_box_value,
    find_label,
    find_text_block,
    lines_of,
    looks_like_name,
    tidy_name,
    words_from_pdf,
)
from taxvault.forms.w2 import W2, W2StateLine, _US_STATES
from taxvault.money import ZERO, money

#: Each money box: the attribute to fill, and the patterns that find its label.
#: Patterns are matched against a whole normalised line, so the box number and
#: the wording are both usable and either alone is enough.
MONEY_BOXES: list[tuple[str, list[str]]] = [
    ("wages", [r"\b1\b.{0,4}wages,? tips", r"wages,? tips,? other comp"]),
    ("federal_withheld", [r"\b2\b.{0,4}federal income tax", r"federal income tax withheld"]),
    ("social_security_wages", [r"\b3\b.{0,4}social security wages", r"social security wages"]),
    ("social_security_withheld", [r"\b4\b.{0,4}social security tax", r"social security tax withheld"]),
    ("medicare_wages", [r"\b5\b.{0,4}medicare wages", r"medicare wages and tips"]),
    ("medicare_withheld", [r"\b6\b.{0,4}medicare tax", r"medicare tax withheld"]),
    ("social_security_tips", [r"\b7\b.{0,4}social security tips", r"social security tips"]),
    ("allocated_tips", [r"\b8\b.{0,4}allocated tips", r"allocated tips"]),
    ("dependent_care_benefits", [r"\b10\b.{0,4}dependent care", r"dependent care benefits"]),
    ("nonqualified_plans", [r"\b11\b.{0,4}nonqualified", r"nonqualified plans"]),
]

#: The boxes whose absence means the read failed rather than the box being blank.
CRITICAL = ("wages", "federal_withheld", "social_security_wages", "medicare_wages")

#: A Box 12 entry: a one- or two-letter code, then an amount. Payroll separates
#: them with a pipe and pads with spaces -- "W |        500.00" -- so the
#: separator is optional and may be punctuation.
BOX12_CODE = re.compile(
    r"\b([A-Z]{1,2})\b\s*[|:\-]?\s*\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)"
)
SSN_SHAPE = re.compile(r"\b(\d{3}|\*{3})[-\s]?(\d{2}|\*{2})[-\s]?(\d{4})\b")
EIN_SHAPE = re.compile(r"\b(\d{2}-\d{7})\b")
YEAR = re.compile(r"\b(20[12]\d)\b")


#: What box 1's label looks like. Finding it more than once on a page means
#: the page carries more than one copy of the form.
_BOX_ONE = re.compile(r"wages,? tips,? other comp", re.I)

#: Two copies' box-1 labels closer together than this are the same copy read
#: twice, not two copies. A W-2 copy is about 280 points wide and 290 deep.
_COPY_GAP = 90.0


def _copy_anchors(page: Page) -> list[Word]:
    """Where each copy of the form starts, found by its box 1 label."""
    anchors: list[Word] = []
    for row in lines_of(page):
        pieces = [re.sub(r"\s+", " ", w.text).strip() for w in row]
        joined = " ".join(pieces)
        for match in _BOX_ONE.finditer(joined):
            # Map the match back to the word it began in.
            offset, found = 0, None
            for word, piece in zip(row, pieces):
                start = joined.find(piece, offset)
                if start <= match.start() < start + len(piece) + 1:
                    found = word
                    break
                offset = start + len(piece)
            anchors.append(found or row[0])
    return anchors


def _split(values: list[float], gap: float) -> list[float]:
    """Cluster a list of coordinates, returning one representative each."""
    out: list[float] = []
    for value in sorted(values):
        if not out or value - out[-1] > gap:
            out.append(value)
    return out


def first_copy_region(page: Page) -> tuple[tuple[float, float, float, float] | None, int]:
    """Where the first copy of the form sits, in the page's own point space.

    Returns ((left, bottom, right, top), copies). The box is None when the
    page carries a single form and there is nothing to narrow down.

    Separate from `one_copy` because filtering words is only half the job: an
    OCR pass over a four-up sheet reads every copy at a quarter of the size
    it could, and the figures come back wrong before there is anything to
    filter. The caller crops the image to this box and reads it again.
    """
    anchors = _copy_anchors(page)
    if len(anchors) <= 1:
        return None, max(1, len(anchors))

    columns = _split([a.x for a in anchors], _COPY_GAP)
    rows = _split([-a.y for a in anchors], _COPY_GAP)
    if len(columns) * len(rows) <= 1:
        return None, len(anchors)

    right = ((columns[0] + columns[1]) / 2) if len(columns) > 1 else page.width
    bottom = (-(rows[0] + rows[1]) / 2) if len(rows) > 1 else 0.0
    return (0.0, bottom, right, page.height), len(anchors)


def one_copy(page: Page) -> tuple[Page, int]:
    """Narrow a multi-copy page down to a single form.

    The IRS sends a W-2 as four copies on one sheet -- Copy B, Copy C and two
    Copy 2s -- and employers print exactly that. Read across the whole sheet
    the figures survive, because each copy agrees with the others, but the
    text blocks do not: the employee's name comes out as "Ann M. Smith Ann M.
    Smith" and the employer's name is not found at all, because the window
    that should hold one name block holds two.

    So the copies are located by their box 1 labels, the page is cut on the
    midpoints between them, and the first complete copy is what gets read.
    Returns the page to read and how many copies were on the sheet.
    """
    anchors = _copy_anchors(page)
    if len(anchors) <= 1:
        return page, max(1, len(anchors))

    columns = _split([a.x for a in anchors], _COPY_GAP)
    rows = _split([-a.y for a in anchors], _COPY_GAP)
    if len(columns) * len(rows) <= 1:
        return page, len(anchors)

    # The first copy: leftmost column, topmost row. Its right edge is halfway
    # to the next column's box 1, its bottom halfway to the next row's.
    right = ((columns[0] + columns[1]) / 2) if len(columns) > 1 else float("inf")
    bottom = (-(rows[0] + rows[1]) / 2) if len(rows) > 1 else float("-inf")
    kept = [w for w in page.words if w.x < right and w.y > bottom]
    return Page(number=page.number, words=kept, height=page.height), len(anchors)


def _line_text(page: Page) -> str:
    from taxvault.forms.layout import lines_of

    return "\n".join(
        " ".join(w.text for w in row) for row in lines_of(page)
    )


#: What starts a box on a W-2: its number, or the letter of a lettered box.
#: Every box on the form is introduced this way, which makes it the one
#: reliable marker of where one box ends and the next begins.
_BOX_START = re.compile(r"^(?:[1-9]|1[0-9]|20|[a-f])$", re.I)


def _cell_width(page: Page, label: Word, default: float) -> float:
    """How wide the box this label sits in actually is.

    A fixed window is wrong in both directions. Too wide and a box reaches
    into its neighbour -- box 3 came back holding box 4's figure, which is
    wrong and wrong quietly. Too narrow and it stops inside its own label.

    So the edge is found rather than assumed: every box on a W-2 is
    introduced by its own number or letter, so the next such marker to the
    right, on the same line, is where this box ends. When OCR has merged a
    label row into the row above or below, that marker is still the only
    thing that reliably separates one column from the next.
    """
    right = None
    for row in lines_of(page):
        if label not in row:
            continue
        for word in row:
            if word is label or word.x <= label.x + 10:
                continue
            head = word.text.strip().split()
            if head and _BOX_START.match(head[0]) and (right is None or word.x < right):
                right = word.x
        break
    if right is None:
        return default
    return max(55.0, right - label.x - 4)


def _read_names(page: Page, form: W2, notes: list[dict[str, str]]) -> None:
    """Employer and employee, from the blocks their labels own.

    A name block holds the name on its first line and the address underneath,
    so the first line that looks like a name is the name. Reading it from the
    box means an address line never gets mistaken for a company.
    """
    # Payroll providers word these labels differently -- ADP prints "e/f
    # Employee's name, address, and ZIP code" where the IRS form says
    # "Employee's first name and initial". Match on the part they agree on.
    employer_label = find_label(page, [
        r"employer'?s? name,? +address",
        r"employer'?s? name,? +and +address",
        r"\bc\b[^a-z]{0,4}employer'?s? name",
        r"employer'?s? name\b",
        r"employer name and address",
    ])
    if employer_label is not None:
        block = find_text_block(page, employer_label,
                                width=_cell_width(page, employer_label, 300), depth=90)
        for line in block:
            if looks_like_name(line):
                form.employer_name = tidy_name(line)
                break
    if not form.employer_name:
        notes.append({"severity": "warning", "box": "c",
                      "message": "The employer's name could not be read from the form."})

    employee_label = find_label(page, [
        r"employee'?s? first name",
        r"employee'?s? name,? +address",
        r"e ?/ ?f[^a-z]{0,4}employee'?s? name",
        r"\be\b[^a-z]{0,4}employee'?s? (first )?name",
        r"employee'?s? name\b",
        r"employee name and address",
    ])
    if employee_label is not None:
        block = find_text_block(page, employee_label,
                                width=_cell_width(page, employee_label, 300), depth=95)
        for line in block:
            if looks_like_name(line):
                # "MARIA J SANTOS-RIVERA": everything up to the last token is
                # the given name and initial, the last token is the surname.
                parts = line.split()
                if len(parts) >= 2:
                    form.employee_first_name = tidy_name(" ".join(parts[:-1]))
                    form.employee_last_name = tidy_name(parts[-1])
                else:
                    form.employee_first_name = tidy_name(line)
                break
    if not form.employee_last_name and not form.employee_first_name:
        notes.append({"severity": "info", "box": "e",
                      "message": "The employee's name could not be read from the form."})


#: Words that make a neighbouring two-letter token part of a printed label
#: rather than a state. "Employer's state ID no." contains "ID"; so does
#: "Employer ID no. (EIN)". Reading either as Idaho puts a Kentucky return
#: on the wrong state's rate table and says nothing.
_LABEL_NEIGHBOURS = {"no", "no.", "number", "num", "#", "nos", "nos."}


#: The state code sits in the same box as the employer's state ID, and OCR
#: does not always put a space between them: "KY A123456789" has come back as
#: "KY_A123456789". Splitting on whitespace alone then finds no state at all,
#: and the return is computed federal-only with the state silently missing.
_LEADING_STATE = re.compile(r"^([A-Za-z]{2})(?![A-Za-z])")


def _leading_state(token: str) -> str:
    match = _LEADING_STATE.match(token.strip())
    if not match:
        return ""
    code = match.group(1).upper()
    return code if code in _US_STATES else ""


def _state_code(page: Page, state_label: Word | None) -> str:
    """The two-letter state code from boxes 15-17, or nothing.

    Nothing is a perfectly good answer. The previous fallback swept the whole
    page for any two-letter run that happened to be a state abbreviation and
    took the first -- which on a real IRS sheet is the "ID" in "Employer ID
    no.", so every form it could not read properly came back as Idaho.
    """
    if state_label is not None:
        # The code sits in the box under the label, often in the same run as
        # the employer's state ID: "KY A123456789".
        candidates = sorted(
            page.words,
            key=lambda w: (state_label.y - w.y, abs(w.x - state_label.x)),
        )
        for word in candidates:
            dy = state_label.y - word.y
            dx = word.x - state_label.x
            if not (0 < dy <= 46 and -6 <= dx <= 60):
                continue
            code = _leading_state(word.text)
            if code:
                return code

    # Failing that, look only along the state row itself, and only at tokens
    # that are not part of a printed label.
    for row in lines_of(page):
        tokens = [t for word in row for t in word.text.split()]
        lowered = [t.lower().strip(":") for t in tokens]
        if not any(t in ("state", "15") for t in lowered):
            continue
        for index, token in enumerate(tokens):
            upper = _leading_state(token)
            if not upper:
                continue
            after = lowered[index + 1] if index + 1 < len(lowered) else ""
            before = lowered[index - 1] if index else ""
            if after in _LABEL_NEIGHBOURS or before in _LABEL_NEIGHBOURS:
                continue
            if before in ("employer", "employer's", "employers"):
                continue
            return upper
    return ""


def _read_state_rows(page: Page, form: W2, notes: list[dict[str, str]]) -> None:
    """Boxes 15-20, which is where a flat read goes most wrong.

    The state row is a wide strip of narrow boxes. Their labels sit side by
    side, so each figure is found under its own label rather than by hoping the
    amounts arrive in order.
    """
    wages_label = find_label(page, [r"\b16\b.{0,4}state wages", r"state wages,? tips"])
    tax_label = find_label(page, [r"\b17\b.{0,4}state income tax", r"state income tax"])
    local_wages_label = find_label(page, [r"\b18\b.{0,4}local wages", r"local wages,? tips"])
    local_tax_label = find_label(page, [r"\b19\b.{0,4}local income tax", r"local income tax"])
    state_label = find_label(page, [r"\b15\b.{0,4}state", r"employer'?s? state id"])

    code = _state_code(page, state_label)

    def value_for(label: Word | None, default: float) -> Any:
        if label is None:
            return None
        return find_box_value(page, label, width=_cell_width(page, label, default))

    wages = value_for(wages_label, 110.0)
    tax = value_for(tax_label, 95.0)
    local_wages = value_for(local_wages_label, 95.0)
    local_tax = value_for(local_tax_label, 70.0)

    if code and (wages is not None or tax is not None):
        form.states.append(W2StateLine(
            state=code,
            state_wages=wages if wages is not None else ZERO,
            state_withheld=tax if tax is not None else ZERO,
            local_wages=local_wages if local_wages is not None else ZERO,
            local_withheld=local_tax if local_tax is not None else ZERO,
        ))
    elif code:
        notes.append({"severity": "warning", "box": "16",
                      "message": (f"A {code} state line is on this form but its wages and "
                                  "withholding could not be read. Enter boxes 16 and 17 by "
                                  "hand or the state estimate will be wrong.")})
    elif wages is not None or tax is not None:
        notes.append({"severity": "warning", "box": "15", "message": (
            "Boxes 16 and 17 hold figures but the state in box 15 could not be "
            "read. Say which state it is: the wrong one puts the return on the "
            "wrong rate table.")})


def _read_box12(page: Page, form: W2, notes: list[dict[str, str]]) -> None:
    """Box 12, read from the boxes rather than from a line of text.

    Each of 12a to 12d is its own box with the code and amount inside it, so
    the reliable way to read them is to find each label and look underneath.
    A line scan is kept as a fallback for forms that print the codes in a list.
    """
    from taxvault.forms.layout import lines_of
    from taxvault.forms.w2 import _BOX12_CODES

    def take(text: str) -> None:
        for code, amount in BOX12_CODE.findall(text):
            if code in _BOX12_CODES and ("." in amount or "," in amount):
                form.box12.setdefault(code, money(amount.replace(",", "")))

    # --- positional: each 12a-12d box in turn --------------------------------
    for slot in ("12a", "12b", "12c", "12d"):
        label = find_label(page, [rf"\b{slot}\b"])
        if label is None:
            continue
        inside = [
            word for word in page.words
            if -6 <= (word.x - label.x) <= 124 and 0 < (label.y - word.y) <= 30
        ]
        if inside:
            take(" ".join(w.text for w in sorted(inside, key=lambda w: (-w.y, w.x))))

    # --- fallback: codes printed as a list ----------------------------------
    if not form.box12:
        for row in lines_of(page):
            text = " ".join(w.text for w in row).strip()
            if text.lower().startswith(("12", "see instructions for box 12")):
                continue
            take(text)


    # Box 13's retirement tick decides whether an IRA deduction is limited, so
    # it is worth reading rather than assuming.
    whole = _line_text(page).lower()
    if re.search(r"x\s*retirement plan|retirement plan\s*x", whole):
        form.retirement_plan = True
    elif form.elective_deferrals > ZERO:
        form.retirement_plan = True


#: Field names fillable W-2 PDFs use, mapped to what they hold. Different
#: producers name them differently, so each target lists its candidates.
ACROFORM_FIELDS = {
    "employer_name": ("employer", "employersname", "cemployersname", "employername"),
    "employee_first_name": ("employeefirstname", "efirstname", "firstname"),
    "employee_last_name": ("employeelastname", "elastname", "lastname"),
    "employee_name": ("employeename", "employeesname", "efemployeesname"),
}


def _read_acroform(blob: bytes, form: W2, notes: list[dict[str, str]]) -> None:
    """Names from a fillable PDF's form fields.

    A W-2 delivered as a fillable form holds its values in the AcroForm rather
    than drawn on the page, so nothing is found by looking at the page at all.
    Only fills what is still missing.
    """
    try:
        from pypdf import PdfReader

        fields = PdfReader(io.BytesIO(blob)).get_fields() or {}
    except Exception:
        return
    if not fields:
        return

    flat = {
        "".join(ch for ch in str(key).lower() if ch.isalnum()): str(
            (value.get("/V") if isinstance(value, dict) else value) or ""
        ).strip()
        for key, value in fields.items()
    }

    def pick(candidates: tuple[str, ...]) -> str:
        for candidate in candidates:
            for key, value in flat.items():
                if candidate in key and value:
                    return value
        return ""

    if not form.employer_name:
        found = pick(ACROFORM_FIELDS["employer_name"])
        if found:
            form.employer_name = tidy_name(found.splitlines()[0])
    if not form.employee_last_name and not form.employee_first_name:
        first = pick(ACROFORM_FIELDS["employee_first_name"])
        last = pick(ACROFORM_FIELDS["employee_last_name"])
        if not first and not last:
            whole = pick(ACROFORM_FIELDS["employee_name"])
            parts = whole.splitlines()[0].split() if whole else []
            if len(parts) >= 2:
                first, last = " ".join(parts[:-1]), parts[-1]
            elif parts:
                first = parts[0]
        if first:
            form.employee_first_name = tidy_name(first)
        if last:
            form.employee_last_name = tidy_name(last)
    if form.employer_name or form.employee_last_name:
        notes.append({"severity": "info", "box": "",
                      "message": "Some details were read from the PDF's form fields."})


#: Regions of a page to try when a whole-page read comes back incomplete,
#: as fractions of (width, height). The IRS issues a W-2 as four copies to a
#: sheet and employers print exactly that, so a quadrant is the likely home
#: of one whole form. Each extends past the halfway mark so a copy that sits
#: slightly off-centre is still contained rather than sliced in half.
_CANDIDATE_REGIONS = (
    (0.0, 0.0, 1.00, 1.00),     # the page as it came
    (0.0, 0.0, 0.56, 0.56),     # top-left quadrant: four copies to a sheet
    (0.0, 0.0, 0.56, 1.00),     # left half: two copies side by side
    (0.0, 0.0, 1.00, 0.56),     # top half: two copies stacked
)


def _score(form: W2, confidence: float) -> tuple:
    """How good a read is, for choosing between crops.

    Critical boxes first, because they are what an estimate needs. Then the
    things that tell a complete read from a lucky one: a state line, an
    employer, an employee, and a form whose own arithmetic holds.
    """
    from taxvault.forms.ocr import cross_check

    return (
        round(confidence, 3),
        1 if form.states else 0,
        1 if (form.employer_name or "").strip() else 0,
        1 if (form.employee_first_name or form.employee_last_name) else 0,
        -len(cross_check(form)),
    )


def read_w2_image(blob: bytes, *,
                  content_type: str = "") -> tuple[W2, float, list[dict[str, str]]]:
    """Read a W-2 off pixels: render it, and read the region that reads best.

    The single entry point for a scan or a photograph, so the API, the CLI
    and the agent cannot drift on how a pixel form is handled.

    The whole page is tried first and usually wins. When it does not, the
    reason is almost always that the sheet carries several copies of the
    form: Tesseract reads straight across two copies printed side by side,
    interleaving them into nonsense, and no amount of filtering afterwards
    recovers figures the reader never saw separately. Cropping first does
    recover them -- on a real IRS four-up sheet the whole page gave box 1 as
    empty and box 3 as box 4's figure, and the top-left quadrant gave every
    value correctly.

    So each candidate region is read and scored, and the best one wins. It
    costs a few seconds on the uncommon path and nothing on the common one,
    which is the right way round.
    """
    from taxvault.forms import ocr as _ocr

    images, dpi = _ocr.rendered(blob, content_type=content_type)
    if not images:
        return W2(), 0.0, []

    best: tuple | None = None
    for index, region in enumerate(_CANDIDATE_REGIONS):
        image = _crop(images[0], region) if index else images[0]
        if image is None:
            continue
        try:
            page = _ocr.positioned(image, page_number=1, dpi=dpi)
        except Exception:
            continue
        form, confidence, notes = read_w2_pages([page])
        score = _score(form, confidence)
        if best is None or score > best[0]:
            best = (score, form, confidence, notes, index)
        if index == 0 and confidence >= 1.0 and form.states:
            # The page read completely as it came. Nothing to look for.
            break

    if best is None:
        return W2(), 0.0, []

    _, form, confidence, notes, index = best
    if index:
        notes = list(notes) + [{"severity": "info", "box": "", "message": (
            "This page holds more than one copy of the form, which is how the "
            "IRS sheet is printed. One copy was read on its own, because "
            "reading the whole sheet runs the copies together."
        )}]
    return form, confidence, notes


def _crop(image: Any, region: tuple[float, float, float, float]) -> Any:
    left, top, right, bottom = region
    box = (int(image.width * left), int(image.height * top),
           int(image.width * right), int(image.height * bottom))
    if box[2] - box[0] < 120 or box[3] - box[1] < 120:
        return None
    try:
        return image.crop(box)
    except Exception:
        return None


def read_w2_layout(blob: bytes) -> tuple[W2, float, list[dict[str, str]]]:
    """Read a W-2 from a PDF's layout. Returns (form, confidence, notes)."""
    try:
        pages = words_from_pdf(blob)
    except Exception as exc:
        return W2(), 0.0, [{"severity": "error", "box": "",
                            "message": f"The PDF's layout could not be read ({type(exc).__name__})."}]
    return read_w2_pages(pages, blob=blob)


def read_w2_pages(pages: list[Page], *,
                  blob: bytes | None = None) -> tuple[W2, float, list[dict[str, str]]]:
    """Read a W-2 from positioned words, wherever they came from.

    A W-2 is a grid, and reading a grid needs the grid. Pulled out as its own
    function so OCR can use it: Tesseract reports where each word sat on the
    page, which is the same information pypdf gives for a text PDF, and a
    scan read through this comes out as accurate as a payroll download. Read
    flat instead, the label row and the value row are separate lines and
    "1 Wages" picks up the 2 from the next box along.

    `blob` is only for the AcroForm fallback, which needs the original PDF.
    """
    notes: list[dict[str, str]] = []
    form = W2()
    if not pages:
        return form, 0.0, notes

    # The W-2 is whichever page carries Box 1; a payroll download often bundles
    # several forms or a cover page.
    page = next(
        (p for p in pages if find_label(p, [r"wages,? tips,? other comp"]) is not None),
        pages[0],
    )
    # An employer prints the IRS sheet as issued: four copies of the same form
    # on one page. Everything below reads one copy.
    page, copies = one_copy(page)
    if copies > 1:
        notes.append({"severity": "info", "box": "", "message": (
            f"This page carries {copies} copies of the same W-2, which is how "
            "the IRS sheet is printed. The first one was read."
        )})

    found = 0
    for attribute, patterns in MONEY_BOXES:
        label = find_label(page, patterns)
        if label is None:
            continue
        # A box ends where the next box's label begins. Fixed at 118 points
        # the window reached into the neighbouring column, so on a form with
        # narrower boxes than that, box 3 came back holding box 4's figure
        # and box 5 holding box 6's -- wrong, and wrong quietly.
        value = find_box_value(page, label, width=_cell_width(page, label, 118.0))
        if value is None:
            continue
        setattr(form, attribute, value)
        if attribute in CRITICAL:
            found += 1

    whole = _line_text(page)
    year = YEAR.search(whole)
    if year:
        form.tax_year = int(year.group(1))
    else:
        notes.append({"severity": "info", "box": "", "message": (
            "No tax year is printed on this form that could be read -- a "
            "blank or sample W-2 shows '20XX' -- so the year you chose is "
            "used. Check it is the right one: the rates change every year."
        )})
    ein = EIN_SHAPE.search(whole)
    if ein:
        form.employer_ein = ein.group(1)
    # Box a. Kept only long enough to check it against the account; it is never
    # stored, because a W-2's payload is figures, not identifiers.
    ssn = SSN_SHAPE.search(whole)
    if ssn:
        form.employee_ssn = "-".join(ssn.groups())

    _read_names(page, form, notes)
    if blob and (not form.employer_name
                 or not (form.employee_first_name or form.employee_last_name)):
        _read_acroform(blob, form, notes)
    _read_box12(page, form, notes)
    _read_state_rows(page, form, notes)

    confidence = round(found / len(CRITICAL), 3)
    if confidence < 1.0:
        missing = [
            name.replace("_", " ") for name, _ in MONEY_BOXES
            if name in CRITICAL and getattr(form, name) == ZERO
        ]
        notes.append({"severity": "warning", "box": "",
                      "message": "Could not locate: " + ", ".join(missing)
                                 + ". Enter these boxes by hand."})
    return form, confidence, notes
