"""Fault / alarm code tables of equipment manuals ("Fault Code | Fault Name | Corrective Measures").

Works on word and line coordinates of a PDF page (pdfplumber), not on the flattened page text,
because in these tables the code is centred vertically in its row, names and steps wrap in
the middle of words and the header is repeated on every page.

Record (``data["fault_codes"]``)::

    {"code": "39", "name": "Low System Insulation Resistance",
     "measures": ["Wait for the inverter ...", "1. Check whether ...", "2. ..."],
     "page": 131, "page_end": 131,
     "code_ranges": [[39, 39]],            # only when the code cell is numbers / ranges
     "shared_measures": True,              # only when one measures cell serves several codes
     "raw": {"name": "...", "measures": "..."}}   # cell lines as read, soft hyphens kept

How a table is found: a line of the page that has the three headings in order, for example
``Fault Code`` / ``Fault Name`` / ``Corrective Measures`` (also Error / Alarm, Description /
Message, Remedy / Solution / Action). The x of each heading word fixes the three columns.

How records are cut: by the horizontal rules that the table draws. A rule that crosses the code
column and the name column ends a record. The code is read from every word of the code column
between two rules, wherever it stands vertically. The measures column is cut by the rules that
cross *it*: when two records share one measures cell (no rule between them in that column) both
get the same measures and ``shared_measures``. When the cut is ambiguous (several records over
several measures cells) the measures are left out and a warning is added.

A table page without such rules is not read at all (a warning says so): guessing row borders
from text positions would put steps under the wrong code.

Joining wrapped text: only a line that ends in a soft hyphen (U+00AD, what Word and InDesign
write when they hyphenate) is joined to the next one without a space, and the marker is dropped;
an ASCII hyphen at the end of a line followed by a lowercase letter is joined without a space and
kept. Every other line break becomes a space, so two different words are never glued; a word
broken without any marker stays broken ("Overfrequen cy") and is visible in ``raw``.
A row that continues on the next page is joined to its first part (``page_end``).
"""
from __future__ import annotations

import re
from typing import Any

SOFT_HYPHEN = "­"
_DASHES = "‐‑‒–—−­"

_CODE_H = r"(?:fault|error|alarm|warning|event)?\s*(?:code|no\.?|number|id)"
_NAME_H = r"(?:fault|error|alarm|warning|event)?\s*(?:name|description|message|text|type)"
_ACT_H = (r"(?:(?:corrective|remedial|recommended|troubleshooting)\s+)?"
          r"(?:measures?|actions?|steps?)|remedy|remedies|solutions?|troubleshooting")
_HEADER = [re.compile(r"^" + p + r"$", re.I) for p in (_CODE_H, _NAME_H, _ACT_H)]

_STEP = re.compile(r"^\s*(\d{1,2})\s*[.)]\s+")
_NUM_CODE = re.compile(r"^\d+(?:-\d+)?(?:,\s*\d+(?:-\d+)?)*$")
_BOTTOM_MARGIN = 0.07      # share of the page height at the bottom that is footer, not table
_PAD = 3.0                 # columns start this far left of their heading
_YTOL = 1.2                # rules this close (points) are the same rule
_MAX_EXPAND = 3000


# ---------------------------------------------------------------- page geometry
def _lines(words: list[dict], tol: float = 2.5) -> list[list[dict]]:
    """Words of one column grouped into text lines (top to bottom, left to right)."""
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if out and abs(w["top"] - out[-1][0]["top"]) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in out]


def find_header(words: list[dict]) -> dict | None:
    """Columns of a fault table header on this page, or None.

    ``{"x": (code_x, name_x, action_x), "top": y, "bottom": y}`` (x = left edge of each heading).
    """
    for line in _lines(words, tol=3.0):
        toks = [(w["text"], w) for w in line]
        # try every split of the line's words into three consecutive headings
        n = len(toks)
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                parts = [toks[:i], toks[i:j], toks[j:]]
                if all(rx.match(" ".join(t for t, _ in p).strip()) for rx, p in zip(_HEADER, parts)):
                    xs = tuple(p[0][1]["x0"] for p in parts)
                    return {"x": xs, "top": min(w["top"] for _, w in toks),
                            "bottom": max(w["bottom"] for _, w in toks)}
    return None


def _rules(edges: list[dict]) -> list[tuple[float, float, float]]:
    """Horizontal segments as (y, x0, x1)."""
    return [(e["top"], e["x0"], e["x1"]) for e in edges
            if e.get("orientation") == "h" and e["x1"] - e["x0"] > 4]


