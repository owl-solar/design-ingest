"""DXF / DWG drawings: layouts, single-line diagrams, cable routes, equipment plans.

Pure-Python reader for ASCII DXF (no extra dependency). DWG is converted to DXF
first with the free ODA File Converter (set ``ODA_FILE_CONVERTER`` to the exe
path if it is not found automatically).

What is extracted - the things a PV/BESS designer actually counts and checks:

data:
  header         AutoCAD version, units, drawing extents
  layers         name, colour, on/frozen/locked
  entity_counts  {"model": {type: n}, "paper": {type: n}}
  blocks         per block: direct inserts and total inserts including nested
                 blocks (e.g. 1 table block x 28 modules -> module count),
                 attribute tags
  inserts        list of block references with position, rotation, attributes
                 (capped by ``max_inserts``)
  title_block    attributes of inserts that look like a title block
  lengths        total LINE/POLYLINE/ARC length per layer (drawing units and m)
                 - cable/fence/road length per layer
  closed_areas   area of closed polylines per layer (m² if units known)
  dimensions     measured value + text override of DIMENSION entities

tables:
  one table per block that carries attributes (rows = inserts, cols = tags),
  layer summary.
chunks:
  all text (TEXT, MTEXT, ATTRIB, dimension overrides) grouped per layer and space.

Limits: dynamic blocks appear as anonymous ``*U..`` names; geometry inside
hatches/solids is not measured; binary DXF is not supported.
"""
from __future__ import annotations

import glob
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..core import register
from ..schema import Chunk, ParseError, Table

