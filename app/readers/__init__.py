"""Pick a reader by extension and check the file content matches it."""
from pathlib import PurePath

from app.readers.base import Block, DocumentError, UnsupportedFileType
from app.readers.docx_reader import read_docx
from app.readers.pdf_reader import read_pdf
from app.readers.text import read_text

SUPPORTED = {".pdf", ".docx", ".txt", ".md"}

__all__ = ["Block", "DocumentError", "UnsupportedFileType", "SUPPORTED", "file_type", "read_document"]


def file_type(filename: str) -> str:
    ext = PurePath(filename or "").suffix.lower()
    if ext == ".markdown":
        ext = ".md"
    if ext not in SUPPORTED:
        raise UnsupportedFileType(
            f"unsupported file type '{ext or filename}'; allowed: {', '.join(sorted(SUPPORTED))}")
    return ext


def read_document(filename: str, data: bytes) -> list[Block]:
    """Blocks in reading order. Raises UnsupportedFileType or DocumentError."""
    ext = file_type(filename)
    if not data:
        raise DocumentError("file is empty")
    if ext == ".pdf":
        if not data[:1024].lstrip().startswith(b"%PDF"):
            raise UnsupportedFileType("file has a .pdf extension but is not a PDF")
        blocks = read_pdf(data)
    elif ext == ".docx":
        if not data.startswith(b"PK"):
            raise UnsupportedFileType("file has a .docx extension but is not a Word .docx file "
                                      "(old .doc files are not supported)")
        blocks = read_docx(data)
    else:
        blocks = read_text(data, markdown=ext == ".md")

    if not any(b.kind in ("text", "table") for b in blocks):
        raise DocumentError("no text found in the document")
    return blocks
