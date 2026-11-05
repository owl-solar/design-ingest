"""LLM extraction into a pydantic schema (optional: ``pip install design-ingest[llm]``).

    from design_ingest import extract
    from design_ingest.schemas import ModuleDatasheet
    ds = extract("JKM580N.pdf", ModuleDatasheet)
    ds.variants[0].pmax_w

Two modes:
* text (default): the parsed Document is sent as markdown - cheap, works for
  every supported format.
* vision=True (PDF only): the PDF itself is sent, so the model also sees
  charts, drawings and pages without a text layer. More tokens per page.

The model is forced to answer through a tool whose input schema is your
pydantic model, and the answer is validated. Results are cached by file hash +
schema + model + instructions, so repeated runs cost nothing.

Requires ANTHROPIC_API_KEY in the environment (API billing is separate from a
Claude.ai subscription). Model: ``DESIGN_INGEST_MODEL`` or argument; default
is a small, cheap model.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .core import default_cache_dir, ingest
from .schema import Document, ParseError

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-haiku-4-5"
SYSTEM = (
    "You extract engineering data from documents for a PV/BESS engineer. "
    "Use only values stated in the document. If a value is missing, leave it null - never estimate. "
    "Convert to the units named in the field names/descriptions. Record page numbers when the document shows them "
    "(markers like '<!-- p.3 -->'). Put conflicts or doubts in 'notes' if the schema has it."
)
MAX_PDF_BYTES = 30 * 1024 * 1024


def _key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def extract(
    source: str | Path | Document,
    schema: type[T],
    *,
    instructions: str | None = None,
    model: str | None = None,
    vision: bool = False,
    max_chars: int = 150_000,
    max_tokens: int = 8192,
    use_cache: bool = True,
    client=None,
) -> T:
    doc = source if isinstance(source, Document) else ingest(source)
    model = model or os.environ.get("DESIGN_INGEST_MODEL", DEFAULT_MODEL)
    schema_json = schema.model_json_schema()

    cache_file = None
    if use_cache:
        k = _key(doc.sha256, schema.__name__, json.dumps(schema_json, sort_keys=True), model,
                 instructions or "", str(vision), str(max_chars))
        cache_file = default_cache_dir() / "extract" / f"{k}.json"
        if cache_file.exists():
            return schema.model_validate_json(cache_file.read_text(encoding="utf-8"))

    task = f"Extract the data into the `{schema.__name__}` structure by calling the tool."
    if instructions:
        task += "\n\nAdditional instructions:\n" + instructions

    if vision:
        if doc.file_type != "pdf":
            raise ParseError("vision=True works for PDF files only")
        raw = Path(doc.source).read_bytes()
        if len(raw) > MAX_PDF_BYTES:
            raise ParseError(f"PDF too large for vision mode ({len(raw) // 1e6:.0f} MB); use text mode or split it")
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.standard_b64encode(raw).decode()}},
            {"type": "text", "text": task},
        ]
    else:
        body = doc.to_markdown(max_table_rows=200)
        if len(body) > max_chars:
            body = body[:max_chars] + f"\n\n[... truncated: document is {len(body)} chars, sent first {max_chars}]"
        content = [{"type": "text", "text": f"<document name=\"{doc.filename}\">\n{body}\n</document>\n\n{task}"}]

    if client is None:
        try:
            import anthropic
        except ImportError as e:
            raise ImportError("LLM extraction needs: pip install design-ingest[llm]") from e
        client = anthropic.Anthropic()
    tool = {"name": "record", "description": f"Record the extracted {schema.__name__}.", "input_schema": schema_json}
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record"},
        messages=[{"role": "user", "content": content}],
    )
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if block is None:
        raise ParseError(f"model returned no structured answer (stop_reason={resp.stop_reason})")
    if resp.stop_reason == "max_tokens":
        raise ParseError("answer was cut off (max_tokens) - raise max_tokens or narrow the schema")
    result = schema.model_validate(block.input)

    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result
