"""Blocks -> chunks.

Rules (in order of importance for the validation agent, which reads ALL chunks):
  1. A chunk never mixes two sections; every chunk carries its section title.
  2. A table is its own chunk and is never cut mid-row. A table that is too big is
     split by rows, and every part repeats the header row.
  3. A sentence ending with ':' directly before a table ("Each claim receives the gift
     listed below:") is kept with the table, so the table keeps its meaning.
  4. Long sections are split at paragraph, then sentence boundaries, never mid-sentence.
  5. Chunks are numbered in document order (chunk_index) so /rules returns the document as written.
"""
import math
import re
from dataclasses import dataclass

from app.readers.base import Block

GENERAL_SECTION = "General"
_SENTENCE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9(\"'“•])")


@dataclass
class Chunk:
    index: int
    section: str
    kind: str               # "text" | "table"
    text: str
    page: int | None
    tokens: int
    part: str | None = None  # "2/3" when a table was split

    def embed_text(self) -> str:
        """What is embedded: the section title gives each chunk its context for search."""
        return f"{self.section}\n{self.text}"


def estimate_tokens(text: str) -> int:
    """~4 characters per token for English. Good enough for sizing chunks."""
    return max(1, math.ceil(len(text) / 4))


def _md_cell(cell: str) -> str:
    return cell.replace("|", "\\|").strip() or "-"


def render_table(rows: list[list[str]]) -> str:
    """Markdown table: LLMs read this format reliably."""
    if len(rows[0]) == 1:                                  # one column: a list, not a table
        return "\n".join(f"- {r[0]}" for r in rows if r[0])
    header, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(_md_cell(c) for c in header) + " |",
             "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in body]
    return "\n".join(lines)


def _split_long(text: str, max_tokens: int) -> list[str]:
    """Split one paragraph into pieces <= max_tokens at sentence, then word boundaries."""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    pieces, cur = [], ""
    for sent in _SENTENCE.split(text):
        if estimate_tokens(sent) > max_tokens:              # a monster sentence: cut on words
            if cur:
                pieces.append(cur)
            buf = ""
            for w in sent.split():
                if buf and estimate_tokens(buf + " " + w) > max_tokens:
                    pieces.append(buf)
                    buf = w
                else:
                    buf = f"{buf} {w}" if buf else w
            cur = buf
            continue
        if cur and estimate_tokens(cur + " " + sent) > max_tokens:
            pieces.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}" if cur else sent
    if cur:
        pieces.append(cur)
    return pieces


def _tail(text: str, tokens: int) -> str:
    """Last whole sentences of text that fit in `tokens` (used for overlap)."""
    if tokens <= 0:
        return ""
    out = ""
    for sent in reversed(_SENTENCE.split(text)):
        cand = f"{sent} {out}".strip()
        if estimate_tokens(cand) > tokens:
            break
        out = cand
    return out


class _Builder:
    def __init__(self, max_tokens: int, overlap_tokens: int, table_max_tokens: int):
        self.max_tokens = max_tokens
        self.overlap = overlap_tokens
        self.table_max = table_max_tokens
        self.chunks: list[Chunk] = []

    def add(self, section: str, kind: str, text: str, page: int | None, part: str | None = None):
        text = text.strip()
        if text:
            self.chunks.append(Chunk(len(self.chunks), section, kind, text, page, estimate_tokens(text), part))

    def text(self, section: str, paras: list[tuple[str, int | None]]):
        """Pack paragraphs into chunks of up to max_tokens."""
        cur, page = "", None
        for para, p in paras:
            for piece in _split_long(para, self.max_tokens):
                if cur and estimate_tokens(cur + "\n\n" + piece) > self.max_tokens:
                    self.add(section, "text", cur, page)
                    tail = _tail(cur, self.overlap)
                    cur, page = (f"{tail}\n\n{piece}" if tail else piece), p
                else:
                    cur = f"{cur}\n\n{piece}" if cur else piece
                    page = page if page is not None else p
        if cur:
            self.add(section, "text", cur, page)

    def table(self, section: str, rows: list[list[str]], caption: str | None, page: int | None):
        full = render_table(rows)
        prefix = f"{caption}\n" if caption else ""
        if estimate_tokens(prefix + full) <= self.table_max or len(rows) <= 2:
            self.add(section, "table", prefix + full, page)
            return
        header, body = rows[0], rows[1:]
        budget = max(50, self.table_max - estimate_tokens(prefix + render_table([header])) - 25)
        base = estimate_tokens(render_table([header]))
        costs = [estimate_tokens(render_table([header, r])) - base for r in body]
        # Balanced parts: aim for equal-sized parts, never above the budget.
        target = sum(costs) / math.ceil(sum(costs) / budget)
        groups, cur, used = [], [], 0
        for r, cost in zip(body, costs):
            if cur and (used + cost > budget or used >= target):
                groups.append(cur)
                cur, used = [], 0
            cur.append(r)
            used += cost
        if cur:
            groups.append(cur)
        n, start = len(groups), 1
        for i, g in enumerate(groups, 1):
            end = start + len(g) - 1
            note = f"(Table part {i} of {n}: rows {start}-{end} of {len(body)}; the header is repeated.)"
            self.add(section, "table", f"{prefix}{note}\n{render_table([header] + g)}", page, part=f"{i}/{n}")
            start = end + 1


def _document_title(blocks: list[Block]) -> Block | None:
    """A single top-level heading at the very start, above every other heading, is the
    document title ("Terms and Conditions - Galaxy Promo"), not a section."""
    headings = [b for b in blocks if b.kind == "heading"]
    if len(headings) < 2 or blocks[0].kind != "heading":
        return None
    first = blocks[0]
    if first.text[:1].isdigit():                    # "1. Definitions" is a section
        return None
    if all(h.level > first.level for h in headings[1:]):
        return first
    # Title directly followed by a section heading ("# Terms" then "## Period",
    # or "PROMOTION TERMS" then "1. Promotion period")
    if len(blocks) > 1 and blocks[1].kind == "heading" and len(headings) >= 3:
        return first
    return None


def chunk_blocks(blocks: list[Block], max_tokens: int = 700, overlap_tokens: int = 0,
                 table_max_tokens: int = 900) -> tuple[list[Chunk], str | None]:
    """Returns (chunks, document_title)."""
    title_block = _document_title(blocks)
    title = title_block.text if title_block else None
    b = _Builder(max_tokens, overlap_tokens, table_max_tokens)

    stack: list[tuple[int, str]] = []          # (level, heading text)
    paras: list[tuple[str, int | None]] = []

    def section() -> str:
        # Last two levels are enough context: "Schedule A > Galaxy S25 models"
        return " > ".join(t for _, t in stack[-2:]) or GENERAL_SECTION

    def flush():
        if paras:
            b.text(section(), paras)
            paras.clear()

    for blk in blocks:
        if blk is title_block:
            continue
        if blk.kind == "heading":
            flush()
            while stack and stack[-1][0] >= blk.level:
                stack.pop()
            stack.append((blk.level, blk.text))
        elif blk.kind == "text":
            paras.append((blk.text, blk.page))
        elif blk.kind == "table":
            caption = None
            if paras and paras[-1][0].rstrip().endswith(":") and estimate_tokens(paras[-1][0]) <= 120:
                caption = paras.pop()[0]
            flush()
            b.table(section(), blk.rows, caption, blk.page)
    flush()
    return b.chunks, title
