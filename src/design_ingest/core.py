"""Entry points: ``ingest`` one file, ``ingest_dir`` a folder.

Parsers register themselves per file extension. A parser is a function
``parse(path: Path, **options) -> dict`` that returns the Document fields it
knows (chunks, tables, data, metadata, warnings); the common fields (hash,
size, names) are filled here.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .schema import Document, ParseError


@dataclass
class ParserSpec:
    name: str
    version: str
    func: Callable[..., dict[str, Any]]
    file_type: str


_REGISTRY: dict[str, ParserSpec] = {}


def register(extensions: Iterable[str], name: str, version: str, file_type: str | None = None):
    """Decorator: ``@register([".pdf"], "pdf", "1")``."""

    def deco(func):
        for ext in extensions:
            ext = ext.lower()
            _REGISTRY[ext] = ParserSpec(name, version, func, file_type or ext.lstrip("."))
        return func

    return deco


def supported_extensions() -> list[str]:
    _load_parsers()
    return sorted(_REGISTRY)


def _load_parsers() -> None:
    # Import for the side effect of registration.
    from .parsers import cad, docx, geo, pdf, pvsyst, tabular  # noqa: F401


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
def default_cache_dir() -> Path:
    env = os.environ.get("DESIGN_INGEST_CACHE")
    return Path(env) if env else Path.home() / ".cache" / "design_ingest"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _cache_key(sha: str, spec: ParserSpec, options: dict[str, Any]) -> str:
    opts = json.dumps(options, sort_keys=True, default=str)
    raw = f"{sha}|{spec.name}|{spec.version}|{opts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def ingest(
    path: str | Path,
    *,
    use_cache: bool = True,
    cache_dir: str | Path | None = None,
    **options: Any,
) -> Document:
    """Parse one file into a :class:`Document`.

    Options are passed to the parser (e.g. ``tables=False`` for a fast PDF read,
    ``max_rows=...`` for spreadsheets). Results are cached by file content, so
    re-reading an unchanged file is instant.
    """
    _load_parsers()
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    ext = path.suffix.lower()
    spec = _REGISTRY.get(ext)
    if spec is None:
        raise ParseError(
            f"Unsupported file type '{ext}' ({path.name}). Supported: {', '.join(supported_extensions())}"
        )

    sha = file_sha256(path)
    cache_file = None
    if use_cache:
        cdir = Path(cache_dir) if cache_dir else default_cache_dir()
        cache_file = cdir / f"{_cache_key(sha, spec, options)}.json"
        if cache_file.exists():
            try:
                doc = Document.load_json(cache_file)
                doc.source = str(path)  # same content may live at another path
                doc.filename = path.name
                return doc
            except Exception:
                pass  # corrupt cache entry -> re-parse

    try:
        result = spec.func(path, **options)
    except ParseError:
        raise
    except Exception as e:  # wrap library errors with the file name
        raise ParseError(f"{path.name}: {type(e).__name__}: {e}") from e

    doc = Document(
        source=str(path),
        filename=path.name,
        file_type=result.pop("file_type", spec.file_type),
        sha256=sha,
        size_bytes=path.stat().st_size,
        parser=spec.name,
        parser_version=spec.version,
        **result,
    )
    if cache_file is not None:
        try:
            doc.save_json(cache_file)
        except OSError:
            doc.warnings.append("could not write cache")
    return doc


def ingest_dir(
    folder: str | Path,
    *,
    recursive: bool = True,
    extensions: Iterable[str] | None = None,
    on_error: str = "warn",
    **kwargs: Any,
) -> list[Document]:
    """Parse every supported file in a folder.

    ``on_error``: "warn" (skip, print reason), "raise", or "ignore".
    Files the parser cannot read are skipped; errors are collected in
    ``ingest_dir.last_errors``.
    """
    _load_parsers()
    folder = Path(folder).expanduser()
    allowed = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions} if extensions else set(_REGISTRY)
    pattern = "**/*" if recursive else "*"
    docs: list[Document] = []
    errors: list[tuple[str, str]] = []
    for p in sorted(folder.glob(pattern)):
        if not p.is_file() or p.suffix.lower() not in allowed or p.name.startswith("~$"):
            continue
        try:
            docs.append(ingest(p, **kwargs))
        except Exception as e:
            if on_error == "raise":
                raise
            errors.append((str(p), str(e)))
            if on_error == "warn":
                print(f"[design-ingest] skipped {p.name}: {e}")
    ingest_dir.last_errors = errors  # type: ignore[attr-defined]
    return docs
