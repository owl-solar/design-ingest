"""Builds small sample files of every supported format in a temp folder."""
from __future__ import annotations

import math
import zipfile
from pathlib import Path

import pytest

PAN_TEXT = """PVObject_=pvModule
  Version=7.4.8
  Flags=$00900243

  PVObject_Commercial=pvCommercial
    Comment=Test module
    Flags=$0041
    Manufacturer=TestSolar
    Model=TS-550M-144
    DataSource=Manufacturer 2024
    Width=1.134
    Height=2.278
    Depth=0.035
    Weight=28.00
  End of PVObject pvCommercial

  Technol=mtSiMono
  NCelS=72
  NCelP=2
  NDiode=3
  GRef=1000
  TRef=25.0
  PNom=550.0
  PNomTolUp=1.80
  BifacialityFactor=0.800
  Isc=14.000
  muISC=7.00
  Voc=49.90
  muVocSpec=-134.7
  Imp=13.150
  Vmp=41.83
  RShunt=400
  RSerie=0.180
  muPmpReq=-0.340
  VMaxIEC=1500

  PVObject_IAM=pvIAM
    Flags=$00
    IAMMode=UserProfile
    IAMProfile=TCubicProfile
      NPtsMax=9
      NPtsEff=3
      Mode=3
      Point_1=0.0,1.00000
      Point_2=60.0,0.96000
      Point_3=90.0,0.00000
    End of TCubicProfile
  End of PVObject pvIAM
End of PVObject pvModule
"""

OND_TEXT = """PVObject_=pvGInverter
  Comment=Test inverter
  Version=7.4.8
  Flags=$00381562

  PVObject_Commercial=pvCommercial
    Manufacturer=TestInv
    Model=TI-320K
    Width=1.100
    Height=0.900
    Depth=0.400
    Weight=110.00
  End of PVObject pvCommercial

  Transfo=Without

  Converter=TConverter
    PNomConv=300.000
    PMaxOUT=320.000
    VOutConv=800.0
    VMppMin=500
    VMPPMax=1500
    VAbsMax=1500
    PSeuil=1.50
    EfficMax=99.01
    EfficEuro=98.50
    MonoTri=Tri
    PNomDC=306.000
    PMaxDC=480.000
    IMaxDC=360.0
    INomDC=330.0
    INomAC=216.5
    IMaxAC=230.9
    ProfilPIO=TCubicProfile
      NPtsMax=11
      NPtsEff=2
      Point_1=1.5,0.0
      Point_2=306.0,300.0
    End of TCubicProfile
  End of TConverter
  NbInputs=12
  NbMPPT=6
End of PVObject pvGInverter
"""


def _square_kml(lat=54.70, lon=25.30, side_m=100.0) -> str:
    dlat = side_m / 111_195.0
    dlon = side_m / (111_195.0 * math.cos(math.radians(lat)))
    ring = [(lon, lat), (lon + dlon, lat), (lon + dlon, lat + dlat), (lon, lat + dlat), (lon, lat)]
    coords = " ".join(f"{x:.8f},{y:.8f},0" for x, y in ring)
    line = f"{lon:.8f},{lat:.8f},0 {lon + dlon:.8f},{lat:.8f},0"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Test site</name>
