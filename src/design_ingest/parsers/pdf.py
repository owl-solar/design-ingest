"""PDF: datasheets, specifications, tender documents, PVsyst reports, CAD plots.

Uses pdfplumber (pure Python on top of pdfminer). Per page: text chunk +
tables. Pages without extractable text are flagged in ``warnings`` (scan or
vector-only drawing) - use ``design_ingest.extract(..., vision=True)`` for those.

data:
  pages            number of pages
  empty_pages      1-based pages with no text layer
  page_sizes_mm    [(w, h), ...]  - helps spot drawing sheets (A1/A0)
  drawing_pages    1-based pages that look like CAD sheets (long side >= 400 mm).
                   No tables are extracted there (a vector drawing has no real
                   tables; pdfplumber returns fragments) unless drawing_tables=True.
  rotated_chars_skipped  characters of rotated text (axis labels, mirrored
                   notes) left out of the text, they would come out reversed
  summary          only for drawing sheets: title_block (Project, Drawing No,
                   Scale, ...), module_tables [{modules_per_table, tables}],
                   total_modules, coordinates [[lat, lon]], transformer_stations

Tables: section header rows ("Input (DC)" with empty value cells) are folded
into the key of the following rows ("Input (DC) > Max. PV input voltage"), and
the text of a page excludes the area of accepted tables (no duplicate chunks).
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


def _fold_sections(rows: list[list]) -> list[list]:
    """Rows with only the first cell filled are section headers: prefix the keys below."""
    if not rows or len(rows[0]) < 2:
        return rows
    out, section = [], None
    for r in rows:
        rest = [c for c in r[1:] if c not in (None, "")]
        if isinstance(r[0], str) and r[0] and not rest:
            section = r[0]
            continue
        if section and isinstance(r[0], str) and r[0]:
            r = [f"{section} > {r[0]}"] + list(r[1:])
        out.append(r)
    return out if len(out) >= 2 else rows


def _table_from_rows(rows: list[list], page: int) -> Table | None:
    rows = [[(c.strip() if isinstance(c, str) else c) for c in r] for r in rows if r]
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if len(rows) < 2:
        return None
    header, body = rows[0], _fold_sections(rows[1:])
    has_header = all(isinstance(c, str) and c for c in header)
    if has_header:
        return Table(page=page, columns=[str(c) for c in header], rows=body)
    return Table(page=page, rows=rows)


_DRAWING_MM = 400  # A3 is 420 mm on the long side
_TITLE_KEYS = ("Project", "Location", "Title", "Drawn", "Checked", "Scale", "Date", "Drawing No", "Rev")
_TABLES_RE = re.compile(r"(?:Table|String)s? of (\d+) modules? \((\d+) pcs?\.?\)", re.I)
_COORD_RE = re.compile(r"(-?\d{1,3}\.\d{3,})\s*[\u00b0\u00ba\ufffd]?\s*,\s*(-?\d{1,3}\.\d{3,})")
_STATION_RE = re.compile(r"(Transformer station|Inverter station|Substation)[^\n(]*\((\d+)\s*[x\u00d7]\s*(\d+)\s*kVA\)", re.I)


def _drawing_summary(text: str) -> dict:
    s: dict = {}
    tb = {}
    key_re = re.compile(r"\b(" + "|".join(re.escape(k) for k in _TITLE_KEYS) + r")\s*:", re.I)
    ms = list(key_re.finditer(text))
    canon = {k.lower(): k for k in _TITLE_KEYS}
    for n, m in enumerate(ms):
        end = ms[n + 1].start() if n + 1 < len(ms) else len(text)
        lines = text[m.end():end].strip().split("\n")
        key = canon[m.group(1).lower()]
        if key == "Location":  # address continues on the next lines up to the coordinates
            keep = []
            for ln in lines:
                if _COORD_RE.search(ln):
                    break
                keep.append(ln.strip())
            val = " ".join(keep[:4])
        else:
            val = lines[0].strip()
        if val:  # last occurrence wins: the title block sits after notes like "Proposed location:"
            tb[key] = val.rstrip(",")
    if tb:
        s["title_block"] = tb
    mt = [(int(a), int(b)) for a, b in _TABLES_RE.findall(text)]
    if mt:
        s["module_tables"] = [{"modules_per_table": a, "tables": b} for a, b in mt]
        s["total_modules"] = sum(a * b for a, b in mt)
    coords = [[float(a), float(b)] for a, b in _COORD_RE.findall(text)]
    if coords:
        s["coordinates"] = coords
    st = [{"name": m.group(1), "units": int(m.group(2)), "kva_each": int(m.group(3))} for m in _STATION_RE.finditer(text)]
    if st:
        s["transformer_stations"] = st
    return s


def _inside(o: dict, boxes: list) -> bool:
    cx, cy = (o["x0"] + o["x1"]) / 2, (o["top"] + o["bottom"]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)


@register([".pdf"], name="pdf", version="2")
def parse(
    path: Path,
    tables: bool = True,
    max_pages: int | None = None,
    password: str | None = None,
    drawing_tables: bool = False,
) -> dict:
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
    drawing: list[int] = []
    rotated = 0
    drawing_text: list[str] = []
    with pdf:
        meta = {k.lower(): v for k, v in (pdf.metadata or {}).items() if isinstance(v, (str, int, float))}
        pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
        for i, page in enumerate(pages, 1):
            w, h = round(page.width * PT_TO_MM), round(page.height * PT_TO_MM)
            sizes.append((w, h))
            is_drawing = max(w, h) >= _DRAWING_MM
            if is_drawing:
                drawing.append(i)
            rotated += sum(1 for c in page.chars if not c.get("upright", True))
            upright = page.filter(lambda o: o.get("object_type") != "char" or o.get("upright", True))
            found: list[tuple[Table, tuple]] = []
            if tables and (drawing_tables or not is_drawing):
                try:
                    for ft in upright.find_tables():
                        t = _table_from_rows(ft.extract(), i)
                        if t is not None:
                            found.append((t, ft.bbox))
                except Exception as e:  # pdfplumber can choke on odd vector graphics
                    meta.setdefault("_table_errors", []).append(f"table extraction failed on p.{i}: {type(e).__name__}")
            boxes = [b for _, b in found]
            body = upright.filter(lambda o: o.get("object_type") != "char" or not _inside(o, boxes)) if boxes else upright
            text = _clean(body.extract_text() or "")
            if text:
                chunks.append(Chunk(text=text, kind="text", page=i))
                if is_drawing:
                    drawing_text.append(text)
            else:
                empty.append(i)
            for t, _ in found:
                out_tables.append(t)
                chunks.append(Chunk(text=t.to_markdown(max_rows=None), kind="table", page=i))
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

    data = {
        "pages": n_pages,
        "empty_pages": empty,
        "page_sizes_mm": sizes,
        "drawing_pages": drawing,
        "rotated_chars_skipped": rotated,
    }
    if drawing_text:
        summ = _drawing_summary("\n".join(drawing_text))
        if summ:
            data["summary"] = summ
    return {
        "metadata": meta,
        "chunks": chunks,
        "tables": out_tables,
        "data": data,
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
