"""Reading a résumé that is a picture: a scan, a phone photo, a printed PDF.

Two engines, tried in order, so the result does not depend on one install:

    tesseract   local and free. Needs the `tesseract` binary plus the
                `ocr` extra (pytesseract, Pillow, pypdfium2 to render PDF pages).
    claude      the configured Anthropic provider reading the image or PDF
                directly. Used when tesseract is missing or reads nothing.

Each engine returns text or raises `OcrUnavailable` saying what is missing.
When neither works the caller gets one message listing both fixes, which is
the difference between "install X" and an import trace.
"""

from __future__ import annotations

import base64
import logging
import shutil
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}

#: Pages past this are ignored. A résumé is one to three pages; a 40-page scan
#: is almost certainly the wrong file and would take minutes to OCR.
MAX_OCR_PAGES = 6

#: 300 dpi is tesseract's sweet spot for body text.
RENDER_SCALE = 300 / 72

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class OcrUnavailable(RuntimeError):
    """This engine cannot run here. The message says what would fix it."""


# ---------------------------------------------------------------------------
# tesseract
# ---------------------------------------------------------------------------
def _tesseract_ready() -> None:
    try:
        import pytesseract  # type: ignore  # noqa: F401
        from PIL import Image  # type: ignore  # noqa: F401
    except ImportError as exc:
        raise OcrUnavailable("pip install 'careeros[ocr]'") from exc
    if shutil.which("tesseract") is None:
        raise OcrUnavailable(
            "install the tesseract program (macOS: `brew install tesseract`, "
            "Ubuntu: `sudo apt install tesseract-ocr`, Windows: the UB-Mannheim installer)"
        )


def _pdf_pages(path: Path):
    """PDF pages as PIL images, rendered at OCR resolution."""
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError as exc:
        raise OcrUnavailable("pip install 'careeros[ocr]' (pypdfium2 renders PDF pages)") from exc
    document = pdfium.PdfDocument(str(path))
    try:
        for index in range(min(len(document), MAX_OCR_PAGES)):
            page = document[index]
            yield page.render(scale=RENDER_SCALE).to_pil()
    finally:
        document.close()


def _prepare(image):
    """Greyscale and upscale small photos; tesseract reads both far better."""
    from PIL import ImageOps  # type: ignore

    image = ImageOps.exif_transpose(image)
    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    image = ImageOps.grayscale(image)
    if image.width < 1600:
        factor = 1600 / max(image.width, 1)
        image = image.resize((int(image.width * factor), int(image.height * factor)))
    return image


def tesseract_text(path: Path) -> str:
    _tesseract_ready()
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore

    if path.suffix.lower() == ".pdf":
        images = list(_pdf_pages(path))
    else:
        with Image.open(path) as opened:
            images = [opened.copy()]
            # Multi-frame TIFFs: every frame is a page.
            frames = getattr(opened, "n_frames", 1)
            for frame in range(1, min(frames, MAX_OCR_PAGES)):
                opened.seek(frame)
                images.append(opened.copy())

    pages = [
        pytesseract.image_to_string(_prepare(image), config="--psm 3")
        for image in images
    ]
    return "\n\n".join(page.strip() for page in pages if page.strip())


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------
_TRANSCRIBE = (
    "Transcribe this résumé exactly as written. Plain text only: keep the line "
    "order, put each bullet on its own line starting with '• ', and keep section "
    "headings on their own lines. Do not summarise, correct, reorder or add "
    "anything that is not on the page."
)


def _image_block(path: Path) -> dict:
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if suffix == ".pdf":
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(data).decode(),
            },
        }
    media_type = _MEDIA_TYPES.get(suffix)
    if media_type is None:
        # TIFF/BMP: the API takes PNG, so convert when Pillow is around.
        try:
            import io

            from PIL import Image  # type: ignore
        except ImportError as exc:
            raise OcrUnavailable(f"convert the {suffix} to PNG or JPEG first") from exc
        with Image.open(path) as image:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
        data, media_type = buffer.getvalue(), "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode(),
        },
    }


def claude_text(path: Path) -> str:
    from careeros.ai.provider import get_provider

    provider = get_provider()
    client = getattr(provider, "client", None)
    if not getattr(provider, "available", False) or client is None:
        raise OcrUnavailable("set ANTHROPIC_API_KEY to let Claude read the scan")
    response = client.messages.create(
        model=provider.model,
        max_tokens=8000,
        messages=[
            {
                "role": "user",
                "content": [_image_block(path), {"type": "text", "text": _TRANSCRIBE}],
            }
        ],
    )
    if getattr(response, "stop_reason", None) == "refusal":
        return ""
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------
Engine = Callable[[Path], str]

ENGINES: list[tuple[str, Engine]] = [("tesseract", tesseract_text), ("claude", claude_text)]


def ocr_text(path: str | Path, engines: list[tuple[str, Engine]] | None = None) -> tuple[str, str]:
    """(text, engine name) from the first engine that reads anything.

    Raises `OcrUnavailable` listing every fix when no engine could run, and
    returns ("", "") when engines ran but the page really is blank.
    """
    file = Path(path)
    missing: list[str] = []
    ran = False
    for name, engine in engines or ENGINES:
        try:
            text = engine(file)
        except OcrUnavailable as exc:
            missing.append(f"{name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one engine failing is not the end
            logger.warning("OCR engine %s failed on %s: %s", name, file.name, exc)
            missing.append(f"{name}: failed ({type(exc).__name__}: {exc})")
            continue
        ran = True
        if text and text.strip():
            return text, name
    if not ran:
        raise OcrUnavailable("; ".join(missing))
    return "", ""
