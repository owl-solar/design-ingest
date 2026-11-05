"""Unified output format: every parser returns a ``Document``.

Design rule: whatever the input (PDF, drawing, PVsyst file...), downstream code
sees the same three things:

* ``chunks``  - pieces of text with their location (page / sheet / layer / section),
                ready for search, RAG or an LLM prompt;
* ``tables``  - tabular data with columns and rows;
* ``data``    - format-specific structured results (PAN parameters, DXF block
                counts, KML geometries...), documented per parser.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

ChunkKind = Literal[
    "text", "heading", "table", "cad_text", "attribute", "placemark", "parameters"
]


class Chunk(BaseModel):
    text: str
    kind: ChunkKind = "text"
    page: int | None = None  # 1-based page (PDF)
    sheet: str | None = None  # sheet name (XLSX)
    layer: str | None = None  # CAD layer (DXF/DWG)
    section: str | None = None  # heading path, e.g. "3 Scope > 3.2 Inverters"
    meta: dict[str, Any] = Field(default_factory=dict)

    def location(self) -> str:
        parts = []
        if self.page is not None:
            parts.append(f"p.{self.page}")
        if self.sheet:
            parts.append(f"sheet '{self.sheet}'")
        if self.layer:
            parts.append(f"layer '{self.layer}'")
        if self.section:
            parts.append(self.section)
        return ", ".join(parts)


class Table(BaseModel):
    name: str | None = None
    page: int | None = None
    sheet: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.rows), len(self.columns)

    def to_markdown(self, max_rows: int | None = 50) -> str:
        cols = self.columns or [f"col{i + 1}" for i in range(_width(self.rows))]

        def cell(v: Any) -> str:
            return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")

        lines = [
            "| " + " | ".join(cell(c) for c in cols) + " |",
            "|" + "---|" * len(cols),
        ]
        rows = self.rows if max_rows is None else self.rows[:max_rows]
        for r in rows:
            r = list(r) + [None] * (len(cols) - len(r))
            lines.append("| " + " | ".join(cell(v) for v in r[: len(cols)]) + " |")
        if max_rows is not None and len(self.rows) > max_rows:
            lines.append(f"... ({len(self.rows) - max_rows} more rows)")
        return "\n".join(lines)

    def to_dataframe(self):
        try:
            import pandas as pd
        except ImportError as e:  # pragma: no cover
            raise ImportError("pip install design-ingest[pandas]") from e
        cols = _unique(self.columns) if self.columns else None
        width = len(cols) if cols else _width(self.rows)
        rows = [list(r)[:width] + [None] * (width - len(r)) for r in self.rows]
        return pd.DataFrame(rows, columns=cols)


class Document(BaseModel):
    source: str  # absolute path of the input file
    filename: str
    file_type: str  # pdf, docx, xlsx, csv, dxf, dwg, kml, kmz, pan, ond
    sha256: str
    size_bytes: int
    parser: str
    parser_version: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunks: list[Chunk] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    # ---- convenience -------------------------------------------------------
    def text(self, sep: str = "\n\n") -> str:
        """All chunk text joined - quick full-text view."""
        return sep.join(c.text for c in self.chunks if c.text)

    def to_markdown(self, max_table_rows: int | None = 50) -> str:
        """Readable dump for an LLM prompt or a human check."""
        out = [f"# {self.filename}  ({self.file_type})"]
        if self.metadata:
            meta = ", ".join(f"{k}: {v}" for k, v in self.metadata.items() if v not in (None, ""))
            if meta:
                out.append(f"_Metadata: {meta}_")
        if self.data.get("summary"):
            out.append("## Summary\n" + json.dumps(self.data["summary"], ensure_ascii=False, indent=2))
        last_loc = None
        for c in self.chunks:
            if c.kind == "table":
                continue  # tables are rendered from self.tables below
            loc = c.location()
            if loc and loc != last_loc:
                out.append(f"\n<!-- {loc} -->")
                last_loc = loc
            out.append(("## " + c.text) if c.kind == "heading" else c.text)
        for i, t in enumerate(self.tables, 1):
            where = f" p.{t.page}" if t.page else (f" sheet '{t.sheet}'" if t.sheet else "")
            out.append(f"\n### Table {i}{where}{(': ' + t.name) if t.name else ''}")
            out.append(t.to_markdown(max_table_rows))
        if self.warnings:
            out.append("\n_Warnings: " + "; ".join(self.warnings) + "_")
        return "\n".join(out)

    def save_json(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load_json(cls, path: str | Path) -> "Document":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def summary(self) -> str:
        return (
            f"{self.filename}: {self.file_type}, {len(self.chunks)} chunks, "
            f"{len(self.tables)} tables"
            + (f", {len(self.warnings)} warnings" if self.warnings else "")
        )


class ParseError(Exception):
    """Raised when a file cannot be parsed (corrupt, encrypted, unsupported variant)."""


def _width(rows: list[list[Any]]) -> int:
    return max((len(r) for r in rows), default=0)


def _unique(cols: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for c in cols:
        c = str(c) if c not in (None, "") else "col"
        if c in seen:
            seen[c] += 1
            out.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out
