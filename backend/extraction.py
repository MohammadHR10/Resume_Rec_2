"""PDF text extraction (PyMuPDF)."""

from __future__ import annotations

import logging

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


def extract_text_from_pdf(file_path: str) -> str:
    """Extract all text from a PDF on disk. Returns '' on any failure."""
    try:
        with fitz.open(file_path) as pdf:
            return "".join(page.get_text() for page in pdf)
    except Exception as exc:  # noqa: BLE001 - a bad PDF must not kill a batch
        logger.error("Error extracting text from %s: %s", file_path, exc)
        return ""


def extract_text_from_bytes(data: bytes) -> str:
    """Extract all text from PDF bytes. Returns '' on any failure."""
    try:
        with fitz.open(stream=data, filetype="pdf") as pdf:
            return "".join(page.get_text() for page in pdf)
    except Exception as exc:  # noqa: BLE001
        logger.error("Error extracting text from uploaded PDF: %s", exc)
        return ""
