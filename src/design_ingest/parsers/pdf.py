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
                   Vector paths are skipped while reading (about 20 % faster);
                   the rest of the time is pdfminer tokenising the content stream.
  rotated_chars    number of characters that are not horizontal (90 deg labels,
                   mirrored or diagonal text). They are NOT mixed into the page
                   text (that scrambles lines) but kept in a separate chunk per
                   page, ``kind="text"``, ``meta={"rotated": True}``, one string
                   per line of rotated text in reading order (e.g. trench sizes).
  report           "pvsyst" for a PVsyst simulation report
  summary          - drawing sheets: title_block (Project, Drawing No, Scale, ...;
                   read only from the title-block area found via "Project:"),
                   module_tables [{modules_per_table, tables}], total_modules,
                   coordinates [[lat, lon]], transformer_stations
                   - PVsyst report: software_version, project, variant,
                   simulation_date, latitude/longitude, system sizes, modules and
                   inverter models, p50/p90/p95 energy, specific production, PR,
                   loss_diagram [{name, pct}], monthly_e_grid_mwh

  low_text_pages   pages with a big image (>= 50 % of the page) and under 400
                   characters of text: scans with a thin OCR layer, full-page
                   covers. A warning points to extract(vision=True). Pages with no
                   text at all are in empty_pages.

