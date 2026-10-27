"""Document text extraction.

PDFs go through PyMuPDF. Word documents are unzipped and read directly from
their XML rather than pulling in a dependency: a .docx is a ZIP holding
``word/document.xml``, and all this needs from it is the text in reading order.
HR sends position descriptions as .docx far more often than as PDF, so treating
Word as a second-class input is not an option.
"""

from __future__ import annotations

import html
import io
import logging
import re
import zipfile

logger = logging.getLogger(__name__)


def _fitz():
    """Import PyMuPDF on first use.

    The chat workspace's analysis tools import the ranking code from
    ``pipeline``, which imports this module — and they run under whatever
    ``python`` a CLI harness finds on PATH, which is not necessarily the one
    holding this app's dependencies. Nothing in those tools reads a PDF, so a
    module-level import would refuse to start them for a library they never use.
    """
    import fitz  # PyMuPDF

    return fitz

#: Paragraph, tab and line-break elements that carry layout we want to keep.
_PARAGRAPH_END = re.compile(r"</w:p>")
_TAB = re.compile(r"<w:tab[^>]*/>")
_BREAK = re.compile(r"<w:br[^>]*/>")
_TAG = re.compile(r"<[^>]+>")


def extract_text_from_pdf(file_path: str) -> str:
    """Extract all text from a PDF on disk. Returns '' on any failure."""
    try:
        with _fitz().open(file_path) as pdf:
            return "".join(page.get_text() for page in pdf)
    except Exception as exc:  # noqa: BLE001 - a bad PDF must not kill a batch
        logger.error("Error extracting text from %s: %s", file_path, exc)
        return ""


def extract_text_from_bytes(data: bytes) -> str:
    """Extract all text from PDF bytes. Returns '' on any failure."""
    try:
        with _fitz().open(stream=data, filetype="pdf") as pdf:
            return "".join(page.get_text() for page in pdf)
    except Exception as exc:  # noqa: BLE001
        logger.error("Error extracting text from uploaded PDF: %s", exc)
        return ""


def extract_text_from_docx(data: bytes) -> str:
    """Extract text from .docx bytes, preserving paragraph and tab structure.

    Bullets, headings and table cells all end up as their own lines, which is
    what the qualification parser needs — it reads a position description as a
    sequence of lines, not as a formatted document.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        logger.error("Error reading uploaded .docx: %s", exc)
        return ""

    xml = _PARAGRAPH_END.sub("\n", xml)
    xml = _TAB.sub("\t", xml)
    xml = _BREAK.sub("\n", xml)
    text = html.unescape(_TAG.sub("", xml))

    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def looks_binary(text: str) -> bool:
    """True when a decode produced mojibake rather than readable text.

    A binary file decoded as UTF-8 comes back mostly as U+FFFD replacement
    characters and control bytes. Catching that here is what stops an
    unsupported upload from reaching a model as garbage and returning an empty
    result that reads like a successful parse.
    """
    if not text:
        return False
    sample = text[:4000]
    unreadable = sum(1 for ch in sample if ch == "�" or (ord(ch) < 32 and ch not in "\t\n\r"))
    return unreadable / len(sample) > 0.05
