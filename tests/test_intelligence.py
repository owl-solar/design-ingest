"""Quantities, specs, classification, registers, profiles, GeoJSON / Shapefile, eval harness.

Everything here runs on generated data: no real project or vendor files.
"""
import json
import struct

import pytest

from design_ingest import ingest
from design_ingest.doctype import classify, parse_filename
from design_ingest.profiles import DEFAULT_PROFILE, load_profile
from design_ingest.quantities import parse_quantities
from design_ingest.registers import extract_registers
from design_ingest.schema import Document, Table
from design_ingest.specs import canonical_specs, extract_specs


# ---------------------------------------------------------------- quantities
def _q(text, **kw):
    return [(q.value, q.value_max, q.unit, q.dimension, q.op, q.count, q.condition.get("temperature_c"))
            for q in parse_quantities(text, **kw)]


@pytest.mark.parametrize("text, expected", [
    ("1500 V", [(1500, None, "V", "voltage", None, None, None)]),
    ("500 V – 1500 V", [(500, 1500, "V", "voltage", None, None, None)]),
    ("640 – 920V", [(640, 920, "V", "voltage", None, None, None)]),
    ("-30 to 60℃", [(-30, 60, "degC", "temperature", None, None, None)]),
    ("- 30 to 60 ℃", [(-30, 60, "degC", "temperature", None, None, None)]),
    ("< 3 % (at nominal power)", [(3, None, "%", "percent", "<", None, None)]),
    ("≤116 kg", [(116, None, "kg", "mass", "<=", None, None)]),
    ("99.02 % / 98.8 %", [(99.02, None, "%", "percent", None, None, None),
                          (98.8, None, "%", "percent", None, None, None)]),
    ("352 kVA @ 30℃ / 320 kVA @40 ℃", [(352, None, "kVA", "apparent_power", None, None, 30),
                                                (320, None, "kVA", "apparent_power", None, None, 40)]),
    ("12 * 40 A (Optional: 16 * 30 A)", [(40, None, "A", "current", None, 12, None),
                                         (30, None, "A", "current", None, 16, None)]),
    ("0,5 %", [(0.5, None, "%", "percent", None, None, None)]),
    ("1,500 V", [(1500, None, "V", "voltage", None, None, None)]),
    ("4000 m (> 3000 m derating)", [(4000, None, "m", "length", None, None, None),
                                    (3000, None, "m", "length", ">", None, None)]),
    ("IP66", []),
    ("X1-20 in 2 boxes", []),
])
def test_parse_quantities(text, expected):
    assert _q(text) == expected


def test_quantity_si_values_and_qualifiers():
    q = parse_quantities("1500 Vdc")[0]
    assert q.unit == "V" and q.qualifier == "dc" and q.si_value == 1500
    assert parse_quantities("0.1 kWh")[0].si_value == pytest.approx(100)  # Wh
    assert parse_quantities("42444 MWh")[0].si_value == pytest.approx(42444e6)
    assert parse_quantities("116 kg")[0].si_value == 116
    assert parse_quantities("2 t")[0].si_value == 2000
    assert parse_quantities("30 mA")[0].si_value == pytest.approx(0.03)
    assert parse_quantities("1.5 kWp")[0].unit == "kWp"


def test_quantity_box_sizes_with_unit_on_each_number():
    for text in ("1136 * 870 * 361 mm", "1136 mm * 870 mm * 361 mm"):
        qs = parse_quantities(text)
        assert [(q.value, q.index, q.unit) for q in qs] == [(1136, 0, "mm"), (870, 1, "mm"), (361, 2, "mm")]
        assert qs[0].si_value == pytest.approx(1.136)


def test_quantity_unitless_only_on_request():
    assert parse_quantities("12") == []
    assert [q.value for q in parse_quantities("12 (optional: 16)", allow_unitless=True)] == [12, 16]


