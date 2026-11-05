"""Fault code tables of manuals, on generated PDFs only (no real manuals in the repository).

The generated table copies what a real manual does: three columns with the heading repeated
on every page, rules between rows only (no vertical lines), the code centred vertically in its
row, steps top-aligned, words hyphenated with a soft hyphen at the line end, a code cell that
lists ranges and breaks after the dash, two codes sharing one measures cell, and a row that
runs over the page boundary.
"""
from __future__ import annotations


import pytest

from design_ingest import ingest
from design_ingest import faults
from design_ingest.search import passages, search

reportlab = pytest.importorskip("reportlab")

H = 842.0
X_CODE, X_NAME, X_MEAS, X_END = 60.0, 130.0, 260.0, 540.0
SHY = "­"
DASH = "–"
SIZE, LH = 9, 12


@pytest.fixture(scope="module")
def font():
    import os

    import reportlab as rl
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    path = os.path.join(os.path.dirname(rl.__file__), "fonts", "Vera.ttf")  # keeps U+00AD and the en dash
    if not os.path.exists(path):
        pytest.skip("reportlab's bundled Vera.ttf not found")
    pdfmetrics.registerFont(TTFont("Vera", path))
    return "Vera"


def _text(c, font, x, top, lines):
    c.setFont(font, SIZE)
    for k, s in enumerate(lines):
        c.drawString(x, H - top - SIZE - k * LH, s)


def _centred(c, font, x, a, b, lines):
    top = (a + b) / 2 - len(lines) * LH / 2
    _text(c, font, x, top, lines)


def _rule(c, y, left=True, meas=True):
    if left:
        c.line(X_CODE - 8, H - y, X_NAME - 5, H - y)      # segment per column, like a real table
        c.line(X_NAME - 5, H - y, X_MEAS - 5, H - y)
    if meas:
        c.line(X_MEAS - 5, H - y, X_END, H - y)


def _header(c, font, rules=True):
    _text(c, font, X_CODE, 100, ["Fault Code"])
    _text(c, font, X_NAME, 100, ["Fault Name"])
    _text(c, font, X_MEAS, 100, ["Corrective Measures"])
    if rules:
        _rule(c, 114)


def _row(c, font, a, b, code, name, meas, meas_top=None):
    _centred(c, font, X_CODE, a, b, code)
    _centred(c, font, X_NAME, a, b, name)
    _text(c, font, X_MEAS, a + 5 if meas_top is None else meas_top, meas)


@pytest.fixture(scope="module")
def manual(tmp_path_factory, font):
    from reportlab.pdfgen import canvas

    p = tmp_path_factory.mktemp("faults") / "manual.pdf"
    c = canvas.Canvas(str(p), pagesize=(595, H))
    # ---- page 1
    _text(c, font, 60, 40, ["8 Troubleshooting"])           # running head, above the table
    _header(c, font)
    _row(c, font, 114, 200, ["17"], ["Grid Overfre" + SHY, "quency"],
         ["1. Measure the grid fre" + SHY, "quency and call the", "operator.", "2. Check the setting."])
    _rule(c, 200)
    # two codes, one measures cell (no rule between them in the measures column)
    _centred(c, font, X_CODE, 200, 260, ["18"]); _centred(c, font, X_NAME, 200, 260, ["Low Voltage"])
    _centred(c, font, X_CODE, 260, 320, ["19"]); _centred(c, font, X_NAME, 260, 320, ["High Voltage"])
    _text(c, font, X_MEAS, 205, ["Generally the unit restarts.", "1. Check the grid."])
    _rule(c, 200); _rule(c, 260, meas=False); _rule(c, 320)
    # code cell with ranges, broken after the dash
    _row(c, font, 320, 400, ["30, 31, 40" + DASH, "45, 50"], ["System Alarm"],
         ["1. Restart.", "2. Call service."])
    _rule(c, 400)
    # the last row of the page has no closing rule: it goes on on page 2
    c.setFont(font, SIZE)
    _centred(c, font, X_CODE, 400, 520, ["99"]); _centred(c, font, X_NAME, 400, 520, ["Long Fault"])
    lines = [f"{n}. Check item {n}." for n in range(1, 25)]
    _text(c, font, X_MEAS, 405, lines)
    c.showPage()
    # ---- page 2
    _header(c, font)
    _text(c, font, X_MEAS, 118, ["25. Check item 25.", "26. Contact service."])
    _rule(c, 150)
    _row(c, font, 150, 230, ["7"], ["Fan Over", "heating"],
         ["1. Replace the DC-", "side fan.", "2. Reset."])
    _rule(c, 230)
    _text(c, font, 60, 300, ["Some paragraph after the table."])
    c.showPage()
    c.save()
    return p


@pytest.fixture(scope="module")
def doc(manual):
    return ingest(manual, use_cache=False)


def _by_code(doc):
    return {r["code"]: r for r in doc.data["fault_codes"]}


