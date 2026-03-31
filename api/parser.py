"""
parser.py — Document text extraction for all supported file types.

Supports: pdf, docx, xlsx/xls, csv, and plain text (including PLC source
files: .st, .scl, .lad, .fbd, .il, .sfc and code files: .py, .c, .h).
"""
import csv
import io

import pypdf
import docx as python_docx
import openpyxl


def parse_file(filename: str, data: bytes) -> str:
    """
    Extract plain text from a file, dispatching on extension.

    :param filename: Original filename including extension.
    :param data:     Raw file bytes.
    :return:         Extracted text, or empty string on failure.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "pdf":
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            return "\n\n".join(
                page.extract_text() or "" for page in reader.pages
            ).strip()
        except Exception:
            return ""

    if ext == "docx":
        try:
            doc = python_docx.Document(io.BytesIO(data))
            return "\n".join(
                p.text for p in doc.paragraphs if p.text.strip()
            ).strip()
        except Exception:
            return ""

    if ext in ("xlsx", "xls"):
        try:
            wb = openpyxl.load_workbook(
                io.BytesIO(data), read_only=True, data_only=True
            )
            lines = []
            for sheet in wb.worksheets:
                lines.append(f"[Sheet: {sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    if any(cells):
                        lines.append("\t".join(cells))
            return "\n".join(lines).strip()
        except Exception:
            return ""

    if ext == "csv":
        try:
            text = data.decode("utf-8", errors="replace")
            reader = csv.reader(io.StringIO(text))
            return "\n".join("\t".join(row) for row in reader).strip()
        except Exception:
            return data.decode("utf-8", errors="replace").strip()

    # Plain text, Markdown, PLC source (.st, .scl, .lad, .fbd, .il, .sfc),
    # and code files (.py, .c, .h, etc.)
    return data.decode("utf-8", errors="replace").strip()