ACAD_VERSIONS = {
    "AC1009": "R12", "AC1012": "R13", "AC1014": "R14", "AC1015": "2000", "AC1018": "2004",
    "AC1021": "2007", "AC1024": "2010", "AC1027": "2013", "AC1032": "2018",
}
INSUNITS = {0: None, 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m", 7: "km", 14: "dm"}
UNIT_TO_M = {"in": 0.0254, "ft": 0.3048, "mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "dm": 0.1}
TITLE_TAGS = re.compile(
    r"(TITLE|TITEL|DWG|DRAWING|SHEET|REV|SCALE|DATE|PROJECT|CLIENT|DRAWN|CHECKED|APPROVED|NUMBER|NO\b)", re.I
)
MAX_TEXT_CHARS_PER_CHUNK = 6000


# ---------------------------------------------------------------------------
# low level: tags -> sections -> entities
# ---------------------------------------------------------------------------
def _decode(raw: bytes) -> str:
    if raw.startswith(b"AutoCAD Binary DXF"):
        raise ParseError("binary DXF is not supported - re-save as ASCII DXF")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    head = raw[:200_000].decode("latin-1")
    m = re.search(r"\$DWGCODEPAGE\s*\n\s*3\s*\n\s*ANSI_(\d+)", head)
    enc = f"cp{m.group(1)}" if m else "cp1252"
    try:
        return raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return raw.decode("latin-1")


def _tags(text: str):
    lines = text.splitlines()
    n = len(lines) - 1
    i = 0
    while i < n:
        code_s = lines[i].strip()
        try:
            code = int(code_s)
        except ValueError as e:
            raise ParseError(f"not a valid ASCII DXF (bad group code '{code_s[:20]}' at line {i + 1})") from e
        yield code, lines[i + 1].rstrip("\r")
        i += 2


class Entity:
    __slots__ = ("type", "tags")

    def __init__(self, etype: str):
        self.type = etype
        self.tags: list[tuple[int, str]] = []

    def get(self, code: int, default: Any = None) -> Any:
        for c, v in self.tags:
            if c == code:
                return v
        return default

    def all(self, code: int) -> list[str]:
        return [v for c, v in self.tags if c == code]

    def f(self, code: int, default: float = 0.0) -> float:
        v = self.get(code)
        try:
            return float(v) if v is not None else default
        except ValueError:
            return default

    def i(self, code: int, default: int = 0) -> int:
        v = self.get(code)
        try:
            return int(v.strip()) if v is not None else default
        except ValueError:
            return default

    @property
    def layer(self) -> str:
        return (self.get(8) or "0").strip()


def _sections(text: str) -> dict[str, list[Entity]]:
    sections: dict[str, list[Entity]] = {}
    current: list[Entity] | None = None
    ent: Entity | None = None
    expect_name = False
    for code, value in _tags(text):
        if code == 0:
            v = value.strip()
            if v == "SECTION":
                expect_name, current, ent = True, None, None
                continue
            if v == "ENDSEC":
                current, ent = None, None
                continue
            if v == "EOF":
                break
            if current is not None:
                ent = Entity(v)
                current.append(ent)
            continue
        if expect_name and code == 2:
            current = sections.setdefault(value.strip(), [])
            expect_name = False
            continue
        if ent is not None:
            ent.tags.append((code, value))
        elif current is not None and not current:
            # HEADER section: no 0-codes; keep variables in a pseudo entity
            ent = Entity("HEADER")
            current.append(ent)
            ent.tags.append((code, value))
    return sections


def _header(sec: list[Entity]) -> dict[str, Any]:
    if not sec:
        return {}
    out: dict[str, Any] = {}
    var = None
    for code, value in sec[0].tags:
        if code == 9:
            var = value.strip()
            out[var] = {}
        elif var:
            out[var][code] = value.strip()
    return out


# ---------------------------------------------------------------------------
# text cleaning
# ---------------------------------------------------------------------------
_UNI = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def clean_text(s: str) -> str:
    s = _UNI.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("%%c", "Ø").replace("%%C", "Ø").replace("%%d", "°").replace("%%D", "°")
    s = s.replace("%%p", "±").replace("%%P", "±")
    s = re.sub(r"%%[uUoOkK]", "", s)
    s = s.replace("\\P", "\n").replace("\\~", " ")
    s = re.sub(r"\\[ACHQTWfFpc][^;\\{}]*;", "", s)  # \fArial|b0;  \H2.5x;  \A1;  \pxqc;
    s = re.sub(r"\\S([^;]*)\^([^;]*);", r"\1/\2", s)  # stacked fractions
    s = re.sub(r"\\[LlOoKk]", "", s)
    s = s.replace("\\\\", "\\").replace("{", "").replace("}", "")
    return re.sub(r"[ \t]+", " ", s).strip()


def _entity_text(e: Entity) -> str | None:
    if e.type in ("TEXT", "ATTRIB", "ATTDEF"):
        t = e.get(1)
    elif e.type == "MTEXT":
        t = "".join(e.all(3)) + (e.get(1) or "")
    elif e.type == "DIMENSION":
        t = e.get(1)
        if t in ("", "<>", None):
            return None
        t = t.replace("<>", f"{e.f(42):g}")
    elif e.type in ("MULTILEADER", "MLEADER"):
        t = e.get(304)
    else:
        return None
    t = clean_text(t or "")
    return t or None


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def _bulge_len(x1, y1, x2, y2, bulge) -> float:
    chord = math.hypot(x2 - x1, y2 - y1)
    if not bulge:
        return chord
    theta = 4 * math.atan(abs(bulge))
    return chord * theta / (2 * math.sin(theta / 2)) if theta else chord


def _lwpoly(e: Entity) -> tuple[float, float, bool]:
    """length, area (shoelace, ignores bulges), closed"""
    pts: list[list[float]] = []
    for c, v in e.tags:
        if c == 10:
            pts.append([float(v), 0.0, 0.0])
        elif c == 20 and pts:
            pts[-1][1] = float(v)
        elif c == 42 and pts:
            pts[-1][2] = float(v)
    closed = bool(e.i(70) & 1)
    if len(pts) < 2:
        return 0.0, 0.0, closed
    segs = list(zip(pts, pts[1:])) + ([(pts[-1], pts[0])] if closed else [])
    length = sum(_bulge_len(a[0], a[1], b[0], b[1], a[2]) for a, b in segs)
    area = 0.0
    if closed and len(pts) >= 3:
        area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))) / 2
    return length, area, closed


def _length(e: Entity) -> float:
    if e.type == "LINE":
        return math.dist((e.f(10), e.f(20), e.f(30)), (e.f(11), e.f(21), e.f(31)))
    if e.type == "ARC":
        a = (e.f(51) - e.f(50)) % 360 or 360
        return math.radians(a) * e.f(40)
    if e.type == "CIRCLE":
        return 2 * math.pi * e.f(40)
    if e.type == "LWPOLYLINE":
        return _lwpoly(e)[0]
    return 0.0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _group_inserts(entities: list[Entity]) -> list[tuple[Entity, list[Entity]]]:
    """Attach ATTRIB entities (until SEQEND) to their INSERT."""
    out: list[tuple[Entity, list[Entity]]] = []
    i = 0
    while i < len(entities):
        e = entities[i]
        if e.type in ("INSERT", "MINSERT"):
            attribs: list[Entity] = []
            if e.i(66) == 1:
                j = i + 1
                while j < len(entities) and entities[j].type == "ATTRIB":
                    attribs.append(entities[j])
                    j += 1
                if j < len(entities) and entities[j].type == "SEQEND":
                    j += 1
                out.append((e, attribs))
                i = j
                continue
            out.append((e, attribs))
        i += 1
    return out


