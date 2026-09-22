"""Getting plain text out of a résumé file.

Stdlib-first, because a career tool that cannot read a .docx without a C
extension is not much use on a fresh machine:

    .txt .md .text   read directly
    .docx            a zip with `word/document.xml` inside -- unpacked here
    .pdf             needs `pypdf`, which is an optional extra. Without it the
                     error says exactly what to install and offers the two
                     workarounds, rather than failing with an import trace.

Nothing here interprets the content. Parsing is `parse.py`'s job, and it works
on text from any of these paths identically.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TEXT_SUFFIXES = {".txt", ".md", ".text", ".rst"}
DOCX_SUFFIXES = {".docx", ".docm"}
PDF_SUFFIXES = {".pdf"}

SUPPORTED = sorted(TEXT_SUFFIXES | DOCX_SUFFIXES | PDF_SUFFIXES)

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


def _from_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:
        raise ExtractionError(
            "Reading a PDF résumé needs one extra package:\n"
            "    pip install 'careeros[resume]'\n"
            "Or export the résumé as .docx, or paste the text in directly -- "
            "both take the same path from here."
        ) from exc

    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises a wide range on malformed files
        raise ExtractionError(f"Could not read {path.name}: {exc}") from exc

    text = "\n".join(pages).strip()
    if not text:
        raise ExtractionError(
            f"{path.name} has no extractable text -- it is probably a scan. "
            "Run it through OCR, or paste the text in directly."
        )
    return text


def extract_text(path: str | Path) -> str:
    """Plain text from a résumé file, or `ExtractionError` saying why not."""
    file = Path(path)
    if not file.exists():
        raise ExtractionError(f"No file at {file}")
    if file.is_dir():
        raise ExtractionError(f"{file} is a directory, not a résumé.")

    suffix = file.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        raw = file.read_text(encoding="utf-8", errors="replace")
    elif suffix in DOCX_SUFFIXES:
        raw = _from_docx(file)
    elif suffix in PDF_SUFFIXES:
        raw = _from_pdf(file)
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
    if len(text) < 80:
        raise ExtractionError(
            f"{file.name} yielded only {len(text)} characters. "
            "That is too little to be a résumé -- check the file."
        )
    return text
