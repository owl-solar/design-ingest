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
    assert s["gamma"] == 1.031 and s["submodule_layout"] == "slTwinHalfCells"
    assert s["lid_loss_pct"] == 0.29 and s["vmax_ul_v"] == 1500
    assert s["low_irradiance_rel_eff_pct"] == [[200.0, -1.12], [1000.0, 0.0]]
    assert not d.warnings


def test_ond(samples):
    s = ingest(samples["ond"], use_cache=False).data["summary"]
    assert s["pnom_ac_kw"] == 300.0 and s["vmpp_min_v"] == 500 and s["vmpp_max_v"] == 1500
    assert s["n_mppt"] == 6 and s["efficiency_euro_pct"] == 98.5
    assert s["dc_ac_ratio_nominal"] == pytest.approx(1.02)
    assert s["threshold_w"] == 1500.0 and s["threshold_kw"] == 1.5  # PSeuil is in W
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


def test_pdf_table_sections_no_duplicate_text(samples):
    if "pdf_sections" not in samples:
        pytest.skip("reportlab not installed")
    d = ingest(samples["pdf_sections"], use_cache=False)
    rows = d.tables[0].rows
    assert ["Input > Max V", "1500 V"] in rows and ["Output > Max I", "254 A"] in rows
    text = next(c.text for c in d.chunks if c.kind == "text")
    assert "Footnote text" in text and "1500 V" not in text  # table area not repeated in text


def test_pdf_drawing_sheet(samples):
    if "pdf_drawing" not in samples:
        pytest.skip("reportlab not installed")
    d = ingest(samples["pdf_drawing"], use_cache=False)
    assert d.data["drawing_pages"] == [1] and not d.tables
    s = d.data["summary"]
    assert s["total_modules"] == 72 * 10 + 24 * 2
    assert s["coordinates"] == [[51.870492, 5.626461]]
    assert s["transformer_stations"][0]["kva_each"] == 6000
    tb = s["title_block"]
    assert tb["Project"] == "Testfield" and tb["Location"] == "Main street 1, Town, Country"
    assert tb["Drawn"] == "AB" and tb["Checked"] == "CD" and tb["Scale"] == "1:2000@A1"
    assert tb["Drawing No"] == "T-101" and tb["Rev"] == "0"
    assert d.data["rotated_chars"] == len("ROTATEDNOTE")
    assert "ROTATEDNOTE" not in d.chunks[0].text  # not mixed into the horizontal lines
    rot = [c for c in d.chunks if c.meta.get("rotated")]
    assert len(rot) == 1 and rot[0].text == "ROTATEDNOTE"  # kept, in reading order


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


def test_dwg_converter_call_survives_brackets_in_name(tmp_path, monkeypatch):
    """The ODA filter argument is a mask: '[...]' in the file name must not reach it."""
    from design_ingest.parsers import cad

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        (tmp_path / "out").mkdir(exist_ok=True)
        (tmp_path / "out" / "input.dxf").write_text("0\nEOF\n")

    monkeypatch.setattr(cad, "find_oda_converter", lambda: "oda.exe")
    monkeypatch.setattr(cad.subprocess, "run", fake_run)
    src = tmp_path / "plan [External Issue].dwg"
    src.write_bytes(b"AC1032")
    dxf = cad.dwg_to_dxf(src, tmp_path / "out")
    assert dxf.name == "input.dxf"
    assert calls[0][-1] == "*.DWG" and "[" not in "".join(calls[0])


