"""XLSX / CSV: equipment lists, BOMs, cable schedules, PVsyst hourly exports, SCADA data.

XLSX: every sheet -> one Table (values, not formulas; empty edges trimmed).
      The header is the first row with >= 3 mostly-text cells among the first 15
      rows (unnamed columns become col1, col2, ...); rows above it are in
      ``data["sheets"][i]["rows_above_header"]``. Merged cells that span several rows
      in one column are repeated down their rows (``merged_filled`` = how many).
      Files over 30 MB are read in read-only mode without merge handling.
CSV:  encoding and delimiter are detected; leading metadata lines (as in PVsyst
      or SCADA exports) are skipped and kept in ``data["preamble"]``, parsed
      "#Key,Value" lines in ``data["metadata"]``. For time series: ``timeseries``
      (start, end, step_minutes, rows, irregular_steps; from Year/Month/Day/Hour
      columns or one date column) and ``column_stats`` (min/max/mean/sum).

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


def _find_header(rows: list[list[Any]], search: int = 15) -> int | None:
    """Index of the header row among the first rows of a sheet, or None.

    A header has at least 3 text cells, mostly text (some columns may be unnamed),
    and a row with data below it. Rows above it (titles, notes) are returned apart.
    """
    for i, r in enumerate(rows[:search]):
        filled = [c for c in r if c not in (None, "")]
        texts = [c for c in filled if isinstance(c, str) and c.strip()]
        if len(texts) >= 3 and len(texts) >= 0.7 * len(filled) and i + 1 < len(rows):
            return i
    return None


def _fill_merged_vertical(raw: list[list[Any]], ranges) -> int:
    """Repeat the value of a merged cell over its rows (one column, several rows only).

    Wider merges are titles or spanning headers: copying them would repeat the text.
    """
    n = 0
    for rng in ranges:
        if rng.max_row <= rng.min_row or rng.max_col != rng.min_col or rng.min_row > len(raw):
            continue
        v = raw[rng.min_row - 1][rng.min_col - 1] if rng.min_col <= len(raw[rng.min_row - 1]) else None
        if v in (None, ""):
            continue
        for r in range(rng.min_row, min(rng.max_row, len(raw)) + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if c <= len(raw[r - 1]):
                    raw[r - 1][c - 1] = v
        n += 1
    return n


@register([".xlsx", ".xlsm"], name="xlsx", version="2", file_type="xlsx")
def parse_xlsx(path: Path, max_rows: int | None = None, preview_rows: int = 30) -> dict:
    import openpyxl

    # merged cells are only visible outside read-only mode; big files stay in read-only mode
    big = path.stat().st_size > 30_000_000
    wb = openpyxl.load_workbook(path, read_only=big, data_only=True)
    tables, chunks, sheets, warnings = [], [], [], []
    try:
        for ws in wb.worksheets:
            raw = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if max_rows is not None and i >= max_rows:
                    warnings.append(f"sheet '{ws.title}' truncated at {max_rows} rows")
                    break
                raw.append([_jsonable(v) for v in row])
            merged = 0 if big else _fill_merged_vertical(raw, ws.merged_cells.ranges)
            rows = _trim(raw)
            sheets.append({"name": ws.title, "rows": len(rows), "state": ws.sheet_state, "merged_filled": merged})
            if not rows:
                continue
            cols, body = _split_header(rows)
            if not cols:
                h = _find_header(rows)
                if h is not None:
                    cols = [str(c).strip() if c not in (None, "") else f"col{j + 1}" for j, c in enumerate(rows[h])]
                    body = rows[h + 1:]
                    above = [" | ".join(str(c) for c in r if c not in (None, "")) for r in rows[:h]]
                    above = [a for a in above if a]
                    if above:
                        sheets[-1]["rows_above_header"] = above
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


_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%y %H:%M",
                 "%d/%m/%Y %H:%M", "%d.%m.%Y %H:%M", "%m/%d/%Y %H:%M", "%Y-%m-%d")


def _parse_date(v: Any) -> dt.datetime | None:
    if not isinstance(v, str):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(v.strip(), fmt)
        except ValueError:
            continue
    return None


def _preamble_metadata(preamble: list[str], delimiter: str) -> dict[str, Any]:
    """'#Latitude,52.83' -> {'Latitude': 52.83}; a line without a value is kept as 'title'."""
    meta: dict[str, Any] = {}
    for line in preamble:
        key, sep, val = line.lstrip("#").strip().partition(delimiter)
        key, val = key.strip(), val.strip().strip(delimiter).strip()
        if sep and key and val and val != "None":
            meta[key] = _num(val)
        elif key and not sep:
            meta.setdefault("title", key)
    return meta


def _timeseries(cols: list[str], body: list[list[Any]]) -> dict[str, Any] | None:
    """Start / end / step of a time series, from Year-Month-Day-Hour columns or one date column."""
    if not cols or len(body) < 2:
        return None
    low = {c.strip().lower(): j for j, c in enumerate(cols)}

    def stamp(r: list[Any]) -> dt.datetime | None:
        try:
            if all(k in low for k in ("year", "month", "day")):
                g = lambda k: int(r[low[k]]) if k in low else 0  # noqa: E731
                return dt.datetime(g("year"), g("month"), g("day"), g("hour"), g("minute"))
            for j, c in enumerate(cols):
                if any(k in c.lower() for k in ("date", "time", "stamp")):
                    return _parse_date(r[j])
        except (ValueError, TypeError, IndexError):
            return None
        return None

    stamps = [stamp(r) for r in body]
    if stamps[0] is None or stamps[-1] is None:
        return None
    ok = [s for s in stamps if s is not None]
    steps = Counter((b - a).total_seconds() / 60 for a, b in zip(ok, ok[1:]))
    step = steps.most_common(1)[0][0] if steps else None
    return {
        "start": ok[0].isoformat(), "end": ok[-1].isoformat(), "rows": len(body), "step_minutes": step,
        "irregular_steps": sum(n for s, n in steps.items() if s != step),
    }


def _column_stats(cols: list[str], body: list[list[Any]]) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for j, c in enumerate(cols):
        vals = [r[j] for r in body if j < len(r) and isinstance(r[j], (int, float)) and not isinstance(r[j], bool)]
        if len(vals) >= 0.9 * len(body) and vals:
            stats[c] = {"min": min(vals), "max": max(vals), "mean": round(sum(vals) / len(vals), 4),
                        "sum": round(sum(vals), 4)}
    return stats


@register([".csv", ".tsv"], name="csv", version="2", file_type="csv")
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
        "data": {
            "preamble": preamble,
            "units": units,
            "metadata": _preamble_metadata(preamble, delimiter),
            **({"timeseries": ts} if (ts := _timeseries(cols, body)) else {}),
            "column_stats": _column_stats(cols, body) if convert_numbers else {},
        },
        "warnings": warnings,
    }
