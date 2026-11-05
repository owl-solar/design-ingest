"""Physical quantities from datasheet text, without an LLM.

``parse_quantities("352 kVA @ 30℃ / 320 kVA @40 ℃")`` returns

    [Quantity(value=352, unit="kVA", dimension="apparent_power", si_value=352000.0,
              condition={"temperature_c": 30.0}),
     Quantity(value=320, ..., condition={"temperature_c": 40.0})]

What is understood:

* a value with a unit: ``1500 V``, ``99.02 %``, ``≤116 kg``, ``< 3 %`` (``op``);
* ranges: ``500 V – 1500 V``, ``500-1500V``, ``-30 to 60℃`` (``value`` .. ``value_max``);
* lists separated by ``/``, ``,`` or ``;`` (every item is its own Quantity);
* counted values: ``12 * 40 A`` -> value 40, count 12;
* conditions: ``@ 40℃`` -> ``condition["temperature_c"]``;
* AC / DC suffixes: ``1500 Vdc`` -> unit V, ``qualifier`` "dc";
* box sizes: ``1136 * 870 * 361 mm`` -> three lengths (``index`` 0, 1, 2);
* decimal commas (``0,5 %``) and thousands separators (``1,500 V``).

Units are mapped to a canonical symbol, a dimension and the factor to the base
unit of that dimension (``si_value``): V, A, W, VA, var, Wh, Ah, Hz, degC, m, kg,
ohm, s, m2, W/m2, Wh/m2, bit/s, Pa, %. Prefixed variants (kW, MW, mA, ...) are
generated, so ``kWp`` and ``MWp`` keep their ``p`` in ``unit`` but share the
dimension ``power`` with W.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


class Quantity(BaseModel):
    value: float | None = None
    value_max: float | None = None  # upper end of a range
    unit: str = ""
    dimension: str | None = None
    si_value: float | None = None
    si_value_max: float | None = None
    op: str | None = None  # "<", "<=", ">", ">=", "~"
    count: int | None = None  # "12 * 40 A" -> 12
    index: int | None = None  # position in "W * H * D"
    qualifier: str | None = None  # "ac" / "dc"
    condition: dict[str, Any] = Field(default_factory=dict)
    raw: str = ""


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------
_PREFIX = {"": 1.0, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9}

# canonical unit -> (dimension, factor to the base unit)
_UNITS: dict[str, tuple[str, float]] = {}


def _family(base: str, dimension: str, prefixes: str = "mkMG", factor: float = 1.0) -> None:
    for p in ("",) + tuple(prefixes):
        _UNITS[p + base] = (dimension, _PREFIX[p] * factor)


_family("V", "voltage", "mk")
_family("A", "current", "mk")
_family("W", "power", "mkMG")
_family("Wp", "power", "kMG")
_family("VA", "apparent_power", "kMG")
_family("var", "reactive_power", "kMG")
_family("Wh", "energy", "kMG")
_family("Ah", "charge", "mk")
_family("Hz", "frequency", "k")
_family("m", "length", "mk")
_family("g", "mass", "mk", factor=1e-3)  # base unit: kg
_family("s", "time", "m")
_family("Pa", "pressure", "kM")
_UNITS["cm"] = ("length", 0.01)
_UNITS["t"] = ("mass", 1000.0)
_UNITS["min"] = ("time", 60.0)
_UNITS["h"] = ("time", 3600.0)
_UNITS["d"] = ("time", 86400.0)
_UNITS["bar"] = ("pressure", 1e5)
_UNITS["inch"] = ("length", 0.0254)
_UNITS["ft"] = ("length", 0.3048)
_UNITS["lb"] = ("mass", 0.45359237)
for _u, _f in (("ohm", 1.0), ("mohm", 1e-3), ("kohm", 1e3), ("Mohm", 1e6)):
    _UNITS[_u] = ("resistance", _f)
_UNITS["degC"] = ("temperature", 1.0)
_UNITS["%"] = ("percent", 1.0)
_UNITS["dB"] = ("level", 1.0)
_UNITS["m2"] = ("area", 1.0)
_UNITS["cm2"] = ("area", 1e-4)
_UNITS["mm2"] = ("area", 1e-6)
_UNITS["km2"] = ("area", 1e6)
_UNITS["ha"] = ("area", 1e4)
_UNITS["W/m2"] = ("irradiance", 1.0)
_UNITS["kW/m2"] = ("irradiance", 1e3)
_UNITS["Wh/m2"] = ("irradiation", 1.0)
_UNITS["kWh/m2"] = ("irradiation", 1e3)
_UNITS["m/s"] = ("speed", 1.0)
_UNITS["km/h"] = ("speed", 1 / 3.6)
_UNITS["bit/s"] = ("data_rate", 1.0)
_UNITS["kbit/s"] = ("data_rate", 1e3)
_UNITS["Mbit/s"] = ("data_rate", 1e6)
_UNITS["Gbit/s"] = ("data_rate", 1e9)
_UNITS["Wh/kg"] = ("specific_energy", 1.0)
_UNITS["rpm"] = ("rotation", 1.0)

# spellings found in datasheets -> canonical symbol
_ALIASES = {
    "℃": "degC", "°C": "degC", "ºC": "degC", "˚C": "degC", "deg C": "degC", "degC": "degC",
    "Ω": "ohm", "ohm": "ohm", "Ohm": "ohm", "mΩ": "mohm", "kΩ": "kohm", "MΩ": "Mohm",
    "m²": "m2", "cm²": "cm2", "mm²": "mm2", "km²": "km2", "m^2": "m2",
    "W/m²": "W/m2", "kW/m²": "kW/m2", "Wh/m²": "Wh/m2", "kWh/m²": "kWh/m2",
    "kVAr": "kvar", "kVar": "kvar", "KVAR": "kvar", "Mvar": "Mvar", "MVAr": "Mvar", "VAr": "var", "Var": "var",
    "KVA": "kVA", "kva": "kVA", "MVa": "MVA", "KW": "kW", "KWh": "kWh", "kwh": "kWh", "MWH": "MWh",
    "kwp": "kWp", "KWp": "kWp", "kWP": "kWp", "MWP": "MWp", "Wp": "Wp",
    "dB(A)": "dB", "dBA": "dB", "bps": "bit/s", "bits/s": "bit/s", "kbps": "kbit/s", "Mbps": "Mbit/s",
    "Gbps": "Gbit/s", "baud": "bit/s", "inches": "inch", "feet": "ft", "kgs": "kg",
    "hr": "h", "hrs": "h", "sec": "s", "VDC": "V", "VAC": "V", "Vdc": "V", "Vac": "V", "kVdc": "kV", "kVac": "kV",
    "Adc": "A", "Aac": "A", "Wac": "W", "Wdc": "W", "kWac": "kW", "kWdc": "kW", "kVAac": "kVA",
    "MWac": "MW", "MWdc": "MW", "kWpdc": "kWp", "kWh/kWp": "kWh/kWp",
}
_QUALIFIER = {"VDC": "dc", "VAC": "ac", "Vdc": "dc", "Vac": "ac", "kVdc": "dc", "kVac": "ac", "Adc": "dc",
              "Aac": "ac", "Wac": "ac", "Wdc": "dc", "kWac": "ac", "kWdc": "dc", "kVAac": "ac",
              "MWac": "ac", "MWdc": "dc", "kWpdc": "dc"}

_TOKENS = sorted(set(_UNITS) | set(_ALIASES), key=len, reverse=True)
_UNIT_RE = "|".join(re.escape(t) for t in _TOKENS)
_NUM = r"[+-]?(?:\d+(?:[.,]\d+)?|\.\d+)"
_UNSIGNED = r"\d+(?:[.,]\d+)?"

_QTY = re.compile(
    rf"(?:(?P<count>\d+)\s*[*x×]\s*)?"
    rf"(?:(?P<op>[<>]=?|[≤≥~≈])\s*)?"
    rf"(?P<v1>{_NUM})\s*(?P<u1>{_UNIT_RE})?(?![A-Za-z])"
    rf"(?:\s*(?:-|–|—|to|~|\.\.\.)\s*(?P<v2>{_UNSIGNED})\s*(?P<u2>{_UNIT_RE})?(?![A-Za-z]))?"
    rf"(?:\s*@\s*(?P<cv>{_NUM})\s*(?P<cu>℃|°C|ºC|˚C))?"
)
_BOXU = r"(?:\s*(?:mm|cm|m|inch)(?![A-Za-z]))?"  # unit may follow every number: 1148 mm * 779 mm * 371 mm
_BOX = re.compile(
    rf"(?P<a>{_UNSIGNED}){_BOXU}\s*[*x×]\s*(?P<b>{_UNSIGNED}){_BOXU}"
    rf"(?:\s*[*x×]\s*(?P<c>{_UNSIGNED}))?\s*(?P<u>mm|cm|m|inch)(?![A-Za-z])"
)
_OPS = {"≤": "<=", "≥": ">=", "≈": "~"}


def _num(s: str) -> float:
    s = s.strip()
    if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+", s):  # 1,500 -> thousands
        return float(s.replace(",", ""))
    return float(s.replace(",", "."))


def _canon(token: str | None) -> tuple[str, str | None]:
    """(canonical unit, ac/dc qualifier) for a unit token, or ("", None)."""
    if not token:
        return "", None
    return _ALIASES.get(token, token), _QUALIFIER.get(token)


def _tidy(x: float | None) -> float | None:
    return None if x is None else float(f"{x:.12g}")


def _make(value, value_max, unit, op, count, qualifier, condition, raw, index=None) -> Quantity:
    dim, factor = _UNITS.get(unit, (None, 1.0))
    return Quantity(
        value=value, value_max=value_max, unit=unit, dimension=dim,
        si_value=None if value is None or dim is None else _tidy(value * factor),
        si_value_max=None if value_max is None or dim is None else _tidy(value_max * factor),
        op=op, count=count, index=index, qualifier=qualifier, condition=condition, raw=raw.strip(),
    )


def parse_quantities(text: Any, allow_unitless: bool = False) -> list[Quantity]:
    """All quantities in ``text``, in order. ``allow_unitless`` also returns plain numbers."""
    if text is None:
        return []
    s = str(text).replace("−", "-").replace(" ", " ")
    s = re.sub(r"(^|[(:=,;/]\s*)([+-])\s+(?=\d)", r"\g<1>\g<2>", s)  # "- 30 to 60" -> "-30 to 60"
    out: list[Quantity] = []

    box = _BOX.search(s)
    if box:
        unit, _ = _canon(box.group("u"))
        for i, g in enumerate(x for x in (box.group("a"), box.group("b"), box.group("c")) if x):
            out.append(_make(_num(g), None, unit, None, None, None, {}, box.group(0), index=i))
        s = s[:box.start()] + " " * (box.end() - box.start()) + s[box.end():]

    for m in _QTY.finditer(s):
        u1, q1 = _canon(m.group("u1"))
        u2, q2 = _canon(m.group("u2"))
        unit = u1 or u2
        if not unit and not allow_unitless:
            continue
        v1 = _num(m.group("v1"))
        v2 = _num(m.group("v2")) if m.group("v2") else None
        if v2 is not None and unit and u1 and u2 and u1 != u2:
            v2, u2 = None, ""  # "5 V - 10 A" is two things, not a range: keep the first only
        op = m.group("op")
        op = _OPS.get(op, op)
        cond = {}
        if m.group("cv"):
            cond["temperature_c"] = _num(m.group("cv"))
        count = int(m.group("count")) if m.group("count") and unit else None
        if m.group("count") and not unit:  # "12 * 40" without unit: not a count
            count = None
        out.append(_make(v1, v2, unit, op, count, q1 or q2, cond, m.group(0)))
    return out


def first_quantity(text: Any, dimension: str | None = None, allow_unitless: bool = False) -> Quantity | None:
    for q in parse_quantities(text, allow_unitless=allow_unitless):
        if dimension is None or q.dimension == dimension:
            return q
    return None
