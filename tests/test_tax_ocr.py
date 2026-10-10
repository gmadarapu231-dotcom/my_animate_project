"""Reading a W-2 that arrived as pixels.

Most clients photograph the form rather than downloading it from a payroll
portal, so this is the common path and not an edge case. Before OCR existed
the application took a scan, produced a W-2 with every box at zero, and
returned 200 -- an estimate that looked complete and quietly left out the
client's job.
"""

from __future__ import annotations

import io
import subprocess

import pytest

from taxvault.forms import ocr
from taxvault.forms.extract import extract_text
from taxvault.forms.w2_layout import read_w2_pages

pytest_plugins = ["tests.conftest_taxvault"]

needs_ocr = pytest.mark.skipif(
    not ocr.available()["images"],
    reason="Tesseract is not installed on this machine",
)
needs_render = pytest.mark.skipif(
    not ocr.available()["pdfs"],
    reason="No PDF rasteriser (poppler-utils) on this machine",
)

WAGES, WITHHELD = 88000.0, 9800.0
STATE_TAX = 3520.0


def _w2_pdf() -> bytes:
    from taxvault.agent.sandbox import w2_pdf

    return w2_pdf(employer="Bluegrass Freight LLC", ein="61-1234567",
                  first="Dana", last="Reed", year=2025, wages=WAGES,
                  withheld=WITHHELD, state="KY", state_wages=WAGES,
                  state_withheld=STATE_TAX)


def _flatten(pdf: bytes, *, rotate: float = -0.7, blur: float = 0.6):
    """Turn a text PDF into pixels, the way a scanner or a phone would.

    Rotated slightly, softened, and unevenly lit, because a W-2 that reads
    only when photographed perfectly does not read.
    """
    import random
    import tempfile
    from pathlib import Path

    from PIL import Image, ImageFilter

    with tempfile.TemporaryDirectory() as work:
        folder = Path(work)
        (folder / "in.pdf").write_bytes(pdf)
        subprocess.run(
            ["pdftoppm", "-png", "-r", "200", str(folder / "in.pdf"), str(folder / "p")],
            check=True, capture_output=True,
        )
        page = Image.open(sorted(folder.glob("p*.png"))[0]).convert("RGB")

    page = page.rotate(rotate, resample=Image.BICUBIC,
                       fillcolor=(255, 255, 255), expand=True)
    page = page.filter(ImageFilter.GaussianBlur(blur))
    pixels = page.load()
    random.seed(7)
    for y in range(0, page.height, 3):
        for x in range(0, page.width, 3):
            shade = int(18 * (x / page.width)) + random.randint(-7, 7)
            r, g, b = pixels[x, y]
            pixels[x, y] = (max(0, min(255, r - shade)),
                            max(0, min(255, g - shade)),
                            max(0, min(255, b - shade)))
    return page


@pytest.fixture(scope="module")
def scanned_pdf() -> bytes:
    page = _flatten(_w2_pdf())
    buffer = io.BytesIO()
    page.convert("RGB").save(buffer, "PDF", resolution=200)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def photo_png() -> bytes:
    buffer = io.BytesIO()
    _flatten(_w2_pdf()).save(buffer, "PNG")
    return buffer.getvalue()


# ------------------------------------------------------------ the premise
@needs_render
def test_the_scan_really_has_no_text_in_it(scanned_pdf):
    """Otherwise these tests would be passing on the text path."""
    from pypdf import PdfReader

    assert PdfReader(io.BytesIO(scanned_pdf)).pages[0].extract_text().strip() == ""


# --------------------------------------------------------------- reading
@needs_render
def test_a_scanned_w2_reads_every_box(scanned_pdf):
    form, confidence, _ = read_w2_pages(
        ocr.pages(scanned_pdf, content_type="application/pdf"))
    assert confidence == 1.0
    assert float(form.wages) == pytest.approx(WAGES, abs=1)
    assert float(form.federal_withheld) == pytest.approx(WITHHELD, abs=1)
    assert float(form.social_security_wages) == pytest.approx(WAGES, abs=1)
    assert float(form.social_security_withheld) == pytest.approx(WAGES * 0.062, abs=2)
    assert float(form.medicare_wages) == pytest.approx(WAGES, abs=1)


@needs_ocr
def test_a_phone_photo_reads_the_same_as_a_scan(photo_png):
    form, confidence, _ = read_w2_pages(ocr.pages(photo_png, content_type="image/png"))
    assert confidence == 1.0
    assert float(form.wages) == pytest.approx(WAGES, abs=1)
    assert float(form.federal_withheld) == pytest.approx(WITHHELD, abs=1)


@needs_render
def test_the_state_comes_off_the_form_not_from_somewhere_else(scanned_pdf):
    """The whole point of the complaint this was built for: a Kentucky W-2
    has to produce Kentucky."""
    form, _, _ = read_w2_pages(ocr.pages(scanned_pdf, content_type="application/pdf"))
    assert [line.state for line in form.states] == ["KY"]
    assert float(form.states[0].state_withheld) == pytest.approx(STATE_TAX, abs=1)
    assert float(form.states[0].state_wages) == pytest.approx(WAGES, abs=1)


