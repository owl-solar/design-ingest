"""DOCX: technical specifications, tender requirements, method statements.

Walks the body in document order (paragraphs and tables interleaved) and keeps
the heading path, so each chunk knows its section, e.g. "4 Technical > 4.2 BESS".
Consecutive paragraphs under one heading are merged into one chunk.

data:
  headings   [{"level": 1, "text": "..."}]
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core import register
from ..schema import Chunk, Table

_HEADING_RE = re.compile(r"^(heading|заголовок|título|titre|überschrift|antraštė)\s*(\d)", re.I)


def _heading_level(par) -> int | None:
    name = (par.style.name if par.style is not None else "") or ""
    if name.lower() == "title":
        return 1
    m = _HEADING_RE.match(name)
    if m:
        return int(m.group(2))
    # outline level set directly on the paragraph
    ppr = par._p.pPr
    if ppr is not None:
        lvl = ppr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}outlineLvl")
        if lvl is not None:
            try:
                return int(lvl.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")) + 1
            except (TypeError, ValueError):
                pass
    return None


def _table(tbl) -> Table:
    rows = []
    for row in tbl.rows:
        cells, prev = [], None
        for cell in row.cells:
            # merged cells appear repeatedly: keep text once
            txt = cell.text.strip()
            cells.append(txt if cell._tc is not prev else "")
            prev = cell._tc
        rows.append(cells)
    if len(rows) >= 2 and all(rows[0]):
        return Table(columns=rows[0], rows=rows[1:])
    return Table(rows=rows)


@register([".docx", ".docm"], name="docx", version="1", file_type="docx")
def parse(path: Path) -> dict:
    import docx
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    cp = d.core_properties
    meta = {
        "title": cp.title,
        "author": cp.author,
        "created": cp.created.isoformat() if cp.created else None,
        "modified": cp.modified.isoformat() if cp.modified else None,
    }

    chunks: list[Chunk] = []
    tables: list[Table] = []
    headings: list[dict] = []
    path_stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def section() -> str | None:
        return " > ".join(t for _, t in path_stack) or None

    def flush():
        if buf:
            chunks.append(Chunk(text="\n".join(buf), kind="text", section=section()))
            buf.clear()

    for el in d.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            par = Paragraph(el, d)
            text = par.text.strip()
            if not text:
                continue
            level = _heading_level(par)
            if level:
                flush()
                while path_stack and path_stack[-1][0] >= level:
                    path_stack.pop()
                path_stack.append((level, text))
                headings.append({"level": level, "text": text})
                chunks.append(Chunk(text=text, kind="heading", section=section(), meta={"level": level}))
            else:
                buf.append(text)
        elif tag == "tbl":
            flush()
            t = _table(DocxTable(el, d))
            t.name = section()
            tables.append(t)
            chunks.append(Chunk(text=t.to_markdown(max_rows=None), kind="table", section=section()))
    flush()

    return {
        "metadata": {k: v for k, v in meta.items() if v},
        "chunks": chunks,
        "tables": tables,
        "data": {"headings": headings},
    }
