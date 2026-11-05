"""XLSX / CSV: equipment lists, BOMs, cable schedules, PVsyst hourly exports, SCADA data.

XLSX: every sheet -> one Table (values, not formulas; empty edges trimmed).
CSV:  encoding and delimiter are detected; leading metadata lines (as in PVsyst
      or SCADA exports) are skipped and kept in ``data["preamble"]``.

Chunks contain at most ``preview_rows`` rows per table so a large 8760-row
export does not flood a prompt; the full data is always in ``tables``.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from collections import Counter
from pathlib import Path
from typing import Any

from ..core import register
from ..schema import Chunk, Table

ENCODINGS = ("utf-8-sig", "cp1252", "cp1257", "latin-1")  # cp1257 = Baltic


def _jsonable(v: Any) -> Any:
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


def _trim(rows: list[list[Any]]) -> list[list[Any]]:
    rows = [list(r) for r in rows]
    while rows and all(c in (None, "") for c in rows[-1]):
        rows.pop()
    while rows and all(c in (None, "") for c in rows[0]):
        rows.pop(0)
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    rows = [r + [None] * (width - len(r)) for r in rows]
    used = [j for j in range(width) if any(r[j] not in (None, "") for r in rows)]
    if not used:
        return []
    lo, hi = used[0], used[-1]
    return [r[lo : hi + 1] for r in rows]


def _split_header(rows: list[list[Any]]) -> tuple[list[str], list[list[Any]]]:
    if len(rows) >= 2 and all(isinstance(c, str) and c.strip() for c in rows[0]):
        return [c.strip() for c in rows[0]], rows[1:]
    return [], rows


@register([".xlsx", ".xlsm"], name="xlsx", version="1", file_type="xlsx")
def parse_xlsx(path: Path, max_rows: int | None = None, preview_rows: int = 30) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    tables, chunks, sheets, warnings = [], [], [], []
    try:
        for ws in wb.worksheets:
            raw = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if max_rows is not None and i >= max_rows:
                    warnings.append(f"sheet '{ws.title}' truncated at {max_rows} rows")
                    break
                raw.append([_jsonable(v) for v in row])
            rows = _trim(raw)
            sheets.append({"name": ws.title, "rows": len(rows), "state": ws.sheet_state})
            if not rows:
                continue
            cols, body = _split_header(rows)
            t = Table(name=ws.title, sheet=ws.title, columns=cols, rows=body)
            tables.append(t)
            chunks.append(Chunk(text=t.to_markdown(max_rows=preview_rows), kind="table", sheet=ws.title))
    finally:
        wb.close()
    if any(s["state"] != "visible" for s in sheets):
        warnings.append("workbook has hidden sheets (included)")
    return {"chunks": chunks, "tables": tables, "data": {"sheets": sheets}, "warnings": warnings}


def _decode(raw: bytes) -> tuple[str, str]:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def _num(v: str) -> Any:
    s = v.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if s.count(",") == 1 and "." not in s:  # decimal comma (EU exports)
        try:
            return float(s.replace(",", "."))
        except ValueError:
            pass
    return s


@register([".csv", ".tsv"], name="csv", version="1", file_type="csv")
def parse_csv(path: Path, delimiter: str | None = None, preview_rows: int = 30, convert_numbers: bool = True) -> dict:
    text, enc = _decode(path.read_bytes())
    lines = text.splitlines()
    sample = "\n".join(lines[:200])
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
        except csv.Error:
            delimiter = max(";,\t|", key=sample.count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any(c.strip() for c in r)]
    warnings = []
    preamble: list[str] = []
    if rows:
        # header = first row that has the most common field count (skips metadata lines)
        modal = Counter(len(r) for r in rows).most_common(1)[0][0]
        candidates = [i for i, r in enumerate(rows) if len(r) == modal]
        # prefer a fully filled, non-numeric row (a real header) among the first modal rows
        start = next(
            (i for i in candidates[:50]
             if all(c.strip() for c in rows[i]) and not any(isinstance(_num(c), (int, float)) for c in rows[i])),
            candidates[0],
        )
        preamble = [delimiter.join(r) for r in rows[:start]]
        rows = rows[start:]
        if preamble:
            warnings.append(f"skipped {len(preamble)} metadata line(s) before the header")
    cols, body = _split_header(rows)
    # PVsyst style: a units row right under the header (e.g. "W/m²", "kWh")
    units = None
    if cols and body and all(_num(c) is None or isinstance(_num(c), str) for c in body[0]) and any(
        c.strip() for c in body[0]
    ):
        if len(body) > 1 and any(isinstance(_num(c), (int, float)) for c in body[1]):
            units, body = body[0], body[1:]
    if convert_numbers:
        body = [[_num(c) for c in r] for r in body]
    t = Table(name=path.stem, columns=cols, rows=body)
    return {
        "metadata": {"encoding": enc, "delimiter": delimiter},
        "chunks": [Chunk(text=t.to_markdown(max_rows=preview_rows), kind="table")] if rows else [],
        "tables": [t] if rows else [],
        "data": {"preamble": preamble, "units": units},
        "warnings": warnings,
    }
