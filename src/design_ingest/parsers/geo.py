"""Site files: KML / KMZ, GeoJSON, Shapefile (.shp with .dbf next to it).

Site boundaries, exclusion zones, cable routes, grid connection points. Each
feature becomes a chunk and an entry in ``data["placemarks"]`` with its folder
path, geometry and computed size (area in m² / ha for polygons, length in m for
lines; spherical-earth approximation, error well below 0.5 %).

data:
  placemarks  [{"name", "folder", "description", "properties", "geometry_type",
                "coordinates", "area_m2", "area_ha", "length_m", "centroid"}]
  bbox        [min_x, min_y, max_x, max_y]   (lon/lat for geographic files)
  summary     totals per geometry type
  crs         "WGS84 lon/lat" or "projected (planar, units assumed metres)"

GeoJSON is lon/lat by definition. A Shapefile is geographic when its .prj has only
a GEOGCS (or, without .prj, when all coordinates are within lon/lat range) and
projected when the .prj has a PROJCS: then areas and lengths are planar and in
metres only if the projection unit is metres (check the .prj, a warning says so).
"""
from __future__ import annotations

import json
import math
import re
import struct
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


def _signed_area(ring) -> float:
    """Shoelace area, positive for counter-clockwise rings (y up)."""
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1])) / 2


def ring_area_planar(ring) -> float:
    return abs(_signed_area(list(ring)))


def line_length_planar(pts) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


