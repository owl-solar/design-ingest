"""Questions about a set of documents, with checked citations.

    from design_ingest.ask import ask
    a = ask("Which register holds the daily energy yield?", "docs/", allow_external=True)
    a.answer, [(c.file, c.page, c.verified) for c in a.citations]

How it works (the model is used for the last step only):

1. ``design_ingest.search`` finds the best passages offline (``top_k``, default 8).
2. Only those passages go to the model, numbered ``[1]``, ``[2]`` ... with file and page.
   Never the whole documents. The call is behind ``design_ingest.llm``: blocked unless
   ``allow_external=True`` (or ``DESIGN_INGEST_ALLOW_EXTERNAL=1``), logged without
   content, optional token budget.
3. The model must answer through a tool: the answer, ``found`` (false when the passages do
   not contain it) and a verbatim ``quote`` per citation.
4. Every quote is **checked here**: ``verified`` is true only if the quote occurs word
   for word (whitespace and case ignored) in the passage it names. Citations to passages
   that were not sent are dropped. An answer without any verified citation is marked
   ``verified=False``: do not rely on it.

If the search finds nothing, no call is made and nothing is sent.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .core import ingest, ingest_dir
from .llm import guarded_create, require_allowed
from .schema import Document
from .search import build_index, location

DEFAULT_MODEL = "claude-haiku-5-5"

SYSTEM = (
    "You answer questions about PV / BESS equipment and project documents for an engineer. "
    "Use ONLY the numbered passages you are given; never use outside knowledge for values, "
    "units or addresses. If the passages do not contain the answer, set found=false and say what is missing. "
    "Every statement of fact needs a citation: the passage number and a quote copied exactly from that "
    "passage (at most 200 characters, no rewording). Keep the answer short and keep the units."
)


class _Cite(BaseModel):
    n: int = Field(description="number of the passage, as in [n]")
    quote: str = Field(description="verbatim quote from that passage, at most 200 characters")


class _ReplySchema(BaseModel):
    """What the model is asked for (all fields required)."""

    answer: str
    found: bool = Field(description="false if the passages do not contain the answer")
    citations: list[_Cite] = Field(description="empty when found is false")


class _Reply(BaseModel):
    """What is accepted: models sometimes leave out an empty field. ``found`` missing is read
    as found when there are citations, otherwise as not found."""

    answer: str = ""
    found: bool | None = None
    citations: list[_Cite] = Field(default_factory=list)


class Citation(BaseModel):
    n: int
    file: str
    page: int | None = None
    kind: str | None = None
    quote: str
    verified: bool


class Answer(BaseModel):
    question: str
    answer: str
    found: bool
    verified: bool  # at least one citation was found word for word in its passage
    citations: list[Citation] = Field(default_factory=list)
    passages_sent: list[dict[str, Any]] = Field(default_factory=list)  # n, file, page, chars (never text)
    model: str | None = None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def _load(docs) -> list[Document]:
    if isinstance(docs, (str, Path)):
        p = Path(docs)
        return [ingest(p)] if p.is_file() else ingest_dir(p, on_error="ignore")
    if isinstance(docs, Document):
        return [docs]
    return list(docs)


def ask(
    question: str,
    docs,
    *,
    top_k: int = 8,
    doc_types=None,
    files=None,
    model: str | None = None,
    max_tokens: int = 1024,
    client=None,
    allow_external: bool | None = None,
) -> Answer:
    """Answer ``question`` from ``docs`` (a path, a Document or a list of Documents)."""
    require_allowed("ask", allow_external)  # before anything is read or sent
    documents = _load(docs)
    hits = build_index(documents).search(question, top_k, doc_types, files)
    return answer_from_hits(question, hits, model=model, max_tokens=max_tokens, client=client,
                            allow_external=allow_external)


def answer_from_hits(
    question: str,
    hits: list[dict[str, Any]],
    *,
    model: str | None = None,
    max_tokens: int = 1024,
    client=None,
    allow_external: bool | None = None,
) -> Answer:
    """Answer ``question`` from passages the caller already found (hits as ``Index.search`` returns them:
    ``file``, ``page``, ``kind``, ``text``). Same guard, prompt, tool and quote check as ``ask``; use it
    when you have your own retrieval (exact lookups, hybrid search, filters)."""
    import os

    require_allowed("ask", allow_external)
    model = model or os.environ.get("DESIGN_INGEST_MODEL", DEFAULT_MODEL)
    if not hits:
        return Answer(question=question, answer="No passage in the documents matches the question.",
                      found=False, verified=False, model=None)
    blocks = [f"[{i}] {location(h)} ({h['kind']})\n{h['text']}" for i, h in enumerate(hits, 1)]
    prompt = "\n\n".join(blocks) + f"\n\nQuestion: {question}"
    if client is None:
        try:
            import anthropic
        except ImportError as e:
            raise ImportError("ask needs: pip install design-ingest[llm]") from e
        client = anthropic.Anthropic()
    tool = {"name": "reply", "description": "Give the answer with checked citations.",
            "input_schema": _ReplySchema.model_json_schema()}
    names = list(dict.fromkeys(h["file"] for h in hits))
    resp = guarded_create(
        client,
        purpose="ask",
        file="; ".join(names[:5]) + (f" (+{len(names) - 5} more)" if len(names) > 5 else ""),
        sha256="",
        kind="text",
        amount=len(prompt),
        allow_external=allow_external,
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM,
        tools=[tool],
        tool_choice={"type": "tool", "name": "reply"},
        messages=[{"role": "user", "content": prompt}],
    )
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if block is None:
        raise RuntimeError(f"model returned no structured answer (stop_reason={resp.stop_reason})")
    try:
        reply = _Reply.model_validate(block.input)
    except ValueError as e:  # pydantic.ValidationError is a ValueError
        raise RuntimeError(f"model answer could not be read: {str(e)[:200]}") from e
    found = reply.found if reply.found is not None else bool(reply.citations)

    cites: list[Citation] = []
    for c in reply.citations:
        if not 1 <= c.n <= len(hits):
            continue  # cites a passage that was not sent
        h = hits[c.n - 1]
        cites.append(Citation(n=c.n, file=h["file"], page=h.get("page"), kind=h.get("kind"),
                              quote=c.quote, verified=bool(c.quote.strip()) and _norm(c.quote) in _norm(h["text"])))
    return Answer(
        question=question,
        answer=reply.answer,
        found=found,
        verified=any(c.verified for c in cites),
        citations=cites,
        passages_sent=[{"n": i, "file": h["file"], "page": h.get("page"), "chars": len(h["text"])}
                       for i, h in enumerate(hits, 1)],
        model=model,
    )
