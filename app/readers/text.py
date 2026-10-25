"""TXT and Markdown reader."""
import re

from app.readers.base import Block, DocumentError, clean, heading_from_line, starts_new_item, table_rows

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def decode(data: bytes) -> str:
    encodings = ["utf-16"] if data[:2] in (b"\xff\xfe", b"\xfe\xff") else []
    for enc in encodings + ["utf-8-sig", "cp1252"]:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def _strip_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)     # [label](url) -> label
    return text


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)]


def read_text(data: bytes, markdown: bool) -> list[Block]:
    text = decode(data)
    if "\x00" in text:
        raise DocumentError("file looks binary, not text")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # When the author marked headings with '#', trust only those (explicit structure wins).
    guess_headings = not (markdown and any(_MD_HEADING.match(ln.strip()) for ln in lines))

    blocks: list[Block] = []
    para: list[str] = []
    in_code = False

    def flush():
        if para:
            joined = clean(" ".join(para))
            if joined:
                blocks.append(Block("text", _strip_md(joined) if markdown else joined))
            para.clear()

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1

        if markdown and line.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code:
            para.append(line)
            continue
        if not line:
            flush()
            continue

        # Markdown heading
        m = _MD_HEADING.match(line) if markdown else None
        if m:
            flush()
            blocks.append(Block("heading", _strip_md(clean(m.group(2))), level=len(m.group(1))))
            continue

        # Setext heading: "Title" followed by ===== or -----
        if markdown and i < len(lines) and re.fullmatch(r"=+|-{3,}", lines[i].strip()) and not para:
            blocks.append(Block("heading", clean(line), level=1 if lines[i].strip()[0] == "=" else 2))
            i += 1
            continue

        # Pipe table: consecutive lines starting with "|" (or containing | with a separator row)
        if line.startswith("|") or (i < len(lines) and _TABLE_SEP.match(lines[i].strip()) and "|" in line):
            flush()
            rows = [_split_row(line)]
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                nxt = lines[i].strip()
                i += 1
                if _TABLE_SEP.match(nxt):
                    continue
                rows.append(_split_row(nxt))
            rows = table_rows([[_strip_md(c) for c in r] for r in rows])
            if rows:
                blocks.append(Block("table", rows=rows))
            continue

        # A short standalone line that looks like a title (numbered / CAPS / Schedule A)
        level = heading_from_line(line) if guess_headings and not para else None
        next_blank = i >= len(lines) or not lines[i].strip() or starts_new_item(lines[i])
        if level and (next_blank or len(line) < 60):
            flush()
            blocks.append(Block("heading", _strip_md(clean(line.lstrip("#").strip())), level=level))
            continue

        # Bullets and numbered clauses start their own paragraph
        if starts_new_item(line):
            flush()
        para.append(line)

    flush()
    return blocks
