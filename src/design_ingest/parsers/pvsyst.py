"""PVsyst component files: .PAN (PV module) and .OND (inverter).

The format is indented ``Key=Value`` text with nested blocks closed by
``End of ...``. ``data["raw"]`` holds the full nested tree; ``data["summary"]``
holds the main parameters with clear names and units, so downstream code does
not need to know PVsyst key names.

PVsyst units kept as in the file: muISC in mA/°C, muVocSpec in mV/°C,
muPmpReq in %/°C, low_irradiance_rel_eff_pct = [G W/m2, % vs STC], PNomConv / PMaxOUT / PNomDC in kW.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..core import register
from ..schema import Chunk, ParseError, Table

_END_RE = re.compile(r"^\s*End of\b", re.I)
_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _value(v: str) -> Any:
    v = v.strip()
    if _NUM_RE.match(v):
        f = float(v)
        return int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f
    if "," in v:
        parts = [p.strip() for p in v.split(",")]
        if all(_NUM_RE.match(p) for p in parts if p):
            return [float(p) for p in parts if p]
    return v


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def parse_pvsyst_text(text: str) -> dict[str, Any]:
    """Parse PVsyst PAN/OND text into a nested dict.

    A ``Key=Value`` line opens a block when the next non-empty line is indented
    deeper; the block's own type is stored under ``"_type"``. Points of
    profiles (``Point_1=0,1``) become lists.
    """
    lines = [l.rstrip("\r\n") for l in text.splitlines()]
    lines = [l for l in lines if l.strip()]
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for i, line in enumerate(lines):
        if _END_RE.match(line):
            if len(stack) > 1:
                stack.pop()
            continue
        if "=" not in line:
            continue
        ind = _indent(line)
        # close blocks whose indentation we have left without an "End of" line
        while len(stack) > 1 and ind <= stack[-1][0]:
            stack.pop()
        key, _, val = line.strip().partition("=")
        key = key.strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        opens_block = bool(nxt) and not _END_RE.match(nxt) and _indent(nxt) > ind
        parent = stack[-1][1]
        if opens_block:
            block: dict[str, Any] = {"_type": val.strip()}
            name = "PVObject" if key == "PVObject_" else key.removeprefix("PVObject_") or key
            parent[name] = block
            stack.append((ind, block))
        else:
            parent[key] = _value(val)
    return root


def _decode(raw: bytes, name: str) -> str:
    if b"\x00" in raw[:2000]:
        raise ParseError(f"{name}: binary/encrypted PVsyst file (protected by the manufacturer?) - cannot read")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _get(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in ("", None):
            return d[k]
    return default


def _profile_points(block: dict | None) -> list[list[float]]:
    if not isinstance(block, dict):
        return []
    pts = [(int(k.split("_")[1]), v) for k, v in block.items() if k.startswith("Point_") and isinstance(v, list)]
    return [v for _, v in sorted(pts)]


def _module_summary(obj: dict) -> dict:
    com = obj.get("Commercial", {})
    s = {
        "manufacturer": _get(com, "Manufacturer"),
        "model": _get(com, "Model"),
        "data_source": _get(com, "DataSource"),
        "technology": _get(obj, "Technol"),
        "pnom_w": _get(obj, "PNom"),
        "pnom_tol_up_pct": _get(obj, "PNomTolUp"),
        "pnom_tol_low_pct": _get(obj, "PNomTolLow"),
        "voc_v": _get(obj, "Voc"),
        "isc_a": _get(obj, "Isc"),
        "vmp_v": _get(obj, "Vmp"),
        "imp_a": _get(obj, "Imp"),
        "cells_series": _get(obj, "NCelS"),
        "cells_parallel": _get(obj, "NCelP"),
        "bypass_diodes": _get(obj, "NDiode"),
        "mu_isc_ma_per_c": _get(obj, "muISC"),
        "mu_voc_mv_per_c": _get(obj, "muVocSpec"),
        "mu_pmp_pct_per_c": _get(obj, "muPmpReq"),
        "r_shunt_ohm": _get(obj, "RShunt"),
        "r_series_ohm": _get(obj, "RSerie"),
        "bifaciality": _get(obj, "BifacialityFactor"),
        "vmax_iec_v": _get(obj, "VMaxIEC"),
        "width_m": _get(com, "Width"),
        "height_m": _get(com, "Height"),
        "depth_m": _get(com, "Depth"),
        "weight_kg": _get(com, "Weight"),
        "gamma": _get(obj, "Gamma"),
        "mu_gamma_per_c": _get(obj, "muGamma"),
        "submodule_layout": _get(obj, "SubModuleLayout"),
        "vmax_ul_v": _get(obj, "VMaxUL"),
        "lid_loss_pct": _get(obj, "LIDLoss"),
        "r_shunt_zero_irr_ohm": _get(obj, "Rp_0"),
        "r_shunt_exp": _get(obj, "Rp_Exp"),
        "cell_area_cm2": _get(obj, "CellArea"),
        "year_begin": _get(com, "YearBeg"),
    }
    if s["width_m"] and s["height_m"] and s["pnom_w"]:
        s["area_m2"] = round(s["width_m"] * s["height_m"], 4)
        s["efficiency_pct"] = round(s["pnom_w"] / (s["area_m2"] * 1000) * 100, 2)
    if s["voc_v"] and s["mu_voc_mv_per_c"]:
        s["beta_voc_pct_per_c"] = round(s["mu_voc_mv_per_c"] / 1000 / s["voc_v"] * 100, 4)
    # low-irradiance behaviour: [[G W/m2, relative efficiency %], ...]
    ops = next((v for k, v in obj.items() if k.startswith("OperPoints") and isinstance(v, dict)), None)
    if ops:
        low = []
        for _, v in sorted((int(k.split("_")[1]), v) for k, v in ops.items() if k.startswith("Point_")):
            parts = [x.strip() for x in v.split(",")] if isinstance(v, str) else []
            if len(parts) >= 4:
                try:
                    low.append([float(parts[1]), float(parts[3])])
                except ValueError:
                    pass
        if low:
            s["low_irradiance_rel_eff_pct"] = low
    iam = obj.get("IAM", {})
    prof = next((v for v in iam.values() if isinstance(v, dict)), None) if isinstance(iam, dict) else None
    if prof:
        s["iam_profile"] = _profile_points(prof)
    return {k: v for k, v in s.items() if v is not None}


def _inverter_summary(obj: dict) -> dict:
    com = obj.get("Commercial", {})
    conv = obj.get("Converter", {}) if isinstance(obj.get("Converter"), dict) else {}
    s = {
        "manufacturer": _get(com, "Manufacturer"),
        "model": _get(com, "Model"),
        "data_source": _get(com, "DataSource"),
        "pnom_ac_kw": _get(conv, "PNomConv"),
        "pmax_ac_kw": _get(conv, "PMaxOUT"),
        "pnom_dc_kw": _get(conv, "PNomDC"),
        "pmax_dc_kw": _get(conv, "PMaxDC"),
        "vout_ac_v": _get(conv, "VOutConv"),
        "vmpp_min_v": _get(conv, "VMppMin"),
        "vmpp_max_v": _get(conv, "VMPPMax", "VMppMax"),
        "vabs_max_v": _get(conv, "VAbsMax"),
        "vmin_pnom_v": _get(conv, "VNomEff"),
        "inom_dc_a": _get(conv, "INomDC"),
        "imax_dc_a": _get(conv, "IMaxDC"),
        "inom_ac_a": _get(conv, "INomAC"),
        "imax_ac_a": _get(conv, "IMaxAC"),
        "efficiency_max_pct": _get(conv, "EfficMax"),
        "efficiency_euro_pct": _get(conv, "EfficEuro"),
        "phases": _get(conv, "MonoTri"),
        "threshold_kw": _get(conv, "PSeuil"),
        "n_inputs": _get(obj, "NbInputs"),
        "n_mppt": _get(obj, "NbMPPT"),
        "transformer": _get(obj, "Transfo"),
        "width_m": _get(com, "Width"),
        "height_m": _get(com, "Height"),
        "depth_m": _get(com, "Depth"),
        "weight_kg": _get(com, "Weight"),
    }
    if s["pnom_ac_kw"] and s["pnom_dc_kw"]:
        s["dc_ac_ratio_nominal"] = round(s["pnom_dc_kw"] / s["pnom_ac_kw"], 3)
    eff = conv.get("ProfilPIO")
    if isinstance(eff, dict):
        s["efficiency_profile_pin_pout_kw"] = _profile_points(eff)
    return {k: v for k, v in s.items() if v is not None}


def _build(path: Path, kind: str) -> dict:
    tree = parse_pvsyst_text(_decode(path.read_bytes(), path.name))
    obj = tree.get("PVObject")
    if not isinstance(obj, dict):
        raise ParseError(f"{path.name}: no 'PVObject_=' block - not a PVsyst {kind.upper()} text file")
    otype = obj.get("_type", "")
    warnings = []
    expected = "pvModule" if kind == "pan" else "pvGInverter"
    if otype and otype.lower() != expected.lower():
        warnings.append(f"object type is '{otype}', expected '{expected}'")
    summary = _module_summary(obj) if kind == "pan" else _inverter_summary(obj)
    label = "PV module" if kind == "pan" else "Inverter"
    scalar = {k: v for k, v in summary.items() if not isinstance(v, list)}
    text = f"{label} {summary.get('manufacturer', '')} {summary.get('model', '')}".strip() + "\n" + "\n".join(
        f"{k}: {v}" for k, v in scalar.items() if k not in ("manufacturer", "model")
    )
    table = Table(name=f"{label} parameters", columns=["parameter", "value"], rows=[[k, v] for k, v in scalar.items()])
    return {
        "metadata": {"pvsyst_version": obj.get("Version"), "object_type": otype},
        "chunks": [Chunk(text=text, kind="parameters")],
        "tables": [table],
        "data": {"summary": summary, "raw": obj},
        "warnings": warnings,
    }


@register([".pan"], name="pvsyst_pan", version="2", file_type="pan")
def parse_pan(path: Path) -> dict:
    return _build(path, "pan")


@register([".ond"], name="pvsyst_ond", version="1", file_type="ond")
def parse_ond(path: Path) -> dict:
    return _build(path, "ond")
