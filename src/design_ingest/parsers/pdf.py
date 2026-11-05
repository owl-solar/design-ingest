"""PDF: datasheets, specifications, tender documents, PVsyst reports, CAD plots.

Uses pdfplumber (pure Python on top of pdfminer). Per page: text chunk +
tables. Pages without extractable text are flagged in ``warnings`` (scan or
vector-only drawing) - use ``design_ingest.extract(..., vision=True)`` for those.

data:
  pages            number of pages
  empty_pages      1-based pages with no text layer
  page_sizes_mm    [(w, h), ...]  - helps spot drawing sheets (A1/A0)
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core import register
from ..schema import Chunk, ParseError, Table

PT_TO_MM = 25.4 / 72


def _clean(text: str) -> str:
    text = text.replace("­", "")  # soft hyphen
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _table_from_rows(rows: list[list], page: int) -> Table | None:
    rows = [[(c.strip() if isinstance(c, str) else c) for c in r] for r in rows if r]
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if len(rows) < 2:
        return None
    header, body = rows[0], rows[1:]
    has_header = all(isinstance(c, str) and c for c in header)
    if has_header:
        return Table(page=page, columns=[str(c) for c in header], rows=body)
    return Table(page=page, rows=rows)


@register([".pdf"], name="pdf", version="1")
def parse(path: Path, tables: bool = True, max_pages: int | None = None, password: str | None = None) -> dict:
    import pdfplumber

    try:
        pdf = pdfplumber.open(path, password=password or "")
    except Exception as e:
        msg = str(e) or type(e).__name__
        if "password" in msg.lower() or "encrypt" in msg.lower():
            raise ParseError(f"{path.name}: PDF is password-protected (pass password=...)") from e
        raise

    chunks: list[Chunk] = []
    out_tables: list[Table] = []
    empty: list[int] = []
    sizes = []
    with pdf:
        meta = {k.lower(): v for k, v in (pdf.metadata or {}).items() if isinstance(v, (str, int, float))}
        pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
        for i, page in enumerate(pages, 1):
            sizes.append((round(page.width * PT_TO_MM), round(page.height * PT_TO_MM)))
            text = _clean(page.extract_text() or "")
            if text:
                chunks.append(Chunk(text=text, kind="text", page=i))
            else:
                empty.append(i)
            if tables:
                try:
                    for rows in page.extract_tables():
                        t = _table_from_rows(rows, i)
                        if t is not None:
                            out_tables.append(t)
                            chunks.append(Chunk(text=t.to_markdown(max_rows=None), kind="table", page=i))
                except Exception as e:  # pdfplumber can choke on odd vector graphics
                    chunks_warn = f"table extraction failed on p.{i}: {type(e).__name__}"
                    meta.setdefault("_table_errors", []).append(chunks_warn)
            page.flush_cache()  # keep memory flat on long tender documents
        n_pages = len(pdf.pages)

    warnings = meta.pop("_table_errors", [])
    if empty:
        if len(empty) == len(sizes):
            warnings.append("no text layer on any page (scanned or pure vector drawing) - use extract(vision=True)")
        else:
            warnings.append(f"{len(empty)} page(s) without text layer: {_ranges(empty)}")
    if max_pages is not None and n_pages > max_pages:
        warnings.append(f"read only first {max_pages} of {n_pages} pages")

    return {
        "metadata": meta,
        "chunks": chunks,
        "tables": out_tables,
        "data": {"pages": n_pages, "empty_pages": empty, "page_sizes_mm": sizes},
        "warnings": warnings,
    }


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)