# --------------------------------------------------------------------- specs
def _spec_table():
    rows = [
        ["Input (DC)", ""],
        ["Max. PV input voltage", "1500 V"],
        ["MPP voltage range", "500 V – 1500 V"],
        ["No. of independent MPP inputs", "12 (optional: 16)"],
        ["Output (AC)", ""],
        ["AC output power", "352 kVA @ 30℃ / 320 kVA @40 ℃"],
        ["Max. efficiency / European efficiency", "99.02 % / 98.8 %"],
        ["General Data", ""],
        ["Dimensions (W*H*D)", "1136 * 870 * 361 mm"],
        ["Weight*", "≤116 kg"],
        ["Degree of protection", "IP66"],
        ["Operating ambient temperature range", "-30 to 60℃"],
    ]
    # section rows as the PDF parser leaves them (folded into the key by _table_from_rows)
    folded, section = [], None
    for k, v in rows:
        if not v:
            section = k
            continue
        folded.append([f"{section} > {k}", v])
    return Table(columns=["Type designation", "X1"], rows=folded)


def test_extract_and_canonical_specs():
    specs = extract_specs([_spec_table()])
    assert list(specs) == ["X1"]
    entry = specs["X1"]["Input (DC) > Max. PV input voltage"]
    assert entry["raw"] == "1500 V" and entry["quantities"][0]["value"] == 1500
    c = canonical_specs(specs["X1"])
    assert c["dc_voltage_max_v"] == 1500 and c["mppt_voltage_range_v"] == [500, 1500]
    assert c["n_mppt"] == 12
    assert c["ac_power"] == [{"value": 352, "unit": "kVA", "temperature_c": 30.0},
                             {"value": 320, "unit": "kVA", "temperature_c": 40.0}]
    assert c["efficiency_max_pct"] == 99.02 and c["efficiency_euro_pct"] == 98.8
    assert c["dimensions"] == {"values_mm": [1136, 870, 361], "order": "W x H x D"}
    assert c["weight_kg"] == 116 and c["ip_rating"] == "IP66" and c["operating_temperature_c"] == [-30, 60]


def test_specs_need_enough_key_value_rows():
    small = Table(columns=["a", "b"], rows=[["x", "1 V"], ["y", "2 V"]])
    assert extract_specs([small]) == {}


# ------------------------------------------------------------ classification
def test_parse_filename():
    f = parse_filename("DS_20231013_Model-1_Datasheet_V5_EN.pdf")
    assert f == {"code": "DS", "date": "2023-10-13", "version": "V5", "language": "EN"}
    f = parse_filename("PROJ1001-BAL-110K Trench Layout.pdf")
    assert f["drawing_no"] == "PROJ1001-BAL-110" and f["revision"] == "K" and f["title"] == "Trench Layout"
    f = parse_filename("PROJ1001-550 Rev00 Earthing Calculations.pdf")
    assert f["revision"] == "00" and f["title"] == "Earthing Calculations"
    assert "language" not in parse_filename("Certificate_Co_IEC 62443-4-2_NL-102202_final.pdf")  # NL is a number
    assert parse_filename("notes.txt") == {}


def _doc(filename, file_type="pdf", **data):
    return Document(source=filename, filename=filename, file_type=file_type, sha256="0", size_bytes=1,
                    parser="t", parser_version="1", data=data)


@pytest.mark.parametrize("filename, file_type, data, expected", [
    ("DS_20231013_X1_Datasheet_V5_EN.pdf", "pdf", {"pages": 2}, "datasheet"),
    ("UM_20211010_Logger_User Manual_V11_EN.pdf", "pdf", {"pages": 94}, "manual"),
    ("Certificate_X1_CE-EMC_202306.pdf", "pdf", {"pages": 1}, "certificate"),
    ("TestReport_X1_IP66.pdf", "pdf", {"pages": 13}, "test_report"),
    ("Logger_communication protocol_1.0.pdf", "pdf", {"pages": 7}, "protocol"),
    ("PROJ1001-110 Trench Layout.pdf", "pdf", {"pages": 1, "drawing_pages": [1]}, "drawing"),
    ("PROJ1001-500 Simulation.pdf", "pdf", {"pages": 14, "report": "pvsyst"}, "pvsyst_report"),
    ("module.PAN", "pan", {}, "component_file"),
    ("site.kmz", "kmz", {}, "site_file"),
    ("area.geojson", "geojson", {}, "site_file"),
    ("plan.dwg", "dwg", {}, "drawing"),
    ("PROJ1001-503 BOM.xlsx", "xlsx", {}, "bom"),
])
def test_classify_types(filename, file_type, data, expected):
    info = classify(_doc(filename, file_type, **data))
    assert info["doc_type"] == expected and info["confidence"] in ("high", "medium")