@needs_render
def test_geometry_beats_flat_text(scanned_pdf):
    """A W-2 is a grid, and OCR hands back the label row and the value row as
    separate lines. Read flat, "1 Wages" picks up the 2 from the box next to
    it; this is what the positional read is for."""
    flat, _, _ = __import__(
        "taxvault.forms.w2", fromlist=["parse_w2_text"]
    ).parse_w2_text(ocr.read(scanned_pdf, content_type="application/pdf").text)
    positioned, _, _ = read_w2_pages(
        ocr.pages(scanned_pdf, content_type="application/pdf"))
    assert float(positioned.wages) == pytest.approx(WAGES, abs=1)
    assert float(flat.wages) != pytest.approx(WAGES, abs=1), (
        "the flat read happens to be right here, so this test proves nothing"
    )


@needs_render
def test_a_baseline_that_drifts_across_the_page_is_still_one_line(scanned_pdf):
    """A scan's baseline is not flat. Comparing each word against the start
    of its row dropped "security" from "4 Social security tax withheld",
    which left box 4 unread."""
    page = ocr.pages(scanned_pdf, content_type="application/pdf")[0]
    from taxvault.forms.layout import lines_of

    joined = [" ".join(w.text for w in row) for row in lines_of(page)]
    assert any("social security tax withheld" in line.lower() for line in joined), joined


# ------------------------------------------------------- the self-check
def test_the_form_is_tested_against_its_own_arithmetic():
    """A misread digit almost always breaks one of the payroll identities,
    which is a better check than any confidence score."""
    from taxvault.forms.w2 import W2

    good = W2.from_dict({
        "box1_wages": 88000, "box3_social_security_wages": 88000,
        "box4_social_security_withheld": 5456, "box5_medicare_wages": 88000,
        "box6_medicare_withheld": 1276,
    })
    assert ocr.cross_check(good, year=2025) == []

    misread = W2.from_dict({
        "box1_wages": 88000, "box3_social_security_wages": 88000,
        "box4_social_security_withheld": 8456,     # a 5 read as an 8
        "box5_medicare_wages": 88000, "box6_medicare_withheld": 1276,
    })
    boxes = [f["box"] for f in ocr.cross_check(misread, year=2025)]
    assert "4" in boxes


def test_a_box_ocr_lost_entirely_is_reported():
    from taxvault.forms.w2 import W2

    missing = W2.from_dict({
        "box1_wages": 88000, "box3_social_security_wages": 88000,
        "box5_medicare_wages": 88000, "box6_medicare_withheld": 1276,
    })
    assert "4" in [f["box"] for f in ocr.cross_check(missing, year=2025)]


def test_box_5_is_never_below_box_3():
    from taxvault.forms.w2 import W2

    wrong = W2.from_dict({
        "box1_wages": 88000, "box3_social_security_wages": 88000,
        "box4_social_security_withheld": 5456,
        "box5_medicare_wages": 8000, "box6_medicare_withheld": 116,
    })
    assert "5" in [f["box"] for f in ocr.cross_check(wrong, year=2025)]


# ----------------------------------------------------------- extraction
@needs_render
def test_extraction_falls_through_to_ocr_and_says_so(scanned_pdf):
    result = extract_text(scanned_pdf, "application/pdf", "scan.pdf")
    assert result.readable is True
    assert result.method == "ocr"
    assert 0.0 < result.confidence <= 1.0


@needs_render
def test_a_successful_read_does_not_also_complain_it_could_not_read(scanned_pdf):
    """The "no text layer" note is a step on the way, not a finding. Left
    beside the figures it tells a client their form could not be read,
    directly above the figures from it."""
    result = extract_text(scanned_pdf, "application/pdf", "scan.pdf")
    assert not [n for n in result.notes if "no text layer" in n["message"]]


@needs_render
def test_text_pdfs_still_take_the_text_path(scanned_pdf):
    """OCR is the fallback, not the default: it costs seconds per page and a
    payroll PDF needs none of it."""
    result = extract_text(_w2_pdf(), "application/pdf", "payroll.pdf")
    assert result.method == "pdf_text"
    assert result.confidence == 1.0


# ---------------------------------------------------------- through the API
def _ready(client):
    from tests.test_tax_journey import sign_in, verify_identity

    return verify_identity(client, sign_in(client))


@needs_render
def test_a_scanned_kentucky_w2_produces_a_kentucky_estimate(client, scanned_pdf):
    """The complaint this was built for, end to end: a scanned Kentucky W-2
    on an account that still says California."""
    from tests.test_tax_journey import auth

    token = _ready(client)        # registers the taxpayer in CA
    upload = client.post(
        "/api/documents/w2/file",
        files={"file": ("ky-scan.pdf", scanned_pdf, "application/pdf")},
        data={"tax_year": "2025"}, headers=auth(token),
    )
    assert upload.status_code == 200, upload.text
    body = upload.json()
    assert body["extraction"]["method"] == "ocr"
    assert float(body["payload"]["box1_wages"]) == pytest.approx(WAGES, abs=1)
    assert [s["state"] for s in body["payload"]["states"]] == ["KY"]

    # Anything read off pixels goes in front of a person, whatever it scored.
    assert body["status"] == "needs_review"

    estimate = client.post("/api/estimates", headers=auth(token),
                           json={"tax_year": 2025, "method": "regular"}).json()
    assert estimate["resident_state"] == "KY"
    assert [s["code"] for s in estimate["states"]] == ["KY"]
    assert any("KY" in w["message"] and "CA" in w["message"]
               for w in estimate["warnings"]), estimate["warnings"]
