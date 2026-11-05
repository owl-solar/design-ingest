"""Search over parsed documents, offline (no model, nothing is sent anywhere).

    from design_ingest import ingest_dir
    from design_ingest.search import search
    for hit in search(ingest_dir("docs/"), "daily power yield register", top_k=5):
        print(hit["score"], hit["file"], hit["page"], hit["text"][:80])

Every document is cut into passages of at most ``max_chars``:

* text chunks (by line), tables (by rows, the header repeated in every piece);
* one short passage per register of a register map (``Register 5003 (U16): Daily power
  yields, scale 0.1, unit kWh``), per datasheet specification (``Max. PV input
  voltage: 1500 V``) and per fault code (``Fault code 39: Low System Insulation Resistance.
  Corrective measures: ...``), so a question about a single value or code finds that
  record, not a page. A code range (``264-283``) is found by any number inside it.

Ranking is BM25 over words and numbers (``5003``, ``0.1``, ``kva`` ...), the file name
counts as text of its passages, and a passage that contains the whole query phrase gets a
bonus. This is the retrieval step of ``ask`` (``design_ingest.ask``), which adds a model
only for the final answer.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable

from .faults import expand as expand_codes
from .schema import Document

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"\d+(?:[.,]\d+)*|[^\W_]+(?:[-.][^\W_]+)*", re.UNICODE)

# words that mean (nearly) the same in equipment documents; added to a query at half weight
SYNONYMS: dict[str, list[str]] = {
    "mppt": ["mpp"], "mpp": ["mppt"], "rating": ["class"], "ip": ["protection"],
    "yield": ["energy"], "energy": ["yield"], "baud": ["bitrate"],
    "alarm": ["fault", "error"], "fault": ["alarm", "error"], "error": ["fault", "alarm"], "event": ["fault"],
}


_RECORD_BOOST = 1.6


def _stem(w: str) -> str:
    """Very light plural stripping: yields -> yield, inverters -> inverter, batteries -> battery."""
    if len(w) > 3 and w.isalpha():
        if w.endswith("ies"):
            return w[:-3] + "y"
        if w.endswith("s") and not w.endswith(("ss", "us", "is")):
            return w[:-1]
    return w


def tokenize(text: str) -> list[str]:
    """Words and numbers. A model name stays whole and in parts: ``AB350XY-20`` gives
    ``ab350xy-20``, ``ab350xy`` and ``20``; ``0,1`` becomes ``0.1``; ``12.12.12.12`` stays one token."""
    out: list[str] = []
    for m in _WORD.findall(text.lower()):
        if _NUMBER.fullmatch(m):
            out.append(m.replace(",", "."))
            continue
        parts = re.split(r"[-.]", m)
        if len(parts) > 1:
            out.append(m)
        out.extend(_stem(x) for x in parts if x)
    return out


def _split_text(text: str, max_chars: int) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) + 1 > max_chars:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return [p.strip() for p in out if p.strip()]


def _split_table(md: str, max_chars: int) -> list[str]:
    lines = md.splitlines()
    head, body = lines[:2], lines[2:]
    if len(md) <= max_chars or len(lines) <= 3:
        return [md]
    out, cur = [], []
    size = sum(len(h) + 1 for h in head)
    for line in body:
        if cur and size + len(line) + 1 > max_chars:
            out.append("\n".join(head + cur))
            cur, size = [], sum(len(h) + 1 for h in head)
        cur.append(line)
        size += len(line) + 1
    if cur:
        out.append("\n".join(head + cur))
    return out


def passages(doc: Document, max_chars: int = 1200) -> list[dict[str, Any]]:
    """Passages of one document, each with its location."""
    base = {"file": doc.filename, "doc_type": (doc.data.get("doc_info") or {}).get("doc_type"), "source": doc.source}
    out: list[dict[str, Any]] = []

    def add(text: str, kind: str, page=None, sheet=None, layer=None, section=None, **extra):
        out.append({**base, "text": text, "kind": kind, "page": page, "sheet": sheet, "layer": layer,
                    "section": section, **extra})

    for c in doc.chunks:
        if not c.text.strip():
            continue
        pieces = _split_table(c.text, max_chars) if c.kind == "table" else _split_text(c.text, max_chars)
        for piece in pieces:
            add(piece, c.kind, c.page, c.sheet, c.layer, c.section)
    for r in doc.data.get("registers", []):
        bits = [f"Register {r['address']}" + (f"-{r['address_end']}" if r.get("address_end") else "")
                + (f" ({r['data_type']})" if r.get("data_type") else "") + f": {r['name']}"]
        if r.get("scale") is not None:
            bits.append(f"scale {r['scale']}")
        if r.get("unit"):
            bits.append(f"unit {r['unit']}")
        if r.get("range"):
            bits.append(f"range {r['range']}")
        if r.get("access"):
            bits.append(f"access {r['access']}")
        add(", ".join(bits), "register", r.get("page"), r.get("sheet"))
    for f in doc.data.get("fault_codes", []):
        head = f"Fault code {f['code']}: {f['name']}."
        lines = []
        if f.get("severity"):
            lines.append(f"Severity: {f['severity']}")
        if f.get("columns"):
            lines.append("; ".join(f"{k}: {v}" for k, v in f["columns"].items() if v))
        for c in f.get("causes") or []:
            if c.get("cause"):
                lines.append(f"Cause {c['id']}: {c['cause']}")
        if f.get("measures"):
            lines.append("Corrective measures: " + " ".join(f["measures"]) if not f.get("causes") else
                         "Corrective measures:")
            if f.get("causes"):
                lines.extend(f"Cause {c['id']}: {c['suggestion']}" for c in f["causes"] if c.get("suggestion"))
                if not any(c.get("suggestion") for c in f["causes"]):
                    lines.append(" ".join(f["measures"]))
        extra = {"_codes": frozenset(tokenize(f["code"]))}
        if f.get("code_ranges"):
            nums = [str(n) for n in expand_codes(f["code_ranges"])]
            extra["_tokens"] = nums
            extra["_codes"] = extra["_codes"] | frozenset(nums)
        # a long record is cut by lines; every piece starts with the code and name
        for piece in _split_text("\n".join(lines), max_chars - len(head) - 1) or [""]:
            add((head + " " + piece).strip(), "fault_code", f.get("page"), **extra)
    for model, entries in (doc.data.get("specs") or {}).items():
        for key, e in entries.items():
            add(f"{model}: {key}: {e['raw']}", "spec")
    return out


class Index:
    """BM25 index over passages."""

    def __init__(self, items: list[dict[str, Any]], k1: float = 1.5, b: float = 0.75):
        self.items, self.k1, self.b = items, k1, b
        self.tf: list[Counter] = []
        self.len: list[int] = []
        df: Counter = Counter()
        for it in items:
            toks = tokenize(it["file"]) + tokenize(it["text"]) + it.get("_tokens", [])
            c = Counter(toks)
            self.tf.append(c)
            self.len.append(len(toks))
            df.update(c.keys())
        n = max(len(items), 1)
        self.avg = sum(self.len) / n if items else 1.0
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, top_k: int = 8, doc_types: Iterable[str] | None = None,
               files: Iterable[str] | None = None) -> list[dict[str, Any]]:
        q = list(dict.fromkeys(tokenize(query)))
        if not q:
            return []
        weight = {t: 1.0 for t in q}
        for t in q:
            for extra in SYNONYMS.get(t, []):
                weight.setdefault(extra, 0.5)
        types = set(doc_types) if doc_types else None
        only = {f.lower() for f in files} if files else None
        phrase = " ".join(tokenize(query))
        numbers = {t for t in q if _NUMBER.fullmatch(t)}
        scored = []
        for i, it in enumerate(self.items):
            if types and it.get("doc_type") not in types:
                continue
            if only and it["file"].lower() not in only:
                continue
            tf, ln = self.tf[i], self.len[i]
            s = 0.0
            for t, w in weight.items():
                f = tf.get(t)
                if f:
                    s += w * self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            if s <= 0:
                continue
            if it.get("kind") == "fault_code":
                # a record of its own is the answer, not the short table row or running head;
                # more so when the question names its code
                s *= _RECORD_BOOST * (2.0 if it["_codes"] & numbers else 1.0)
            if len(q) > 1 and phrase in " ".join(tokenize(it["text"])):
                s *= 1.3  # the whole phrase appears
            scored.append((s, i))
        scored.sort(key=lambda x: -x[0])
        return [{**{k: v for k, v in self.items[i].items() if k not in ("_tokens", "_codes")}, "score": round(s, 3)}
                for s, i in scored[:top_k]]


def build_index(docs: Iterable[Document], max_chars: int = 1200) -> Index:
    items: list[dict[str, Any]] = []
    for d in docs:
        items.extend(passages(d, max_chars))
    return Index(items)


def search(docs: Iterable[Document], query: str, top_k: int = 8, doc_types: Iterable[str] | None = None,
           files: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Best passages for ``query`` across ``docs`` (see the module docstring)."""
    return build_index(docs).search(query, top_k, doc_types, files)


def location(hit: dict[str, Any]) -> str:
    bits = [hit["file"]]
    if hit.get("page") is not None:
        bits.append(f"p.{hit['page']}")
    if hit.get("sheet"):
        bits.append(f"sheet '{hit['sheet']}'")
    if hit.get("layer"):
        bits.append(f"layer '{hit['layer']}'")
    return ", ".join(bits)