def test_classify_unknown_is_other_and_low():
    info = classify(_doc("scan0001.pdf", pages=3))
    assert info["doc_type"] == "other" and info["confidence"] == "low"


# ----------------------------------------------------------------- registers
def test_extract_registers_formats():
    t = Table(page=7, columns=["No.", "Name", "Address", "Data type", "Data range", "Unit", "Note"], rows=[
        ["1.", "Protocol num", "4950 - 4951", "U32", "", "", ""],
        ["2.", "Daily power yields", "5003", "U16", "", "0.1 kWh", ""],
        ["3.", "Internal temperature", "0x1F40", "S16", "-400-1500", "0.1℃", "R"],
        ["4.", "Serial number", "5010 - 5019", "UTF-8", "", "", ""],
        ["", "section row without address", "", "", "", "", ""],
    ])
    r = extract_registers([t])
    assert [x["address"] for x in r] == [4950, 5003, 8000, 5010]
    assert r[0]["address_end"] == 4951 and r[0]["n_registers"] == 2 and r[0]["data_type"] == "U32"
    assert r[1]["scale"] == 0.1 and r[1]["unit"] == "kWh" and r[1]["n_registers"] == 1
    assert r[2]["scale"] == 0.1 and r[2]["unit"] == "℃" and r[2]["range"] == "-400-1500"
    assert r[3]["n_registers"] == 10 and r[3]["page"] == 7


def test_extract_registers_synonyms_and_scale_column():
    t = Table(columns=["Register", "Signal", "Type", "Scale", "Unit", "R/W"], rows=[
        ["30001", "Grid voltage", "UINT16", "0.1", "V", "R"],
        ["30002", "Grid current", "UINT16", "0.01", "A", "R"],
        ["40001", "Power limit", "UINT16", "1", "%", "RW"],
    ])
    r = extract_registers([t])
    assert [(x["name"], x["address"], x["scale"], x["unit"], x["access"]) for x in r] == [
        ("Grid voltage", 30001, 0.1, "V", "R"), ("Grid current", 30002, 0.01, "A", "R"),
        ("Power limit", 40001, 1.0, "%", "RW")]


def test_registers_continuation_and_no_false_positive():
    head = Table(page=1, columns=["No.", "Name", "Address", "Type"], rows=[
        ["1", "A", "100", "U16"], ["2", "B", "101", "U16"], ["3", "C", "102", "U16"]])
    cont = Table(page=2, rows=[["4", "D", "103", "U16"], ["5", "E", "104", "U16"], ["6", "F", "105", "U16"]])
    # a model table of the same width with its own header must not be read with the register layout
    models = Table(page=3, columns=["Model", "Code", "MPPT", "Strings"], rows=[
        ["M1", "0x27", "2", "4"], ["M2", "0x26", "2", "3"], ["M3", "0x29", "2", "3"]])
    r = extract_registers([head, cont, models])
    assert [x["name"] for x in r] == ["A", "B", "C", "D", "E", "F"]


