"""Alarm references that give one section per alarm and real tables (Huawei style).

Reads ``doc.tables`` and ``doc.chunks`` (nothing from page coordinates), so it works on any format
that gives tables. Sequence per alarm, in document order::

    table  Alarm ID | Alarm Name | Alarm Severity        -> a new record
    table  Cause ID | Possible Cause   (one or more, the heading repeats on the next page)
    table  Cause ID | Suggestion       (one or more)  - or the suggestion as page text after
                                                          the heading line "Suggestion"

Record (added to ``data["fault_codes"]``; same keys as ``faults.py`` plus the ones below)::

    {"code": "2001", "name": "String Voltage High", "severity": "Major",
     "causes": [{"id": "13", "cause": "...", "suggestion": "..."}],
     "measures": ["suggestion of cause 1", ...] or the steps of the common suggestion text,
     "page": 12, "page_end": 14}

A suggestion is tied to a cause only by the cause ID printed in both tables (exact match, a range such
as ``1-10`` is one ID). A text suggestion (no table) belongs to the alarm whose heading line
(``<ID> <Name>``, repeated in the running head of every page) is above it; it is not attached to a
single cause. Text of a page whose running head does not name the open alarm is not used.
"""
from __future__ import annotations

import re
from typing import Any

from .faults import _steps

_DASH = re.compile("[‐-―−­]")
_ID = re.compile(r"^(?:alarm|fault|error|event)\s*(?:id|code|no\.?|number)$", re.I)
_NAME = re.compile(r"^(?:alarm|fault|error|event)\s*(?:name|description|message|text)$", re.I)
_SEV = re.compile(r"^(?:alarm|fault|error|event)?\s*(?:severity|level|class|category)$", re.I)
_CAUSE_ID = re.compile(r"^cause\s*(?:id|no\.?|number)$", re.I)
_CAUSE = re.compile(r"^(?:possible\s+)?causes?$", re.I)
_SUGG = re.compile(r"^(?:suggestions?|(?:corrective\s+)?(?:measures?|actions?)|remedy|solutions?)$", re.I)
_FOOTER = re.compile(r"copyright|^issue\s+\d+", re.I)


def _flat(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _kind(columns: list[Any]) -> str | None:
    c = [_flat(x) for x in columns]
    if len(c) >= 2 and _ID.match(c[0]) and _NAME.match(c[1]):
        return "alarm"
    if len(c) >= 2 and _CAUSE_ID.match(c[0]):
        if _CAUSE.match(c[1]):
            return "cause"
        if _SUGG.match(c[1]):
            return "suggestion"
    return None


def has_alarm_tables(tables) -> bool:
    return any(_kind(t.columns) == "alarm" for t in tables)


def _key(s: str) -> str:
    """Text for comparing a heading with page text: wrapped lines and 'short- circuited' compare equal."""
    return re.sub(r"[\s­-]+", "", s).lower()


def _norm_id(s: str) -> str:
    return re.sub(r"\s*-\s*", "-", _DASH.sub("-", _flat(s)))


def extract_alarm_sections(chunks, tables) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    by_page_text: dict[int, str] = {}
    for c in chunks:
        if c.kind == "text" and not (c.meta or {}).get("rotated") and c.page is not None:
            by_page_text[c.page] = by_page_text.get(c.page, "") + "\n" + c.text
    recs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    text_done: set[tuple[int, str]] = set()
    skipped_pages: set[int] = set()

    def heads(rec) -> str:
        return f"{rec['code']} {rec['name']}"

    def take_text(rec: dict, page: int) -> None:
        """Suggestion text of this page (lines after the line "Suggestion") for the alarm ``rec``."""
        if (page, rec["code"]) in text_done:
            return
        text_done.add((page, rec["code"]))
        lines = [ln.strip() for ln in by_page_text.get(page, "").splitlines()]
        if _key(heads(rec)) not in _key(" ".join(lines)):
            return
        for k, ln in enumerate(lines):
            if ln == "Suggestion":
                body = []
                for nxt in lines[k + 1:]:
                    if _FOOTER.search(nxt) or nxt == "Alarm Attribute":
                        break
                    body.append(nxt)
                txt = " ".join(b for b in body if b)
                if txt:
                    rec.setdefault("_text", []).append(txt)
                return

    for t in tables:
        kind = _kind(t.columns)
        if kind is None:
            continue
        if kind == "alarm":
            if not t.rows or not _flat(t.rows[0][0]):
                continue
            row = [_flat(x) for x in t.rows[0]]
            sev_ix = next((i for i, h in enumerate(t.columns) if _SEV.match(_flat(h))), None)
            cur = {"code": row[0], "name": row[1], "measures": None, "page": t.page, "page_end": t.page,
                   "causes": []}
            if sev_ix is not None and sev_ix < len(row) and row[sev_ix]:
                cur["severity"] = row[sev_ix]
            notes = [_flat(r[0]) for r in t.rows[1:] if r and _flat(r[0]) and not any(_flat(x) for x in r[1:])]
            if notes and notes[0].lower().startswith("note"):
                cur["severity_note"] = " ".join(notes)
                m = re.match(r"note\s*(\[\w+\]|\w)\s*[:.]", notes[0], re.I)   # "Note a:" / "Note [1]:"
                sev = cur.get("severity")
                if m and sev and sev.endswith(m.group(1)) and len(sev) > len(m.group(1)):
                    cur["severity"] = sev[: -len(m.group(1))].strip()
            recs.append(cur)
            continue
        if cur is None:
            continue
        if _key(heads(cur)) not in _key(by_page_text.get(t.page, "")):
            if t.page not in skipped_pages:
                skipped_pages.add(t.page)
                warnings.append(f"alarm tables on p.{t.page} do not belong to the open alarm {cur['code']}: skipped")
            continue
        cur["page_end"] = max(cur["page_end"], t.page)
        field = "cause" if kind == "cause" else "suggestion"
        for r in t.rows:
            cid, txt = _norm_id(r[0]) if r else "", _flat(r[1]) if len(r) > 1 else ""
            if not cid:  # row of the same cause continued from the previous page
                if cur["causes"] and txt:
                    last = cur["causes"][-1]
                    last[field] = ((last.get(field) or "") + " " + txt).strip()
                continue
            for c in cur["causes"]:
                if c["id"] == cid and not c.get(field):
                    c[field] = txt
                    break
            else:
                cur["causes"].append({"id": cid, field: txt})
    for rec in recs:
        for p in range(rec["page"], rec["page_end"] + 1):
            take_text(rec, p)
    out = []
    for rec in recs:
        texts = rec.pop("_text", [])
        for c in rec["causes"]:
            c.setdefault("cause", None)
            c.setdefault("suggestion", None)
        sugg = [c["suggestion"] for c in rec["causes"] if c["suggestion"]]
        if sugg:
            rec["measures"] = sugg
        elif texts:
            rec["measures"] = _steps(" ".join(texts))
        else:
            warnings.append(f"alarm {rec['code']} (p.{rec['page']}): no suggestion found")
        if not rec["causes"]:
            del rec["causes"]
        out.append(rec)
    return out, warnings