<Folder><name>Boundaries</name>
<Placemark><name>Plot A</name><description><![CDATA[<b>Owner:</b> farm<br/>Cadastral 4400-0001]]></description>
<ExtendedData><Data name="zone"><value>agri</value></Data></ExtendedData>
<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Folder>
<Folder><name>Grid</name>
<Placemark><name>MV route</name><LineString><coordinates>{line}</coordinates></LineString></Placemark>
<Placemark><name>POC substation</name><Point><coordinates>{lon:.6f},{lat:.6f},0</coordinates></Point></Placemark>
</Folder></Document></kml>"""


def _dxf() -> str:
    tags: list[tuple[int, str]] = []

    def t(*pairs):
        for i in range(0, len(pairs), 2):
            tags.append((pairs[i], str(pairs[i + 1])))

    t(0, "SECTION", 2, "HEADER", 9, "$ACADVER", 1, "AC1032", 9, "$INSUNITS", 70, 6,
      9, "$EXTMIN", 10, 0.0, 20, 0.0, 30, 0.0, 9, "$EXTMAX", 10, 500.0, 20, 300.0, 30, 0.0, 0, "ENDSEC")
    t(0, "SECTION", 2, "TABLES", 0, "TABLE", 2, "LAYER", 70, 4)
    for name, color, flags in [("0", 7, 0), ("PV_TABLES", 3, 0), ("CABLE_MV", 1, 0), ("FENCE", 8, 0), ("TEXT", 7, 0), ("OLD", -2, 1)]:
        t(0, "LAYER", 2, name, 70, flags, 62, color, 6, "CONTINUOUS")
    t(0, "ENDTAB", 0, "ENDSEC")
    t(0, "SECTION", 2, "BLOCKS")
    t(0, "BLOCK", 8, "0", 2, "MODULE", 70, 0, 10, 0, 20, 0, 30, 0)
    t(0, "LWPOLYLINE", 8, "0", 90, 4, 70, 1, 10, 0, 20, 0, 10, 1.134, 20, 0, 10, 1.134, 20, 2.278, 10, 0, 20, 2.278)
    t(0, "ENDBLK", 8, "0")
    t(0, "BLOCK", 8, "0", 2, "TABLE_2x14", 70, 2, 10, 0, 20, 0, 30, 0)
    for k in range(28):
        t(0, "INSERT", 8, "0", 2, "MODULE", 10, (k % 14) * 1.15, 20, (k // 14) * 2.3, 30, 0)
    t(0, "ATTDEF", 8, "0", 2, "TABLE_ID", 3, "Table id", 1, "T-0")
    t(0, "ENDBLK", 8, "0")
    t(0, "BLOCK", 8, "0", 2, "TITLE", 70, 2, 10, 0, 20, 0, 30, 0)
    for tag in ("PROJECT", "DWG_NO", "REV", "SCALE", "DATE"):
        t(0, "ATTDEF", 8, "0", 2, tag, 3, tag, 1, "")
    t(0, "ENDBLK", 8, "0")
    t(0, "ENDSEC")
    t(0, "SECTION", 2, "ENTITIES")
    for k in range(10):
        t(0, "INSERT", 8, "PV_TABLES", 66, 1, 2, "TABLE_2x14", 10, k * 20.0, 20, 0.0, 30, 0, 50, 0)
        t(0, "ATTRIB", 8, "PV_TABLES", 10, k * 20.0, 20, 0.0, 1, f"T-{k + 1:02d}", 2, "TABLE_ID")
        t(0, "SEQEND", 8, "PV_TABLES")
    # array insert: 3 columns x 2 rows = 6 tables
    t(0, "INSERT", 8, "PV_TABLES", 2, "TABLE_2x14", 10, 0.0, 20, 50.0, 30, 0, 70, 3, 71, 2, 44, 20.0, 45, 6.0)
    t(0, "LINE", 8, "CABLE_MV", 10, 0, 20, 0, 30, 0, 11, 60, 21, 80, 31, 0)  # 100 m
    t(0, "LWPOLYLINE", 8, "FENCE", 90, 4, 70, 1, 10, 0, 20, 0, 10, 50, 20, 0, 10, 50, 20, 50, 10, 0, 20, 50)
    t(0, "MTEXT", 8, "TEXT", 10, 5, 20, 5, 30, 0, 40, 2.5, 3, "{\\fArial|b1;Inverter station}\\PIS-01 ", 1, "300 kVA")
    t(0, "TEXT", 8, "TEXT", 10, 5, 20, 10, 30, 0, 40, 2.5, 1, "Tilt 25%%d, cable %%c50")
    t(0, "DIMENSION", 8, "TEXT", 2, "*D1", 1, "<> m", 42, 99.5)
    t(0, "INSERT", 8, "0", 67, 1, 66, 1, 2, "TITLE", 10, 0, 20, 0, 30, 0)
    for tag, val in [("PROJECT", "Test PV 10 MW"), ("DWG_NO", "PV-LAY-001"), ("REV", "B"), ("SCALE", "1:500"), ("DATE", "2026-11-05")]:
        t(0, "ATTRIB", 8, "0", 67, 1, 1, val, 2, tag)
    t(0, "SEQEND", 8, "0", 67, 1)
    t(0, "ENDSEC", 0, "EOF")
    return "\n".join(f"{c:>3}\n{v}" for c, v in tags) + "\n"


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("samples")
    out: dict[str, Path] = {}

    (d / "module.pan").write_text(PAN_TEXT, encoding="cp1252")
    (d / "inverter.ond").write_text(OND_TEXT, encoding="cp1252")
    out["pan"], out["ond"] = d / "module.pan", d / "inverter.ond"

    kml = _square_kml()
    (d / "site.kml").write_text(kml, encoding="utf-8")
    with zipfile.ZipFile(d / "site.kmz", "w") as z:
        z.writestr("doc.kml", kml)
        z.writestr("files/photo.txt", "x")
    out["kml"], out["kmz"] = d / "site.kml", d / "site.kmz"

    (d / "layout.dxf").write_text(_dxf(), encoding="utf-8")
    out["dxf"] = d / "layout.dxf"

    csv_text = (
        "PVSYST V7.4.8;;;\nProject: Test;;;\n\n"
        "date;GlobHor;E_Grid;T_Amb\n;W/m²;kWh;°C\n"
        "01/06/90 10:00;650,5;4100,2;18,1\n01/06/90 11:00;720,0;4500,0;19,0\n"
    )
    (d / "hourly.csv").write_bytes(csv_text.encode("cp1252"))
    out["csv"] = d / "hourly.csv"

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BOM"
    ws.append([])
    ws.append([None, "Item", "Qty", "Unit"])
    ws.append([None, "PV module 550 W", 7840, "pcs"])
    ws.append([None, "String inverter 300 kW", 12, "pcs"])
    ws2 = wb.create_sheet("Cables")
    ws2.append(["Cable", "Length m"])
    ws2.append(["MV 3x240", 1200.5])
    wb.save(d / "bom.xlsx")
    out["xlsx"] = d / "bom.xlsx"

    import docx

    doc = docx.Document()
    doc.add_heading("Technical Specification", level=1)
    doc.add_paragraph("General requirements for the BESS.")
    doc.add_heading("Battery", level=2)
    doc.add_paragraph("The BESS shall provide 50 MW / 100 MWh.")
    doc.add_paragraph("Round-trip efficiency shall be at least 88 %.")
    tbl = doc.add_table(rows=3, cols=2)
    for r, (a, b) in enumerate([("Parameter", "Value"), ("Energy", "100 MWh"), ("Power", "50 MW")]):
        tbl.cell(r, 0).text, tbl.cell(r, 1).text = a, b
    doc.add_heading("Schedule", level=1)
    doc.add_paragraph("COD by 2028-06-30.")
    doc.save(d / "spec.docx")
    out["docx"] = d / "spec.docx"

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet

        st = getSampleStyleSheet()
        tb = Table([["Parameter", "Value"], ["Pmax", "550 W"], ["Voc", "49.9 V"]])
        tb.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
        SimpleDocTemplate(str(d / "datasheet.pdf"), pagesize=A4).build(
            [Paragraph("TS-550M module datasheet", st["Title"]), tb, PageBreak(),
             Paragraph("Warranty 30 years linear", st["Normal"]), PageBreak(), HRFlowable(width="100%")]
        )
        out["pdf"] = d / "datasheet.pdf"
    except ImportError:
        pass
    return out