# ------------------------------------------------------------------ profiles
def test_load_profile_merge(tmp_path):
    p = tmp_path / "office.json"
    p.write_text(json.dumps({"cad": {"layer_groups": {"mv_cables": "kabel mv", "fence": None},
                                     "table_block_regex": "^TABLE"}}), encoding="utf-8")
    prof = load_profile(p)
    groups = list(prof["cad"]["layer_groups"])
    assert groups[0] == "mv_cables" and prof["cad"]["layer_groups"]["mv_cables"] == "kabel mv"  # new group first
    assert "fence" not in groups and "trenches" in groups  # removed / kept
    assert prof["cad"]["table_block_regex"] == "^TABLE" and prof["pdf"] == DEFAULT_PROFILE["pdf"]
    assert load_profile({"pdf": {"title_keys": ["Projekt"]}})["pdf"]["title_keys"] == ["Projekt"]
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / "missing.json")


def test_profile_from_environment(tmp_path, monkeypatch):
    p = tmp_path / "env.json"
    p.write_text(json.dumps({"cad": {"fence_layer_regex": "zaun"}}), encoding="utf-8")
    monkeypatch.setenv("DESIGN_INGEST_PROFILE", str(p))
    assert load_profile()["cad"]["fence_layer_regex"] == "zaun"


def test_cad_summary_follows_profile():
    from design_ingest.parsers.cad import _build_summary

    blocks = {"TABLE 2x12": {"inserts_model_total": 5}, "3P24 PVBlock": {"inserts_model_total": 9}}
    lengths = {"Kabel MV Strecke": {"m": 100.0}, "Zaun": {"m": 300.0}}
    areas = {"Zaun": {"m2": 40_000.0}}
    prof = load_profile({"cad": {"layer_groups": {"mv_cables": "kabel mv", "fence": "zaun"},
                                 "table_block_regex": "^TABLE (\\d+)x(\\d+)",
                                 "fence_layer_regex": "zaun"}})
    s = _build_summary("m", 2, 14, [], blocks, lengths, areas, prof)
    assert s["modules_estimate"]["total"] == 5 * 24  # only the profile's block pattern counts
    assert s["length_m_by_group"] == {"fence": 300.0, "mv_cables": 100.0}
    assert s["fenced_area_ha"] == 4.0


def test_profile_option_ignored_by_parsers_without_one(samples):
    d = ingest(samples["pan"], use_cache=False, profile={"cad": {"fence_layer_regex": "x"}})
    assert d.data["summary"]["pnom_w"] == 550.0