def test_records_and_pages(doc):
    recs = doc.data["fault_codes"]
    assert [r["code"] for r in recs] == ["17", "18", "19", "30, 31, 40-45, 50", "99", "7"]
    assert [r["page"] for r in recs] == [1, 1, 1, 1, 1, 2]
    assert [r["page_end"] for r in recs] == [1, 1, 1, 1, 2, 2]
    assert not [w for w in doc.warnings if "fault" in w]


def test_soft_hyphen_join_and_vertically_centred_code(doc):
    r = _by_code(doc)["17"]
    assert r["name"] == "Grid Overfrequency"
    assert r["measures"][0] == "1. Measure the grid frequency and call the operator."
    assert r["measures"][1] == "2. Check the setting."
    assert SHY in r["raw"]["name"]                       # the lines as read stay available


def test_no_marker_no_join(doc):
    r = _by_code(doc)["7"]
    assert r["name"] == "Fan Over heating"               # two words are not glued without a marker
    assert r["raw"]["name"] == "Fan Over\nheating"
    assert r["measures"][0] == "1. Replace the DC-side fan."   # a hyphen at the line end is kept, no space
    assert r["page"] == 2


def test_shared_measures_cell(doc):
    by = _by_code(doc)
    assert by["18"]["measures"] == by["19"]["measures"] == ["Generally the unit restarts.", "1. Check the grid."]
    assert by["18"]["shared_measures"] and by["19"]["shared_measures"]
    assert "shared_measures" not in by["17"]


def test_code_cell_ranges(doc):
    r = _by_code(doc)["30, 31, 40-45, 50"]
    assert r["code_ranges"] == [[30, 30], [31, 31], [40, 45], [50, 50]]
    assert r["name"] == "System Alarm"
    assert r["measures"] == ["1. Restart.", "2. Call service."]


def test_row_across_the_page_boundary(doc):
    r = _by_code(doc)["99"]
    assert r["name"] == "Long Fault"
    assert len(r["measures"]) == 26
    assert r["measures"][0] == "1. Check item 1."
    assert r["measures"][-1] == "26. Contact service."


def test_repeated_header_and_text_are_not_records(doc):
    names = [r["name"] for r in doc.data["fault_codes"]]
    assert "Fault Name" not in names and not any("paragraph" in n for n in names)
    assert not any("Corrective" in " ".join(r["measures"] or []) for r in doc.data["fault_codes"])


def test_search_finds_the_record_not_the_page(doc):
    ps = [p for p in passages(doc) if p["kind"] == "fault_code"]
    assert len(ps) == 6 and ps[0]["page"] == 1
    assert ps[0]["text"].startswith("Fault code 17: Grid Overfrequency.")
    assert search([doc], "fault 17", top_k=1)[0]["text"].startswith("Fault code 17")
    assert search([doc], "overfrequency", top_k=1)[0]["kind"] == "fault_code"
    hit = search([doc], "fault code 42", top_k=1)[0]          # inside the range 40-45
    assert hit["kind"] == "fault_code" and "System Alarm" in hit["text"]
    assert "_tokens" not in hit
    assert search([doc], "fault 18", top_k=1)[0]["text"].startswith("Fault code 18")


def test_table_without_row_rules_is_not_guessed(tmp_path, font):
    from reportlab.pdfgen import canvas

    p = tmp_path / "norules.pdf"
    c = canvas.Canvas(str(p), pagesize=(595, H))
    _header(c, font, rules=False)
    _row(c, font, 114, 200, ["17"], ["Grid Overfrequency"], ["1. Wait."])
    c.save()
    d = ingest(p, use_cache=False)
    assert "fault_codes" not in d.data
    assert any("no row rules" in w for w in d.warnings)


def test_other_pdfs_have_no_fault_codes(samples):
    assert "fault_codes" not in ingest(samples["pdf"], use_cache=False).data


def test_header_variants():
    def w(t, x, top=100.0):
        return {"text": t, "x0": x, "x1": x + 30, "top": top, "bottom": top + 9}

    hdr = faults.find_header([w("Error", 50), w("Code", 80), w("Description", 130), w("Remedy", 260)])
    assert hdr and hdr["x"] == (50, 130, 260)
    assert faults.find_header([w("Alarm", 50), w("No.", 80), w("Alarm", 130), w("Message", 160),
                               w("Troubleshooting", 260)])
    assert faults.find_header([w("Fault", 50), w("Code", 80), w("Corrective", 260), w("Measures", 300)]) is None


def test_join_rule_unit():
    def line(*t):
        return [{"text": s, "x0": i * 10, "top": 0.0, "bottom": 9.0} for i, s in enumerate(t)]

    text, raw = faults._join([line("oc" + SHY), line("curs", "twice")])
    assert text == "occurs twice" and SHY in raw
    assert faults._join([line("two"), line("words")])[0] == "two words"
    assert faults._steps("Generally it restarts. 1. A. 2. B. *Note x. 3. C") == [
        "Generally it restarts.", "1. A.", "2. B.", "*Note x.", "3. C"]
    assert faults._steps("Wait 2. place") == ["Wait 2. place"]        # "2." is not a step before "1."
