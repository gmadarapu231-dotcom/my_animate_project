"""Reading an uploaded W-2.

The case that matters is the ordinary one: somebody downloads their W-2 from a
payroll portal and uploads the PDF. That file carries its text, so the boxes
should come out of it. A scan carries pixels instead, and the system has to say
so rather than quietly producing an estimate from nothing.
"""

from __future__ import annotations

import io

import pytest

from taxvault.forms.extract import extract_text
from taxvault.forms.w2 import parse_w2_text


def build_w2_pdf() -> bytes:
    """A text-based W-2, of the kind a payroll portal emits."""
    reportlab = pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(60, 740, "2025 Form W-2  Wage and Tax Statement")
    c.setFont("Helvetica", 9)
    c.drawString(60, 722, "Employer's name, address, and ZIP code")
    c.drawString(60, 710, "ACME CORPORATION")
    c.drawString(60, 698, "EIN 12-3456789")
    rows = [
        ("1 Wages, tips, other compensation", "118,000.00"),
        ("2 Federal income tax withheld", "14,200.00"),
        ("3 Social security wages", "126,000.00"),
        ("4 Social security tax withheld", "7,812.00"),
        ("5 Medicare wages and tips", "126,000.00"),
        ("6 Medicare tax withheld", "1,827.00"),
    ]
    y = 650
    for label, amount in rows:
        c.drawString(60, y, label)
        c.drawRightString(320, y, amount)
        y -= 18
    c.drawString(60, y - 10, "12a D  8,000.00")
    c.drawString(60, y - 30, "15 State  CA    16 State wages 118,000.00    17 State income tax 6,800.00")
    c.save()
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def test_a_payroll_pdf_is_read():
    result = extract_text(build_w2_pdf(), "application/pdf", "w2.pdf")
    assert result.readable is True
    assert result.method == "pdf_text"
    assert "118,000.00" in result.text


def test_a_scanned_pdf_says_it_needs_ocr():
    """No text in the file means pixels, and pixels need OCR we do not have."""
    reportlab = pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.rect(100, 100, 300, 300, fill=0)  # a shape, no text
    c.save()

    result = extract_text(buffer.getvalue(), "application/pdf", "scan.pdf")
    assert result.readable is False
    assert any("OCR" in n["message"] for n in result.notes)


def test_a_photo_is_refused_clearly():
    result = extract_text(b"\xff\xd8\xff\xe0 jpeg bytes", "image/jpeg", "w2.jpg")
    assert result.readable is False
    assert any("OCR" in n["message"] for n in result.notes)


def test_a_corrupt_pdf_explains_itself_rather_than_exploding():
    result = extract_text(b"%PDF-1.4 truncated", "application/pdf", "broken.pdf")
    assert result.readable is False
    assert any(n["severity"] == "error" for n in result.notes)


def test_an_empty_file_is_refused():
    assert extract_text(b"", "application/pdf").notes[0]["message"] == "That file is empty."


def test_type_is_inferred_from_the_extension_when_the_browser_omits_it():
    result = extract_text(build_w2_pdf(), "", "payroll-w2.pdf")
    assert result.readable is True


# ---------------------------------------------------------------------------
# parsing what came out
# ---------------------------------------------------------------------------
def test_every_box_comes_out_of_a_payroll_pdf():
    extraction = extract_text(build_w2_pdf(), "application/pdf", "w2.pdf")
    form, confidence, warnings = parse_w2_text(extraction.text)

    assert confidence == 1.0 and warnings == []
    assert str(form.wages) == "118000.00"
    assert str(form.federal_withheld) == "14200.00"
    assert str(form.social_security_wages) == "126000.00"
    assert str(form.medicare_wages) == "126000.00"
    assert str(form.medicare_withheld) == "1827.00"
    assert form.tax_year == 2025
    assert form.employer_ein == "12-3456789"