def _multiplicity(e: Entity) -> int:
    # In DXF a MINSERT (array insert) is an INSERT with column count (70) and row count (71)
    return max(1, e.i(70, 1)) * max(1, e.i(71, 1))


def parse_dxf_text(text: str, max_inserts: int = 5000) -> dict:
    secs = _sections(text)
    hdr = _header(secs.get("HEADER", []))
    warnings: list[str] = []

    acadver = hdr.get("$ACADVER", {}).get(1)
    units = INSUNITS.get(int(hdr.get("$INSUNITS", {}).get(70, 0) or 0))
    to_m = UNIT_TO_M.get(units) if units else None

    def _pt(var):
        d = hdr.get(var, {})
        try:
            return [float(d[10]), float(d[20])]
        except (KeyError, ValueError):
            return None

    header = {
        "acad_version": acadver,
        "acad_release": ACAD_VERSIONS.get(acadver or "", None),
        "units": units,
        "extmin": _pt("$EXTMIN"),
        "extmax": _pt("$EXTMAX"),
        "codepage": hdr.get("$DWGCODEPAGE", {}).get(3),
    }
    if not units:
        warnings.append("drawing units ($INSUNITS) not set - lengths/areas given in drawing units only")

    # layers
    layers = []
    for e in secs.get("TABLES", []):
        if e.type == "LAYER" and e.get(2):
            color = e.i(62, 7)
            flags = e.i(70)
            layers.append({
                "name": e.get(2).strip(), "color": abs(color), "on": color >= 0,
                "frozen": bool(flags & 1), "locked": bool(flags & 4), "linetype": (e.get(6) or "").strip() or None,
            })

    # block definitions
    block_defs: dict[str, list[Entity]] = {}
    block_layer: dict[str, str] = {}
    current: list[Entity] | None = None
    for e in secs.get("BLOCKS", []):
        if e.type == "BLOCK":
            name = (e.get(2) or "").strip()
            current = block_defs.setdefault(name, [])
            block_layer[name] = e.layer
        elif e.type == "ENDBLK":
            current = None
        elif current is not None:
            current.append(e)

    # model/paper space entities
    space_entities: dict[str, list[Entity]] = {"model": [], "paper": []}
    for e in secs.get("ENTITIES", []):
        space_entities["paper" if e.i(67) == 1 else "model"].append(e)
    for name, ents in block_defs.items():
        if name.upper().startswith("*PAPER_SPACE"):
            space_entities["paper"].extend(ents)
        elif name.upper() == "*MODEL_SPACE" and not secs.get("ENTITIES"):
            space_entities["model"].extend(ents)

    # nested block contents (memoised, cycle-safe)
    memo: dict[str, Counter] = {}

    def nested(name: str, seen: frozenset = frozenset()) -> Counter:
        if name in memo:
            return memo[name]
        c: Counter = Counter()
        if name in seen:
            return c
        for ins, _ in _group_inserts(block_defs.get(name, [])):
            child = (ins.get(2) or "").strip()
            m = _multiplicity(ins)
            c[child] += m
            for k, v in nested(child, seen | {name}).items():
                c[k] += m * v
        memo[name] = c
        return c

    entity_counts = {s: dict(Counter(e.type for e in ents)) for s, ents in space_entities.items()}

    direct: dict[str, Counter] = {"model": Counter(), "paper": Counter()}
    total_model: Counter = Counter()
    inserts: list[dict] = []
    attr_rows: dict[str, list[dict]] = defaultdict(list)
    title_block: dict[str, str] = {}
    texts: dict[tuple[str, str], list[str]] = defaultdict(list)

    for space, ents in space_entities.items():
        for ins, attribs in _group_inserts(ents):
            name = (ins.get(2) or "").strip()
            m = _multiplicity(ins)
            direct[space][name] += m
            if space == "model":
                total_model[name] += m
                for k, v in nested(name).items():
                    total_model[k] += m * v
            attrs = {}
            for a in attribs:
                tag = (a.get(2) or "").strip()
                val = _entity_text(a) or ""
                attrs[tag] = val
                if val:
                    texts[(space, a.layer)].append(f"{tag}: {val}")
            if attrs:
                attr_rows[name].append({"_layer": ins.layer, **attrs})
                if sum(bool(TITLE_TAGS.search(t)) for t in attrs) >= 3:
                    title_block.update({k: v for k, v in attrs.items() if v})
            if len(inserts) < max_inserts:
                inserts.append({
                    "block": name, "layer": ins.layer, "space": space,
                    "x": round(ins.f(10), 4), "y": round(ins.f(20), 4),
                    "rotation": round(ins.f(50), 3), "scale_x": ins.f(41, 1.0), "scale_y": ins.f(42, 1.0),
                    "count": m, "attributes": attrs,
                })
        for e in ents:
            if e.type in ("INSERT", "ATTRIB", "SEQEND", "MINSERT"):
                continue
            t = _entity_text(e)
            if t:
                texts[(space, e.layer)].append(t)

    n_ins = sum(sum(c.values()) for c in direct.values())
    if len(inserts) < sum(len(_group_inserts(v)) for v in space_entities.values()):
        warnings.append(f"insert list capped at {max_inserts} (counts are complete)")
    anon = sum(v for k, v in total_model.items() if k.startswith("*U"))
    if anon:
        warnings.append(f"{anon} insert(s) of anonymous/dynamic blocks (*U...) - original block names not resolved")

    # attribute definitions per block
    attdefs = {
        name: [(e.get(2) or "").strip() for e in ents if e.type == "ATTDEF"]
        for name, ents in block_defs.items()
    }
    blocks = {}
    for name in sorted(set(block_defs) | set(total_model) | set(direct["paper"])):
        if name.startswith("*") and name.upper().startswith(("*MODEL_SPACE", "*PAPER_SPACE")):
            continue
        if not (direct["model"][name] or direct["paper"][name] or total_model[name] or attdefs.get(name)):
            continue
        blocks[name] = {
            "inserts_model_direct": direct["model"][name],
            "inserts_model_total": total_model[name],
            "inserts_paper": direct["paper"][name],
            "attribute_tags": attdefs.get(name, []),
            "defined": name in block_defs,
        }

    # lengths and areas per layer (model space entities only; geometry inside blocks is not counted)
    lengths: Counter = Counter()
    areas: Counter = Counter()
    for e in space_entities["model"]:
        L = _length(e)
        if L:
            lengths[e.layer] += L
        if e.type == "LWPOLYLINE":
            _, A, closed = _lwpoly(e)
            if closed and A:
                areas[e.layer] += A
    lengths_out = {
        k: {"drawing_units": round(v, 3), "m": round(v * to_m, 3) if to_m else None}
        for k, v in sorted(lengths.items(), key=lambda kv: -kv[1])
    }
    areas_out = {
        k: {"drawing_units2": round(v, 3), "m2": round(v * to_m**2, 3) if to_m else None}
        for k, v in sorted(areas.items(), key=lambda kv: -kv[1])
    }

    dimensions = [
        {"layer": e.layer, "measured": round(e.f(42), 4), "text": _entity_text(e)}
        for e in space_entities["model"] + space_entities["paper"] if e.type == "DIMENSION"
    ]

    # chunks: text per (space, layer)
    chunks: list[Chunk] = []
    for (space, layer), items in sorted(texts.items()):
        joined, part = "", 1
        for t in items:
            if len(joined) + len(t) > MAX_TEXT_CHARS_PER_CHUNK and joined:
                chunks.append(Chunk(text=joined.strip(), kind="cad_text", layer=layer, meta={"space": space, "part": part}))
                joined, part = "", part + 1
            joined += t + "\n"
        if joined.strip():
            chunks.append(Chunk(text=joined.strip(), kind="cad_text", layer=layer, meta={"space": space, "part": part}))

    # summary chunk: what is in the drawing
    top = sorted(((k, v) for k, v in blocks.items() if v["inserts_model_total"]), key=lambda kv: -kv[1]["inserts_model_total"])
    lines = [
        f"Drawing: AutoCAD {header['acad_release'] or acadver}, units {units or 'unknown'}, "
        f"{len(layers)} layers, {n_ins} block inserts"
    ]
    if title_block:
        lines.append("Title block: " + "; ".join(f"{k}={v}" for k, v in title_block.items()))
    if top:
        lines.append("Block counts in model space (incl. nested): " + ", ".join(
            f"{k} x{v['inserts_model_total']}" for k, v in top[:30]))
    if lengths_out:
        lines.append("Line length per layer: " + ", ".join(
            f"{k} {v['m'] if v['m'] is not None else v['drawing_units']}{' m' if v['m'] is not None else ''}"
            for k, v in list(lengths_out.items())[:20]))
    chunks.insert(0, Chunk(text="\n".join(lines), kind="parameters"))

    # tables
    tables = [Table(
        name="block counts",
        columns=["block", "model_direct", "model_total_incl_nested", "paper", "attribute_tags"],
        rows=[[k, v["inserts_model_direct"], v["inserts_model_total"], v["inserts_paper"], ", ".join(v["attribute_tags"])]
              for k, v in blocks.items()],
    )]
    for bname, rows in attr_rows.items():
        cols = ["_layer"] + sorted({k for r in rows for k in r if k != "_layer"})
        tables.append(Table(name=f"attributes: {bname}", columns=cols, rows=[[r.get(c) for c in cols] for r in rows]))
    if layers:
        ent_by_layer = Counter(e.layer for e in space_entities["model"])
        tables.append(Table(
            name="layers", columns=["layer", "on", "frozen", "entities_model", "length_m"],
            rows=[[l["name"], l["on"], l["frozen"], ent_by_layer.get(l["name"], 0),
                   (lengths_out.get(l["name"]) or {}).get("m")] for l in layers],
        ))

    return {
        "metadata": {k: v for k, v in header.items() if v},
        "chunks": chunks,
        "tables": tables,
        "data": {
            "header": header,
            "layers": layers,
            "entity_counts": entity_counts,
            "blocks": blocks,
            "inserts": inserts,
            "title_block": title_block,
            "lengths": lengths_out,
            "closed_areas": areas_out,
            "dimensions": dimensions,
            "summary": {
                "units": units,
                "layers": len(layers),
                "block_inserts": n_ins,
                "top_blocks": {k: v["inserts_model_total"] for k, v in top[:15]},
            },
        },
        "warnings": warnings,
    }


