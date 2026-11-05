import pytest

from design_ingest import Document, ParseError, ingest, ingest_dir, supported_extensions
from design_ingest.cli import main


def test_formats():
    exts = supported_extensions()
    for e in [".pdf", ".docx", ".xlsx", ".csv", ".dxf", ".dwg", ".kml", ".kmz", ".pan", ".ond"]:
        assert e in exts


def test_pan(samples):
    d = ingest(samples["pan"], use_cache=False)
    s = d.data["summary"]
    assert s["manufacturer"] == "TestSolar" and s["model"] == "TS-550M-144"
    assert s["pnom_w"] == 550.0 and s["voc_v"] == 49.9 and s["cells_series"] == 72
    assert s["mu_pmp_pct_per_c"] == -0.34
    assert s["efficiency_pct"] == pytest.approx(21.29, abs=0.01)
    assert s["iam_profile"] == [[0.0, 1.0], [60.0, 0.96], [90.0, 0.0]]
    assert d.data["raw"]["Commercial"]["Width"] == 1.134
    assert not d.warnings


def test_ond(samples):
    s = ingest(samples["ond"], use_cache=False).data["summary"]
    assert s["pnom_ac_kw"] == 300.0 and s["vmpp_min_v"] == 500 and s["vmpp_max_v"] == 1500
    assert s["n_mppt"] == 6 and s["efficiency_euro_pct"] == 98.5
    assert s["dc_ac_ratio_nominal"] == pytest.approx(1.02)
    assert s["efficiency_profile_pin_pout_kw"][-1] == [306.0, 300.0]


def test_pan_rejects_binary(tmp_path):
    p = tmp_path / "x.pan"
    p.write_bytes(b"\x00\x01binary")
    with pytest.raises(ParseError):
        ingest(p, use_cache=False)


@pytest.mark.parametrize("key", ["kml", "kmz"])
def test_kml(samples, key):
    d = ingest(samples[key], use_cache=False)
    pms = {p["name"]: p for p in d.data["placemarks"]}
    assert pms["Plot A"]["area_m2"] == pytest.approx(10_000, rel=0.005)
    assert pms["Plot A"]["folder"] == "Boundaries"
    assert pms["Plot A"]["properties"] == {"zone": "agri"}
    assert "Cadastral 4400-0001" in pms["Plot A"]["description"]
    assert pms["MV route"]["length_m"] == pytest.approx(100, rel=0.005)
    assert pms["POC substation"]["geometry_type"] == "Point"
    assert d.data["summary"]["Polygon"]["count"] == 1


def test_dxf(samples):
    d = ingest(samples["dxf"], use_cache=False)
    b = d.data["blocks"]
    assert b["TABLE_2x14"]["inserts_model_direct"] == 16  # 10 single + 3x2 array
    assert b["MODULE"]["inserts_model_total"] == 16 * 28
    assert d.data["header"]["units"] == "m"
    assert d.data["lengths"]["CABLE_MV"]["m"] == pytest.approx(100.0)
    assert d.data["closed_areas"]["FENCE"]["m2"] == pytest.approx(2500.0)
    assert d.data["title_block"]["DWG_NO"] == "PV-LAY-001"
    text = d.text()
    assert "Inverter station\nIS-01 300 kVA" in text
    assert "Tilt 25°, cable Ø50" in text
    assert "99.5 m" in text
    attr = next(t for t in d.tables if t.name == "attributes: TABLE_2x14")
    assert len(attr.rows) == 10
    layers = {l["name"]: l for l in d.data["layers"]}
    assert layers["OLD"]["on"] is False and layers["OLD"]["frozen"] is True


def test_dwg_without_converter(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    import design_ingest.parsers.cad as cad
    monkeypatch.setattr(cad, "find_oda_converter", lambda: None)
    p = tmp_path / "a.dwg"
    p.write_bytes(b"AC1032....")
    with pytest.raises(ParseError, match="ODA File Converter"):
        ingest(p, use_cache=False)


def test_csv(samples):
    d = ingest(samples["csv"], use_cache=False)
    t = d.tables[0]
    assert t.columns == ["date", "GlobHor", "E_Grid", "T_Amb"]
    assert d.data["units"] == ["", "W/m²", "kWh", "°C"]
    assert t.rows[0][1] == 650.5 and t.rows[1][2] == 4500.0
    assert d.metadata["delimiter"] == ";"
    assert len(d.data["preamble"]) == 2


def test_xlsx(samples):
    d = ingest(samples["xlsx"], use_cache=False)
    bom = next(t for t in d.tables if t.sheet == "BOM")
    assert bom.columns == ["Item", "Qty", "Unit"]
    assert bom.rows[0] == ["PV module 550 W", 7840, "pcs"]
    assert {s["name"] for s in d.data["sheets"]} == {"BOM", "Cables"}


def test_docx(samples):
    d = ingest(samples["docx"], use_cache=False)
    body = [c for c in d.chunks if c.kind == "text"]
    rte = next(c for c in body if "Round-trip" in c.text)
    assert rte.section == "Technical Specification > Battery"
    assert d.tables[0].columns == ["Parameter", "Value"]
    sched = next(c for c in body if "COD" in c.text)
    assert sched.section == "Schedule"


def test_pdf(samples):
    if "pdf" not in samples:
        pytest.skip("reportlab not installed")
    d = ingest(samples["pdf"], use_cache=False)
    assert d.data["pages"] == 3
    assert d.chunks[0].page == 1 and "TS-550M" in d.chunks[0].text
    assert any(t.columns == ["Parameter", "Value"] for t in d.tables)
    assert d.data["empty_pages"] == [3]
    assert any("without text layer" in w for w in d.warnings)


def test_cache_and_roundtrip(samples, tmp_path):
    a = ingest(samples["pan"], cache_dir=tmp_path)
    assert list(tmp_path.glob("*.json"))
    b = ingest(samples["pan"], cache_dir=tmp_path)
    assert a.model_dump() == b.model_dump()
    p = a.save_json(tmp_path / "x.json")
    assert Document.load_json(p).data["summary"]["pnom_w"] == 550.0
    assert "PV module" in a.to_markdown()


def test_unsupported(tmp_path):
    p = tmp_path / "a.xyz"
    p.write_text("x")
    with pytest.raises(ParseError, match="Unsupported"):
        ingest(p)


def test_ingest_dir_and_cli(samples, tmp_path, capsys):
    folder = samples["pan"].parent
    docs = ingest_dir(folder, use_cache=False)
    assert {d.file_type for d in docs} >= {"pan", "ond", "kml", "kmz", "dxf", "csv", "xlsx", "docx"}
    assert main(["parse", str(samples["ond"]), "--no-cache"]) == 0
    assert "TI-320K" in capsys.readouterr().out
    assert main(["dir", str(folder), "--out", str(tmp_path / "out"), "--md"]) == 0
    assert (tmp_path / "out" / "layout.dxf.json").exists()


def test_table_to_dataframe(samples):
    pd = pytest.importorskip("pandas")
    t = ingest(samples["csv"], use_cache=False).tables[0]
    df = t.to_dataframe()
    assert list(df.columns) == ["date", "GlobHor", "E_Grid", "T_Amb"] and len(df) == 2
