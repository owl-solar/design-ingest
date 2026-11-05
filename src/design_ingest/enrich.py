"""Post-processing shared by all formats, run once after a parser (before caching).

Adds to ``doc.data`` (never changes what a parser returned, never raises):

* ``registers``   register / point list rows, when a table looks like a register map
                  (see ``registers.extract_registers``)
* ``specs``       {model: {key: {"raw", "quantities"}}} of a datasheet's key/value
                  tables, and the fixed-name fields in ``summary`` when the parser
                  gave no summary (see ``specs.canonical_specs``)
* ``doc_info``    {"doc_type", "confidence", "signals", "filename": {...}}
                  (see ``doctype.classify``)

Bump ``ENRICH_VERSION`` when any of these change: it is part of the cache key.
"""
from __future__ import annotations

from .schema import Document

ENRICH_VERSION = "2"

# documents with at most this many pages are treated as datasheets for ``specs``
_DATASHEET_MAX_PAGES = 8


def enrich(doc: Document) -> Document:
    try:
        _enrich(doc)
    except Exception as e:  # enrichment must never break a successful parse
        doc.warnings.append(f"enrichment skipped: {type(e).__name__}")
    return doc


def _enrich(doc: Document) -> None:
    from .doctype import classify
    from .registers import extract_registers
    from .specs import canonical_specs, extract_specs

    if doc.file_type in ("pdf", "docx", "xlsx", "xlsm") and doc.tables:
        regs = extract_registers(doc.tables)
        if regs:
            doc.data["registers"] = regs

    info = classify(doc)
    doc.data["doc_info"] = info

    pages = doc.data.get("pages") or 0
    is_sheet = info["doc_type"] in ("datasheet", "technical_note") and 0 < pages <= _DATASHEET_MAX_PAGES
    if doc.file_type == "pdf" and is_sheet and not doc.data.get("registers"):
        specs = extract_specs(doc.tables)
        if specs:
            doc.data["specs"] = specs
            if "summary" not in doc.data:
                canon = {m: canonical_specs(e) for m, e in specs.items()}
                canon = {m: c for m, c in canon.items() if c}
                if len(canon) == 1:
                    doc.data["summary"] = next(iter(canon.values()))
                elif canon:
                    doc.data["summary"] = {"models": canon}