def test_the_words_form_w2_do_not_become_a_box_12_entry():
    """"Form W-2" parsed as code W for $2 once, inventing an HSA contribution."""
    extraction = extract_text(build_w2_pdf(), "application/pdf", "w2.pdf")
    form, _, _ = parse_w2_text(extraction.text)
    assert set(form.box12) == {"D"}
    assert str(form.box12["D"]) == "8000.00"
    assert form.hsa_via_employer == 0


def test_the_state_line_is_read_from_its_row():
    extraction = extract_text(build_w2_pdf(), "application/pdf", "w2.pdf")
    form, _, _ = parse_w2_text(extraction.text)
    assert len(form.states) == 1
    line = form.states[0]
    assert line.state == "CA"
    assert str(line.state_wages) == "118000.00"
    assert str(line.state_withheld) == "6800.00"


def test_a_bare_integer_is_not_mistaken_for_an_amount():
    form, _, _ = parse_w2_text("2025 Form W-2\n15 State CA 16 wages 50,000.00 17 tax 2,100.00")
    assert form.states[0].state_wages != 15
    assert str(form.states[0].state_wages) == "50000.00"


# ---------------------------------------------------------------------------
# through the API
# ---------------------------------------------------------------------------
def _ready(client):
    from tests.test_tax_journey import auth, sign_in, verify_identity

    return verify_identity(client, sign_in(client))


