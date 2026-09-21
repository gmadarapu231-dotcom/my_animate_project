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


def _line_text(page: Page) -> str:
    from taxvault.forms.layout import lines_of

    return "\n".join(
        " ".join(w.text for w in row) for row in lines_of(page)
    )


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
        block = find_text_block(page, employer_label, width=300, depth=90)
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
        block = find_text_block(page, employee_label, width=300, depth=95)
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

    code = ""
    if state_label is not None:
        # The two-letter code sits under the "15 State" label.
        for word in sorted(page.words, key=lambda w: (state_label.y - w.y, abs(w.x - state_label.x))):
            dy = state_label.y - word.y
            dx = word.x - state_label.x
            if 0 < dy <= 46 and -6 <= dx <= 60 and word.text.strip().upper() in _US_STATES:
                code = word.text.strip().upper()
                break
    if not code:
        text = _line_text(page)
        found = [c for c in re.findall(r"\b([A-Z]{2})\b", text) if c in _US_STATES]
        code = found[0] if found else ""

    wages = find_box_value(page, wages_label, width=110) if wages_label else None
    tax = find_box_value(page, tax_label, width=95) if tax_label else None
    local_wages = find_box_value(page, local_wages_label, width=95) if local_wages_label else None
    local_tax = find_box_value(page, local_tax_label, width=70) if local_tax_label else None

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


def read_w2_layout(blob: bytes) -> tuple[W2, float, list[dict[str, str]]]:
    """Read a W-2 from a PDF's layout. Returns (form, confidence, notes)."""
    notes: list[dict[str, str]] = []
    form = W2()

    try:
        pages = words_from_pdf(blob)
    except Exception as exc:
        return form, 0.0, [{"severity": "error", "box": "",
                            "message": f"The PDF's layout could not be read ({type(exc).__name__})."}]
    if not pages:
        return form, 0.0, notes

    # The W-2 is whichever page carries Box 1; a payroll download often bundles
    # several forms or a cover page.
    page = next(
        (p for p in pages if find_label(p, [r"wages,? tips,? other comp"]) is not None),
        pages[0],
    )

    found = 0
    for attribute, patterns in MONEY_BOXES:
        label = find_label(page, patterns)
        if label is None:
            continue
        value = find_box_value(page, label)
        if value is None:
            continue
        setattr(form, attribute, value)
        if attribute in CRITICAL:
            found += 1

    whole = _line_text(page)
    year = YEAR.search(whole)
    if year:
        form.tax_year = int(year.group(1))
    ein = EIN_SHAPE.search(whole)
    if ein:
        form.employer_ein = ein.group(1)
    # Box a. Kept only long enough to check it against the account; it is never
    # stored, because a W-2's payload is figures, not identifiers.
    ssn = SSN_SHAPE.search(whole)
    if ssn:
        form.employee_ssn = "-".join(ssn.groups())

    _read_names(page, form, notes)
    if not form.employer_name or not (form.employee_first_name or form.employee_last_name):
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
