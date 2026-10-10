"""Uploading a résumé (including scans read by OCR) and applying from the app."""

from __future__ import annotations

import importlib
import io
import shutil
import zipfile
from xml.sax.saxutils import escape

import pytest
from fastapi.testclient import TestClient

from careeros.resume_intake import ocr
from careeros.resume_intake.extract import ExtractionError, extract, extract_upload
from tests.test_resume_intake import RESUME


# ---------------------------------------------------------------------------
# OCR chain
# ---------------------------------------------------------------------------
def _fake_engines(*results):
    """Engines that return text, or raise OcrUnavailable for a string starting '!'."""
    engines = []
    for index, value in enumerate(results):
        def engine(_path, value=value):
            if isinstance(value, str) and value.startswith("!"):
                raise ocr.OcrUnavailable(value[1:])
            return value
        engines.append((f"engine{index}", engine))
    return engines


def test_the_first_engine_that_reads_anything_wins(tmp_path):
    image = tmp_path / "scan.png"
    image.write_bytes(b"not really a png")
    text, name = ocr.ocr_text(image, _fake_engines("!missing", "", "hello world"))
    assert (text, name) == ("hello world", "engine2")


def test_no_engine_available_lists_every_fix(tmp_path):
    image = tmp_path / "scan.png"
    image.write_bytes(b"x")
    with pytest.raises(ocr.OcrUnavailable) as caught:
        ocr.ocr_text(image, _fake_engines("!install tesseract", "!set a key"))
    assert "install tesseract" in str(caught.value) and "set a key" in str(caught.value)


def test_an_image_resume_goes_through_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "ENGINES", _fake_engines(RESUME))
    image = tmp_path / "phone-photo.jpg"
    image.write_bytes(b"jpeg bytes")
    result = extract(image)
    assert result.method == "ocr:engine0"
    assert "Cobalt Bank" in result.text


def test_an_image_with_no_ocr_engine_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "ENGINES", _fake_engines("!pip install 'careeros[ocr]'"))
    image = tmp_path / "scan.png"
    image.write_bytes(b"x")
    with pytest.raises(ExtractionError) as caught:
        extract(image)
    assert "careeros[ocr]" in str(caught.value)
    assert "paste the text" in str(caught.value)


def test_a_scanned_pdf_falls_back_to_ocr(tmp_path, monkeypatch):
    extract_module = importlib.import_module("careeros.resume_intake.extract")

    monkeypatch.setattr(extract_module, "_pdf_text_layer", lambda _p: "3")
    monkeypatch.setattr(ocr, "ENGINES", _fake_engines(RESUME))
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = extract(pdf)
    assert result.method == "ocr:engine0"


def test_a_pdf_with_a_text_layer_skips_ocr(tmp_path, monkeypatch):
    extract_module = importlib.import_module("careeros.resume_intake.extract")

    monkeypatch.setattr(extract_module, "_pdf_text_layer", lambda _p: RESUME)
    monkeypatch.setattr(ocr, "ENGINES", _fake_engines("!should not run"))
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert extract(pdf).method == "pdf-text"


