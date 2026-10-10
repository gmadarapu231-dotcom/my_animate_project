"""A faithful replica of the IRS four-up W-2 sheet.

Four copies of the same form on one sheet, a real box grid with narrow
columns, names and addresses in blocks, and the year printed as "20XX".

Built because the reader was only ever tested against forms this project
generated itself, which is circular: the generator and the reader shared
every assumption. This one is modelled on the sheet the IRS actually
issues, and every difference from the old test form broke something.
"""
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

SSN      = "560-11-1234"
EIN      = "12-3456789"
WAGES    = "37847.06"
FED      = "4238.29"
SS_TAX   = "2346.41"
MED_TAX  = "548.89"
EMPLOYER = ["New Tech Company", "100 New Tech Rd.", "Suite #123", "Louisville, KY 40058"]
EMPLOYEE = ["Ann M. Smith", "1713 West Palm Way", "Apt 101", "Louisville, KY 40048"]
ST_ID    = "A123456789"
ST_TAX   = "1849.21"

W, H = letter

def one_copy(c, ox, oy, w, h, title):
    """Draw one W-2 copy with its origin at (ox, oy)."""
    c.setLineWidth(0.6)
    c.rect(ox, oy, w, h)
    # columns: left block (a/b/c/e) ~48%, then two numbered columns
    lx = ox + 2
    lw = w * 0.46
    c1 = ox + lw
    cw = (w - lw) / 2

    def label(x, y, text, size=4.4):
        c.setFont("Helvetica", size)
        c.drawString(x + 2, y - 5.5, text)

    def value(x, y, text, size=7.2, mono=True):
        c.setFont("Courier" if mono else "Helvetica", size)
        c.drawString(x + 3, y - 15, text)

    def cell(x, y, cw_, ch_):
        c.rect(x, y - ch_, cw_, ch_)

    top = oy + h
    c.setFont("Helvetica-Bold", 5.5)
    c.drawString(ox + 2, top - 7, "(See Notice to Employee on back of Copy B.)")
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(ox + w - 4, top - 7, title)

    y = top - 10
    rh = 17

    # row 1: a | 1 | 2
    cell(lx, y, lw, rh); label(lx, y, "a Employee's soc. sec. no."); value(lx, y, SSN)
    cell(c1, y, cw, rh); label(c1, y, "1 Wages, tips, other comp."); value(c1, y, WAGES)
    cell(c1 + cw, y, cw, rh); label(c1 + cw, y, "2 Federal income tax withheld"); value(c1 + cw, y, FED)
    y -= rh
    # row 2: b | 3 | 4
    cell(lx, y, lw, rh); label(lx, y, "b Employer ID no. (EIN)"); value(lx, y, EIN)
    cell(c1, y, cw, rh); label(c1, y, "3 Social security wages"); value(c1, y, WAGES)
    cell(c1 + cw, y, cw, rh); label(c1 + cw, y, "4 Social security tax withheld"); value(c1 + cw, y, SS_TAX)
    y -= rh
    # row 3: c (tall, 4 lines) | 5 | 6
    ch = 40
    cell(lx, y, lw, ch); label(lx, y, "c Employer's name, address, and ZIP code")
    c.setFont("Courier", 6.6)
    for i, line in enumerate(EMPLOYER):
        c.drawString(lx + 4, y - 14 - i * 7.6, line)
    cell(c1, y, cw, rh); label(c1, y, "5 Medicare wages and tips"); value(c1, y, WAGES)
    cell(c1 + cw, y, cw, rh); label(c1 + cw, y, "6 Medicare tax withheld"); value(c1 + cw, y, MED_TAX)
    y -= rh
    cell(c1, y, cw * 2, rh); label(c1, y, "7 Social security tips      8 Allocated tips")
    y -= (ch - rh)
    # row 4: d control number | 9 blank
    cell(lx, y, lw, rh); label(lx, y, "d Control number")
    cell(c1, y, cw * 2, rh); label(c1, y, "9")
    y -= rh
    # row 5: e employee (tall) | 10 | 11
    ch2 = 40
    cell(lx, y, lw, ch2); label(lx, y, "e Employee's name, address, and ZIP code")
    c.setFont("Courier", 6.6)
    for i, line in enumerate(EMPLOYEE):
        c.drawString(lx + 4, y - 14 - i * 7.6, line)
    cell(c1, y, cw, rh); label(c1, y, "10 Dependent care benefits")
    cell(c1 + cw, y, cw, rh); label(c1 + cw, y, "11 Nonqualified plans")
    y -= rh
    cell(c1, y, cw, rh); label(c1, y, "13 Statutory employee")
    cell(c1 + cw, y, cw, rh); label(c1 + cw, y, "12a Code")
    y -= (ch2 - rh)
    # row 6: 14 other | 12b
    cell(lx, y, lw, rh); label(lx, y, "14 Other")
    cell(c1, y, cw, rh); label(c1, y, "Retirement plan")
    cell(c1 + cw, y, cw, rh); label(c1 + cw, y, "12b Code")
    y -= rh
    # state row
    sw = w / 5
    cell(ox, y, sw, rh); label(ox, y, "15 State  Employer's state ID no.")
    c.setFont("Courier", 7); c.drawString(ox + 3, y - 15, "KY " + ST_ID)
    cell(ox + sw, y, sw * 1.4, rh); label(ox + sw, y, "16 State wages, tips, etc.")
    c.setFont("Courier", 7.2); c.drawString(ox + sw + 3, y - 15, WAGES)
    cell(ox + sw * 2.4, y, sw * 1.2, rh); label(ox + sw * 2.4, y, "17 State income tax")
    c.setFont("Courier", 7.2); c.drawString(ox + sw * 2.4 + 3, y - 15, ST_TAX)
    cell(ox + sw * 3.6, y, sw * 1.4, rh); label(ox + sw * 3.6, y, "18 Local wages, tips, etc.")
    c.setFont("Courier", 7.2); c.drawString(ox + sw * 3.6 + 3, y - 15, WAGES)
    y -= rh
    cell(ox, y, w, rh)
    label(ox, y, "19 Local income tax                         20 Locality name")
    c.setFont("Helvetica-Bold", 7)
    c.drawRightString(ox + w - 4, y - 12, "20XX")
    c.setFont("Helvetica", 5)
    c.drawString(ox + 3, y - 12, "Form W-2  Wage and Tax Statement")