# ---------------------------------------------------------------------------
# features -> Document fields (shared by all formats)
# ---------------------------------------------------------------------------
def _records(features: list[dict], doc_name: str | None, planar: bool = False) -> dict:
    """features: [{"name", "folder", "description", "properties", "geoms": [(type, outer, inners)]}]"""
    area_fn = ring_area_planar if planar else ring_area_m2
    len_fn = line_length_planar if planar else line_length_m
    placemarks, chunks = [], []
    xs, ys = [], []
    totals: dict[str, dict[str, float]] = {}
    for ft in features:
        pname, folder, desc, props = ft["name"], ft.get("folder"), ft.get("description"), ft.get("properties") or {}
        for gtype, outer, inners in ft["geoms"]:
            if not outer:
                continue
            xs += [p[0] for p in outer]
            ys += [p[1] for p in outer]
            rec = {
                "name": pname,
                "folder": folder or None,
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
                area = area_fn(outer) - sum(area_fn(r) for r in inners)
                rec["area_m2"], rec["area_ha"] = round(area, 1), round(area / 1e4, 4)
                rec["perimeter_m"] = round(len_fn(list(outer) + [outer[0]]), 1)
                rec["holes"] = len(inners)
                agg["area_ha"] = round(agg.get("area_ha", 0) + area / 1e4, 4)
            elif gtype == "LineString":
                rec["length_m"] = round(len_fn(outer), 1)
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
                lines.append(str(desc))
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
    return {
        "metadata": {"name": doc_name},
        "chunks": chunks,
        "tables": [table] if placemarks else [],
        "data": {
            "placemarks": placemarks,
            "bbox": [min(xs), min(ys), max(xs), max(ys)] if xs else None,
            "summary": totals,
            "crs": "projected (planar, units assumed metres)" if planar else "WGS84 lon/lat",
        },
        "warnings": [] if placemarks else ["no features with geometry found"],
    }


# ---------------------------------------------------------------------------
# KML / KMZ
# ---------------------------------------------------------------------------
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
    features = [
        {
            "name": _text(pm, "name") or "(unnamed)",
            "folder": " / ".join(folder) or None,
            "description": _strip_html(_text(pm, "description")),
            "properties": _properties(pm),
            "geoms": list(_geometries(pm)),
        }
        for folder, pm in found
    ]
    doc_name = next((_text(d, "name") for d in _iter(root, "Document")), None)
    result = _records(features, doc_name)
    result["warnings"] = [] if features and result["data"]["placemarks"] else ["no placemarks with geometry found"]
    return result


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


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------
def _pt(c) -> tuple[float, ...]:
    return tuple(float(v) for v in c[:3])


def _geojson_geoms(g: dict | None):
    """(type, outer, inners) for a GeoJSON geometry; Multi* split into parts."""
    if not g:
        return
    t, c = g.get("type"), g.get("coordinates")
    if t == "Point":
        yield "Point", [_pt(c)], []
    elif t == "MultiPoint":
        for p in c:
            yield "Point", [_pt(p)], []
    elif t == "LineString":
        yield "LineString", [_pt(p) for p in c], []
    elif t == "MultiLineString":
        for line in c:
            yield "LineString", [_pt(p) for p in line], []
    elif t == "Polygon":
        yield "Polygon", [_pt(p) for p in c[0]], [[_pt(p) for p in r] for r in c[1:]]
    elif t == "MultiPolygon":
        for poly in c:
            yield "Polygon", [_pt(p) for p in poly[0]], [[_pt(p) for p in r] for r in poly[1:]]
    elif t == "GeometryCollection":
        for sub in g.get("geometries", []):
            yield from _geojson_geoms(sub)


_NAME_KEYS = ("name", "Name", "NAME", "title", "Title", "label", "Label", "id", "ID", "Id")


def _feature_name(props: dict, fallback) -> str:
    for k in _NAME_KEYS:
        v = props.get(k)
        if v not in (None, ""):
            return str(v)
    return str(fallback)


@register([".geojson"], name="geojson", version="1", file_type="geojson")
def parse_geojson(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ParseError(f"{path.name}: invalid GeoJSON ({e})") from e
    if obj.get("type") == "FeatureCollection":
        feats = obj.get("features", [])
    elif obj.get("type") == "Feature":
        feats = [obj]
    elif "coordinates" in obj or obj.get("type") == "GeometryCollection":
        feats = [{"type": "Feature", "properties": {}, "geometry": obj}]
    else:
        raise ParseError(f"{path.name}: not GeoJSON (no FeatureCollection, Feature or geometry)")
    features = []
    for i, f in enumerate(feats, 1):
        props = dict(f.get("properties") or {})
        try:
            geoms = list(_geojson_geoms(f.get("geometry")))
        except (TypeError, IndexError, ValueError) as e:
            raise ParseError(f"{path.name}: bad geometry in feature {i} ({e})") from e
        features.append({
            "name": _feature_name(props, f.get("id", f"feature {i}")),
            "folder": props.get("folder"),
            "description": props.get("description"),
            "properties": {k: v for k, v in props.items() if k not in ("folder", "description")},
            "geoms": geoms,
        })
    xs = [p[0] for ft in features for _, outer, _ in ft["geoms"] for p in outer]
    ys = [p[1] for ft in features for _, outer, _ in ft["geoms"] for p in outer]
    planar = bool(xs) and (max(abs(v) for v in xs) > 180 or max(abs(v) for v in ys) > 90)
    result = _records(features, obj.get("name"), planar=planar)
    if planar:
        result["warnings"].append("coordinates outside lon/lat range: areas and lengths are planar, units assumed metres")
    return result


# ---------------------------------------------------------------------------
# Shapefile
# ---------------------------------------------------------------------------
_SHP_POINT = {1, 11, 21}
_SHP_LINE = {3, 13, 23}
_SHP_POLY = {5, 15, 25}
_SHP_MULTIPOINT = {8, 18, 28}


def _read_shp(raw: bytes, name: str) -> list[list[tuple]]:
    """Per record: list of (geometry type, outer, inners). XY only (Z and M are skipped)."""
    if len(raw) < 100 or struct.unpack(">i", raw[:4])[0] != 9994:
        raise ParseError(f"{name}: not a shapefile (bad file code)")
    pos, out = 100, []
    while pos + 8 <= len(raw):
        _, words = struct.unpack(">ii", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + words * 2]
        pos += 8 + words * 2
        if len(body) < 4:
            break
        st = struct.unpack("<i", body[:4])[0]
        geoms: list[tuple] = []
        if st in _SHP_POINT:
            x, y = struct.unpack("<2d", body[4:20])
            geoms.append(("Point", [(x, y)], []))
        elif st in _SHP_MULTIPOINT:
            n = struct.unpack("<i", body[36:40])[0]
            for i in range(n):
                x, y = struct.unpack("<2d", body[40 + 16 * i:56 + 16 * i])
                geoms.append(("Point", [(x, y)], []))
        elif st in _SHP_LINE | _SHP_POLY:
            n_parts, n_pts = struct.unpack("<2i", body[36:44])
            parts = list(struct.unpack(f"<{n_parts}i", body[44:44 + 4 * n_parts]))
            base = 44 + 4 * n_parts
            pts = [struct.unpack("<2d", body[base + 16 * i:base + 16 * i + 16]) for i in range(n_pts)]
            rings = [pts[a:b] for a, b in zip(parts, parts[1:] + [n_pts])]
            if st in _SHP_LINE:
                geoms += [("LineString", r, []) for r in rings]
            else:
                for r in rings:  # clockwise = outer ring, counter-clockwise = hole
                    if _signed_area(r) <= 0 or not geoms:
                        geoms.append(("Polygon", r, []))
                    else:
                        geoms[-1][2].append(r)
        out.append(geoms)
    return out


def _read_dbf(raw: bytes) -> list[dict]:
    if len(raw) < 32:
        return []
    n_rec, head_len, rec_len = struct.unpack("<IHH", raw[4:12])
    fields, p = [], 32
    while p + 32 <= len(raw) and raw[p] != 0x0D:
        fname = raw[p:p + 11].split(b"\x00")[0].decode("ascii", "replace")
        fields.append((fname, chr(raw[p + 11]), raw[p + 16]))
        p += 32
    rows = []
    for i in range(n_rec):
        rec = raw[head_len + i * rec_len: head_len + (i + 1) * rec_len]
        if not rec or rec[:1] == b"*":  # deleted
            rows.append(None)
            continue
        q, row = 1, {}
        for fname, ftype, flen in fields:
            cell = rec[q:q + flen]
            q += flen
            try:
                s = cell.decode("utf-8")
            except UnicodeDecodeError:
                s = cell.decode("cp1252", "replace")
            s = s.strip()
            if ftype in "NF" and s:
                try:
                    row[fname] = float(s) if ("." in s or "e" in s.lower()) else int(s)
                except ValueError:
                    row[fname] = s
            elif ftype == "L":
                row[fname] = {"T": True, "Y": True, "F": False, "N": False}.get(s.upper())
            else:
                row[fname] = s or None
        rows.append(row)
    return rows


@register([".shp"], name="shapefile", version="1", file_type="shp")
def parse_shapefile(path: Path) -> dict:
    records = _read_shp(path.read_bytes(), path.name)
    dbf = next((p for p in path.parent.glob(path.stem + ".*") if p.suffix.lower() == ".dbf"), None)
    prj = next((p for p in path.parent.glob(path.stem + ".*") if p.suffix.lower() == ".prj"), None)
    attrs = _read_dbf(dbf.read_bytes()) if dbf else []
    warnings = [] if dbf else ["no .dbf next to the .shp: features have no attributes"]
    features = []
    for i, geoms in enumerate(records):
        props = attrs[i] if i < len(attrs) and attrs[i] else {}
        features.append({"name": _feature_name(props, f"feature {i + 1}"), "folder": None,
                         "description": None, "properties": props, "geoms": geoms})
    xs = [p[0] for ft in features for _, outer, _ in ft["geoms"] for p in outer]
    ys = [p[1] for ft in features for _, outer, _ in ft["geoms"] for p in outer]
    wkt = prj.read_text(encoding="utf-8", errors="replace") if prj else ""
    if wkt.lstrip().startswith("PROJCS") or "PROJCS[" in wkt[:20]:
        planar = True
    elif wkt:
        planar = False
    else:
        planar = bool(xs) and (max(abs(v) for v in xs) > 180 or max(abs(v) for v in ys) > 90)
        warnings.append("no .prj: coordinate system guessed from the value range")
    result = _records(features, path.stem, planar=planar)
    result["warnings"] = warnings + result["warnings"]
    if planar:
        result["warnings"].append("projected coordinates: areas and lengths are planar; metres only if the .prj unit is metres")
    if wkt:
        result["metadata"]["crs_wkt"] = wkt.strip()[:200]
    return result
