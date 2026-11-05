"""Register / point lists (Modbus, SCADA, EMS) from tables, vendor-independent.

``extract_registers(tables)`` finds tables whose header (or first row) has an address
column and a name column and returns one record per register:

    {"name": "Daily power yields", "address": 5003, "address_end": None,
     "n_registers": 1, "data_type": "U16", "scale": 0.1, "unit": "kWh",
     "range": None, "access": None, "note": None, "page": 8, "table": 12}

Understood spellings
* address: ``5003``, ``4950 - 4951`` (range -> ``address_end``), ``0x0149``;
* type: ``U16 S16 U32 S32 U64 S64 F32 F64 FLOAT UINT16 INT32 BOOL STRING`` ... (-> ``n_registers``);
* unit with a scale in front: ``0.1 kWh`` -> scale 0.1, unit kWh; or a separate scale column
  (``Scale``, ``Gain``, ``Multiplier``, ``Resolution``);
* column names by synonym (``Register``/``Address``/``Addr``, ``Signal``/``Name``/``Point``,
  ``Data type``/``Type``, ``R/W``/``Access``, ``Remark``/``Note``).

Header-less continuation pages are handled upstream (the PDF parser lets a table that
continues the last one take its header), and also here: a table with the same width as
the previous register table and an address in the right column is read with that layout.
"""
from __future__ import annotations

import re
from typing import Any

from .schema import Table

# column role -> header regex (tested in this order on the lower-cased, flattened header)
_ROLES: list[tuple[str, str]] = [
    ("scale", r"^(scale|scaling|gain|multiplier|factor|resolution|coefficient|ratio)\b"),
    ("data_type", r"(data\s*)?type|format|datatype"),
    ("range", r"range|min\.?\s*/?\s*max|limits?"),
    ("access", r"^(r\s*/\s*w|rw|access|read\s*/\s*write|operation|permission)"),
    ("unit", r"^units?\b|unit$"),
    ("address", r"address|addr\b|register|reg\.?\s*(no|addr)|offset|modbus"),
    ("name", r"^(name|signal|point|parameter|description|variable|measuring\s*point|item|data\s*name|function)"),
    ("note", r"^(notes?|remarks?|comments?)"),
]
_ADDR = re.compile(r"^\s*(0x[0-9A-Fa-f]+|\d+)\s*(?:[-–~]\s*(0x[0-9A-Fa-f]+|\d+))?\s*$")
_TYPE = re.compile(r"^\s*(?:(U|S|I|UINT|INT|UI)\s*(8|16|32|64)|(F|FLOAT|FP)\s*(32|64)?|DOUBLE|BOOL|BIT|COIL|STR(?:ING)?|UTF8|ASCII)\b", re.I)


def _flat(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _role_map(header: list[Any]) -> dict[str, int]:
    roles: dict[str, int] = {}
    for j, h in enumerate(header):
        t = _flat(h).lower()
        if not t:
            continue
        for role, rx in _ROLES:
            if role not in roles and re.search(rx, t):
                roles[role] = j
                break
    return roles


def _to_int(s: str) -> int:
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def _data_type(s: str) -> tuple[str | None, int | None]:
    """(normalised type, number of 16-bit registers)."""
    m = _TYPE.match(s or "")
    if not m:
        return None, None
    t = s.strip().upper()
    if m.group(1):
        bits = int(m.group(2))
        kind = "U" if m.group(1).upper() in ("U", "UINT", "UI") else "S"
        return f"{kind}{bits}", max(1, bits // 16)
    if m.group(3) or t.startswith(("DOUBLE",)):
        bits = int(m.group(4)) if m.group(4) else (64 if t.startswith("DOUBLE") else 32)
        return f"F{bits}", bits // 16
    if t.startswith(("BOOL", "BIT", "COIL")):
        return "BOOL", 1
    return "STRING", None


def _scale_unit(unit_cell: str, scale_cell: str | None) -> tuple[float | None, str | None]:
    scale = None
    if scale_cell:
        try:
            scale = float(scale_cell.replace(",", "."))
        except ValueError:
            m = re.search(r"(?:x|×|\*|1/)\s*([\d.,]+)", scale_cell)
            if m:
                v = float(m.group(1).replace(",", "."))
                scale = 1 / v if "1/" in scale_cell else v
    unit = unit_cell or None
    if unit_cell:
        m = re.match(r"^\s*(\d*\.?\d+)\s*(\S.*)?$", unit_cell)  # "0.1 kWh", "0.1℃", "10"
        if m:
            if scale is None:
                scale = float(m.group(1))
            unit = _flat(m.group(2)) or None
    return scale, unit


def _row_record(row: list[Any], roles: dict[str, int]) -> dict[str, Any] | None:
    def cell(role: str) -> str:
        j = roles.get(role)
        return _flat(row[j]) if j is not None and j < len(row) else ""

    m = _ADDR.match(cell("address"))
    name = cell("name")
    if not m or not name:
        return None
    a = _to_int(m.group(1))
    b = _to_int(m.group(2)) if m.group(2) else None
    dtype, n_words = _data_type(cell("data_type"))
    if b is not None and b >= a:
        n_words = b - a + 1
    scale, unit = _scale_unit(cell("unit"), cell("scale") or None)
    return {
        "name": name,
        "address": a,
        "address_end": b,
        "n_registers": n_words or 1,
        "data_type": dtype or (cell("data_type") or None),
        "scale": scale,
        "unit": unit,
        "range": cell("range") or None,
        "access": cell("access") or None,
        "note": cell("note") or None,
    }


def extract_registers(tables: list[Table], min_rows: int = 3) -> list[dict[str, Any]]:
    """Register records from every table that looks like a register map."""
    out: list[dict[str, Any]] = []
    prev_roles: dict[str, int] | None = None
    prev_width = 0
    for ti, t in enumerate(tables):
        header: list[Any] = list(t.columns)
        rows = t.rows
        roles = _role_map(header) if header else {}
        if "address" not in roles or "name" not in roles:
            # the header may be the first row of the table, or missing on a continuation page
            first = _role_map(rows[0]) if rows else {}
            if "address" in first and "name" in first:
                roles, rows = first, rows[1:]
            elif not t.columns and prev_roles and rows and len(rows[0]) == prev_width:
                roles = prev_roles  # headerless continuation of the previous register table
            else:
                roles = {}
        if "address" not in roles or "name" not in roles:
            continue
        recs = [r for r in (_row_record(row, roles) for row in rows) if r]
        if len(recs) < min_rows:
            continue
        for r in recs:
            r["page"] = t.page
            r["table"] = ti
            if t.sheet:
                r["sheet"] = t.sheet
        out.extend(recs)
        prev_roles, prev_width = roles, max(len(r) for r in rows) if rows else 0
    return out
