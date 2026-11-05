"""Datasheet tables -> typed specification fields (classic rules, no LLM).

``extract_specs(tables)`` reads every key/value table of a datasheet:

    {"X1": {"Input (DC) > Max. PV input voltage":
                     {"raw": "1500 V", "quantities": [{"value": 1500, "unit": "V", ...}]},
                 ...}}

The first level is the model (column header), or ``"default"`` when the table has
no model columns. ``canonical_specs(entries)`` maps the keys of one model to fixed
field names with units in the name (``dc_voltage_max_v``, ``mppt_voltage_v``,
``ac_power`` by temperature, ``efficiency_max_pct``, ``ip_rating``, ...), so code
does not depend on how a vendor words the row.

The synonym rules were written against inverter datasheets; fields that no rule
matches stay available in ``specs`` with their parsed quantities.
"""
from __future__ import annotations

import re
from typing import Any

from .quantities import Quantity, parse_quantities
from .schema import Table


def _dump(q: Quantity) -> dict[str, Any]:
    d = q.model_dump(exclude_none=True, exclude={"raw"})
    if not d.get("condition"):
        d.pop("condition", None)
    return d


def extract_specs(tables: list[Table], min_rows: int = 4) -> dict[str, dict[str, dict[str, Any]]]:
    """Key/value tables -> {model: {key: {"raw", "quantities"}}}. Empty dict when none."""
    specs: dict[str, dict[str, dict[str, Any]]] = {}
    for t in tables:
        width = max((len(r) for r in t.rows), default=0)
        if width < 2:
            continue
        keyed = [r for r in t.rows if r and isinstance(r[0], str) and r[0].strip()
                 and any(c not in (None, "") for c in r[1:])]
        if len(keyed) < min_rows:
            continue
        for j in range(1, width):
            label = t.columns[j].strip() if len(t.columns) > j and str(t.columns[j]).strip() else "default"
            entries = specs.setdefault(label, {})
            for r in keyed:
                if j >= len(r) or r[j] in (None, ""):
                    continue
                key, raw = r[0].strip(), str(r[j]).strip()
                if key in entries:
                    continue
                qs = parse_quantities(raw)
                entries[key] = {"raw": raw, "quantities": [_dump(q) for q in qs]} if qs else {"raw": raw}
    return {k: v for k, v in specs.items() if v}


