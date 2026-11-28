"""Render trusted Markdown reference text as a paginated, searchable PDF."""

from io import BytesIO
from threading import Lock

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
_LOCK = Lock()


def build_pdf(name: str, markdown: str) -> bytes:
    # ReportLab's global font registry and dynamic TrueType subsets are shared.
    with _LOCK:
        return _build_pdf(name, markdown)


def _build_pdf(name, markdown):
    for font, filename in (
        ("NeironychSans", "DejaVuSans.ttf"),
        ("NeironychBold", "DejaVuSans-Bold.ttf"),
        ("NeironychMono", "DejaVuSansMono.ttf"),
    ):
        if font not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(font, f"{_FONT_DIR}/{filename}"))
    out = BytesIO()
    canvas = Canvas(out, pagesize=A4, pageCompression=1)
    canvas.setTitle(f"{name} — Нейроныч API")
    canvas.setAuthor("Нейроныч API")
    width, height = A4
    left, right = 46, width - 46
    number = 0

    def new_page():
        nonlocal number
        if number:
            canvas.showPage()
        number += 1
        canvas.setFillColor(HexColor("#172039"))
        canvas.rect(0, height - 58, width, 58, fill=1, stroke=0)
        canvas.setFillColor(HexColor("#ffffff"))
        canvas.setFont("NeironychBold", 13)
        canvas.drawString(left, height - 34, "НЕЙРОНЫЧ / API REFERENCE")
        canvas.setFillColor(HexColor("#526078"))
        canvas.setFont("NeironychSans", 9)
        canvas.drawString(left, 26, "Документация для разработчиков")
        canvas.drawRightString(right, 26, str(number))
        return height - 82

    y = new_page()
    code = False
    for raw in markdown.splitlines():
        if raw.startswith("```"):
            code = not code
            continue
        if not raw:
            y -= 8
            continue
        heading = not code and raw.startswith("#")
        title = heading and raw.startswith("# ")
        text = raw.lstrip("# ").strip() if heading else raw
        font = "NeironychMono" if code else "NeironychBold" if heading else "NeironychSans"
        size = 9 if code else 19 if title else 13 if heading else 10
        leading = size + 5
        available = right - left - (12 if code else 0)
        if heading:
            if y < 110:
                y = new_page()
            y -= 8
        if code:
            # Preserve indentation and JSON formatting; wrap only unusually long lines.
            chars = max(1, int(available / pdfmetrics.stringWidth("M", font, size)))
            lines = [text[i:i + chars] for i in range(0, len(text), chars)]
        else:
            lines = []
            for line in simpleSplit(text, font, size, available):
                # simpleSplit does not break long unspaced URLs/IDs.
                while pdfmetrics.stringWidth(line, font, size) > available:
                    end = 1
                    while end < len(line) and pdfmetrics.stringWidth(line[:end + 1], font, size) <= available:
                        end += 1
                    lines.append(line[:end])
                    line = line[end:]
                lines.append(line)
        for line in lines:
            if y < 58:
                y = new_page()
            if code:
                canvas.setFillColor(HexColor("#eef2f8"))
                canvas.rect(left, y - 4, right - left, leading, fill=1, stroke=0)
            canvas.setFillColor(HexColor("#172039" if heading else "#283449"))
            canvas.setFont(font, size)
            canvas.drawString(left + (6 if code else 0), y, line)
            y -= leading
    canvas.save()
    return out.getvalue()