Tables: line breaks inside a cell are joined with a space ("4950 -\n4951" ->
"4950 - 4951"). A table at the top of a page that continues the last table of the
previous page (same width) takes its header: either it has none, or its first data
row was mistaken for a header and its running number continues the numbering
("8." -> "9."), as in long Modbus register maps. Section header rows ("Input (DC)" with empty value cells) are folded
into the key of the following rows ("Input (DC) > Max. PV input voltage"), cells
that hold several stacked lines (a monthly table drawn as one cell per column)
are split into rows, and the text of a page excludes the area of accepted
tables (no duplicate chunks).
"""
from __future__ import annotations

import math
import re
from contextlib import contextmanager
from pathlib import Path

from ..core import register
from ..schema import Chunk, ParseError, Table

PT_TO_MM = 25.4 / 72


def _clean(text: str) -> str:
    text = text.replace("­", "")  # soft hyphen
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------
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


def _explode_multiline(rows: list[list]) -> list[list]:
    """A row whose filled cells all hold the same number (>= 2) of stacked lines is n rows."""
    out = []
    for r in rows:
        filled = [c for c in r if isinstance(c, str) and c.strip()]
        counts = {len(c.split("\n")) for c in filled}
        if len(r) > 1 and len(filled) == len([c for c in r if c not in (None, "")]) and len(counts) == 1 and counts != {1}:
            n = counts.pop()
            parts = [c.split("\n") if isinstance(c, str) and c.strip() else [c] * n for c in r]
            out.extend([[p[k].strip() if isinstance(p[k], str) else p[k] for p in parts] for k in range(n)])
        else:
            out.append(r)
    return out


def _flat(c):
    """Join the wrapped lines of a text cell with single spaces."""
    return re.sub(r"\s*\n\s*", " ", c).strip() if isinstance(c, str) else c


def _inherit_header(t: Table, prev: Table | None, first_on_page: bool) -> Table:
    """A table without header at the top of a page that continues the last table of the
    previous page (same number of columns) takes that table's header.

    Long register maps repeat the header only on their first page.
    """
    if prev is None or not prev.columns or not first_on_page or t.page != prev.page + 1:
        return t
    if not t.columns:
        if t.rows and len(t.rows[0]) == len(prev.columns):
            return t.model_copy(update={"columns": list(prev.columns)})
        return t
    # an all-text first data row was taken for a header: it is one when its running
    # number continues the numbering of the previous table ("8." -> "9.")
    if len(t.columns) == len(prev.columns) and prev.rows and t.columns != prev.columns:
        a, b = _row_number(prev.rows[-1]), _row_number(t.columns)
        if a is not None and b is not None and b == a + 1:
            return t.model_copy(update={"columns": list(prev.columns), "rows": [list(t.columns)] + t.rows})
    return t


def _row_number(row: list) -> int | None:
    m = re.fullmatch(r"(\d+)[.)]?", str(row[0]).strip()) if row else None
    return int(m.group(1)) if m else None


def _table_from_rows(rows: list[list], page: int) -> Table | None:
    rows = [[(c.strip() if isinstance(c, str) else c) for c in r] for r in rows if r]
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if len(rows) < 2:
        return None
    header, body = rows[0], _fold_sections(_explode_multiline(rows[1:]))
    # line breaks left inside a cell are text wrapping ("4950 -\n4951", "Arm\nsoftware\nver")
    header = [_flat(c) for c in header]
    body = [[_flat(c) for c in r] for r in body]
    # header: every cell is text; the top-left corner cell of a cross table may be empty
    has_header = all(isinstance(c, str) and c for c in header[1:]) and isinstance(header[0], str) \
        and (bool(header[0]) or len(header) > 2)
    if has_header:
        return Table(page=page, columns=[str(c) for c in header], rows=body)
    return Table(page=page, rows=[header] + body)


# ---------------------------------------------------------------------------
# characters: horizontal text vs rotated / mirrored / diagonal text
# ---------------------------------------------------------------------------
def _horizontal(o: dict) -> bool:
    """True for left-to-right text. pdfminer's ``upright`` is True for 180 deg and
    some diagonal text, so the text matrix is checked instead."""
    if o.get("object_type") != "char":
        return True
    m = o.get("matrix")
    if not m:
        return o.get("upright", True)
    return m[0] > 0 and abs(m[1]) <= 0.05 * m[0]


def _rotated_lines(chars: list[dict]) -> list[str]:
    """Strings of rotated text in content-stream order (= reading order of each label)."""
    lines: list[str] = []
    cur: list[str] = []
    prev = None

    def flush():
        nonlocal cur, prev
        s = "".join(cur).strip()
        if len(s) >= 2 and (not lines or lines[-1] != s):
            lines.append(s)
        cur, prev = [], None

    for ch in chars:
        if _horizontal(ch):
            if cur:
                flush()
            continue
        if prev is not None:
            size = max(prev.get("size") or 0, 1.0)
            dist = math.hypot(ch["x0"] - prev["x0"], ch["top"] - prev["top"])
            if dist > 4 * size:
                flush()
            elif dist > (prev.get("adv") or size * 0.5) + 0.35 * size:
                cur.append(" ")
        cur.append(ch.get("text", ""))
        prev = ch
    flush()
    return lines


@contextmanager
def _skip_vector_paths(enabled: bool):
    """Drawings hold 100k+ vector segments we never use: do not build objects for them."""
    cls = orig = None
    if enabled:
        try:
            import pdfplumber.page as pp

            cls = pp.PDFPageAggregatorWithMarkedContent
            orig = cls.paint_path
            cls.paint_path = lambda self, *a, **k: None
        except Exception:  # internals of pdfplumber changed: just read everything
            cls = None
    try:
        yield
    finally:
        if cls is not None:
            cls.paint_path = orig


# ---------------------------------------------------------------------------
# CAD sheets
# ---------------------------------------------------------------------------
_DRAWING_MM = 400  # A3 is 420 mm on the long side
_TITLE_KEYS = ("Project", "Location", "Title", "Drawn", "Checked", "Scale", "Date", "Drawing No", "Rev")
_TABLES_RE = re.compile(r"(?:Table|String)s? of (\d+) modules? \((\d+) pcs?\.?\)", re.I)
_COORD_RE = re.compile(r"(-?\d{1,3}\.\d{3,})\s*[\u00b0\u00ba\ufffd]?\s*,\s*(-?\d{1,3}\.\d{3,})")
_STATION_RE = re.compile(r"(Transformer station|Inverter station|Substation)[^\n(]*\((\d+)\s*[x\u00d7]\s*(\d+)\s*kVA\)", re.I)


def _parse_title_block(text: str) -> dict:
    tb: dict = {}
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
    return tb


def _title_block_text(page) -> str:
    """Text of the title-block area: from the last "Project:" label to the page edge.

    The plan itself carries labels that share the same text lines as the title
    block and would otherwise be glued to its values.
    """
    try:
        horiz = page.filter(_horizontal)  # rotated labels would hide the "Project:" label
        # words are searched in the right-hand strip first: on a whole A1 sheet the label
        # can be merged with plan text that lies on the same line
        hits = []
        for x0 in (page.width * 0.7, 0):
            area = horiz.within_bbox((x0, 0, page.width, page.height))
            hits = [w for w in area.extract_words() if w["text"].lower() == "project:"]
            if hits:
                break
        if not hits:
            return ""
        h = hits[-1]
        bbox = (max(h["x0"] - 12, 0), max(h["top"] - 14, 0), page.width, page.height)
        return _clean(horiz.within_bbox(bbox).extract_text() or "")
    except Exception:
        return ""


def _drawing_summary(text: str, title_text: str = "") -> dict:
    s: dict = {}
    tb = _parse_title_block(title_text) if title_text else {}
    if not tb.get("Project"):  # label not found or area empty: fall back to the whole sheet
        tb = {**_parse_title_block(text), **tb}
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


# ---------------------------------------------------------------------------
# PVsyst simulation report
# ---------------------------------------------------------------------------
def _num(s: str) -> float | int:
    f = float(s)
    return int(f) if f.is_integer() and "." not in s else f


def _first(pattern: str, text: str, flags: int = 0):
    m = re.search(pattern, text, flags)
    return m.group(1) if m else None


def _pvsyst_summary(pages: list[str], tables: list[Table]) -> dict:
    text = "\n".join(pages)
    s: dict = {}

    def put(key, pattern, conv=_num, src=text, flags=0):
        v = _first(pattern, src, flags)
        if v is not None:
            s[key] = conv(v) if conv else v

    put("software_version", r"PVsyst V([\d.]+)", None)
    put("project", r"^Project:\s*(.+)$", None, flags=re.M)
    put("variant", r"^Variant:\s*(.+)$", None, flags=re.M)
    put("simulation_date", r"Simulation date:\s*(\d\d/\d\d/\d\d \d\d:\d\d)", None)
    put("system_power_mwp", r"System power:\s*([\d.]+)\s*MWp")
    for key, label, neg in (("latitude", "Latitude", "S"), ("longitude", "Longitude", "W")):
        m = re.search(rf"{label}\s+(-?[\d.]+)\s*\u00b0\s*([NSEW])", text)
        if m:
            v = abs(float(m.group(1)))
            s[key] = round(-v if m.group(2) == neg else v, 4)
    put("altitude_m", r"Altitude\s+(-?\d+)\s*m")
    put("albedo", r"Albedo\s+([\d.]+)")
    orient = []
    for tilt, az in re.findall(r"Tilt/Azimuth\s+(-?\d+)\s*/\s*(-?\d+)", text):
        if [int(tilt), int(az)] not in orient:
            orient.append([int(tilt), int(az)])
    if orient:
        s["tilt_azimuth_deg"] = orient
    put("n_modules", r"Nb\. of modules\s+(\d+)\s*units")
    put("pnom_dc_mwp", r"Pnom total\s+([\d.]+)\s*MWp")
    put("n_inverters", r"Nb\. of units\s+(\d+)\s*units")
    put("pnom_ac_mw", r"Pnom total\s+([\d.]+)\s*MWac")
    put("dc_ac_ratio", r"Pnom ratio\s+([\d.]+)")
    put("gcr_pct", r"Average GCR\s+([\d.]+)\s*%")
    put("pitch_m", r"Sheds spacing\s+([\d.]+)\s*m")
    put("collector_width_m", r"Collector width\s+([\d.]+)\s*m")
    # module / inverter pair: "Manufacturer A Manufacturer B", "Model X Model Y"
    m = re.search(r"^Manufacturer (.+?) Manufacturer (.+)$", text, re.M)
    if m:
        s["module_manufacturer"], s["inverter_manufacturer"] = m.group(1).strip(), m.group(2).strip()
    m = re.search(r"^PV module Inverter\s*\n(?:.*\n)?Model (.+?) Model (.+)$", text, re.M)
    if m:
        s["module_model"], s["inverter_model"] = m.group(1).strip(), m.group(2).strip()
    m = re.search(r"Unit Nom\. Power\s+([\d.]+)\s*Wp\s+Unit Nom\. Power\s+([\d.]+)\s*kWac", text)
    if m:
        s["module_wp"], s["inverter_kwac"] = _num(m.group(1)), _num(m.group(2))
    # results
    put("energy_p50_mwh_year", r"Produced Energy (?:\(P50\) )?([\d.]+)\s*MWh/year")
    put("energy_p90_mwh_year", r"Produced Energy \(P90\)\s*([\d.]+)\s*MWh/year")
    put("energy_p95_mwh_year", r"Produced Energy \(P95\)\s*([\d.]+)\s*MWh/year")
    put("specific_production_kwh_kwp_year", r"Specific production (?:\(P50\) )?([\d.]+)\s*kWh/kWp/year")
    put("performance_ratio_pct", r"Perf\. Ratio PR\s+([\d.]+)\s*%")
    put("ghi_kwh_m2_year", r"([\d.]+) kWh/m\u00b2 Global horizontal irradiation")
    put("energy_into_grid_mwh_year", r"([\d.]+) MWh Energy injected into grid")
    # loss diagram: "-4.3% Near Shadings: irradiance loss"
    # (the table of contents also says "Loss diagram": take the page with most loss lines)
    loss_re = re.compile(r"^([+-]?\d+(?:\.\d+)?)%\s*(.+)$", re.M)
    loss_page = max((p for p in pages if "Loss diagram" in p), key=lambda p: len(loss_re.findall(p)), default="")
    losses = [{"name": n.strip(), "pct": float(v)} for v, n in loss_re.findall(loss_page)]
    if losses:
        s["loss_diagram"] = losses
    for t in tables:  # monthly balance table
        cols = [str(c) for c in t.columns]
        if any(c.startswith("E_Grid") for c in cols):
            k = next(i for i, c in enumerate(cols) if c.startswith("E_Grid"))
            monthly = {}
            for r in t.rows:
                try:
                    if str(r[0]) not in ("Year",):
                        monthly[str(r[0])] = float(r[k])
                except (ValueError, IndexError, TypeError):
                    continue
            if monthly:
                s["monthly_e_grid_mwh"] = monthly
            break
    return s


# ---------------------------------------------------------------------------
@register([".pdf"], name="pdf", version="4")
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
    low_text: list[int] = []
    sizes = []
    drawing: list[int] = []
    rotated = 0
    drawing_text: list[str] = []
    title_texts: list[str] = []
    page_texts: list[str] = []
    with pdf:
        meta = {k.lower(): v for k, v in (pdf.metadata or {}).items() if isinstance(v, (str, int, float))}
        pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
        for i, page in enumerate(pages, 1):
            w, h = round(page.width * PT_TO_MM), round(page.height * PT_TO_MM)
            sizes.append((w, h))
            is_drawing = max(w, h) >= _DRAWING_MM
            if is_drawing:
                drawing.append(i)
            with _skip_vector_paths(is_drawing and not drawing_tables):
                all_chars = page.chars
                rot_lines = _rotated_lines(all_chars)
                rotated += sum(1 for c in all_chars if not _horizontal(c))
                horiz = page.filter(_horizontal)
                found: list[tuple[Table, tuple]] = []
                if tables and (drawing_tables or not is_drawing):
                    try:
                        for ft in horiz.find_tables():
                            t = _table_from_rows(ft.extract(), i)
                            if t is not None:
                                found.append((t, ft.bbox))
                    except Exception as e:  # pdfplumber can choke on odd vector graphics
                        meta.setdefault("_table_errors", []).append(f"table extraction failed on p.{i}: {type(e).__name__}")
                boxes = [b for _, b in found]
                body = horiz.filter(lambda o: o.get("object_type") != "char" or not _inside(o, boxes)) if boxes else horiz
                text = _clean(body.extract_text() or "")
                if is_drawing:
                    tt = _title_block_text(page)
                    if tt:
                        title_texts.append(tt)
            page_texts.append(text)
            if text:
                chunks.append(Chunk(text=text, kind="text", page=i))
                if is_drawing:
                    drawing_text.append(text)
            elif not rot_lines:
                empty.append(i)
            if rot_lines:
                chunks.append(Chunk(text="\n".join(rot_lines), kind="text", page=i, meta={"rotated": True}))
            for n, (t, _) in enumerate(found):
                t = _inherit_header(t, out_tables[-1] if out_tables else None, n == 0)
                out_tables.append(t)
                chunks.append(Chunk(text=t.to_markdown(max_rows=None), kind="table", page=i))
            if not is_drawing and i not in empty:
                n_chars = len(text) + sum(len(s) for s in rot_lines) + sum(
                    len(str(c)) for t, _ in found for r in t.rows for c in r if c)
                if n_chars < 400 and _image_cover(page) >= 0.5:  # a scan: big image, thin text layer
                    low_text.append(i)
            page.flush_cache()  # keep memory flat on long tender documents
        n_pages = len(pdf.pages)

    warnings = meta.pop("_table_errors", [])
    if empty:
        if len(empty) == len(sizes):
            warnings.append("no text layer on any page (scanned or pure vector drawing) - use extract(vision=True)")
        else:
            warnings.append(f"{len(empty)} page(s) without text layer: {_ranges(empty)}")
    if low_text:
        warnings.append(
            f"{len(low_text)} page(s) with almost no text, probably scanned or image-only: {_ranges(low_text)}"
            " - use extract(vision=True)"
        )
    if max_pages is not None and n_pages > max_pages:
        warnings.append(f"read only first {max_pages} of {n_pages} pages")

    data = {
        "pages": n_pages,
        "empty_pages": empty,
        "low_text_pages": low_text,
        "page_sizes_mm": sizes,
        "drawing_pages": drawing,
        "rotated_chars": rotated,
    }
    if drawing_text:
        summ = _drawing_summary("\n".join(drawing_text), "\n".join(title_texts))
        if summ:
            data["summary"] = summ
    elif page_texts and "PVsyst - Simulation report" in page_texts[0]:
        data["report"] = "pvsyst"
        data["summary"] = _pvsyst_summary(page_texts, out_tables)
    return {
        "metadata": meta,
        "chunks": chunks,
        "tables": out_tables,
        "data": data,
        "warnings": warnings,
    }


def _image_cover(page) -> float:
    """Share of the page area covered by images (a scan is one big image with a thin text layer)."""
    area = page.width * page.height
    if not area:
        return 0.0
    covered = sum(
        max(0.0, min(im["x1"], page.width) - max(im["x0"], 0)) * max(0.0, min(im["bottom"], page.height) - max(im["top"], 0))
        for im in page.images
    )
    return min(covered / area, 1.0)


def _inside(o: dict, boxes: list) -> bool:
    cx, cy = (o["x0"] + o["x1"]) / 2, (o["top"] + o["bottom"]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)


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
