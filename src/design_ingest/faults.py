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

More than one table can stand on a page. Headings between the name and the action heading ("Inverter
behavior", sub-headings S1 S2 R on the next line) are extra columns, read into ``columns``; every column
is cut by its own rules and linked to the code cells by overlap, so a code with its own code cell can share
the other cells with the next code. A bullet sign starts a step of its own (and is dropped), a ``*`` after
the code goes to ``code_marker``. The span between the last row and the heading block of the next table is
not a row. A heading with S1 S2 R written inline is a "how to read" figure and is skipped.

A table page without such rules is not read at all (a warning says so): guessing row borders
from text positions would put steps under the wrong code.

Joining wrapped text: only a line that ends in a soft hyphen (U+00AD, what Word and InDesign
write when they hyphenate) is joined to the next one without a space, and the marker is dropped;
an ASCII hyphen at the end of a line followed by a lowercase letter is joined without a space and
kept, unless the two halves together are a word that occurs in the document (``vocab``): then the hyphen goes. Every other line break becomes a space, so two different words are never glued; a word
broken without any marker stays broken ("Overfrequen cy") and is visible in ``raw``.
A row that continues on the next page is joined to its first part (``page_end``).
"""
from __future__ import annotations

import re
from typing import Any

SOFT_HYPHEN = "­"
_DASHES = "‐‑‒–—−­"

_CODE_H = r"(?:fault|error|alarm|warning|event)?\s*(?:code|no\.?|number|id)"
_NAME_H = r"(?:fault|error|alarm|warning|event)?\s*(?:name|description|message|text|type|explanation)"
_ACT_H = (r"(?:(?:corrective|remedial|recommended|troubleshooting)\s+)?"
          r"(?:measures?|actions?|steps?)|remedy|remedies|solutions?|troubleshooting")
_HEADER = [re.compile(r"^" + p + r"$", re.I) for p in (_CODE_H, _NAME_H, _ACT_H)]

_STEP = re.compile(r"^\s*(\d{1,2})\s*[.)]\s+")
_NUM_CODE = re.compile(r"^\d+(?:-\d+)?(?:,\s*\d+(?:-\d+)?)*$")
_BOTTOM_MARGIN = 0.07      # share of the page height at the bottom that is footer, not table
_PAD = 3.0                 # columns start this far left of their heading
_YTOL = 1.2                # rules this close (points) are the same rule
_MAX_EXPAND = 3000
_HEADING_BLOCK = 20.0      # a heading's top rule is at most this far above its text (points)


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


def find_headers(words: list[dict]) -> list[dict]:
    """Fault table headings on this page (a page can hold several tables), top to bottom.

    ``{"x": (code_x, name_x, action_x), "extra": [(label, x)], "top": y, "bottom": y}`` where x is the
    left edge of each heading. Headings between the name and the action heading (SMA: "Inverter
    behavior", with S1 / S2 / R below it) are ``extra`` columns; their sub-headings are looked up
    in ``read_page``.
    """
    out = []
    for line in _lines(words, tol=3.0):
        toks = [(w["text"], w) for w in line]
        n = len(toks)
        found = None
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                for k in range(j, min(n, j + 4)):          # words j..k-1: optional extra heading
                    parts = [toks[:i], toks[i:j], toks[k:]]
                    if all(rx.match(" ".join(t for t, _ in pt).strip()) for rx, pt in zip(_HEADER, parts)):
                        extra = toks[j:k]
                        found = {"x": (toks[0][1]["x0"], toks[i][1]["x0"], toks[k][1]["x0"]),
                                 "extra": [(" ".join(t for t, _ in extra), extra[0][1]["x0"])] if extra else [],
                                 # S1 S2 R written inline in the heading line: a "how to read" figure, not a table
                                 "legend": len(extra) >= 2 and all(len(t) <= 2 for t, _ in extra),
                                 "top": min(w["top"] for _, w in toks), "bottom": max(w["bottom"] for _, w in toks)}
                        break
                if found:
                    break
            if found:
                break
        if found:
            out.append(found)
    return out


def find_header(words: list[dict]) -> dict | None:
    """First fault table heading of the page, or None (see ``find_headers``)."""
    hs = find_headers(words)
    return hs[0] if hs else None


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
              header, tail: bool) -> dict[str, Any]:
    """Cells of one table page (``header``: one heading dict or the list from ``find_headers``).

    Returns ``{"page", "cells": [...], "tail": cell | None, "warnings": [...]}``. A cell is
    ``{"code": [words], "name": [words], "extra": {label: [words]}, "meas": [words] | None,
    "shared": bool}``. ``tail`` (asked for when the table goes on on the next page) is the open
    row below the last rule of the last table. Word ``top`` / ``bottom`` are shifted by
    ``page_no * 10000`` so that words of several pages keep their reading order when a row is
    joined across pages.
    """
    headers = [header] if isinstance(header, dict) else list(header)
    y_end = height * (1 - _BOTTOM_MARGIN)
    res: dict[str, Any] = {"page": page_no, "cells": [], "tail": None, "warnings": []}
    for n, h in enumerate(headers):
        last = n == len(headers) - 1
        stop = y_end if last else headers[n + 1]["top"] - 1
        if h.get("legend"):
            continue
        cells, open_cell, warns, last_rule = _read_table(page_no, words, edges, h, stop, tail and last)
        # The last rule above the heading of the next table is the top of its heading block: what lies
        # between the last row and that rule (a section title, a footnote) is not a row.
        if not last and cells and 0 <= headers[n + 1]["top"] - last_rule <= _HEADING_BLOCK:
            cells.pop()
        res["cells"].extend(cells)
        res["warnings"].extend(m for k, m in warns if k is None or k < len(cells))
        if open_cell is not None:
            res["tail"] = open_cell
    return res


def _read_table(page_no, words, edges, header, y_end, tail):
    cx, nx, ax = header["x"]
    y0 = header["bottom"]
    rules = _rules(edges)
    code_b = _boundaries(rules, [cx], y0, y_end)
    if len(code_b) < 2:
        return [], None, [(None, f"fault table on p.{page_no} has no row rules: not extracted")], 0.0

    # extra columns: the sub-headings between the header and the first row (S1 S2 R), else the heading itself
    extra: list[tuple[str, float]] = []
    if header["extra"]:
        lo = header["extra"][0][1] - 5
        sub = [w for w in words if header["bottom"] < w["top"] < code_b[0] and lo <= w["x0"] < ax - _PAD]
        extra = [(w["text"], w["x0"]) for w in sorted(sub, key=lambda w: w["x0"])] or list(header["extra"])
    cols = [("code", cx), ("name", nx), *extra, ("meas", ax)]
    cuts = [x - _PAD for _, x in cols]

    off = page_no * 10000.0
    body = [{**w, "top": w["top"] + off, "bottom": w["bottom"] + off}
            for w in words if y0 <= w["top"] < y_end and w["x0"] >= cuts[0]]

    def col(w):
        return max(i for i, c in enumerate(cuts) if w["x0"] >= c)

    def between(a, b):
        return [w for w in body if a + off <= (w["top"] + w["bottom"]) / 2 < b + off]

    def spans_of(bounds):
        sp = list(zip(bounds[:-1], bounds[1:]))
        if tail:
            sp.append((bounds[-1] if bounds else code_b[-1], y_end))
        return sp

    rec_spans = spans_of(code_b)
    warns: list[str] = []
    per_col = []                       # for every other column: (label, cell spans, link info per record)
    for name, x in cols[1:]:
        sp = spans_of(_boundaries(rules, [x], y0, y_end))
        per_col.append((name, sp, _link(rec_spans, sp)))

    cells = []
    for r, (a, b) in enumerate(rec_spans):
        cell: dict[str, Any] = {"code": [w for w in between(a, b) if col(w) == 0], "name": [], "extra": {},
                                "meas": None, "shared": False}
        for ci, (name, sp, link) in enumerate(per_col, start=1):
            touch, shared, unclear = link[r]
            if unclear:
                words_ = None
                warns.append((r, f"fault table on p.{page_no}: '{name}' cells and code cells do not line up near "
                                 f"y={a:.0f}, left out"))
            else:
                lo, hi = min(sp[m][0] for m in touch), max(sp[m][1] for m in touch)
                words_ = [w for w in between(lo, hi) if col(w) == ci]
            if ci == len(per_col):            # the last column is the measures
                cell["meas"], cell["shared"] = words_, shared
            elif name == "name" and ci == 1:
                cell["name"] = words_ or []
            else:
                cell["extra"][name] = words_ or []
        cells.append(cell)
    open_cell = cells.pop() if tail else None
    return cells, open_cell, warns, code_b[-1]


def _link(spans, mspans) -> list[tuple[list[int], bool, bool]]:
    """Per record span: (cell spans of another column it overlaps, shares one with another record?, unclear?).

    One cell over several records is a merged cell: the text belongs to all of them.
    A record over several cells that are also shared with other records cannot be
    resolved without guessing: unclear. No rules in that column at all: unclear."""
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
def _join(lines: list[list[dict]], vocab: set[str] | None = None) -> tuple[str, str]:
    """(joined text, raw text) of cell lines; see the module docstring for the join rule.

    ``vocab``: words (lower case) of the document. An ASCII hyphen at the end of a line is dropped
    when the two halves together are a word of the document ("reconnec-" + "tion"), else kept
    ("DC-" + "side")."""
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
            a = re.search(r"([A-Za-z]+)-$", out)
            b = re.match(r"[a-z]+", t)
            if vocab and a and b and (a.group(1) + b.group(0)).lower() in vocab:
                out = out[:-1] + t
            else:
                out += t
        else:
            out += " " + t
    return out.replace(SOFT_HYPHEN, "").strip(), raw


_SPLIT = re.compile(r"(?:(?<=\s)|^)(?=\d{1,2}\.\s+\S|\*\S|[•●▪‣]\s)")


def _steps(text: str) -> list[str]:
    """Split a measures text into items.

    Numbered steps "1." "2." ... (only in sequence; a stray "3." inside a sentence is not a step), bullets
    (the bullet sign is dropped) and footnotes starting with "*" are items of their own; text before the
    first marker is an item too."""
    items: list[str] = []
    expect = 1
    for p in _SPLIT.split(text):
        p = p.strip()
        if not p:
            continue
        m = _STEP.match(p)
        if p[0] in "•●▪‣":
            items.append(p[1:].strip())
        elif m and int(m.group(1)) == expect:
            items.append(p)
            expect += 1
        elif p.startswith("*") or not items:
            items.append(p)
        else:
            items[-1] = (items[-1] + " " + p).strip()
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
def build_records(pages: list[dict[str, Any]], vocab: set[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
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
    return _finish(records, vocab), warnings


def _merge(a: dict, b: dict) -> dict:
    meas = None if a["meas"] is None or b["meas"] is None else a["meas"] + b["meas"]
    extra = {k: (a["extra"].get(k) or []) + (b["extra"].get(k) or []) for k in {**a["extra"], **b["extra"]}}
    return {"code": a["code"] + b["code"], "name": a["name"] + b["name"], "extra": extra, "meas": meas,
            "shared": a["shared"] or b["shared"]}


def _emit(records, c, page, page_end, warnings):
    code, code_raw = _code_text(c["code"])
    if not code:
        if c["name"] or c["meas"]:
            warnings.append(f"fault table on p.{page_end}: a row without a code was left out")
        return
    records.append({"code": code, "name": "", "measures": None, "page": page, "page_end": page_end,
                    "_cell": c, "_extra": [], "_code_raw": code_raw})


def _finish(records: list[dict[str, Any]], vocab: set[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for r in records:
        c = r.pop("_cell")
        extra = r.pop("_extra")
        code_raw = r.pop("_code_raw")
        r["name"], name_raw = _join(_lines(c["name"]), vocab)
        raw_m = ""
        if c["meas"] is not None:
            text, raw_m = _join(_lines(c["meas"] + [w for e in extra for w in e]), vocab)
            r["measures"] = _steps(text)
        m = re.match(r"^(.*?)(\*+)$", r["code"])         # "0801*": a footnote sign, not part of the code
        if m and m.group(1).strip():
            r["code"], r["code_marker"] = m.group(1).strip(), m.group(2)
        rng = _ranges(r["code"])
        if rng:
            r["code_ranges"] = rng
        if c["shared"] and c["meas"] is not None:
            r["shared_measures"] = True
        cols = {k: _join(_lines(w), vocab)[0] for k, w in c["extra"].items()}
        if any(cols.values()):
            r["columns"] = cols
        r["raw"] = {"name": name_raw, "measures": raw_m}
        if code_raw != r["code"] and code_raw != r["code"] + r.get("code_marker", ""):
            r["raw"]["code"] = code_raw
        out.append(r)
    return out
