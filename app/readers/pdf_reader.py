"""PDF reader (text PDFs only; scanned / image PDFs are rejected).

Per page: tables are found with pdfplumber, text outside the tables is read as lines,
and both are merged top-to-bottom. Then:
  - headings: larger font / bold short lines / numbered titles ('3. Gifts')
  - paragraphs: lines joined until a gap, a bullet or a numbered clause
  - repeated page headers / footers and page numbers are removed
  - a table that continues on the next page is merged into one table
"""
import io
import re
from collections import Counter

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError

from app.readers.base import (
    Block,
    DocumentError,
    clean,
    heading_from_line,
    numbered_heading_level,
    starts_new_item,
    table_rows,
)

_PAGE_NO = re.compile(r"^(page\s*)?\d{1,3}(\s*(of|/)\s*\d{1,3})?$", re.IGNORECASE)


def _inside(obj: dict, bbox) -> bool:
    x0, top, x1, bottom = bbox
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return x0 - 1 <= cx <= x1 + 1 and top - 1 <= cy <= bottom + 1


def _line_info(line: dict) -> dict:
    chars = [c for c in line.get("chars", []) if c.get("text", "").strip()]
    size = sum(c.get("size", 0) for c in chars) / len(chars) if chars else 0
    bold = bool(chars) and all("bold" in (c.get("fontname") or "").lower() for c in chars)
    return {"text": clean(line["text"]), "top": line["top"], "bottom": line["bottom"],
            "size": round(size, 1), "bold": bold}


def _norm_repeat(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower())


def read_pdf(data: bytes) -> list[Block]:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except PDFSyntaxError as e:
        raise DocumentError("not a readable PDF (syntax error)") from e
    except Exception as e:  # pdfminer raises many different types for broken files
        if e.__class__.__name__ == "PDFPasswordIncorrect":
            raise DocumentError("PDF is password protected") from e
        raise DocumentError(f"not a readable PDF ({e.__class__.__name__})") from e

    try:
        pages = _extract_pages(pdf)
    except DocumentError:
        raise
    except Exception as e:
        raise DocumentError(f"could not read PDF content ({e.__class__.__name__})") from e
    return _to_blocks(pages)


def _extract_pages(pdf) -> list[list[dict]]:
    pages: list[list[dict]] = []          # per page: items sorted by top
    with pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            bboxes = [t.bbox for t in tables]
            items: list[dict] = []
            for t in tables:
                rows = table_rows(t.extract())
                if rows:
                    items.append({"type": "table", "top": t.bbox[1], "rows": rows, "page": pno})
            body = page.filter(lambda o: o.get("object_type") != "char" or not any(_inside(o, b) for b in bboxes))
            for line in body.extract_text_lines(strip=True, return_chars=True):
                info = _line_info(line)
                if info["text"]:
                    items.append({"type": "line", "page": pno, **info})
            items.sort(key=lambda x: x["top"])
            pages.append(items)
    return pages


def _to_blocks(pages: list[list[dict]]) -> list[Block]:
    lines =[it for p in pages for it in p if it["type"] == "line"]
    if not lines and not any(it["type"] == "table" for p in pages for it in p):
        raise DocumentError("no text found in PDF (scanned / image-only PDFs are not supported)")

    # --- repeated headers / footers (same text in the top or bottom 2 lines of most pages)
    repeated: set[str] = set()
    if len(pages) >= 3:
        edge = Counter()
        for p in pages:
            ls = [it for it in p if it["type"] == "line"]
            for it in {_norm_repeat(x["text"]) for x in ls[:2] + ls[-2:]}:
                edge[it] += 1
        repeated = {t for t, n in edge.items() if n >= max(3, len(pages) * 0.6)}

    # --- body font size = most common size, weighted by characters
    sizes = Counter()
    for it in lines:
        sizes[it["size"]] += len(it["text"])
    body_size = sizes.most_common(1)[0][0] if sizes else 0
    heading_sizes = sorted({it["size"] for it in lines if it["size"] >= body_size * 1.15 and len(it["text"]) <= 100},
                           reverse=True)

    def heading_level(it: dict) -> int | None:
        text = it["text"]
        if len(text) > 100 or text[-1] in ".,;":
            return numbered_heading_level(text)
        if it["size"] in heading_sizes:
            return min(heading_sizes.index(it["size"]) + 1, 3)
        if it["bold"] and len(text) <= 80:
            return numbered_heading_level(text) or len(heading_sizes) + 1
        return heading_from_line(text)

    # --- normal gap between two lines of the same paragraph
    gaps = sorted(
        b["top"] - a["bottom"]
        for p in pages
        for a, b in zip([x for x in p if x["type"] == "line"], [x for x in p if x["type"] == "line"][1:])
        if a["size"] == body_size == b["size"] and b["top"] > a["bottom"]
    )
    line_gap = gaps[len(gaps) // 10] if gaps else body_size * 0.3   # low percentile: in-paragraph gaps are the smallest

    blocks: list[Block] = []
    para: list[str] = []
    para_page: int | None = None
    last: dict | None = None

    def flush():
        nonlocal para_page
        if para:
            text = ""
            for ln in para:
                if text.endswith("-") and ln[:1].islower():
                    text = text[:-1] + ln                 # de-hyphenate
                else:
                    text = f"{text} {ln}" if text else ln
            blocks.append(Block("text", clean(text), page=para_page))
            para.clear()
        para_page = None

    for p in pages:
        first_on_page = True
        for it in p:
            if it["type"] == "table":
                flush()
                prev = blocks[-1] if blocks else None
                # Continuation of a table from the previous page: same width, nothing in between
                if (first_on_page and prev is not None and prev.kind == "table"
                        and len(prev.rows[0]) == len(it["rows"][0])):
                    rows = it["rows"][1:] if it["rows"][0] == prev.rows[0] else it["rows"]
                    prev.rows.extend(rows)
                else:
                    blocks.append(Block("table", rows=it["rows"], page=it["page"]))
                first_on_page = False
                last = None
                continue

            text = it["text"]
            if _norm_repeat(text) in repeated or _PAGE_NO.match(text):
                continue
            first_on_page = False

            level = heading_level(it)
            if level:
                flush()
                # A heading wrapped onto two lines (same style, directly below) is one heading.
                if (blocks and blocks[-1].kind == "heading" and last is not None and last.get("heading")
                        and last["page"] == it["page"] and it["top"] - last["bottom"] < it["size"] * 0.6
                        and last["size"] == it["size"]):
                    blocks[-1].text += " " + text
                else:
                    blocks.append(Block("heading", text, level=level, page=it["page"]))
                last = {**it, "heading": True}
                continue

            new_para = (
                not para
                or starts_new_item(text)
                or (last is not None and (last["page"] != it["page"]
                                          and para[-1][-1:] in ".:;")
                    )
                or (last is not None and last["page"] == it["page"]
                    and it["top"] - last["bottom"] > line_gap + max(it["size"], 6) * 0.3)
            )
            if new_para:
                flush()
                para_page = it["page"]
            para.append(text)
            last = it
    flush()
    return blocks
