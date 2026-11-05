"""KML / KMZ: site boundaries, exclusion zones, cable routes, grid connection points.

Each Placemark becomes a chunk and an entry in ``data["placemarks"]`` with its
folder path, geometry and computed size (area in m² / ha for polygons, length
in m for lines; spherical-earth approximation, error well below 0.5 %).

data:
  placemarks  [{"name", "folder", "description", "properties", "geometry_type",
                "coordinates", "area_m2", "area_ha", "length_m", "centroid"}]
  bbox        [min_lon, min_lat, max_lon, max_lat]
  summary     totals per geometry type
"""
from __future__ import annotations

import math
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ..core import register
from ..schema import Chunk, ParseError, Table

R_EARTH = 6_371_008.8


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _iter(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            yield c


def _coords(el) -> list[tuple[float, ...]]:
    c = next(_iter(el, "coordinates"), None)
    if c is None or not c.text:
        return []
    pts = []
    for tok in c.text.split():
        parts = tok.split(",")
        try:
            pts.append(tuple(float(p) for p in parts[:3]))
        except ValueError:
            continue
    return pts


def ring_area_m2(ring: list[tuple[float, ...]]) -> float:
    """Area of a lon/lat ring on a sphere (same method as geojson-area)."""
    n = len(ring)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        lon1, lat1 = ring[i][0], ring[i][1]
        lon2, lat2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += math.radians(lon2 - lon1) * (2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    return abs(total * R_EARTH**2 / 2)


def line_length_m(pts: list[tuple[float, ...]]) -> float:
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        p1, p2 = math.radians(a[1]), math.radians(b[1])
        dp, dl = p2 - p1, math.radians(b[0] - a[0])
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        total += 2 * R_EARTH * math.asin(min(1.0, math.sqrt(h)))
    return total


def _geometries(pm):
    """Yield (type, outer_coords, inner_rings) for all geometries in a placemark."""
    for el in pm.iter():
        t = _local(el.tag)
        if t == "Point":
            yield "Point", _coords(el), []
        elif t == "LineString":
            yield "LineString", _coords(el), []
        elif t == "Polygon":
            outer = _child(el, "outerBoundaryIs")
            inners = _children(el, "innerBoundaryIs")
            yield "Polygon", _coords(outer) if outer is not None else [], [_coords(i) for i in inners]


def _text(el, name) -> str | None:
    c = _child(el, name)
    return c.text.strip() if c is not None and c.text and c.text.strip() else None


def _strip_html(s: str | None) -> str | None:
    if not s:
        return s
    s = re.sub(r"<br\s*/?>|</p>|</tr>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _properties(pm) -> dict[str, str]:
    props = {}
    ext = _child(pm, "ExtendedData")
    if ext is None:
        return props
    for d in _iter(ext, "Data"):
        v = _text(d, "value")
        if d.get("name"):
            props[d.get("name")] = v
    for sd in _iter(ext, "SimpleData"):
        props[sd.get("name", "")] = (sd.text or "").strip()
    return props


def _walk(el, folder: list[str], out: list[tuple[list[str], ET.Element]]):
    for c in el:
        t = _local(c.tag)
        if t in ("Folder", "Document"):
            name = _text(c, "name")
            _walk(c, folder + ([name] if name and t == "Folder" else []), out)
        elif t == "Placemark":
            out.append((folder, c))


def _parse_kml_bytes(raw: bytes, name: str) -> dict:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ParseError(f"{name}: invalid KML ({e})") from e

    found: list[tuple[list[str], ET.Element]] = []
    _walk(root, [], found)

    placemarks, chunks = [], []
    lons, lats = [], []
    totals: dict[str, dict[str, float]] = {}
    for folder, pm in found:
        pname = _text(pm, "name") or "(unnamed)"
        desc = _strip_html(_text(pm, "description"))
        props = _properties(pm)
        for gtype, outer, inners in _geometries(pm):
            if not outer:
                continue
            lons += [p[0] for p in outer]
            lats += [p[1] for p in outer]
            rec = {
                "name": pname,
                "folder": " / ".join(folder) or None,
                "description": desc,
                "properties": props,
                "geometry_type": gtype,
                "coordinates": [list(p) for p in outer],
                "area_m2": None,
                "area_ha": None,
                "length_m": None,
                "centroid": [sum(p[0] for p in outer) / len(outer), sum(p[1] for p in outer) / len(outer)],
            }
            agg = totals.setdefault(gtype, {"count": 0})
            agg["count"] += 1
            if gtype == "Polygon":
                area = ring_area_m2(outer) - sum(ring_area_m2(r) for r in inners)
                rec["area_m2"], rec["area_ha"] = round(area, 1), round(area / 1e4, 4)
                rec["perimeter_m"] = round(line_length_m(outer + [outer[0]]), 1)
                rec["holes"] = len(inners)
                agg["area_ha"] = round(agg.get("area_ha", 0) + area / 1e4, 4)
            elif gtype == "LineString":
                rec["length_m"] = round(line_length_m(outer), 1)
                agg["length_m"] = round(agg.get("length_m", 0) + rec["length_m"], 1)
            placemarks.append(rec)

            size = (
                f", area {rec['area_ha']} ha" if rec["area_ha"] is not None
                else f", length {rec['length_m']} m" if rec["length_m"] is not None
                else f", at {rec['centroid'][1]:.6f}, {rec['centroid'][0]:.6f}"
            )
            lines = [f"{pname} ({gtype}{size})"]
            if rec["folder"]:
                lines.append(f"Folder: {rec['folder']}")
            if desc:
                lines.append(desc)
            for k, v in props.items():
                lines.append(f"{k}: {v}")
            chunks.append(Chunk(text="\n".join(lines), kind="placemark", section=rec["folder"]))

    table = Table(
        name="placemarks",
        columns=["name", "folder", "geometry_type", "area_ha", "length_m", "centroid_lat", "centroid_lon"],
        rows=[
            [p["name"], p["folder"], p["geometry_type"], p["area_ha"], p["length_m"],
             round(p["centroid"][1], 6), round(p["centroid"][0], 6)]
            for p in placemarks
        ],
    )
    doc_name = next((_text(d, "name") for d in _iter(root, "Document")), None)
    return {
        "metadata": {"name": doc_name},
        "chunks": chunks,
        "tables": [table] if placemarks else [],
        "data": {
            "placemarks": placemarks,
            "bbox": [min(lons), min(lats), max(lons), max(lats)] if lons else None,
            "summary": totals,
        },
        "warnings": [] if placemarks else ["no placemarks with geometry found"],
    }


@register([".kml"], name="kml", version="1", file_type="kml")
def parse_kml(path: Path) -> dict:
    return _parse_kml_bytes(path.read_bytes(), path.name)


@register([".kmz"], name="kmz", version="1", file_type="kmz")
def parse_kmz(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not kmls:
            raise ParseError(f"{path.name}: no .kml inside KMZ")
        main = "doc.kml" if "doc.kml" in kmls else kmls[0]
        result = _parse_kml_bytes(z.read(main), path.name)
        others = [n for n in z.namelist() if n != main and not n.endswith("/")]
        result["data"]["embedded_files"] = others
    return result