def _boundaries(rules, px: list[float], y_min: float, y_max: float) -> list[float]:
    """y of the rules (below the header) that cross every x in ``px``."""
    ys = sorted({round(y, 1) for y, x0, x1 in rules if y_min <= y <= y_max})
    out: list[float] = []
    for y in ys:
        if all(any(abs(yy - y) <= _YTOL and x0 <= x + 0.5 and x1 >= x - 0.5 for yy, x0, x1 in rules) for x in px):
            if not out or y - out[-1] > _YTOL:
                out.append(y)
    return out


def read_page(page_no: int, words: list[dict], edges: list[dict], height: float,
              header: dict, tail: bool) -> dict[str, Any]:
    """Cells of one table page.

    Returns ``{"page", "cells": [...], "tail": cell | None, "warnings": [...]}``. A cell is
    ``{"code": [words], "name": [words], "meas": [words] | None, "shared": bool}``.
    ``tail`` (asked for when the table goes on on the next page) is the open row below the
    last rule. Word ``top`` / ``bottom`` are shifted by ``page_no * 10000`` so that words of
    several pages keep their reading order when a row is joined across pages.
    """
    cx, nx, ax = header["x"]
    x_cut = (cx - _PAD, nx - _PAD, ax - _PAD)
    y0 = header["bottom"]
    y_end = height * (1 - _BOTTOM_MARGIN)
    rules = _rules(edges)
    left = _boundaries(rules, [cx, nx], y0, y_end)
    meas = _boundaries(rules, [ax], y0, y_end)
    res: dict[str, Any] = {"page": page_no, "cells": [], "tail": None, "warnings": []}
    if len(left) < 2:
        res["warnings"].append(f"fault table on p.{page_no} has no row rules: not extracted")
        return res

    body = [w for w in words if y0 <= w["top"] < y_end and w["x0"] >= x_cut[0]]
    off = page_no * 10000.0
    body = [{**w, "top": w["top"] + off, "bottom": w["bottom"] + off} for w in body]

    def col(w):
        return 0 if w["x0"] < x_cut[1] else (1 if w["x0"] < x_cut[2] else 2)

    def between(a, b):
        return [w for w in body if a + off <= (w["top"] + w["bottom"]) / 2 < b + off]

    spans = list(zip(left[:-1], left[1:]))
    mspans = list(zip(meas[:-1], meas[1:]))
    if tail:
        spans.append((left[-1], y_end))
        mspans.append((meas[-1] if meas else left[-1], y_end))
    for (a, b), (touch, shared, unclear) in zip(spans, _link(spans, mspans)):
        rows = between(a, b)
        cell: dict[str, Any] = {"code": [w for w in rows if col(w) == 0], "name": [w for w in rows if col(w) == 1],
                                "shared": shared, "meas": None}
        if unclear:
            res["warnings"].append(
                f"fault table on p.{page_no}: measures cells and code cells do not line up near y={a:.0f}, measures left out")
        else:
            lo, hi = min(mspans[m][0] for m in touch), max(mspans[m][1] for m in touch)
            cell["meas"] = [w for w in between(lo, hi) if col(w) == 2]
        res["cells"].append(cell)
    if tail:
        res["tail"] = res["cells"].pop()
    return res


def _link(spans, mspans) -> list[tuple[list[int], bool, bool]]:
    """Per record span: (measures spans it overlaps, shares one with another record?, unclear?).

    One measures cell over several records is a merged cell: the text belongs to all of them.
    A record over several measures cells that are also shared with other records cannot be
    resolved without guessing: unclear. No rules in the measures column at all: unclear."""
    touch = [[j for j, (ma, mb) in enumerate(mspans) if min(b, mb) - max(a, ma) > 1.0] for a, b in spans]
    users: dict[int, int] = {}
    for t in touch:
        for j in t:
            users[j] = users.get(j, 0) + 1
    out = []
    for t in touch:
        shared = any(users[j] > 1 for j in t)
        out.append((t, shared, not t or (shared and len(t) > 1)))
    return out


# ---------------------------------------------------------------- text of cells
def _join(lines: list[list[dict]]) -> tuple[str, str]:
    """(joined text, raw text) of cell lines; see the module docstring for the join rule."""
    texts = [" ".join(w["text"] for w in ln).strip() for ln in lines]
    texts = [t for t in texts if t]
    raw = "\n".join(texts)
    out = ""
    for t in texts:
        if not out:
            out = t
        elif out.endswith(SOFT_HYPHEN):
            out = out[:-1] + t
        elif re.search(r"[A-Za-z0-9]-$", out) and t[:1].islower():
            out += t
        else:
            out += " " + t
    return out.replace(SOFT_HYPHEN, "").strip(), raw


