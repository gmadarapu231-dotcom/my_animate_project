"""Getting text out of an uploaded document.

Three kinds of upload arrive, and they deserve three different answers:

* **A text PDF** -- what every payroll portal produces (ADP, Workday, Gusto,
  Paychex). The text is already in the file; it only has to be pulled out.
  This is the common case and it is handled, so uploading a W-2 downloaded
  from a payroll site produces figures rather than homework.
* **A scanned PDF or a photo** -- pixels, no text. Reading it needs OCR, which
  this server does not have. Saying so plainly is the only honest answer;
  pretending otherwise would put an estimate on figures nobody read.
* **Plain text** -- pasted or exported. Decoded and used as-is.

`extract_text` never raises on a bad file. A corrupt PDF is a thing a client
will hand you, and the right response is a sentence explaining it, not a 500.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

#: Below this many characters, a PDF is almost certainly a scan with a few
#: stray glyphs (a header, a page number) rather than a readable document.
MIN_MEANINGFUL_CHARS = 120

PDF_TYPES = {"application/pdf"}
TEXT_TYPES = {"text/plain", "text/csv"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/heic", "image/tiff", "image/webp"}


@dataclass
class Extraction:
    text: str = ""
    method: str = "none"          # pdf_text | plain_text | none
    readable: bool = False
    pages: int = 0
    notes: list[dict[str, str]] = field(default_factory=list)

    def note(self, severity: str, message: str) -> None:
        self.notes.append({"severity": severity, "box": "", "message": message})

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "readable": self.readable,
                "pages": self.pages, "notes": self.notes}


def _pdf_text(blob: bytes, result: Extraction) -> None:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf ships as a dependency
        result.note("warning",
                    "This server cannot read PDFs: the pypdf package is not installed. "
                    "Enter the boxes by hand.")
        return

    try:
        reader = PdfReader(io.BytesIO(blob))
    except Exception as exc:
        result.note("error",
                    f"That PDF could not be opened ({type(exc).__name__}). It may be "
                    "corrupt or password-protected. Try re-downloading it from your "
                    "payroll site, or enter the boxes by hand.")
        return

    if getattr(reader, "is_encrypted", False):
        # An empty password covers the common "protected but not locked" case.
        try:
            reader.decrypt("")
        except Exception:
            result.note("error",
                        "That PDF is password-protected. Remove the password, or enter "
                        "the boxes by hand.")
            return

    result.pages = len(reader.pages)
    chunks: list[str] = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(chunks).strip()

    if len(text) < MIN_MEANINGFUL_CHARS:
        result.note("warning",
                    "This PDF holds no readable text, which means it is a scan or a "
                    "photograph saved as a PDF. Reading it needs OCR, which this server "
                    "does not run. The file is stored securely — enter the boxes by hand "
                    "and the estimate will follow.")
        return

    result.text = text
    result.method = "pdf_text"
    result.readable = True


def extract_text(blob: bytes, content_type: str = "", filename: str = "") -> Extraction:
    """Pull whatever text an upload contains, and say how it went."""
    result = Extraction()
    kind = (content_type or "").split(";")[0].strip().lower()
    name = (filename or "").lower()

    # Browsers are inconsistent about the type they report, so fall back to the
    # file's own extension rather than refusing a perfectly good PDF.
    if not kind:
        if name.endswith(".pdf"):
            kind = "application/pdf"
        elif name.endswith((".txt", ".csv")):
            kind = "text/plain"

    if not blob:
        result.note("error", "That file is empty.")
        return result

    if kind in PDF_TYPES or blob[:5] == b"%PDF-":
        _pdf_text(blob, result)
        return result

    if kind in TEXT_TYPES or kind.startswith("text/"):
        result.text = blob.decode("utf-8", errors="replace")
        result.method = "plain_text"
        result.readable = len(result.text.strip()) >= 20
        if not result.readable:
            result.note("warning", "That file holds almost no text.")
        return result

    if kind in IMAGE_TYPES:
        result.note("warning",
                    "A photograph cannot be read without OCR, which this server does not "
                    "run. The image is stored securely — enter the boxes by hand, which "
                    "takes about a minute and is more accurate than any scan.")
        return result

    result.note("warning",
                f"{kind or 'That file type'} is stored but was not read. Enter the boxes "
                "by hand to produce an estimate.")
    return result


# ---------------------------------------------------------------------------
# layout repair
# ---------------------------------------------------------------------------
#: A W-2 is a grid, and pulling text out of a grid loses the grid. Extractors
#: commonly emit every label first and then every value, so a label and its
#: amount end up paragraphs apart. These are the box labels, in form order, so
#: a run of bare amounts can be matched back onto them.
BOX_SEQUENCE = [
    (1, "wages"), (2, "federal_withheld"), (3, "social_security_wages"),
    (4, "social_security_withheld"), (5, "medicare_wages"), (6, "medicare_withheld"),
    (7, "social_security_tips"), (8, "allocated_tips"),
]

_AMOUNT = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})(?![\d])")


def pair_orphan_amounts(text: str) -> str:
    """Re-attach amounts to box labels when extraction separated them.

    Only used when the ordinary label-and-value parse comes back poor. It looks
    for a run of at least four bare money amounts and, if the surrounding text
    shows the form is a W-2, writes them back out in `1 Wages ... <amount>`
    form so the normal parser can read them.

    This is a repair, not a guess: it requires the amounts to be consecutive
    and in form order, and the caller lowers the confidence accordingly.
    """
    lowered = text.lower()
    if "w-2" not in lowered and "w2" not in lowered and "wage and tax" not in lowered:
        return ""

    amounts = _AMOUNT.findall(text)
    if len(amounts) < 4:
        return ""

    rebuilt = []
    for (number, _field), amount in zip(BOX_SEQUENCE, amounts):
        rebuilt.append(f"{number} box {amount}")
    return "\n".join(rebuilt)