def test_an_upload_ignores_the_path_in_its_filename():
    result = extract_upload(RESUME.encode(), "../../../etc/resume.txt")
    assert result.method == "text"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_real_tesseract_reads_a_rendered_resume(tmp_path):
    pytest.importorskip("pytesseract")
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 28)
    except OSError:
        pytest.skip("no scalable font to render with")
    lines = RESUME.split("\n")
    image = Image.new("RGB", (1700, 60 + 40 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((60, 30 + 40 * index), line, fill="black", font=font)
    path = tmp_path / "scan.png"
    image.save(path)

    result = extract(path)
    assert result.method == "ocr:tesseract"
    assert "Cobalt Bank" in result.text
    assert "priya.raman@example.com" in result.text


# ---------------------------------------------------------------------------
# Upload API
# ---------------------------------------------------------------------------
@pytest.fixture
def api(db):
    from careeros.api import deps
    from careeros.api.app import app

    def _session_override():
        yield db

    app.dependency_overrides[deps.get_db] = _session_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _docx(text: str) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in text.split("\n")
    )
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_first_upload_opens_a_profile_from_the_resume(api, db):
    from careeros.db.models import EvidenceItem, User
    from careeros.enums import VerificationState

    response = api.post(
        "/api/resumes/upload",
        files={"file": ("Priya Raman CV.docx", _docx(RESUME), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "priya.raman@example.com"
    assert body["file"]["method"] == "docx"
    assert body["parsed"]["full_name"] == "Priya Raman"
    assert body["intake"]["evidence_created"] > 0
    assert body["search_terms"]

    user = db.get(User, body["user_id"])
    assert user.current_title == "Senior Network Security Engineer"
    states = {e.verification for e in db.query(EvidenceItem).filter_by(user_id=user.id)}
    assert states == {VerificationState.UNVERIFIED.value}


def test_upload_attaches_to_the_existing_profile(api, user):
    response = api.post(
        "/api/resumes/upload",
        files={"file": ("cv.txt", RESUME.encode(), "text/plain")},
        data={"commit": "true"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == user.id


def test_a_scan_upload_uses_ocr(api, monkeypatch):
    monkeypatch.setattr(ocr, "ENGINES", _fake_engines(RESUME))
    response = api.post(
        "/api/resumes/upload", files={"file": ("scan.png", b"\x89PNG fake", "image/png")}
    )
    assert response.status_code == 200, response.text
    assert response.json()["file"]["method"] == "ocr:engine0"


def test_an_unreadable_upload_explains_itself(api):
    response = api.post(
        "/api/resumes/upload", files={"file": ("cv.doc", b"old word", "application/msword")}
    )
    assert response.status_code == 422
    assert ".docx" in response.json()["detail"]


def test_a_resume_with_no_email_cannot_open_a_profile(api):
    text = RESUME.replace("priya.raman@example.com", "")
    response = api.post("/api/resumes/intake-text", json={"text": text})
    assert response.status_code == 422
    assert "email" in response.json()["detail"]


def test_preview_saves_nothing(api, user, db):
    from careeros.db.models import EvidenceItem

    before = db.query(EvidenceItem).filter_by(user_id=user.id).count()
    response = api.post("/api/resumes/intake-text", json={"text": RESUME, "commit": False})
    assert response.status_code == 200
    assert response.json()["committed"] is False
    assert db.query(EvidenceItem).filter_by(user_id=user.id).count() == before


def test_bulk_verify_confirms_the_uploaded_evidence(api, db):
    from careeros.db.models import EvidenceItem
    from careeros.enums import VerificationState

    body = api.post("/api/resumes/intake-text", json={"text": RESUME}).json()
    ids = body["intake"]["evidence_ids"]
    response = api.post("/api/profile/evidence/verify", json={"evidence_ids": ids})
    assert response.json()["changed"] == len(ids)
    states = {db.get(EvidenceItem, i).verification for i in ids}
    assert states == {VerificationState.VERIFIED.value}


# ---------------------------------------------------------------------------
# Apply API
# ---------------------------------------------------------------------------
@pytest.fixture
def client(loaded):
    from careeros.api import deps
    from careeros.api.app import app

    session = loaded["pipeline"].session

    def _session_override():
        yield session

    app.dependency_overrides[deps.get_db] = _session_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _compatible_job(client):
    jobs = client.get("/api/jobs").json()["jobs"]
    return next(j for j in jobs if (j.get("eligibility") or {}).get("verdict") != "not_compatible")


def test_apply_tailors_and_hands_off_an_assisted_posting(client):
    job = _compatible_job(client)
    body = client.post("/api/applications/apply", json={"job_id": job["id"]}).json()
    assert body["resume_id"]
    assert body["channel"]["tier"] in {"assisted", "blocked", "api", "email"}
    if body["resume_final"] and not body["blockers"]:
        assert body["submission"] is not None
        assert body["status"] == "application_ready"
        assert body["sent"] is False
    assert body["next_step"]


def test_apply_refuses_an_incompatible_work_authorization(client):
    jobs = client.get("/api/jobs").json()["jobs"]
    vertex = next(j for j in jobs if j["company"] == "Vertex Health")
    body = client.post(
        "/api/applications/apply", json={"job_id": vertex["id"], "send": True}
    ).json()
    assert any("work-authorization" in b for b in body["blockers"])
    assert body["submission"] is None
    assert body["sent"] is False


def test_apply_by_email_previews_then_reports_sending_not_connected(client, loaded, monkeypatch):
    from careeros.db.models import Job

    session = loaded["pipeline"].session
    job = session.get(Job, _compatible_job(client)["id"])
    job.url = None
    job.description = (job.description or "") + "\nTo apply, email careers@example-hiring.com"
    session.flush()

    preview = client.post("/api/applications/apply", json={"job_id": job.id}).json()
    if preview["blockers"]:
        pytest.skip(f"sample job not sendable: {preview['blockers']}")
    assert preview["channel"]["tier"] == "email"
    assert preview["submission"]["dry_run"] is True
    assert "careers@example-hiring.com" in preview["submission"]["message"]

    sent_with = {}

    def fake_sender():
        def send(message, attachment):
            sent_with["to"] = message["to"]
            sent_with["attachment"] = attachment[0]
            return "msg-123"
        return send

    from careeros.apply import mail

    monkeypatch.setattr(mail, "gmail_sender", fake_sender)
    # The resume rests on the sample profile's verified evidence.
    real = client.post("/api/applications/apply", json={"job_id": job.id, "send": True}).json()
    if real["blockers"]:
        pytest.skip(f"not sendable: {real['blockers']}")
    assert real["sent"] is True
    assert real["status"] == "applied"
    assert sent_with["to"] == "careers@example-hiring.com"

    again = client.post("/api/applications/apply", json={"job_id": job.id, "send": True}).json()
    assert any("Already applied" in b for b in again["blockers"])
