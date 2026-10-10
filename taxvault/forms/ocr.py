"""Reading a W-2 that arrived as pixels.

Most clients do not download a PDF from a payroll portal. They photograph the
form on the kitchen table, or scan it at a library, and what arrives is an
image with no text in it at all. Before this module the application answered
that by storing the file and producing a W-2 with every box at zero -- which
is the single most dangerous thing it could do, because an estimate built on
zeros looks like an estimate.

So: rasterise the page, run Tesseract over it, and hand the text to the same
parser a payroll PDF goes through. Three decisions are worth stating.

**OCR output is a draft, never a figure.** A character-recognition engine
reading a dense grid of digits will sometimes give you 8 for 3, or lose a
decimal point. Anything read this way comes back marked, at the confidence
the engine reported, and the caller is expected to put it in front of a
person. The read-back panel already exists for exactly this.

**A W-2 checks itself.** Box 4 should be 6.2% of box 3; box 6 should be 1.45%
of box 5 plus the additional Medicare tax; boxes 3 and 5 relate to box 1 in
known ways. Those identities turn a pile of OCR'd digits into something that
can be tested, and `cross_check` reports what fails rather than trusting the
engine. A misread digit usually breaks one of them.

**Subprocess, not a library binding.** Poppler's `pdftoppm` renders the page
and Tesseract reads it, both as separate programs. That keeps this file clear
of PyMuPDF, whose AGPL terms would reach a practice's own source, and keeps
the dependency a package manager can satisfy.

Nothing here raises for a bad file. An unreadable upload is an ordinary
Tuesday, and the answer to it is a sentence, not a 500.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Rendering resolution. Tesseract wants roughly 300 DPI for body text; a W-2's
#: box figures are smaller than body text, so going below this loses decimal
#: points, and going far above mostly costs time.
DPI = 300

#: A tax form is one or two pages. A client who attaches their whole document
#: folder should not tie up a worker rendering forty pages.
MAX_PAGES = 4

#: Tesseract's mean word confidence, 0-100, below which the read is not worth
#: showing as figures. Chosen from what a genuinely unreadable page scores
#: (noise returns in the teens and twenties) rather than from a round number.
MIN_CONFIDENCE = 45.0

#: How long the two external programs get. A hung render must not hold a
#: request open.
RENDER_TIMEOUT = 60
OCR_TIMEOUT = 60


class OcrUnavailable(RuntimeError):
    """Tesseract or the PDF rasteriser is not installed on this machine."""


@dataclass
class OcrResult:
    text: str = ""
    confidence: float = 0.0        # 0-1, the mean word confidence
    pages: int = 0
    engine: str = ""
    readable: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text_length": len(self.text), "confidence": round(self.confidence, 3),
            "pages": self.pages, "engine": self.engine, "readable": self.readable,
            "notes": self.notes,
        }


# ===========================================================================
# Is it even possible here?
# ===========================================================================
def _tesseract() -> str | None:
    return shutil.which(os.getenv("TAXVAULT_TESSERACT", "tesseract"))


def _rasteriser() -> str | None:
    """Poppler renders the page. `pdftoppm` is the one that ships everywhere."""
    return shutil.which("pdftoppm") or shutil.which("pdftocairo")


def available() -> dict[str, Any]:
    """What this machine can actually do, for a readiness check to report."""
    engine, render = _tesseract(), _rasteriser()
    try:
        import pytesseract  # noqa: F401
        binding = True
    except ImportError:
        binding = False
    return {
        "tesseract": engine or "",
        "rasteriser": render or "",
        "binding": binding,
        "images": bool(engine and binding),
        "pdfs": bool(engine and binding and render),
    }


# ===========================================================================
# Reading
# ===========================================================================
def _prepare(image: Any) -> Any:
    """Make a phone photo legible to an OCR engine.

    Greyscale, because colour tells a character recogniser nothing; then
    autocontrast, which is what rescues a page photographed in a dim kitchen.
    Deliberately conservative: aggressive thresholding wins on clean scans and
    destroys the thin strokes in a decimal point on a soft one.
    """
    from PIL import Image, ImageOps

    if image.mode != "L":
        image = image.convert("L")
    image = ImageOps.autocontrast(image, cutoff=1)
    # Tesseract does better with more pixels per character than a phone gives.
    if min(image.size) < 1400:
        factor = 1400 / max(1, min(image.size))
        image = image.resize(
            (int(image.width * factor), int(image.height * factor)),
            Image.LANCZOS,
        )
    return image


def _read_image(image: Any) -> tuple[str, float]:
    """Text and mean word confidence from one page."""
    import pytesseract
    from pytesseract import Output

    # psm 6 -- "a single uniform block of text". A W-2 is a grid, and the
    # layout-detection modes above this one split it into columns and shuffle
    # the reading order, which is how a box 2 figure ends up beside box 16.
    config = "--oem 1 --psm 6"
    data = pytesseract.image_to_data(
        _prepare(image), config=config, output_type=Output.DICT,
        timeout=OCR_TIMEOUT,
    )
    words, scores = [], []
    for word, score in zip(data.get("text", []), data.get("conf", [])):
        try:
            value = float(score)
        except (TypeError, ValueError):
            continue
        if value < 0 or not str(word).strip():
            continue        # -1 marks a layout box that holds no word
        words.append(str(word))
        scores.append(value)
    text = pytesseract.image_to_string(
        _prepare(image), config=config, timeout=OCR_TIMEOUT
    )
    mean = sum(scores) / len(scores) if scores else 0.0
    return text, mean


#: PostScript points per inch. The layout reader's box geometry is written in
#: points because that is what a PDF uses, so an OCR'd page is converted into
#: the same space and every constant downstream keeps working.
POINTS_PER_INCH = 72.0


def _positioned(image: Any, *, page_number: int, dpi: int) -> Any:
    """One rendered page as positioned words, in PDF point space.

    This is the function that makes a photographed W-2 as readable as a
    payroll download. Tesseract reports a bounding box for every word; turned
    into the same coordinates pypdf reports, the existing grid reader finds
    box 1's figure under box 1's label instead of picking up whatever number
    came next in a flattened dump.

    Two conversions matter. Pixels become points, because the reader's notion
    of "a W-2 box is 118 wide and 46 deep" is in points. And the y axis is
    flipped: an image counts down from the top, a PDF counts up from the
    bottom, and the reader's "below the label" test is written for the latter.
    """
    import pytesseract
    from pytesseract import Output

    from taxvault.forms.layout import Page, Word

    prepared = _prepare(image)
    # _prepare may have scaled the image up; the scale back to the original
    # page size has to follow it or every coordinate is wrong.
    scale = prepared.width / max(1, image.width)
    data = pytesseract.image_to_data(
        prepared, config="--oem 1 --psm 6", output_type=Output.DICT,
        timeout=OCR_TIMEOUT,
    )
    height_points = (image.height / dpi) * POINTS_PER_INCH

    words: list[Any] = []
    count = len(data.get("text", []))
    for index in range(count):
        text = str(data["text"][index]).strip()
        if not text:
            continue
        try:
            if float(data["conf"][index]) < 0:
                continue
        except (TypeError, ValueError, KeyError):
            continue
        left = data["left"][index] / scale
        top = data["top"][index] / scale
        word_height = data["height"][index] / scale
        x_points = (left / dpi) * POINTS_PER_INCH
        # Anchor on the word's BASELINE, near the bottom of its box, so that a
        # label and the figure under it differ by the real gap rather than by
        # the gap plus a line height.
        y_points = height_points - (((top + word_height) / dpi) * POINTS_PER_INCH)
        words.append(Word(text=text, x=x_points, y=y_points, page=page_number))

    return Page(number=page_number, words=_snap_rows(words), height=height_points)

#: How far apart two baselines can be and still be the same printed line.
#: A PDF reports exact baselines, so the reader downstream uses 3 points. OCR
#: measures them off pixels and wobbles by more than that: on a real scan the
#: word "security" came back 3.6 points below the rest of its own line, which
#: split "4 Social security tax withheld" into two rows and left box 4 unread.
ROW_TOLERANCE = 7.0


def _snap_rows(words: list[Any]) -> list[Any]:
    """Put words that share a printed line on exactly the same baseline.

    The grid reader groups words into lines before matching a label, and a
    label broken across two lines does not match. Rather than loosening that
    tolerance for every caller -- where it would start merging genuinely
    adjacent rows on a tight payroll PDF -- the wobble is taken out here, in
    the one place that knows the coordinates came from pixels.
    """
    from dataclasses import replace

    if not words:
        return words
    out: list[Any] = []
    for cluster in _clusters(sorted(words, key=lambda w: -w.y)):
        # The median is steadier than the mean when one word in the row sits
        # low because it has a descender.
        ys = sorted(word.y for word in cluster)
        baseline = ys[len(ys) // 2]
        out.extend(replace(word, y=baseline) for word in cluster)
    return out


def _clusters(ordered: list[Any]) -> list[list[Any]]:
    """Group words into printed lines, allowing a baseline to drift.

    A scan's baseline is not flat: across the width of a page the same
    printed line can fall several points, so each word is compared against
    the one most recently added rather than against the row's first word.
    On the scan that exposed this, "security" sat 3.4 points below the word
    before it but 6.8 below the start of its line -- near enough to the
    tolerance either way that comparing against the start dropped it, which
    broke the label "4 Social security tax withheld" and left box 4 unread.

    The total spread is still bounded, so a drifting row cannot chain all
    the way into the one beneath it.
    """
    rows: list[list[Any]] = []
    for word in ordered:
        if rows:
            edge = rows[-1][-1].y
            start = rows[-1][0].y
            if (abs(edge - word.y) <= ROW_TOLERANCE
                    and abs(start - word.y) <= ROW_TOLERANCE * 1.8):
                rows[-1].append(word)
                continue
        rows.append([word])
    return rows


def pages(blob: bytes, *, content_type: str = "", dpi: int = DPI,
          max_pages: int = MAX_PAGES) -> list[Any]:
    """Positioned words for every page of an upload. Empty when it cannot read."""
    if not blob or not available()["images"]:
        return []
    kind = (content_type or "").split(";")[0].strip().lower()
    is_pdf = kind == "application/pdf" or blob[:5] == b"%PDF-"
    try:
        from PIL import Image

        if is_pdf:
            images = _pdf_pages(blob, dpi=dpi, limit=max_pages)
        else:
            with Image.open(io.BytesIO(blob)) as handle:
                images = [handle.copy()]
            dpi = _guess_dpi(images[0])
    except Exception as exc:
        logger.info("ocr could not rasterise the upload: %s", exc)
        return []

    out = []
    for number, image in enumerate(images[:max_pages], 1):
        try:
            out.append(_positioned(image, page_number=number, dpi=dpi))
        except Exception as exc:
            logger.info("ocr could not position page %s: %s", number, exc)
    return out


def _guess_dpi(image: Any) -> float:
    """What resolution a photograph was effectively taken at.

    A phone photo carries no meaningful DPI tag, so it is inferred from the
    width, on the assumption the form fills the frame: a W-2 is 8.5 inches
    across. Only the ratio of the page's size to the box geometry matters, so
    this needs to be about right rather than exact.
    """
    reported = (image.info or {}).get("dpi")
    if reported:
        try:
            value = float(reported[0])
            if 72 <= value <= 1200:
                return value
        except (TypeError, ValueError, IndexError):
            pass
    return max(72.0, image.width / 8.5)


def _pdf_pages(blob: bytes, *, dpi: int, limit: int) -> list[Any]:
    """Render a PDF to images with Poppler, in a directory of its own."""
    from PIL import Image

    renderer = _rasteriser()
    if not renderer:
        raise OcrUnavailable(
            "No PDF rasteriser. Install poppler-utils (pdftoppm) to read "
            "scanned PDFs; photographs still work without it."
        )
    with tempfile.TemporaryDirectory(prefix="taxvault-ocr-") as work:
        folder = Path(work)
        (folder / "in.pdf").write_bytes(blob)
        command = [
            renderer, "-png", "-r", str(dpi),
            "-f", "1", "-l", str(limit),
            str(folder / "in.pdf"), str(folder / "page"),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True,
                           timeout=RENDER_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise OcrUnavailable("Rendering that PDF took too long.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace")[:200]
            raise OcrUnavailable(f"That PDF could not be rendered. {detail}") from exc

        images = []
        for path in sorted(folder.glob("page*.png")):
            with Image.open(path) as handle:
                images.append(handle.copy())
        return images


def read(blob: bytes, *, content_type: str = "", dpi: int = DPI,
         max_pages: int = MAX_PAGES) -> OcrResult:
    """OCR an uploaded PDF or image to plain text. Never raises for a bad file.

    The flat text is the fallback. `pages` is the one to prefer for a form,
    because a W-2 read without its geometry puts the figure from the next box
    along into whichever box was asked for.
    """
    result = OcrResult(engine="tesseract")
    if not blob:
        result.notes.append("The file is empty.")
        return result

    capability = available()
    if not capability["images"]:
        missing = "Tesseract" if not capability["tesseract"] else "the pytesseract package"
        result.notes.append(
            f"{missing} is not installed on this server, so a photographed or "
            "scanned form cannot be read here."
        )
        return result

    kind = (content_type or "").split(";")[0].strip().lower()
    is_pdf = kind == "application/pdf" or blob[:5] == b"%PDF-"

    try:
        from PIL import Image

        if is_pdf:
            images = _pdf_pages(blob, dpi=dpi, limit=max_pages)
        else:
            with Image.open(io.BytesIO(blob)) as handle:
                images = [handle.copy()]
    except OcrUnavailable as exc:
        result.notes.append(str(exc))
        return result
    except Exception as exc:                       # a corrupt upload
        logger.info("ocr could not open the upload: %s", exc)
        result.notes.append("That file could not be opened as a page image.")
        return result

    if not images:
        result.notes.append("That file holds no pages.")
        return result

    chunks, scores = [], []
    for index, image in enumerate(images[:max_pages], 1):
        try:
            text, confidence = _read_image(image)
        except Exception as exc:
            logger.info("ocr failed on page %s: %s", index, exc)
            result.notes.append(f"Page {index} could not be read.")
            continue
        if text.strip():
            chunks.append(text)
            scores.append(confidence)

    result.pages = len(images)
    result.text = "\n".join(chunks)
    result.confidence = (sum(scores) / len(scores) / 100.0) if scores else 0.0

    if not result.text.strip():
        result.notes.append(
            "Nothing legible came off that image. A straight-on photo in good "
            "light, filling the frame, usually reads; a photo at an angle "
            "usually does not."
        )
        return result

    if result.confidence * 100 < MIN_CONFIDENCE:
        result.notes.append(
            f"The text came back at {result.confidence:.0%} confidence, which is "
            "too low to put figures on a tax return. Check every box against "
            "the form, or type them in."
        )
        return result

    result.readable = True
    result.notes.append(
        f"Read by OCR at {result.confidence:.0%} confidence. Every figure needs "
        "checking against the form before it is filed -- a character reader "
        "mistakes 3 for 8, and a tax return is not the place to find out."
    )
    return result


# ===========================================================================
# Does the form agree with itself?
# ===========================================================================
#: Tolerance on the payroll identities. Rounding on a real W-2 is cents, but
#: an employer with mid-year payroll corrections can be a dollar or two out,
#: and flagging that as an OCR error sends people hunting for nothing.
TOLERANCE = Decimal("2.00")


def cross_check(form: Any, *, year: int | None = None) -> list[dict[str, str]]:
    """Test a W-2 against the arithmetic a real W-2 obeys.

    This is what makes OCR usable rather than merely available. A misread
    digit almost always breaks one of these identities, so a form that passes
    them all has been checked by something better than a confidence score,
    and one that fails names the box to look at.
    """
    from taxvault.money import ZERO, money

    findings: list[dict[str, str]] = []

    def add(box: str, message: str) -> None:
        findings.append({"severity": "warning", "box": box, "message": message})

    ss_wages = money(getattr(form, "social_security_wages", 0) or 0)
    ss_withheld = money(getattr(form, "social_security_withheld", 0) or 0)
    med_wages = money(getattr(form, "medicare_wages", 0) or 0)
    med_withheld = money(getattr(form, "medicare_withheld", 0) or 0)
    wages = money(getattr(form, "wages", 0) or 0)

    if ss_wages > ZERO and ss_withheld > ZERO:
        expected = (ss_wages * Decimal("0.062")).quantize(Decimal("0.01"))
        if abs(ss_withheld - expected) > max(TOLERANCE, expected * Decimal("0.02")):
            add("4", f"Box 4 is {ss_withheld:,.2f} but 6.2% of box 3 is "
                     f"{expected:,.2f}. One of the two was probably misread.")

    if med_wages > ZERO and med_withheld > ZERO:
        expected = (med_wages * Decimal("0.0145")).quantize(Decimal("0.01"))
        extra = max(ZERO, med_wages - Decimal("200000")) * Decimal("0.009")
        expected += extra.quantize(Decimal("0.01"))
        if abs(med_withheld - expected) > max(TOLERANCE, expected * Decimal("0.02")):
            add("6", f"Box 6 is {med_withheld:,.2f} but 1.45% of box 5"
                     + (" plus the additional Medicare tax" if extra > ZERO else "")
                     + f" is {expected:,.2f}. One of the two was probably misread.")

    # Box 5 has no wage base, so it is never below box 3 on a correct form.
    if ss_wages > ZERO and med_wages > ZERO and med_wages + TOLERANCE < ss_wages:
        add("5", f"Box 5 ({med_wages:,.2f}) is below box 3 ({ss_wages:,.2f}). "
                 "Medicare wages have no cap, so box 5 is never the smaller.")

    if year is not None and ss_wages > ZERO:
        try:
            from taxvault.config import federal

            base = federal(year).amount("payroll", "social_security_wage_base")
            if base and ss_wages > base + TOLERANCE:
                add("3", f"Box 3 is {ss_wages:,.2f}, above the {base:,.0f} Social "
                         f"Security wage base for {year}. Box 3 cannot exceed it.")
        except Exception:
            pass

    # A box that OCR simply lost reads as zero, which no real payroll produces
    # beside a populated wage box.
    if ss_wages > ZERO and ss_withheld == ZERO:
        add("4", "Box 4 came back empty beside Social Security wages of "
                 f"{ss_wages:,.2f}. Withholding of about "
                 f"{(ss_wages * Decimal('0.062')).quantize(Decimal('0.01')):,.2f} "
                 "is expected; check the form.")
    if med_wages > ZERO and med_withheld == ZERO:
        add("6", "Box 6 came back empty beside Medicare wages of "
                 f"{med_wages:,.2f}. Check the form.")

    if wages > ZERO and ss_wages > ZERO and wages > ss_wages * Decimal("1.5"):
        add("1", f"Box 1 ({wages:,.2f}) is far above box 3 ({ss_wages:,.2f}). "
                 "That happens, but it is unusual enough to check.")

    return findings
