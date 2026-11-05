"""Embeddings for the optional hybrid search (``search(..., mode="hybrid")``).

Needs the extra ``embeddings`` (``pip install "design-ingest[embeddings]"``: fastembed, ONNX, no PyTorch).
The core works without it and without any model.

* Model: ``BAAI/bge-small-en-v1.5`` (English), about 65 MB, **downloaded only on request**:
  ``design-ingest download-model`` or ``design_ingest.embeddings.download_model()``. That is a network
  request to the model host (Hugging Face); no document is sent. ``hybrid`` never downloads by itself:
  without the model it raises ``EmbeddingsUnavailable`` that names the command.
* Model folder: ``DESIGN_INGEST_MODEL_DIR`` or ``~/.cache/design_ingest/models``.
* Vectors of passages are cached in ``<cache dir>/vectors.sqlite`` (key: sha256 of the text + model name),
  so building the same index again does not run the model again.
"""
from __future__ import annotations

import hashlib
import math
import os
import sqlite3
from array import array
from pathlib import Path
from typing import Any, Protocol, Sequence

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
MODEL_SIZE_MB = 65


class EmbeddingsUnavailable(RuntimeError):
    """The extra or the model is missing; the message says what to run."""


class Embedder(Protocol):
    """What hybrid search needs. Tests use a fake one."""

    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def model_dir() -> Path:
    env = os.environ.get("DESIGN_INGEST_MODEL_DIR")
    if env:
        return Path(env)
    from .core import default_cache_dir

    return default_cache_dir() / "models"


def _fastembed(local_only: bool):
    try:
        from fastembed import TextEmbedding
    except ImportError as e:
        raise EmbeddingsUnavailable(
            'hybrid search needs the extra "embeddings": pip install "design-ingest[embeddings]"') from e
    kwargs: dict[str, Any] = {"cache_dir": str(model_dir())}
    if local_only:
        kwargs["local_files_only"] = True
    return TextEmbedding(model_name=DEFAULT_MODEL, **kwargs)


class FastEmbedder:
    """BAAI/bge-small-en-v1.5 through fastembed, from the local model folder only."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.name = model
        try:
            self._m = _fastembed(local_only=True)
        except EmbeddingsUnavailable:
            raise
        except Exception as e:  # fastembed raises different errors for a missing model
            raise EmbeddingsUnavailable(
                f"the embedding model {DEFAULT_MODEL} (about {MODEL_SIZE_MB} MB) is not in {model_dir()}. "
                "Download it once, on purpose: design-ingest download-model") from e

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._m.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return list(map(float, next(iter(self._m.query_embed(text)))))


def download_model() -> Path:
    """Download the model into ``model_dir()`` (a network request; call it on purpose). Returns the folder."""
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    _fastembed(local_only=False)
    return d


def get_embedder() -> Embedder:
    return FastEmbedder()


# ---------------------------------------------------------------- vectors
def normalize(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class VectorCache:
    """sha256(text) + model name -> unit vector, in one sqlite file."""

    def __init__(self, path: Path | None = None):
        if path is None:
            from .core import default_cache_dir

            path = default_cache_dir() / "vectors.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute("CREATE TABLE IF NOT EXISTS vec (key TEXT PRIMARY KEY, v BLOB NOT NULL)")

    @staticmethod
    def key(text: str, model: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest() + ":" + model

    def get_many(self, keys: list[str]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            q = "SELECT key, v FROM vec WHERE key IN (%s)" % ",".join("?" * len(part))
            for k, blob in self._db.execute(q, part):
                a = array("f")
                a.frombytes(blob)
                out[k] = list(a)
        return out

    def put_many(self, items: dict[str, list[float]]) -> None:
        with self._db:
            self._db.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?)",
                                 [(k, array("f", v).tobytes()) for k, v in items.items()])

    def close(self) -> None:
        self._db.close()


def embed_texts(embedder: Embedder, texts: list[str], cache: VectorCache | None) -> list[list[float]]:
    """Unit vectors of ``texts``; those seen before (same text, same model) come from the cache."""
    keys = [VectorCache.key(t, embedder.name) for t in texts]
    have = cache.get_many(list(dict.fromkeys(keys))) if cache else {}
    missing = list(dict.fromkeys(t for t, k in zip(texts, keys) if k not in have))
    if missing:
        new = {VectorCache.key(t, embedder.name): normalize(v) for t, v in zip(missing, embedder.embed(missing))}
        if cache:
            cache.put_many(new)
        have.update(new)
    return [have[k] for k in keys]
