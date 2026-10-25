"""Common document model produced by every reader.

A document is a flat list of blocks in reading order:
  heading  - a section title (level 1 = top)
  text     - one paragraph / clause / bullet
  table    - rows of cells; the first row is the header
The chunker turns these into chunks; readers never decide chunk boundaries.
"""
import re
from dataclasses import dataclass, field


class DocumentError(ValueError):
    """The file could not be read or has no usable text. Maps to HTTP 422."""


class UnsupportedFileType(ValueError):
    """Extension / content not supported. Maps to HTTP 415."""


@dataclass
class Block:
    kind: str                      # "heading" | "text" | "table"
    text: str = ""
    level: int = 1                 # headings only
    page: int | None = None
    rows: list[list[str]] = field(default_factory=list)   # tables only


# "1. Eligibility", "2.3 Gift table", "4) Limits", "Clause 5 - Gifts", "Section 3: Claims"
_NUMBERED = re.compile(r"^(?:(?:clause|section|article|part)\s+)?(\d{1,2}(?:\.\d{1,2}){0,3})[.)]?\s*[-–:]?\s+(\S.*)$",
                       re.IGNORECASE)
_SCHEDULE = re.compile(r"^(schedule|annexure|annex|appendix)\s+[A-Z0-9]{1,3}\b", re.IGNORECASE)
# "- x", "• x", "(a) x", "a) x", "(iv) x", "3. x", "3) x", "3.1 x", "3.1.2. x"
_BULLET = re.compile(r"^\s*(?:[-•*▪●◦–]|\(?[a-z]{1,3}\)|\(?[ivx]{1,5}\)|\d{1,2}[.)]|\d{1,2}(?:\.\d{1,2})+\.?)\s+")


def clean(text: str) -> str:
    text = text.replace(" ", " ").replace("​", "").replace("﻿", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def numbered_heading_level(line: str) -> int | None:
    """Level of a short numbered title like '3.1 Gift table', else None.

    A numbered *clause* ('3.1 Purchases must be made at ...') is long or ends with
    punctuation, so it stays body text.
    """
    line = line.strip()
    m = _NUMBERED.match(line)
    if not m:
        return None
    title = m.group(2).strip()
    if len(line) > 80 or len(title.split()) > 10 or title[-1] in ".,;" or not title[0].isalpha():
        return None
    if not (title[0].isupper() or title.isupper()):
        return None
    return m.group(1).count(".") + 1


def caps_heading(line: str) -> bool:
    """'GIFTS AND VOUCHERS' style titles."""
    line = line.strip()
    letters = [c for c in line if c.isalpha()]
    return (4 <= len(line) <= 70 and len(letters) >= 4 and line[-1] not in ".,;"
            and sum(c.isupper() for c in letters) / len(letters) > 0.9)


def schedule_heading(line: str) -> bool:
    line = line.strip()
    return len(line) <= 80 and bool(_SCHEDULE.match(line)) and line[-1] not in ".,;"


def starts_new_item(line: str) -> bool:
    """A bullet / lettered / numbered clause starts a new paragraph."""
    return bool(_BULLET.match(line))


def heading_from_line(line: str) -> int | None:
    """Heading level from text alone (used when there is no style or font information)."""
    lvl = numbered_heading_level(line)
    if lvl:
        return lvl
    if schedule_heading(line) or caps_heading(line):
        return 1
    return None


def table_rows(raw_rows) -> list[list[str]]:
    """Clean a raw table: None -> '', newlines -> spaces, drop empty rows and columns."""
    rows = [[clean(str(c or "")).replace("\n", " ") for c in r] for r in raw_rows or []]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i] for r in rows)]
    return [[r[i] for i in keep] for r in rows]