def test_uploading_a_payroll_pdf_produces_a_usable_document(client):
    from tests.test_tax_journey import auth

    token = _ready(client)
    response = client.post(
        "/api/documents/w2/file",
        files={"file": ("w2.pdf", build_w2_pdf(), "application/pdf")},
        data={"tax_year": "2025"},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["extraction"]["method"] == "pdf_text"
    assert body["parse_confidence"] == 1.0
    assert body["status"] == "parsed"
    assert body["payload"]["box1_wages"] == "118000.00"
    assert body["state_code"] == "CA"


def test_an_uploaded_pdf_alone_is_enough_for_an_estimate(client):
    """The whole point: upload, then get a figure without typing anything."""
    from tests.test_tax_journey import auth

    token = _ready(client)
    client.post(
        "/api/documents/w2/file",
        files={"file": ("w2.pdf", build_w2_pdf(), "application/pdf")},
        data={"tax_year": "2025"},
        headers=auth(token),
    )
    estimate = client.post("/api/estimates", headers=auth(token), json={
        "tax_year": 2025, "method": "regular",
        "situation": {"filing_status": "single", "resident_state": "CA"},
    })
    assert estimate.status_code == 200, estimate.text
    body = estimate.json()
    assert float(body["federal"]["agi"]) == 118000.0
    assert [s["code"] for s in body["states"]] == ["CA"]


def test_an_unreadable_scan_still_says_so_through_the_api(client):
    from tests.test_tax_journey import auth

    token = _ready(client)
    response = client.post(
        "/api/documents/w2/file",
        files={"file": ("scan.png", b"\x89PNG\r\n\x1a\n fake", "image/png")},
        data={"tax_year": "2025"},
        headers=auth(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["parse_confidence"] == 0.0
    assert body["status"] == "needs_review"
    assert any("OCR" in w["message"] for w in body["warnings"])


# ---------------------------------------------------------------------------
# layout reading
# ---------------------------------------------------------------------------
def build_grid_w2_pdf() -> bytes:
    """A W-2 laid out as the real form is: a grid of boxes, two columns.

    The important property is that boxes sit side by side, so one visual line
    carries several labels. A reader that anchors to the start of a line gets
    every one of them wrong.
    """
    pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)

    def box(x, y, w, h, label, value, small=7):
        c.setLineWidth(0.6)
        c.rect(x, y, w, h)
        c.setFont("Helvetica", small)
        c.drawString(x + 3, y + h - 9, label)
        if value:
            c.setFont("Helvetica", 9)
            c.drawString(x + 5, y + 5, value)

    box(40, 640, 300, 46, "a  Employee's social security number", "123-45-6789")
    box(40, 586, 300, 48, "b  Employer identification number (EIN)", "12-3456789")
    box(40, 496, 300, 84, "c  Employer's name, address, and ZIP code", "")
    c.setFont("Helvetica", 9)
    c.drawString(45, 556, "NORTHWIND LOGISTICS LLC")
    c.drawString(45, 544, "1400 Harbor Parkway, Suite 210")
    box(40, 400, 300, 90, "e  Employee's first name and initial   Last name", "")
    c.setFont("Helvetica", 9)
    c.drawString(45, 460, "MARIA J")
    c.drawString(130, 460, "SANTOS-RIVERA")
    c.drawString(45, 440, "88 Cedar Street Apt 4B")

    rows = [
        ("1  Wages, tips, other compensation", "96,480.00",
         "2  Federal income tax withheld", "11,235.40"),
        ("3  Social security wages", "104,980.00",
         "4  Social security tax withheld", "6,508.76"),
        ("5  Medicare wages and tips", "104,980.00",
         "6  Medicare tax withheld", "1,522.21"),
        ("7  Social security tips", "", "8  Allocated tips", ""),
        ("9", "", "10  Dependent care benefits", "5,000.00"),
        ("11  Nonqualified plans", "", "12a  See instructions for box 12", "D  8,500.00"),
    ]
    y = 640
    for l1, v1, l2, v2 in rows:
        box(350, y, 115, 46, l1, v1)
        box(465, y, 115, 46, l2, v2)
        y -= 46
    box(350, y, 115, 46, "13  Statutory  Retirement  Third-party", "X  Retirement plan", small=6)
    y -= 98

    heads = ["15 State", "Employer's state ID number", "16 State wages, tips, etc.",
             "17 State income tax", "18 Local wages, tips, etc.", "19 Local income tax"]
    vals = ["CA", "123-4567-8", "96,480.00", "5,142.90", "", ""]
    xs = [40, 95, 210, 310, 395, 480]
    widths = [55, 115, 100, 85, 85, 60]
    for x, w, head, value in zip(xs, widths, heads, vals):
        box(x, y, w, 40, head, value, small=6)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y - 24, "Form W-2   Wage and Tax Statement                    2025")
    c.save()
    return buffer.getvalue()


def test_layout_reading_gets_every_money_box_from_a_grid_form():
    from taxvault.forms.w2_layout import read_w2_layout

    form, confidence, _ = read_w2_layout(build_grid_w2_pdf())
    assert confidence == 1.0
    assert str(form.wages) == "96480.00"
    assert str(form.federal_withheld) == "11235.40"
    assert str(form.social_security_wages) == "104980.00"
    assert str(form.social_security_withheld) == "6508.76"
    assert str(form.medicare_wages) == "104980.00"
    assert str(form.medicare_withheld) == "1522.21"
    assert str(form.dependent_care_benefits) == "5000.00"


def test_side_by_side_boxes_are_not_confused_for_each_other():
    """Boxes 3 and 4 share a line. Anchoring to the line start gave both the
    same figure, and Box 1 the figure from the identity box beside it."""
    from taxvault.forms.w2_layout import read_w2_layout

    form, _, _ = read_w2_layout(build_grid_w2_pdf())
    assert form.social_security_wages != form.social_security_withheld
    assert form.medicare_wages != form.medicare_withheld
    assert str(form.wages) == "96480.00"


def test_the_state_row_is_read_from_the_page(): 
    """Boxes 16 and 17 are adjacent on the page and far apart in a text dump.
    Reading the dump reported no state tax, which is a wrong estimate."""
    from taxvault.forms.w2_layout import read_w2_layout

    form, _, _ = read_w2_layout(build_grid_w2_pdf())
    assert len(form.states) == 1
    assert form.states[0].state == "CA"
    assert str(form.states[0].state_wages) == "96480.00"
    assert str(form.states[0].state_withheld) == "5142.90"


def test_names_come_off_the_form():
    from taxvault.forms.w2_layout import read_w2_layout

    form, _, _ = read_w2_layout(build_grid_w2_pdf())
    assert form.employer_name == "Northwind Logistics LLC"
    assert form.employee_first_name == "Maria J"
    assert form.employee_last_name == "Santos-Rivera"
    assert form.employer_ein == "12-3456789"


def test_an_address_line_is_never_mistaken_for_a_name():
    from taxvault.forms.w2_layout import read_w2_layout

    form, _, _ = read_w2_layout(build_grid_w2_pdf())
    assert "Harbor" not in form.employer_name
    assert "Cedar" not in (form.employee_first_name + form.employee_last_name)


@pytest.mark.parametrize("raw,expected", [
    ("NORTHWIND LOGISTICS LLC", "Northwind Logistics LLC"),
    ("ACME CORPORATION", "Acme Corporation"),
    ("SANTOS-RIVERA", "Santos-Rivera"),
    ("BANK OF THE WEST", "Bank of the West"),
    ("O'BRIEN AND SONS", "O'Brien and Sons"),
    ("Dana Reed", "Dana Reed"),
])
def test_names_are_cased_like_names_not_title_cased(raw, expected):
    """`str.title()` renders LLC as "Llc", which is how the employer's name
    came back looking wrong."""
    from taxvault.forms.layout import tidy_name

    assert tidy_name(raw) == expected


def test_a_grid_pdf_upload_produces_a_correct_state_estimate(client):
    """End to end: the state figures have to reach the estimate."""
    from tests.test_tax_journey import auth

    token = _ready(client)
    uploaded = client.post(
        "/api/documents/w2/file",
        files={"file": ("w2.pdf", build_grid_w2_pdf(), "application/pdf")},
        data={"tax_year": "2025"},
        headers=auth(token),
    ).json()
    assert uploaded["extraction"]["strategy"] == "layout"
    assert uploaded["employee_name"] == "Maria J Santos-Rivera"
    assert uploaded["employer_name"] == "Northwind Logistics LLC"

    estimate = client.post("/api/estimates", headers=auth(token), json={
        "tax_year": 2025, "method": "regular",
        "situation": {"filing_status": "single", "resident_state": "CA"},
    }).json()
    california = next(s for s in estimate["states"] if s["code"] == "CA")
    # The withholding from Box 17 must be in the state result, not zero.
    assert float(california["withheld"]) == 5142.90
    assert float(estimate["federal"]["agi"]) == 96480.0


def test_a_correction_replaces_what_was_read(client):
    """The read-back panel has to actually change the figures."""
    from tests.test_tax_journey import auth

    token = _ready(client)
    uploaded = client.post(
        "/api/documents/w2/file",
        files={"file": ("w2.pdf", build_grid_w2_pdf(), "application/pdf")},
        data={"tax_year": "2025"},
        headers=auth(token),
    ).json()

    fixed = client.patch(f"/api/documents/{uploaded['id']}", headers=auth(token), json={
        "tax_year": 2025,
        "employer_name": "Northwind Logistics LLC",
        "box1_wages": 99000,
        "box2_federal_withheld": 12000,
        "box3_social_security_wages": 99000,
        "box4_social_security_withheld": 6138,
        "box5_medicare_wages": 99000,
        "box6_medicare_withheld": 1435.50,
        "states": [{"state": "CA", "state_wages": 99000, "state_withheld": 5500}],
    })
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["payload"]["box1_wages"] == "99000.00"

    estimate = client.post("/api/estimates", headers=auth(token), json={
        "tax_year": 2025, "method": "regular",
        "situation": {"filing_status": "single", "resident_state": "CA"},
    }).json()
    assert float(estimate["federal"]["agi"]) == 99000.0