@register([".dxf"], name="dxf", version="1", file_type="dxf")
def parse_dxf(path: Path, max_inserts: int = 5000) -> dict:
    return parse_dxf_text(_decode(path.read_bytes()), max_inserts=max_inserts)


# ---------------------------------------------------------------------------
# DWG via ODA File Converter
# ---------------------------------------------------------------------------
def find_oda_converter() -> str | None:
    env = os.environ.get("ODA_FILE_CONVERTER")
    if env and Path(env).exists():
        return env
    for name in ("ODAFileConverter", "ODAFileConverter.exe"):
        p = shutil.which(name)
        if p:
            return p
    patterns = [
        r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
        r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe",
        "/usr/bin/ODAFileConverter",
        "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
    ]
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def dwg_to_dxf(path: Path, out_dir: Path, version: str = "ACAD2018") -> Path:
    exe = find_oda_converter()
    if not exe:
        raise ParseError(
            "DWG needs the free ODA File Converter (https://www.opendesign.com/guestfiles/oda_file_converter). "
            "Install it or set ODA_FILE_CONVERTER to ODAFileConverter.exe; or save the drawing as DXF in AutoCAD."
        )
    with tempfile.TemporaryDirectory() as tmp_in:
        src = Path(tmp_in) / path.name
        shutil.copy2(path, src)
        cmd = [exe, tmp_in, str(out_dir), version, "DXF", "0", "1", path.name]
        try:
            subprocess.run(cmd, check=True, timeout=600, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise ParseError(f"ODA File Converter failed: {e.stderr.decode(errors='replace')[:300]}") from e
    hits = [p for p in out_dir.glob("*") if p.suffix.lower() == ".dxf" and p.stem.lower() == path.stem.lower()]
    if not hits:
        raise ParseError(f"ODA File Converter produced no DXF for {path.name} (corrupt or newer DWG version?)")
    return hits[0]


@register([".dwg"], name="dwg", version="1", file_type="dwg")
def parse_dwg(path: Path, max_inserts: int = 5000) -> dict:
    with tempfile.TemporaryDirectory() as tmp_out:
        dxf = dwg_to_dxf(path, Path(tmp_out))
        result = parse_dxf_text(_decode(dxf.read_bytes()), max_inserts=max_inserts)
    result["warnings"].insert(0, "converted from DWG with ODA File Converter")
    return result
