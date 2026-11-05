"""Document type and file-name metadata (classic rules, no LLM).

``classify(doc)`` returns

    {"doc_type": "datasheet", "confidence": "high",
     "signals": ["filename code DS", "2 pages with a key/value table"],
     "filename": {"date": "2023-10-13", "version": "V5", "language": "EN", ...}}

Types: datasheet, manual, drawing, report, pvsyst_report, protocol, certificate,
test_report, calculation, bom, component_file (PAN/OND), site_file (KML/GeoJSON/
shapefile), timeseries, spreadsheet, document, other.

The scoring is a sum of weighted signals from the file name, the file format and the
first page(s) of text; the type with the highest score wins and ``confidence`` says
how clear the win was. Nothing here looks at vendor names.
"""
from __future__ import annotations

import re
from typing import Any

from .schema import Document

# two/three-letter prefixes used by several vendors in "DS_20231013_Name_V5_EN.pdf"
_CODE = {"DS": "datasheet", "UM": "manual", "DW": "drawing", "TI": "technical_note", "IM": "manual",
         "QG": "manual", "CE": "certificate", "TR": "test_report"}

# words in the file name or the title -> type, with a weight
_WORDS: list[tuple[str, str, int]] = [
    (r"data\s*-?\s*sheet", "datasheet", 4),
    (r"user\s*manual|operation\s*manual|instruction\s*manual|installation\s*guide|quick\s*(installation\s*)?guide|\bmanual\b", "manual", 4),
    (r"certificate|certification", "certificate", 4),
    (r"test\s*report|testreport", "test_report", 4),
    (r"communication\s*protocol|modbus|\bprotocol\b|register\s*(map|list)|point\s*list|signal\s*list", "protocol", 4),
    (r"calculations?\b", "calculation", 3),
    (r"bill\s*of\s*quantities|\bbom\b", "bom", 4),
    (r"single\s*line\s*diagram|\bsld\b|layout|drawings?\b|cabling|trench|stringing|site\s*plan", "drawing", 2),
    (r"simulation\s*report|pvsyst", "pvsyst_report", 3),
    (r"\breport\b", "report", 1),
    (r"specification|requirements|\btender\b|\brfp\b|scope\s*of\s*work", "specification", 3),
]


def parse_filename(name: str) -> dict[str, Any]:
    """Date, version, revision, language, vendor-style code and drawing number from a file name."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name)
    info: dict[str, Any] = {}
    m = re.match(r"^([A-Z]{2})[_ -](20\d{6})[_ -]", stem)
    if m:
        info["code"] = m.group(1)
    m = re.search(r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)", stem)
    if m:
        info["date"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(?<![A-Za-z0-9])(V\d+(?:\.\d+){0,3})(?![A-Za-z0-9])", stem, re.I)
    if m:
        info["version"] = m.group(1).upper()
    m = re.search(r"\b(?:rev(?:ision)?\.?\s*-?\s*)([A-Za-z]{1,2}|\d{1,3})\b", stem, re.I)
    if m:
        info["revision"] = m.group(1).upper()
    # a language code stands alone between separators ("..._EN.pdf"), not inside "NL-102202"
    m = re.search(r"(?<![A-Za-z0-9-])(EN|DE|FR|ES|IT|PL|NL|PT|RU|ZH|CN)(?![A-Za-z0-9-])", stem)
    if m:
        info["language"] = m.group(1)
    # drawing number such as "PROJ1001-BAL-110K Trench Layout" / "ABC123-101 General Layout"
    m = re.match(r"^([A-Za-z]{2,}\d*(?:-[A-Za-z0-9]{2,})+)\s+(\S.*)$", stem)
    if m and re.search(r"\d", m.group(1)):
        number, title = m.group(1), m.group(2).strip()
        rev = re.match(r"^(.*\d)([A-Z])$", number)  # trailing revision letter: ...-110K
        if rev:
            number = rev.group(1)
            info.setdefault("revision", rev.group(2))
        info["drawing_no"] = number
        info["title"] = re.sub(r"^rev(?:ision)?\.?\s*\w+\s+", "", title, flags=re.I)
    return info


def _first_text(doc: Document, chars: int = 1500) -> str:
    out, n = [], 0
    for c in doc.chunks:
        if c.kind == "table":
            continue
        out.append(c.text)
        n += len(c.text)
        if n >= chars:
            break
    return "\n".join(out)[:chars]


def classify(doc: Document) -> dict[str, Any]:
    """Classify an already parsed document."""
    score: dict[str, float] = {}
    signals: list[str] = []

    def add(t: str, w: float, why: str) -> None:
        score[t] = score.get(t, 0) + w
        signals.append(f"{why} -> {t}")

    fname = parse_filename(doc.filename)
    code = fname.get("code")
    if code in _CODE:
        add(_CODE[code], 3, f"filename code {code}")
    low_name = re.sub(r"[_\-.]+", " ", doc.filename.lower())
    for rx, t, w in _WORDS:
        if re.search(rx, low_name):
            add(t, w, f"filename word '{re.search(rx, low_name).group(0).strip()}'")

    ft = doc.file_type
    if ft in ("pan", "ond"):
        add("component_file", 10, f"format {ft}")
    elif ft in ("kml", "kmz", "geojson", "shp"):
        add("site_file", 10, f"format {ft}")
    elif ft in ("dxf", "dwg"):
        add("drawing", 10, f"format {ft}")
    elif ft == "csv":
        add("timeseries" if "timeseries" in doc.data else "spreadsheet", 5, "format csv")
    elif ft in ("xlsx", "xlsm"):
        add("spreadsheet", 2, "format xlsx")
        txt = " ".join(" ".join(str(c) for c in r if c) for t in doc.tables[:2] for r in t.rows[:6]).lower()
        if "bill of quantities" in txt or "quantities" in " ".join(s["name"].lower() for s in doc.data.get("sheets", [])):
            add("bom", 4, "bill of quantities in the sheet")
    elif ft == "docx":
        add("document", 2, "format docx")

    if ft == "pdf":
        if doc.data.get("drawing_pages"):
            add("drawing", 8, f"{len(doc.data['drawing_pages'])} page(s) of A3 or larger")
        if doc.data.get("report") == "pvsyst":
            add("pvsyst_report", 10, "PVsyst report header")
        text = _first_text(doc).lower()
        head = text[:400]
        for rx, t, w in _WORDS:
            if re.search(rx, head):
                add(t, w, f"title text '{re.search(rx, head).group(0).strip()}'")
        pages = doc.data.get("pages") or 0
        if any(len(t.rows) >= 8 and len(t.columns) <= 3 for t in doc.tables) and pages <= 6:
            add("datasheet", 2, "short document with a key/value table")
        if pages >= 20:
            add("manual", 1, f"{pages} pages")
        if doc.data.get("registers"):
            add("protocol", 6, f"{len(doc.data['registers'])} register rows")
        if doc.data.get("empty_pages") and len(doc.data["empty_pages"]) == pages:
            signals.append("no text layer: type from the file name only")

    if not score:
        return {"doc_type": "other", "confidence": "low", "signals": signals, "filename": fname}
    ranked = sorted(score.items(), key=lambda kv: -kv[1])
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if top >= 8 or (top >= 4 and top - second >= 3):
        conf = "high"
    elif top - second >= 2:
        conf = "medium"
    else:
        conf = "low"
    return {"doc_type": best, "confidence": conf, "signals": signals[:8], "filename": fname}
