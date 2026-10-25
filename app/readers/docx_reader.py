"""DOCX reader: paragraphs and tables in document order, headings from Word styles."""
import io
import re
import zipfile

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.readers.base import Block, DocumentError, clean, heading_from_line, table_rows

_HEADING_STYLE = re.compile(r"^heading\s*(\d)", re.IGNORECASE)


def _style(p: Paragraph) -> str:
    return ((p.style.name if p.style is not None else "") or "").strip()


def _paragraph_heading(p: Paragraph, guess: bool) -> int | None:
    style = _style(p)
    if style.lower() == "title":
        return 1
    m = _HEADING_STYLE.match(style)
    if m:
        return int(m.group(1)) + 1          # Heading 1 sits below Title
    if not guess:
        return None
    text = p.text.strip()
    # Whole paragraph bold and short, e.g. a manually formatted "Gifts" title.
    runs = [r for r in p.runs if r.text.strip()]
    if runs and all(r.bold for r in runs) and len(text) <= 70 and text[-1] not in ".,;:":
        return heading_from_line(text) or 2
    return heading_from_line(text)


def _table(t: Table) -> list[list[str]]:
    rows = []
    for row in t.rows:
        seen, cells = set(), []
        for cell in row.cells:
            if id(cell._tc) in seen:           # merged cells repeat the same element
                continue
            seen.add(id(cell._tc))
            cells.append(" ".join(clean(p.text) for p in cell.paragraphs if p.text.strip()))
        rows.append(cells)
    return table_rows(rows)


def read_docx(data: bytes) -> list[Block]:
    try:
        doc = Document(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise DocumentError(f"not a valid .docx file ({e.__class__.__name__})") from e

    body = list(doc.element.body.iterchildren())
    paragraphs = [Paragraph(c, doc) for c in body if c.tag.endswith("}p")]
    # When the author used Heading styles, trust only those (explicit structure wins).
    guess = not any(_HEADING_STYLE.match(_style(p)) for p in paragraphs if p.text.strip())

    blocks: list[Block] = []
    for child in body:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = clean(p.text)
            if not text:
                continue
            level = _paragraph_heading(p, guess)
            if level:
                blocks.append(Block("heading", text, level=level))
            else:
                blocks.append(Block("text", f"• {text}" if "list" in _style(p).lower() else text))
        elif tag == "tbl":
            rows = _table(Table(child, doc))
            if rows:
                blocks.append(Block("table", rows=rows))
    return blocks