def _steps(text: str) -> list[str]:
    """Split a measures text at "1." "2." ... markers; text before the first marker is its own item."""
    parts = re.split(r"(?:(?<=\s)|^)(?=\d{1,2}\.\s+\S|\*\S)", text)  # a footnote "*..." is its own item
    items: list[str] = []
    expect = 1
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = _STEP.match(p)
        if m and int(m.group(1)) == expect:
            items.append(p)
            expect += 1
        elif p.startswith("*") or not items:
            items.append(p)
        elif items:
            items[-1] = (items[-1] + " " + p).strip()   # a stray "3. " inside a sentence is not a step
    return items


def _code_text(words: list[dict]) -> tuple[str, str]:
    """Code cell: every word of the cell; a range split after its dash is glued back ("76-" + "81")."""
    lines = _lines(words, tol=2.5)
    texts = [" ".join(w["text"] for w in ln).strip() for ln in lines]
    raw = "\n".join(t for t in texts if t)
    out = ""
    for t in (t for t in texts if t):
        if out and out[-1] in _DASHES and t[:1].isdigit():
            out += t
        else:
            out += (" " if out else "") + t
    out = re.sub("[" + _DASHES + "]", "-", out)
    out = re.sub(r"\s*-\s*", "-", out)
    return re.sub(r"\s+", " ", out).strip(), raw


def _ranges(code: str) -> list[list[int]] | None:
    if not _NUM_CODE.match(code):
        return None
    out = []
    for part in code.split(","):
        a, _, b = part.strip().partition("-")
        out.append([int(a), int(b or a)])
    return out


def expand(code_ranges: list[list[int]], limit: int = _MAX_EXPAND) -> list[int]:
    """All numbers of ``code_ranges`` (capped), for search."""
    out: list[int] = []
    for a, b in code_ranges:
        if b < a or b - a > limit:
            continue
        out.extend(range(a, b + 1))
        if len(out) > limit:
            break
    return out[:limit]


# ---------------------------------------------------------------- whole document
def build_records(pages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Records from ``read_page`` results of the table pages, in page order."""
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    carry: dict | None = None  # open row at the bottom of the previous page: {"cell", "page"}
    prev = None
    for pg in pages:
        warnings.extend(pg["warnings"])
        consecutive = prev is not None and pg["page"] == prev + 1
        if carry is not None and not consecutive:
            _emit(records, carry["cell"], carry["page"], carry["page"], warnings)
            carry = None
        for k, c in enumerate(pg["cells"]):
            first = pg["page"]
            if k == 0 and carry is not None:
                if carry["cell"]["code"] and c["code"]:      # two rows: the open one was complete
                    _emit(records, carry["cell"], carry["page"], carry["page"], warnings)
                else:
                    c, first = _merge(carry["cell"], c), carry["page"]
                carry = None
            elif k == 0 and consecutive and records and not c["code"] and not c["name"] and c["meas"]:
                records[-1]["_extra"].append(c["meas"])       # the rest of the previous row's measures
                records[-1]["page_end"] = pg["page"]
                continue
            _emit(records, c, first, pg["page"], warnings)
        t = pg["tail"]
        if t is not None and (t["code"] or t["name"] or t["meas"]):
            carry = {"cell": t, "page": pg["page"]}
        prev = pg["page"]
    if carry is not None:  # the table ends with an open row and nothing follows
        _emit(records, carry["cell"], carry["page"], carry["page"], warnings)
    return _finish(records), warnings


def _merge(a: dict, b: dict) -> dict:
    meas = None if a["meas"] is None or b["meas"] is None else a["meas"] + b["meas"]
    return {"code": a["code"] + b["code"], "name": a["name"] + b["name"], "meas": meas,
            "shared": a["shared"] or b["shared"]}


def _emit(records, c, page, page_end, warnings):
    code, code_raw = _code_text(c["code"])
    if not code:
        if c["name"] or c["meas"]:
            warnings.append(f"fault table on p.{page_end}: a row without a code was left out")
        return
    name, name_raw = _join(_lines(c["name"]))
    records.append({"code": code, "name": name, "measures": None, "page": page, "page_end": page_end,
                    "_meas": c["meas"], "_extra": [], "_name_raw": name_raw, "_shared": c["shared"],
                    "_code_raw": code_raw})


def _finish(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        mw = r.pop("_meas")
        extra = r.pop("_extra")
        name_raw = r.pop("_name_raw")
        shared = r.pop("_shared")
        code_raw = r.pop("_code_raw")
        raw_m = ""
        if mw is not None:
            text, raw_m = _join(_lines(mw + [w for e in extra for w in e]))
            r["measures"] = _steps(text)
        rng = _ranges(r["code"])
        if rng:
            r["code_ranges"] = rng
        if shared and mw is not None:
            r["shared_measures"] = True
        r["raw"] = {"name": name_raw, "measures": raw_m}
        if code_raw != r["code"]:
            r["raw"]["code"] = code_raw
        out.append(r)
    return out
