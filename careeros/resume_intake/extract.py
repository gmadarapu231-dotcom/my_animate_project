"""Getting plain text out of a résumé file.

Stdlib-first, because a career tool that cannot read a .docx without a C
extension is not much use on a fresh machine:

    .txt .md .text   read directly
    .docx            a zip with `word/document.xml` inside -- unpacked here
    .pdf             the text layer via `pypdf` (or `pypdfium2`), both optional
                     extras. A PDF with no text layer -- a scan -- goes to OCR.
    .png .jpg ...    OCR (see `ocr.py`): tesseract locally, Claude as fallback.

When nothing can read the file, the error says exactly what to install and
offers the workarounds, rather than failing with an import trace.

Nothing here interprets the content. Parsing is `parse.py`'s job, and it works
on text from any of these paths identically.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from careeros.resume_intake.ocr import IMAGE_SUFFIXES, OcrUnavailable, ocr_text

TEXT_SUFFIXES = {".txt", ".md", ".text", ".rst"}
DOCX_SUFFIXES = {".docx", ".docm"}
PDF_SUFFIXES = {".pdf"}

SUPPORTED = sorted(TEXT_SUFFIXES | DOCX_SUFFIXES | PDF_SUFFIXES | IMAGE_SUFFIXES)

#: Below this many characters a PDF's text layer is treated as absent. Scans
#: often carry a stray page number or a "Scanned by" stamp and nothing else.
MIN_TEXT_LAYER = 200
MIN_RESUME_CHARS = 80

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MULTISPACE = re.compile(r"[ \t ]+")
_MULTIBREAK = re.compile(r"\n{3,}")


class ExtractionError(RuntimeError):
    """The file could not be read. The message is safe to show the user."""


def normalise(text: str) -> str:
    """Collapse whitespace without losing the line structure a parser needs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("•", "\n• ").replace("●", "\n• ")
    lines = [_MULTISPACE.sub(" ", line).strip() for line in text.split("\n")]
    return _MULTIBREAK.sub("\n\n", "\n".join(lines)).strip()


def _from_docx(path: Path) -> str:
    """Paragraphs and table cells, in document order.

    A .docx is a zip; `word/document.xml` holds the body. Walking it directly
    avoids a dependency, and handles the one thing naive extraction gets wrong:
    a `<w:br/>` or `<w:tab/>` between runs is real whitespace, and dropping it
    welds two bullets into one sentence.
    """
    try:
        with zipfile.ZipFile(path) as bundle:
            xml = bundle.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ExtractionError(
            f"{path.name} does not look like a Word document. "
            "If it was renamed from .doc, re-save it as .docx."
        ) from exc

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ExtractionError(
            f"{path.name} has a damaged document body ({exc}). "
            "Open it in Word and re-save it."
        ) from exc
    out: list[str] = []
    for paragraph in root.iter(f"{_W_NS}p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            tag = node.tag
            if tag == f"{_W_NS}t":
                pieces.append(node.text or "")
            elif tag in (f"{_W_NS}br", f"{_W_NS}cr"):
                pieces.append("\n")
            elif tag == f"{_W_NS}tab":
                pieces.append(" ")
        line = "".join(pieces).strip()
        if line:
            out.append(line)
    if not out:
        raise ExtractionError(f"{path.name} has no readable text in it.")
    return "\n".join(out)


def _pdf_text_layer(path: Path) -> str | None:
    """The PDF's embedded text, or None when no PDF library is installed."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        PdfReader = None  # noqa: N806

    if PdfReader is not None:
        try:
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except Exception as exc:  # pypdf raises a wide range on malformed files
            raise ExtractionError(f"Could not read {path.name}: {exc}") from exc

    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        return None
    try:
        document = pdfium.PdfDocument(str(path))
        try:
            pages = [document[i].get_textpage().get_text_range() for i in range(len(document))]
        finally:
            document.close()
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"Could not read {path.name}: {exc}") from exc
    return "\n".join(pages).strip()


def _ocr(path: Path, *, what: str) -> tuple[str, str]:
    try:
        text, engine = ocr_text(path)
    except OcrUnavailable as exc:
        raise ExtractionError(
            f"{path.name} is {what}, so it has to be read with OCR, and no OCR engine "
            f"is available here ({exc}).\n"
            "Or export the résumé as .docx, or paste the text in directly."
        ) from exc
    if not text.strip():
        raise ExtractionError(
            f"OCR found no text in {path.name}. Try a sharper scan or photo "
            "(flat, well lit, 300 dpi), or paste the text in directly."
        )
    return text, f"ocr:{engine}"


def _from_pdf(path: Path) -> tuple[str, str]:
    text = _pdf_text_layer(path)
    if text is not None and len(normalise(text)) >= MIN_TEXT_LAYER:
        return text, "pdf-text"
    try:
        return _ocr(path, what="a scanned PDF" if text is not None else "a PDF")
    except ExtractionError:
        if text is None:
            raise ExtractionError(
                "Reading a PDF résumé needs one extra package:\n"
                "    pip install 'careeros[resume]'\n"
                "Or export the résumé as .docx, or paste the text in directly -- "
                "both take the same path from here."
            ) from None
        if text:
            # A thin text layer is still better than nothing.
            return text, "pdf-text"
        raise


@dataclass
class Extraction:
    text: str
    method: str  # text | docx | pdf-text | ocr:tesseract | ocr:claude


def extract(path: str | Path) -> Extraction:
    """Plain text from a résumé file and how it was read, or `ExtractionError`."""
    file = Path(path)
    if not file.exists():
        raise ExtractionError(f"No file at {file}")
    if file.is_dir():
        raise ExtractionError(f"{file} is a directory, not a résumé.")

    suffix = file.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        raw, method = file.read_text(encoding="utf-8", errors="replace"), "text"
    elif suffix in DOCX_SUFFIXES:
        raw, method = _from_docx(file), "docx"
    elif suffix in PDF_SUFFIXES:
        raw, method = _from_pdf(file)
    elif suffix in IMAGE_SUFFIXES:
        raw, method = _ocr(file, what="an image")
    elif suffix in (".doc",):
        raise ExtractionError(
            "Old-format .doc is not readable without Word. Open it and "
            "'Save As' .docx, then try again."
        )
    else:
        raise ExtractionError(
            f"Cannot read {suffix or 'a file with no extension'}. "
            f"Supported: {', '.join(SUPPORTED)}."
        )

    text = normalise(raw)
    if len(text) < MIN_RESUME_CHARS:
        raise ExtractionError(
            f"{file.name} yielded only {len(text)} characters. "
            "That is too little to be a résumé -- check the file."
        )
    return Extraction(text=text, method=method)


def extract_text(path: str | Path) -> str:
    """Plain text from a résumé file, or `ExtractionError` saying why not."""
    return extract(path).text


def extract_upload(data: bytes, filename: str) -> Extraction:
    """`extract` for bytes that arrived over HTTP rather than from disk.

    Only the suffix of the client's filename is used, so a name like
    `../../etc/passwd.pdf` cannot steer where anything is written.
    """
    suffix = Path(filename or "").suffix.lower()
    if not data:
        raise ExtractionError("The uploaded file is empty.")
    with tempfile.TemporaryDirectory(prefix="careeros-upload-") as folder:
        target = Path(folder) / f"resume{suffix}"
        target.write_bytes(data)
        return extract(target)