# ------------------------------------------------------------ enrich via PDFs
def test_ingest_datasheet_gets_type_specs_and_summary(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    from reportlab.platypus import Table as RLTable
    from reportlab.platypus import TableStyle

    rows = [["Type designation", "X1"]] + [[k, v] for k, v in [
        ("Max. PV input voltage", "1500 V"), ("MPP voltage range", "500 V – 1500 V"),
        ("AC output power", "320 kVA @40 C"), ("Max. AC output current", "254 A"),
        ("Degree of protection", "IP66"), ("Weight*", "≤116 kg")]]
    t = RLTable(rows)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    p = tmp_path / "DS_20240101_X1_Datasheet_V1_EN.pdf"
    SimpleDocTemplate(str(p), pagesize=A4).build([Paragraph("X1 Inverter Datasheet", getSampleStyleSheet()["Title"]), t])
    d = ingest(p, use_cache=False)
    assert d.data["doc_info"]["doc_type"] == "datasheet" and d.data["doc_info"]["filename"]["date"] == "2024-01-01"
    assert d.data["specs"]["X1"]["Max. PV input voltage"]["quantities"][0]["value"] == 1500
    s = d.data["summary"]
    assert s["dc_voltage_max_v"] == 1500 and s["mppt_voltage_range_v"] == [500, 1500]
    assert s["ac_current_max_a"] == 254 and s["ip_rating"] == "IP66" and s["weight_kg"] == 116


def test_ingest_register_map_over_two_pages(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate
    from reportlab.platypus import Table as RLTable
    from reportlab.platypus import TableStyle

    rows = [["No.", "Name", "Address", "Data type", "Unit"]]
    rows += [[f"{i}.", f"Signal number {i}", str(5000 + i), "U16", "0.1 V" if i % 2 else "A"] for i in range(1, 91)]
    t = RLTable(rows, repeatRows=0)  # header only on the first page, as in long vendor protocols
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    p = tmp_path / "Device_Communication Protocol_V1.pdf"
    SimpleDocTemplate(str(p), pagesize=A4).build([t])
    d = ingest(p, use_cache=False)
    assert d.data["pages"] >= 2
    regs = d.data["registers"]
    assert len(regs) == 90 and regs[0]["address"] == 5001 and regs[-1]["address"] == 5090
    assert regs[0]["scale"] == 0.1 and regs[0]["unit"] == "V" and regs[1]["unit"] == "A"
    assert {r["page"] for r in regs} == set(range(1, d.data["pages"] + 1))
    assert d.data["doc_info"]["doc_type"] == "protocol"


def test_enrich_never_breaks_a_parse(monkeypatch, samples):
    import design_ingest.doctype as cl

    monkeypatch.setattr(cl, "classify", lambda doc: 1 / 0)
    d = ingest(samples["pan"], use_cache=False)
    assert d.data["summary"]["pnom_w"] == 550.0
    assert any("enrichment skipped" in w for w in d.warnings)


# ------------------------------------------------------------ GeoJSON / SHP
def test_geojson_polygon_line_multi_and_planar(tmp_path):
    fc = {"type": "FeatureCollection", "name": "Site", "features": [
        {"type": "Feature", "properties": {"name": "Plot", "folder": "Boundaries", "zone": "agri"},
         "geometry": {"type": "Polygon", "coordinates": [
             [[10, 50], [10.002, 50], [10.002, 50.001], [10, 50.001], [10, 50]],
             [[10.0005, 50.0002], [10.0005, 50.0004], [10.0007, 50.0004], [10.0007, 50.0002], [10.0005, 50.0002]]]}},
        {"type": "Feature", "properties": {"name": "Cable"},
         "geometry": {"type": "LineString", "coordinates": [[10, 50], [10, 50.001]]}},
        {"type": "Feature", "properties": {"name": "Two"},
         "geometry": {"type": "MultiPoint", "coordinates": [[10, 50], [10.1, 50.1]]}},
    ]}
    p = tmp_path / "site.geojson"
    p.write_text(json.dumps(fc), encoding="utf-8")
    d = ingest(p, use_cache=False)
    pm = {r["name"]: r for r in d.data["placemarks"] if r["geometry_type"] != "Point"}
    plot = pm["Plot"]
    assert plot["folder"] == "Boundaries" and plot["holes"] == 1
    assert plot["properties"] == {"name": "Plot", "zone": "agri"}  # all attributes kept except folder/description
    # 0.002 x 0.001 deg at 50N = about 143 m x 111 m, minus a 14 m x 22 m hole
    assert plot["area_m2"] == pytest.approx(15_914 - 311, rel=0.01)
    assert pm["Cable"]["length_m"] == pytest.approx(111.2, rel=0.01)
    assert d.data["summary"]["Point"]["count"] == 2  # MultiPoint split
    assert d.data["crs"] == "WGS84 lon/lat" and not d.warnings

    planar = {"type": "Feature", "properties": {"name": "UTM"}, "geometry": {"type": "Polygon", "coordinates": [
        [[500000, 5500000], [501000, 5500000], [501000, 5500500], [500000, 5500500], [500000, 5500000]]]}}
    q = tmp_path / "utm.geojson"
    q.write_text(json.dumps(planar), encoding="utf-8")
    d2 = ingest(q, use_cache=False)
    assert d2.data["placemarks"][0]["area_m2"] == 500_000 and "planar" in d2.warnings[0]


def test_geojson_rejects_other_json(tmp_path):
    from design_ingest import ParseError

    p = tmp_path / "x.geojson"
    p.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(ParseError):
        ingest(p, use_cache=False)


def _write_shapefile(base, rings, attrs, prj=None):
    """A minimal polygon shapefile: one record, rings given as lists of (x, y)."""
    pts = [pt for r in rings for pt in r]
    parts, n = [], 0
    for r in rings:
        parts.append(n)
        n += len(r)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    body = struct.pack("<i4d2i", 5, min(xs), min(ys), max(xs), max(ys), len(rings), len(pts))
    body += struct.pack(f"<{len(parts)}i", *parts)
    for x, y in pts:
        body += struct.pack("<2d", x, y)
    content_words = len(body) // 2
    header = struct.pack(">7i", 9994, 0, 0, 0, 0, 0, 50 + 4 + content_words)
    header += struct.pack("<2i8d", 1000, 5, min(xs), min(ys), max(xs), max(ys), 0, 0, 0, 0)
    (base.with_suffix(".shp")).write_bytes(header + struct.pack(">2i", 1, content_words) + body)
    # dBase: one field NAME (C, 20) and one AREA_HA (N, 10)
    fields = [(b"NAME", b"C", 20, 0), (b"CAP", b"N", 10, 0)]
    rec_len = 1 + sum(f[2] for f in fields)
    head_len = 32 + 32 * len(fields) + 1
    dbf = struct.pack("<4BIHH20x", 3, 24, 1, 1, 1, head_len, rec_len)
    for name, typ, ln, dec in fields:
        dbf += name.ljust(11, b"\x00") + typ + b"\x00" * 4 + bytes([ln, dec]) + b"\x00" * 14
    dbf += b"\x0d" + b" " + attrs["NAME"].encode().ljust(20) + str(attrs["CAP"]).rjust(10).encode()
    (base.with_suffix(".dbf")).write_bytes(dbf)
    if prj:
        base.with_suffix(".prj").write_text(prj, encoding="utf-8")


def test_shapefile_projected_polygon_with_hole(tmp_path):
    outer = [(0, 0), (0, 500), (1000, 500), (1000, 0), (0, 0)]  # clockwise = outer ring
    hole = [(100, 100), (200, 100), (200, 200), (100, 200), (100, 100)]  # counter-clockwise = hole
    _write_shapefile(tmp_path / "plot", [outer, hole], {"NAME": "Plot A", "CAP": 42},
                     prj='PROJCS["ETRS89 / UTM 32N",GEOGCS["ETRS89"],UNIT["metre",1]]')
    d = ingest(tmp_path / "plot.shp", use_cache=False)
    pm = d.data["placemarks"][0]
    assert pm["name"] == "Plot A" and pm["properties"] == {"NAME": "Plot A", "CAP": 42}
    assert pm["holes"] == 1 and pm["area_m2"] == 500_000 - 10_000
    assert d.data["crs"].startswith("projected") and any("planar" in w for w in d.warnings)


def test_shapefile_geographic_without_projected_crs(tmp_path):
    ring = [(10, 50), (10, 50.001), (10.002, 50.001), (10.002, 50), (10, 50)]
    _write_shapefile(tmp_path / "geo", [ring], {"NAME": "G", "CAP": 1}, prj='GEOGCS["WGS 84",DATUM["D_WGS_1984"]]')
    d = ingest(tmp_path / "geo.shp", use_cache=False)
    assert d.data["crs"] == "WGS84 lon/lat" and d.data["placemarks"][0]["area_m2"] == pytest.approx(15_914, rel=0.01)
    _write_shapefile(tmp_path / "noprj", [ring], {"NAME": "G", "CAP": 1})
    d2 = ingest(tmp_path / "noprj.shp", use_cache=False)
    assert d2.data["crs"] == "WGS84 lon/lat" and any("no .prj" in w for w in d2.warnings)


def test_shapefile_rejects_other_files(tmp_path):
    from design_ingest import ParseError

    p = tmp_path / "x.shp"
    p.write_bytes(b"not a shapefile" * 10)
    with pytest.raises(ParseError):
        ingest(p, use_cache=False)


# ------------------------------------------------------------------ eval
def test_eval_matches_semantics():
    from design_ingest.eval import _MISSING, matches

    assert matches(1500, 1500)[0] and matches(1500.0, 1500)[0] and not matches(1501, 1500)[0]
    assert matches(99.02, {"value": 99.0, "tol": 0.05})[0] and not matches(99.2, {"value": 99.0, "tol": 0.05})[0]
    assert matches(1010, {"value": 1000, "rel": 0.02})[0]
    assert matches(5, {"min": 3})[0] and not matches(2, {"min": 3})[0] and matches(2, {"max": 3})[0]
    assert matches("a datasheet", {"contains": "sheet"})[0] and matches([1, 2], {"contains": 2})[0]
    assert matches([500, 1500], [500, 1500])[0] and not matches([500, 1400], [500, 1500])[0]
    ok, why = matches(_MISSING, 1)
    assert not ok and why == "missing"


def test_eval_run_pass_fail_and_snapshot(samples, tmp_path, capsys):
    import shutil

    from design_ingest.eval import resolve, run

    folder = tmp_path / "set"
    folder.mkdir()
    shutil.copy(samples["pan"], folder / "module.pan")
    shutil.copy(samples["ond"], folder / "inv.ond")
    (folder / "expected.json").write_text(json.dumps({
        "module.pan": {"summary.pnom_w": 550, "summary.voc_v": {"value": 49.9, "tol": 0.01},
                       "doc_info.doc_type": "component_file", "warnings": [], "summary.iam_profile.1.0": 60},
        "inv.ond": {"summary.pnom_ac_kw": 999},  # wrong on purpose
        "gone.pdf": {"n_tables": 1},
    }), encoding="utf-8")
    assert run(folder, use_cache=False, snapshot_to=tmp_path / "snap.json") == 1
    out = capsys.readouterr().out
    assert "PASS  module.pan  (5 checks)" in out
    assert "FAIL  inv.ond" in out and "summary.pnom_ac_kw: got 300.0, expected 999" in out
    assert "gone.pdf: listed in the expected file but not found" in out
    assert "1 passed, 2 failed" in out
    snap = json.loads((tmp_path / "snap.json").read_text(encoding="utf-8"))
    assert snap["module.pan"]["summary.pnom_w"] == 550 and snap["module.pan"]["doc_info.doc_type"] == "component_file"
    # fixing the expectations gives exit code 0
    (folder / "expected.json").write_text(json.dumps({"module.pan": {"summary.pnom_w": 550}}), encoding="utf-8")
    assert run(folder, use_cache=False) == 0
    assert resolve(ingest(folder / "module.pan", use_cache=False), "summary.iam_profile.len") == 3


def test_cli_eval_and_profile_flags(samples, tmp_path, capsys):
    from design_ingest.cli import main

    (tmp_path / "m.pan").write_bytes(samples["pan"].read_bytes())
    (tmp_path / "expected.json").write_text(json.dumps({"m.pan": {"summary.model": "TS-550M-144"}}), encoding="utf-8")
    assert main(["eval", str(tmp_path), "--no-cache"]) == 0
    prof = tmp_path / "p.json"
    prof.write_text("{}", encoding="utf-8")
    assert main(["parse", str(tmp_path / "m.pan"), "--no-cache", "--profile", str(prof)]) == 0
    assert "type: component_file" in capsys.readouterr().out


def test_schema_version_and_public_api(samples):
    import design_ingest as di

    d = ingest(samples["pan"], use_cache=False)
    assert d.schema_version == di.SCHEMA_VERSION == "1"
    assert json.loads(d.model_dump_json())["schema_version"] == "1"
    for name in ("ingest", "ingest_dir", "extract", "Document", "Chunk", "Table", "parse_quantities",
                 "classify", "extract_registers", "extract_specs", "canonical_specs"):
        assert hasattr(di, name)
    # a cached document written before the field existed still loads
    old = json.loads(d.model_dump_json())
    del old["schema_version"]
    assert di.Document.model_validate(old).schema_version == "1"