def test_cad_summary_groups_and_module_estimate():
    from design_ingest.parsers.cad import _build_summary

    blocks = {
        "3P24@11DEG F FX ID2 PVBlock": {"inserts_model_total": 10},
        "3P8@11DEG F FX ID3 PVBlock": {"inserts_model_total": 2},
        "3P24W_area_A": {"inserts_model_total": 99},  # area block, not a table
    }
    lengths = {
        "PLANT MV Cable": {"m": 100.0}, "PLANT MV trench 1.0m wide": {"m": 40.0},
        "PLANT Fence": {"m": 500.0}, "Hedge (green fence)": {"m": 70.0},
        "PVcase String Cables": {"m": 900.0}, "PLANT LV AC cable": {"m": 60.0},
        "Unknown units": {"m": None},
    }
    s = _build_summary("m", 7, 3, [], blocks, lengths, {"PLANT Fence": {"m2": 25_000.0}})
    assert s["modules_estimate"]["total"] == 72 * 10 + 24 * 2
    assert list(s["modules_estimate"]["by_block"]) == ["3P24@11DEG F FX ID2 PVBlock", "3P8@11DEG F FX ID3 PVBlock"]
    g = s["length_m_by_group"]
    assert g["mv_cables"] == 100 and g["trenches"] == 40 and g["fence"] == 500 and g["hedges"] == 70
    assert g["string_cables_dc"] == 900 and g["lv_ac_cables"] == 60
    assert s["fenced_area_ha"] == 2.5


def test_pdf_multiline_cells_become_rows():
    from design_ingest.parsers.pdf import _table_from_rows

    t = _table_from_rows(
        [["", "E_Grid\nMWh", "PR\nratio"], ["January\nFebruary", "10\n20", "0.7\n0.8"], ["Year", "30", "0.75"]], 1
    )
    assert t.columns == ["", "E_Grid MWh", "PR ratio"]  # wrapped header text is joined
    assert t.rows == [["January", "10", "0.7"], ["February", "20", "0.8"], ["Year", "30", "0.75"]]


def test_pdf_pvsyst_report_summary():
    from design_ingest.parsers.pdf import _pvsyst_summary
    from design_ingest.schema import Table

    p1 = "PVsyst V8.0.5\nPVsyst - Simulation report\nProject: P1\nVariant: 500N\nSystem power: 42.89 MWp"
    p2 = (
        "Simulation date:\n14/05/25 14:32\nLatitude 52.30\u00b0N Albedo 0.20\nLongitude -8.69\u00b0W\nAltitude 75m\n"
        "Tilt/Azimuth 20 / 0\u00b0 Tilt/Azimuth 20 / 0\u00b0\nNb. of modules 69174units Nb. of units 176units\n"
        "Pnom total 42.89MWp Pnom total 37.84MWac\nPnom ratio 1.133\n"
        "Produced Energy 42444MWh/year Specific production 990kWh/kWp/year Perf. Ratio PR 89.49%\n"
        "PV module Inverter\nManufacturer A Manufacturer B\nModel M1 Model I1\n"
        "Unit Nom. Power 620Wp Unit Nom. Power 215kWac\n"
        "Produced Energy (P90) 39251MWh/year"
    )
    p3 = "Loss diagram\n971 kWh/m\u00b2 Global horizontal irradiation\n-4.3% Near Shadings: irradiance loss\n+0.1% Ground reflection"
    t = Table(page=2, columns=["", "E_Grid\nMWh"], rows=[["January", "1069"], ["Year", "42444"]])
    s = _pvsyst_summary([p1, p2, p3], [t])
    assert s["project"] == "P1" and s["software_version"] == "8.0.5" and s["system_power_mwp"] == 42.89
    assert s["latitude"] == 52.3 and s["longitude"] == -8.69 and s["altitude_m"] == 75
    assert s["n_modules"] == 69174 and s["n_inverters"] == 176 and s["pnom_ac_mw"] == 37.84
    assert s["energy_p50_mwh_year"] == 42444 and s["energy_p90_mwh_year"] == 39251
    assert s["performance_ratio_pct"] == 89.49 and s["specific_production_kwh_kwp_year"] == 990
    assert s["module_model"] == "M1" and s["inverter_model"] == "I1" and s["module_wp"] == 620
    assert s["tilt_azimuth_deg"] == [[20, 0]]
    assert s["loss_diagram"] == [{"name": "Near Shadings: irradiance loss", "pct": -4.3},
                                 {"name": "Ground reflection", "pct": 0.1}]
    assert s["monthly_e_grid_mwh"] == {"January": 1069.0}