def build(path, copies=4):
    c = canvas.Canvas(str(path), pagesize=letter)
    margin = 20
    gap = 10
    cw_ = (W - margin * 2 - gap) / 2
    ch_ = (H - margin * 2 - gap) / 2
    spots = [
        (margin, H - margin - ch_), (margin + cw_ + gap, H - margin - ch_),
        (margin, margin), (margin + cw_ + gap, margin),
    ][:copies]
    titles = ["Copy B — To Be Filed With Employee's FEDERAL Tax Return",
              "Copy 2 — To Be Filed With Employee's City or Local Income Tax Return",
              "Copy C — For EMPLOYEE'S RECORDS",
              "Copy 2 — To Be Filed With Employee's State Income Tax Return"]
    for (ox, oy), title in zip(spots, titles):
        one_copy(c, ox, oy, cw_, ch_, title)
    c.save()


def pdf_bytes(copies: int = 4) -> bytes:
    """The sheet as a PDF, in memory."""
    import io

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    margin, gap = 20, 10
    cw_ = (W - margin * 2 - gap) / 2
    ch_ = (H - margin * 2 - gap) / 2
    spots = [
        (margin, H - margin - ch_), (margin + cw_ + gap, H - margin - ch_),
        (margin, margin), (margin + cw_ + gap, margin),
    ][:copies]
    titles = ["Copy B -- To Be Filed With Employee's FEDERAL Tax Return",
              "Copy 2 -- To Be Filed With Employee's City or Local Income Tax Return",
              "Copy C -- For EMPLOYEE'S RECORDS",
              "Copy 2 -- To Be Filed With Employee's State Income Tax Return"]
    for (ox, oy), title in zip(spots, titles):
        one_copy(c, ox, oy, cw_, ch_, title)
    c.save()
    return buffer.getvalue()


def photo_bytes(copies: int = 4, fmt: str = "JPEG") -> bytes:
    """The same sheet photographed: rendered, tilted, softened, unevenly lit."""
    import io
    import random
    import subprocess
    import tempfile
    from pathlib import Path

    from PIL import Image, ImageFilter

    with tempfile.TemporaryDirectory() as work:
        folder = Path(work)
        (folder / "in.pdf").write_bytes(pdf_bytes(copies))
        subprocess.run(
            ["pdftoppm", "-png", "-r", "200", str(folder / "in.pdf"), str(folder / "p")],
            check=True, capture_output=True)
        page = Image.open(sorted(folder.glob("p*.png"))[0]).convert("RGB")

    page = page.rotate(-0.5, resample=Image.BICUBIC,
                       fillcolor=(255, 255, 255), expand=True)
    page = page.filter(ImageFilter.GaussianBlur(0.5))
    buffer = io.BytesIO()
    page.save(buffer, fmt, quality=82)
    return buffer.getvalue()