# ---------------------------------------------------------------------------
# canonical fields
# ---------------------------------------------------------------------------
def _qs(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return entry.get("quantities", [])


def _val(q: dict[str, Any]) -> float | int:
    v = q["value"]
    return int(v) if float(v).is_integer() else v


def _first(entry, dimension=None):
    for q in _qs(entry):
        if dimension is None or q.get("dimension") == dimension:
            return q
    return None


def _rng(entry, dimension):
    q = _first(entry, dimension)
    if q and "value_max" in q:
        return [_val({"value": q["value"]}), _val({"value": q["value_max"]})]
    return None


def _scalar(dimension, si_base=None):
    """First quantity of a dimension as a number; converted to ``si_base`` units if given."""

    def f(entry):
        q = _first(entry, dimension)
        if not q:
            return None
        if si_base:
            return round(q["si_value"] / si_base, 6)
        return _val(q)

    return f


def _by_condition(entry):
    out = []
    for q in _qs(entry):
        if q.get("dimension") in ("apparent_power", "power") and "value_max" not in q:
            item = {"value": _val(q), "unit": q["unit"]}
            if q.get("condition"):
                item.update(q["condition"])
            out.append(item)
    return out or None


def _pair(entry, dimension, idx):
    qs = [q for q in _qs(entry) if q.get("dimension") == dimension]
    return _val(qs[idx]) if len(qs) > idx else None


def _count(entry):
    m = re.search(r"\d+", entry["raw"])
    return int(m.group(0)) if m else None


def _ip(entry):
    m = re.search(r"\bIP\s?(\d{2})[A-Z]?\b", entry["raw"], re.I)
    return f"IP{m.group(1)}" if m else None


def _dimensions(entry):
    boxes = sorted((q for q in _qs(entry) if q.get("index") is not None), key=lambda q: q["index"])
    if len(boxes) < 2:
        return None
    mm = [round(q["si_value"] * 1000, 3) for q in boxes]
    return {"values_mm": mm, "order": "W x H x D" if len(mm) == 3 else None}


def _op_value(entry):
    q = _first(entry)
    if not q:
        return None
    return {"value": _val(q), "unit": q["unit"], **({"op": q["op"]} if q.get("op") else {})}


def _plain(entry):
    return entry["raw"]


def _first_number(entry):
    qs = parse_quantities(entry["raw"], allow_unitless=True)
    return qs[0].value if qs else None


def _pf(entry):
    qs = parse_quantities(entry["raw"], allow_unitless=True)
    q = next((q for q in qs if q.value is not None and 0 < q.value <= 1 and not q.unit), None)
    return {"value": q.value, **({"op": q.op} if q.op else {})} if q else None


# (field, key regex, extractor). First rule that gives a value wins per field.
_RULES: list[tuple[str, str, Any]] = [
    ("dc_voltage_max_v", r"max\.?\s*(pv\s*|dc\s*)?(input\s*)?(dc\s*)?voltage(?!.*range)(?!.*(min|start))", _scalar("voltage")),
    ("dc_voltage_min_v", r"min\.?\s*(pv\s*|dc\s*)?(input\s*)?voltage", lambda e: _pair(e, "voltage", 0)),
    ("dc_startup_voltage_v", r"start.?up", lambda e: _pair(e, "voltage", 1) if len([q for q in _qs(e) if q.get("dimension") == "voltage"]) > 1 else _pair(e, "voltage", 0)),
    ("dc_voltage_nominal_v", r"nominal\s*(pv\s*|dc\s*)?(input\s*)?voltage", _scalar("voltage")),
    ("mppt_voltage_range_v", r"mpp\w*\s*(voltage\s*)?range|operating\s*(dc\s*)?voltage\s*range", lambda e: _rng(e, "voltage")),
    ("n_mppt", r"(no\.?|number)\s*of\s*(independent\s*)?mpp", _count),
    ("dc_current_max_per_mppt_a", r"max\.?\s*(pv\s*|dc\s*)?input\s*current", _scalar("current")),
    ("dc_short_circuit_current_per_mppt_a", r"short.?circuit\s*current", _scalar("current")),
    ("ac_power", r"(ac\s*)?(output|rated|nominal)\s*(ac\s*)?power", _by_condition),
    ("ac_current_max_a", r"max\.?\s*(ac\s*)?(output\s*)?current|rated\s*(ac\s*)?output\s*current", _scalar("current")),
    ("ac_voltage_nominal_v", r"nominal\s*(ac\s*|grid\s*)?voltage|rated\s*(ac\s*|grid\s*)?voltage", _scalar("voltage")),
    ("ac_voltage_range_v", r"ac\s*voltage\s*range|grid\s*voltage\s*range", lambda e: _rng(e, "voltage")),
    ("grid_frequency_hz", r"nominal\s*grid\s*frequency|grid\s*frequency", _scalar("frequency")),
    ("efficiency_max_pct", r"max\.?\s*efficiency", lambda e: _pair(e, "percent", 0)),
    ("efficiency_euro_pct", r"^(?!.*max)(.*> )?(european|euro)\s*efficiency", lambda e: _pair(e, "percent", 0)),
    ("thd_pct", r"(^|> )thd|total\s*harmonic", _op_value),
    ("power_factor", r"power\s*factor", _pf),
    ("weight_kg", r"weight", _scalar("mass", si_base=1)),
    ("dimensions", r"dimensions", _dimensions),
    ("ip_rating", r"degree\s*of\s*protection|ingress|protection\s*(class|rating)|\bip\s*rating", _ip),
    ("operating_temperature_c", r"operating\s*(ambient\s*)?temperature", lambda e: _rng(e, "temperature")),
    ("altitude_max_m", r"(operating\s*|max\.?\s*)altitude", _scalar("length")),
    ("night_consumption_w", r"consumption\s*at\s*night|night\s*consumption", lambda e: (lambda q: round(q["si_value"], 6) if q else None)(_first(e, "power"))),
    ("isolation", r"isolation\s*method|topology", _plain),
    ("cooling", r"cooling", _plain),
    ("communication", r"(^|> )communication", _plain),
]


def canonical_specs(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Fixed field names for one model's ``entries`` (see ``extract_specs``)."""
    out: dict[str, Any] = {}
    for key, entry in entries.items():
        k = key.lower()
        for field, rx, fn in _RULES:
            if field in out or not re.search(rx, k):
                continue
            try:
                v = fn(entry)
            except (KeyError, TypeError, ValueError):
                v = None
            if v not in (None, [], {}):
                out[field] = v
    # "Max. efficiency / European efficiency  99.02 % / 98.8 %": the max rule took the first value,
    # the euro rule needs the second one of the same row
    for key, entry in entries.items():
        if re.search(r"max\.?\s*efficiency\s*/\s*european", key.lower()):
            v = _pair(entry, "percent", 1)
            if v is not None:
                out["efficiency_euro_pct"] = v
    return out
