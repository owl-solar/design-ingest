"""Fault code layouts besides the Sungrow one, on generated PDFs only.

* alarm reference with a section and real tables per alarm (Huawei style): ``faults_tables``
* error table with an extra "behavior" column, bullets, codes at the top of the row, a code that
  shares its cells with the next one, hyphenated words, two tables on one page (SMA style): ``faults``
"""
from __future__ import annotations

import pytest

from design_ingest import ingest
from design_ingest.search import search

pytest.importorskip("reportlab")

H = 842.0


# ------------------------------------------------------------------ section per alarm (tables)
@pytest.fixture(scope="module")
def alarm_ref(tmp_path_factory):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = getSampleStyleSheet()["Normal"]
    heads = {1: "2001 String Voltage High", 2: "2001 String Voltage High", 3: "2002 DC Arc Fault",
             4: "2003 Phase wire short-circuited to PE"}

    def on_page(c, doc):
        c.setFont("Helvetica", 8)
        c.drawString(60, H - 30, f"Inverter Alarm Reference 2 {heads[doc.page]}")
        c.drawString(60, 25, "Issue 14 (2026-06-15) Copyright Test Co., Ltd.")

    def grid(rows):
        t = Table(rows, colWidths=[70, 130, 250] if len(rows[0]) == 3 else [70, 380])
        style = [("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 8)]
        style += [("SPAN", (0, i), (-1, i)) for i, r in enumerate(rows) if i and r[1:] == [None] * (len(r) - 1)]  # note row
        t.setStyle(TableStyle(style))
        return t

    def p(text):
        return Paragraph(text, st)

    story = [
        p("Alarm Attribute"), grid([["Alarm ID", "Alarm Name", "Alarm Severity"], ["2001", "String Voltage High", "Major"]]),
        Spacer(1, 6), p("Possible Cause"),
        grid([["Cause ID", "Possible Cause"], ["1–10", "The PV array is not properly configured."],
              ["13", "Too many modules in MPPT1."]]),
        Spacer(1, 6), p("Suggestion"), PageBreak(),
        # page 2: the cause table goes on with a repeated heading, then the suggestions
        grid([["Cause ID", "Possible Cause"], ["14", "Too many modules in MPPT2."]]),
        Spacer(1, 6),
        grid([["Cause ID", "Suggestion"],
              ["1–10", "1. Check the series connection. 2. Contact your vendor."],
              ["13", "Check MPPT1."], ["14", "Check MPPT2."]]),
        PageBreak(),
        # page 3: one suggestion as page text (not per cause), severity with a footnote letter
        p("Alarm Attribute"),
        grid([["Alarm ID", "Alarm Name", "Alarm Severity"], ["2002", "DC Arc Fault", "Minora"],
              ["Note a: Minor for cause IDs 1 to 9, Warning for 10.", None, None]]),
        Spacer(1, 6), p("Possible Cause"),
        grid([["Cause ID", "Possible Cause"], ["1–9", "The cables generate arcs."]]),
        Spacer(1, 6), p("Suggestion"),
        p("1. Check the alarm information and the MPPT."), p("2. Check the cables for damage."),
        PageBreak(),
        # page 4: alarm name wrapped in the running head, no suggestion at all
        p("Alarm Attribute"),
        grid([["Alarm ID", "Alarm Name", "Alarm Severity"], ["2003", "Phase wire short- circuited to PE", "Major"]]),
        Spacer(1, 6), p("Possible Cause"),
        grid([["Cause ID", "Possible Cause"], ["1", "The phase wire is short-circuited."]]),
    ]
    path = tmp_path_factory.mktemp("alarms") / "alarm_reference.pdf"
    SimpleDocTemplate(str(path), pagesize=A4, topMargin=50, bottomMargin=50).build(
        story, onFirstPage=on_page, onLaterPages=on_page)
    return path


@pytest.fixture(scope="module")
def alarms(alarm_ref):
    d = ingest(alarm_ref, use_cache=False)
    return d, {r["code"]: r for r in d.data.get("fault_codes", [])}


def test_alarm_sections_are_records(alarms):
    d, by = alarms
    assert list(by) == ["2001", "2002", "2003"]
    r = by["2001"]
    assert r["name"] == "String Voltage High" and r["severity"] == "Major"
    assert (r["page"], r["page_end"]) == (1, 2)
    assert [c["id"] for c in r["causes"]] == ["1-10", "13", "14"]       # the range dash is normalised


def test_suggestion_is_tied_to_cause_by_id(alarms):
    r = alarms[1]["2001"]
    c = {c["id"]: c for c in r["causes"]}
    assert c["13"]["cause"] == "Too many modules in MPPT1." and c["13"]["suggestion"] == "Check MPPT1."
    assert c["14"]["suggestion"] == "Check MPPT2."
    assert r["measures"] == ["1. Check the series connection. 2. Contact your vendor.", "Check MPPT1.", "Check MPPT2."]


def test_suggestion_as_page_text_and_severity_footnote(alarms):
    r = alarms[1]["2002"]
    assert r["measures"] == ["1. Check the alarm information and the MPPT.", "2. Check the cables for damage."]
    assert r["severity"] == "Minor"                                      # "Minora" minus the marker "a"
    assert r["severity_note"].startswith("Note a:")
    assert r["causes"] == [{"id": "1-9", "cause": "The cables generate arcs.", "suggestion": None}]


def test_alarm_without_suggestion_is_flagged_not_guessed(alarms):
    d, by = alarms
    assert by["2003"]["measures"] is None and by["2003"]["name"] == "Phase wire short- circuited to PE"
    assert any("2003" in w and "no suggestion" in w for w in d.warnings)


def test_alarm_search(alarms):
    d, _ = alarms
    hit = search([d], "alarm 2002", top_k=1)[0]
    assert hit["kind"] == "fault_code" and hit["text"].startswith("Fault code 2002: DC Arc Fault.")
    assert "Severity: Minor" in hit["text"] and "Check the cables" in hit["text"]


# ------------------------------------------------------------------ error table with a behavior column
X = {"code": 43.0, "name": 96.0, "s1": 250.0, "s2": 285.0, "r": 319.0, "meas": 350.0, "end": 539.0}
SEGS = [("code", 40, 92), ("name", 92, 246), ("s1", 246, 281), ("s2", 281, 315), ("r", 315, 340),
        ("meas", 340, 539)]
SIZE, LH = 9, 12
FONT = ["Helvetica"]


def _t(c, x, top, lines):
    c.setFont(FONT[0], SIZE)
    for k, s in enumerate(lines):
        c.drawString(x, H - top - SIZE - k * LH, s)


def _rule(c, y, only=None):
    for name, x0, x1 in SEGS:
        if only is None or name in only:
            c.line(x0, H - y, x1, H - y)


def _header(c, top):
    _t(c, X["code"] + 4, top, ["Error no."]); _t(c, X["name"], top, ["Explanation"])
    _t(c, X["s1"], top, ["Inverter behavior"]); _t(c, X["meas"] - 6, top, ["Corrective measures"])
    _t(c, X["s1"], top + 21, ["S1"]); _t(c, X["s2"], top + 21, ["S2"]); _t(c, X["r"], top + 21, ["R"])
    _rule(c, top + 36)


def _row(c, a, b, code, name, s1, s2, r, meas, codes_only=False):
    _t(c, X["code"] + 4, a + 5, code)
    if not codes_only:
        _t(c, X["name"], a + 5, name); _t(c, X["s1"], a + 5, s1); _t(c, X["s2"], a + 5, s2)
        _t(c, X["r"], a + 5, r); _t(c, X["meas"], a + 5, meas)


@pytest.fixture(scope="module")
def error_table(tmp_path_factory):
    import os

    import reportlab as rl
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    vera = os.path.join(os.path.dirname(rl.__file__), "fonts", "Vera.ttf")   # has the bullet sign
    if not os.path.exists(vera):
        pytest.skip("reportlab's bundled Vera.ttf not found")
    pdfmetrics.registerFont(TTFont("Vera", vera))
    FONT[0] = "Vera"

    path = tmp_path_factory.mktemp("sma") / "errors.pdf"
    c = canvas.Canvas(str(path), pagesize=(595, H))
    # page 1: a "how to read" figure with S1 S2 R inline in the heading line: not a table to read
    _t(c, 43, 60, ["Error no."])
    _t(c, 96, 60, ["Explanation"]); _t(c, 250, 60, ["S1"]); _t(c, 285, 60, ["S2"]); _t(c, 319, 60, ["R"])
    _t(c, 350, 60, ["Corrective measures"])
    _rule(c, 75)
    _row(c, 75, 125, ["9999"], ["Example row"], ["30 s"], ["Q"], ["-"], ["• Example."])
    _rule(c, 125)
    c.showPage()
    # page 2: two tables
    _t(c, 43, 40, ["7 Troubleshooting"])
    _header(c, 60)
    top = 96
    _rule(c, top)
    # 0801 and 0802: own code cells, one shared explanation / behavior / measures
    _row(c, 96, 140, ["0801*"], ["One line conductor", "has failed."], ["30 s"], ["30 s"], ["-"],
         ["• Check the grid voltage.", "• Make sure the fuses work."])
    _t(c, X["code"] + 4, 146, ["0802*"])
    _rule(c, 96); _rule(c, 140, only={"code"}); _rule(c, 190)
    _row(c, 190, 250, ["1500"], ["The conditions for grid", "reconnec-", "tion are not met."], ["W"], ["W"], ["-"],
         ["• Check the power", "  frequency."])
    _rule(c, 250)
    _row(c, 250, 320, ["3403"], ["Grid reconnection", "is pending."], ["15", "min"], ["30", "min"], ["-"],
         ["• Check the DC-", "side cabling.", "• Check the design."])
    _rule(c, 320)
    # text between the tables, then the next table
    _t(c, 43, 330, ["7.5.4 Error numbers 34xx"])
    _rule(c, 350, only={"code", "name"})
    _header(c, 365)
    _rule(c, 401)
    _row(c, 401, 450, ["3504"], ["Insulation error", "detected."], ["W"], ["W"], ["-"], ["• Check the PV array."])
    _rule(c, 450)
    c.showPage()
    c.save()
    return path


@pytest.fixture(scope="module")
def errors(error_table):
    d = ingest(error_table, use_cache=False)
    return d, {r["code"]: r for r in d.data.get("fault_codes", [])}


def test_behavior_columns_and_order(errors):
    d, by = errors
    assert list(by) == ["0801", "0802", "1500", "3403", "3504"]          # the "how to read" example is not a record
    assert by["1500"]["columns"] == {"S1": "W", "S2": "W", "R": "-"}
    assert by["3403"]["columns"] == {"S1": "15 min", "S2": "30 min", "R": "-"}
    assert not [w for w in d.warnings if "fault" in w]


def test_codes_with_own_cells_share_the_merged_ones(errors):
    by = errors[1]
    for code in ("0801", "0802"):
        r = by[code]
        assert r["code_marker"] == "*" and r["shared_measures"]
        assert r["name"] == "One line conductor has failed."
        assert r["columns"]["S1"] == "30 s"
        assert r["measures"] == ["Check the grid voltage.", "Make sure the fuses work."]   # bullets are not text


def test_hyphen_join_uses_the_documents_words(errors):
    by = errors[1]
    assert by["1500"]["name"] == "The conditions for grid reconnection are not met."   # "reconnection" occurs in the document
    assert by["3403"]["measures"][0] == "Check the DC-side cabling."                   # "dcside" does not: hyphen kept
    assert by["1500"]["raw"]["name"].splitlines()[1] == "reconnec-"


def test_second_table_on_the_page_and_section_title_ignored(errors):
    by = errors[1]
    assert by["3504"]["measures"] == ["Check the PV array."] and by["3504"]["columns"]["S1"] == "W"
    assert not any("7.5.4" in r["name"] or "34xx" in " ".join(r["measures"] or []) for r in by.values())


def test_error_table_search(errors):
    d, _ = errors
    assert search([d], "error 3504 insulation", top_k=1)[0]["text"].startswith("Fault code 3504")
    assert "S1: W" in search([d], "3504", top_k=1)[0]["text"]


# ------------------------------------------------------------------ footnotes are not codes
@pytest.fixture(scope="module")
def footnoted(tmp_path_factory, error_table):          # error_table registers the font
    from reportlab.pdfgen import canvas

    path = tmp_path_factory.mktemp("notes") / "errors_notes.pdf"
    c = canvas.Canvas(str(path), pagesize=(595, H))
    _header(c, 60)
    _rule(c, 96)
    _row(c, 96, 150, ["0103"], ["Grid voltage", "is too high."], ["30 s"], ["30 s"], ["-"], ["• Check the grid."])
    _rule(c, 150)
    _row(c, 150, 200, ["0104"], ["Grid voltage", "is too low."], ["C"], ["C"], ["-"], ["• Check the grid."])
    _rule(c, 200)
    # footnote under the table (two lines) and a section title, then the heading block of the next table
    _t(c, 63, 210, ["*Depending on the parameterization, the", "message may have to be acknowledged manually."])
    _t(c, 43, 250, ["7.5.4 Error numbers 34xx to 40xx"])
    _rule(c, 270, only={"code", "name"})
    _header(c, 285)
    _rule(c, 321)
    _row(c, 321, 370, ["3504"], ["Insulation error", "detected."], ["W"], ["W"], ["-"], ["• Check the PV array."])
    _rule(c, 370)
    # a "code" cell inside the ruled area that is text, not a code
    _row(c, 370, 400, ["*See"], ["the note above."], [""], [""], [""], [""])
    _rule(c, 400)
    _t(c, 63, 410, ["*Last note under the table."])
    c.showPage()
    c.save()
    return path


def test_footnote_is_not_a_code_and_is_kept_as_a_note(footnoted):
    d = ingest(footnoted, use_cache=False)
    codes = [r["code"] for r in d.data["fault_codes"]]
    assert codes == ["0103", "0104", "3504"]                       # no "*Depending" or "*See", count unchanged
    notes = d.data["fault_code_notes"]
    assert len(notes) == 3 and {n["page"] for n in notes} == {1}
    assert notes[0]["text"] == "*Depending on the parameterization, the message may have to be acknowledged manually."
    assert any(n["text"] == "*See the note above." for n in notes)       # a text cell in the ruled area
    assert any(n["text"] == "*Last note under the table." for n in notes)
    assert not any("7.5.4" in n["text"] for n in notes)            # a section title is not a note


def test_is_code_format():
    from design_ingest.faults import is_code

    for ok in ("0103", "3504", "39", "264-283", "59, 70-74, 76-83", "E01", "F-102"):
        assert is_code(ok), ok
    for bad in ("*Depending", "Depending", "7.5.4 Error numbers", "Note", "*1", "", "-"):
        assert not is_code(bad), bad