def test_csv_metadata_timeseries_stats(tmp_path):
    p = tmp_path / "tmy.csv"
    p.write_text(
        "#TMY hourly data\n#Site,None\n#Latitude,52.8312\n#Longitude,-6.1191\n#Time Zone,0\n"
        "Year,Month,Day,Hour,Minute,GHI\n1990,1,1,0,0,0.0\n1990,1,1,1,0,100.0\n1990,1,1,2,0,300.0\n1990,1,1,3,0,0.0\n1990,1,1,4,0,0.0\n",
        encoding="utf-8",
    )
    d = ingest(p, use_cache=False)
    m = d.data["metadata"]
    assert m["title"] == "TMY hourly data" and m["Latitude"] == 52.8312 and m["Longitude"] == -6.1191
    assert "Site" not in m  # "None" is no value
    ts = d.data["timeseries"]
    assert ts["start"] == "1990-01-01T00:00:00" and ts["end"] == "1990-01-01T04:00:00"
    assert ts["step_minutes"] == 60 and ts["irregular_steps"] == 0 and ts["rows"] == 5
    assert d.data["column_stats"]["GHI"] == {"min": 0.0, "max": 300.0, "mean": 80.0, "sum": 400.0}


def test_csv_pvsyst_date_column(tmp_path):
    p = tmp_path / "hourly.csv"
    p.write_text("date;GlobHor\n01/01/90 00:00;0\n01/01/90 01:00;5\n01/01/90 02:00;9\n", encoding="utf-8")
    ts = ingest(p, use_cache=False).data["timeseries"]
    assert ts["start"] == "1990-01-01T00:00:00" and ts["step_minutes"] == 60


def test_xlsx_header_row_below_title_and_merged_cells(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["STRING DISTRIBUTION", None, None, None, None])
    ws.append([])
    ws.append([None, None, "No. of Modules", "DC, kWp", "AC, kW"])  # first two columns unnamed
    ws.append(["TS-1", "INV1", 638, 395.56, 300])
    ws.append([None, "INV2", 686, 425.32, 300])
    ws.append([None, "INV3", 676, 419.12, 300])
    ws.merge_cells("A4:A6")
    ws.merge_cells("A1:E1")  # a title spanning columns must not be copied sideways
    wb.save(tmp_path / "s.xlsx")
    d = ingest(tmp_path / "s.xlsx", use_cache=False)
    t = d.tables[0]
    assert t.columns == ["col1", "col2", "No. of Modules", "DC, kWp", "AC, kW"]
    assert [r[0] for r in t.rows] == ["TS-1", "TS-1", "TS-1"]  # merged cell repeated down its rows
    assert t.rows[1] == ["TS-1", "INV2", 686, 425.32, 300]
    assert d.data["sheets"][0]["rows_above_header"] == ["STRING DISTRIBUTION"]


def test_ond_efficiency_per_voltage_and_profile_padding():
    from design_ingest.parsers.pvsyst import _inverter_summary, parse_pvsyst_text

    text = """PVObject_=pvGInverter
  PVObject_Converter=TConverter
    PNomConv=320.0
    PSeuil=1600.0
    VNomEff=860.0,1300.0
    EfficMaxV=98.5,98.9
    EfficEuroV=98.2,98.7
    PVObject_ProfilPIO=TCubicProfile
      NPtsMax=4
      NPtsEff=2
      Point_1=60.0,0.0
      Point_2=90.0,41.0
      Point_3=0.0,0.0
      Point_4=0.0,0.0
    End of TCubicProfile
    PVObject_ProfilPIOV1=TCubicProfile
      NPtsEff=2
      Point_1=1600.0,0.0
      Point_2=16680.6,16000.0
      Point_3=0.0,0.0
    End of TCubicProfile
  End of PVObject pvConverter
End of PVObject pvGInverter
"""
    obj = parse_pvsyst_text(text)["PVObject"]
    s = _inverter_summary(obj)
    assert s["efficiency_profile_pin_pout_kw"] == [[60.0, 0.0], [90.0, 41.0]]  # padding dropped
    assert s["vdc_eff_v"] == [860.0, 1300.0]
    assert s["efficiency_max_by_vdc_pct"] == [98.5, 98.9] and s["efficiency_euro_by_vdc_pct"] == [98.2, 98.7]
    assert s["efficiency_profile_by_vdc_pin_pout_w"]["860.0"] == [[1600.0, 0.0], [16680.6, 16000.0]]
    assert s["threshold_w"] == 1600.0 and s["threshold_kw"] == 1.6


def test_pdf_multiline_cells_with_empty_cell_in_row():
    from design_ingest.parsers.pdf import _table_from_rows

    # as on a PVsyst horizon table: stacked cells next to an empty one (used to raise IndexError)
    t = _table_from_rows([["A", "B", "C"], ["1\n2", "3\n4", ""], ["5", "6", "7"]], 1)
    assert t.rows == [["1", "3", ""], ["2", "4", ""], ["5", "6", "7"]]


def test_pdf_wrapped_cells_and_continued_table_header():
    from design_ingest.parsers.pdf import _inherit_header, _table_from_rows

    first = _table_from_rows(
        [["No.", "Name", "Address"], ["1.", "Protocol\nnum", "4950 -\n4951"], ["2.", "Daily yields", "5003"]], 7
    )
    assert first.columns == ["No.", "Name", "Address"]
    assert first.rows[0] == ["1.", "Protocol num", "4950 - 4951"]
    # next page: header not repeated, and the first data row (all text) is mistaken for a header
    cont = _table_from_rows([["3.", "Output type", "5002"], ["4.", "Total yields", "5004 -\n5005"]], 8)
    got = _inherit_header(cont, first, first_on_page=True)
    assert got.columns == ["No.", "Name", "Address"]
    assert got.rows == [["3.", "Output type", "5002"], ["4.", "Total yields", "5004 - 5005"]]
    # header missing altogether (an empty cell in the first row): inherited as it is
    nohead = _table_from_rows([["3.", "Output type", ""], ["4.", "Total yields", "5004"]], 8)
    assert nohead.columns == []
    assert _inherit_header(nohead, first, True).columns == ["No.", "Name", "Address"]
    # not inherited: second table on the page, a page gap, another width, or a new table with its own header
    assert _inherit_header(cont, first, first_on_page=False).columns == cont.columns
    assert _inherit_header(cont.model_copy(update={"page": 10}), first, True).columns == cont.columns
    narrow = _table_from_rows([["3.", "x"], ["4.", "y"]], 8)
    assert _inherit_header(narrow, first, True).columns == narrow.columns
    other = _table_from_rows([["Item", "Qty", "Unit"], ["a", "1", "pcs"]], 8)
    assert _inherit_header(other, first, True).columns == ["Item", "Qty", "Unit"]


def test_pdf_scanned_page_is_flagged(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from PIL import Image

    img = Image.new("RGB", (200, 280), "white")
    cv = canvas.Canvas(str(tmp_path / "scan.pdf"), pagesize=(595, 842))
    cv.drawImage(ImageReader(img), 0, 0, 595, 842)  # full-page image
    cv.drawString(50, 50, "page 1 of 1")  # thin text layer
    cv.showPage()
    cv.drawString(100, 700, "A normal short page without any picture")
    cv.save()
    d = ingest(tmp_path / "scan.pdf", use_cache=False)
    assert d.data["low_text_pages"] == [1]
    assert any("almost no text" in w for w in d.warnings)
