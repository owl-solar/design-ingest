"""Hybrid search (BM25 + embeddings) with a fake embedder: no model is downloaded or loaded."""
import math
import re
import sys

import pytest

from design_ingest import embeddings
from design_ingest.cli import main
from design_ingest.embeddings import EmbeddingsUnavailable, VectorCache
from design_ingest.schema import Chunk, Document
from design_ingest.search import RRF_K, build_index, search

# words that "mean" the same get the same dimension, the way a real model puts them close together
CONCEPTS = [{"fan", "fans", "blower", "blowers", "cooling"}, {"run", "operate", "operating", "start", "work"},
            {"insulation", "isolation"}, {"filter", "dust"}, {"voltage", "volt"}]


class FakeEmbedder:
    name = "fake-concepts"

    def __init__(self):
        self.calls = 0           # texts embedded (not queries)

    def _vec(self, text):
        words = re.findall(r"[a-z]+", text.lower())
        v = [float(sum(w in c for w in words)) for c in CONCEPTS]
        return v + [0.01 * (len(text) % 7)]        # a little noise so that no vector is zero

    def embed(self, texts):
        self.calls += len(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def _doc(name, texts, doc_type="manual"):
    return Document(source=f"/x/{name}", filename=name, file_type="pdf", sha256="cd" * 32, size_bytes=1,
                    parser="t", parser_version="1", chunks=[Chunk(text=t, kind="text", page=i + 1) for i, t in enumerate(texts)],
                    data={"doc_info": {"doc_type": doc_type}})


@pytest.fixture
def docs():
    return [
        _doc("UM_A.pdf", ["After power on the fans start to run for ten seconds.",
                          "Replace the dust filter every six months.",
                          "Maximum DC voltage is 1500 V."]),
        _doc("DS_B.pdf", ["The blowers operate at full speed above 40 degrees.", "Insulation resistance fault 39."],
             doc_type="datasheet"),
    ]


@pytest.fixture
def vcache(tmp_path):
    c = VectorCache(tmp_path / "v.sqlite")
    yield c
    c.close()


def test_default_mode_is_bm25_and_unchanged(docs):
    base = search(docs, "fans run")
    assert base == search(docs, "fans run", mode="bm25")
    assert all("bm25_rank" not in h and "embedding_rank" not in h for h in base)
    assert base[0]["text"].startswith("After power on the fans")
    with pytest.raises(ValueError):
        search(docs, "x", mode="semantic")


def test_hybrid_finds_a_passage_with_no_common_word(docs, vcache):
    q = "verify cooling blowers operate"
    assert [h for h in search(docs, q) if "fans start" in h["text"]] == []     # BM25 alone: no shared word
    idx = build_index(docs, mode="hybrid", embedder=FakeEmbedder(), vector_cache=vcache)
    hits = idx.search(q, top_k=3)
    texts = [h["text"] for h in hits]
    assert any("fans start to run" in t for t in texts[:2])
    h = next(h for h in hits if "fans start" in h["text"])
    assert h["bm25_rank"] is None and h["embedding_rank"] >= 1
    assert {"file", "page", "kind", "score", "text"} <= set(h)


def test_rrf_score_and_ranks_of_a_passage_in_both_lists(docs, vcache):
    idx = build_index(docs, mode="hybrid", embedder=FakeEmbedder(), vector_cache=vcache)
    top = idx.search("dust filter", top_k=1)[0]
    assert "dust filter" in top["text"]
    assert top["bm25_rank"] == 1 and top["embedding_rank"] == 1
    assert top["score"] == round(2 / (RRF_K + 1), 3)


def test_filters_apply_in_hybrid(docs, vcache):
    idx = build_index(docs, mode="hybrid", embedder=FakeEmbedder(), vector_cache=vcache)
    only_sheet = idx.search("cooling blowers operate", top_k=10, doc_types=["datasheet"])
    assert only_sheet and {h["file"] for h in only_sheet} == {"DS_B.pdf"}
    one_file = idx.search("cooling blowers operate", top_k=10, files=["um_a.pdf"])
    assert one_file and {h["file"] for h in one_file} == {"UM_A.pdf"}


def test_vectors_are_cached_by_text_and_model(docs, vcache):
    e1 = FakeEmbedder()
    build_index(docs, mode="hybrid", embedder=e1, vector_cache=vcache)
    assert e1.calls == 5
    e2 = FakeEmbedder()
    build_index(docs, mode="hybrid", embedder=e2, vector_cache=vcache)
    assert e2.calls == 0                                   # all five came from the cache
    docs[0].chunks.append(Chunk(text="A new sentence about voltage.", kind="text", page=9))
    e3 = FakeEmbedder()
    build_index(docs, mode="hybrid", embedder=e3, vector_cache=vcache)
    assert e3.calls == 1                                   # only the new passage
    other = FakeEmbedder()
    other.name = "another-model"
    build_index(docs, mode="hybrid", embedder=other, vector_cache=vcache)
    assert other.calls == 6                                # another model: no reuse


def test_default_vector_cache_lives_in_the_cache_dir(docs, tmp_path, monkeypatch):
    monkeypatch.setenv("DESIGN_INGEST_CACHE", str(tmp_path / "c"))
    build_index(docs, mode="hybrid", embedder=FakeEmbedder())
    assert (tmp_path / "c" / "vectors.sqlite").exists()
    e = FakeEmbedder()
    build_index(docs, mode="hybrid", embedder=e)
    assert e.calls == 0


def test_missing_extra_gives_a_clear_error(docs, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)           # import fails
    with pytest.raises(EmbeddingsUnavailable, match=r"design-ingest\[embeddings\]"):
        build_index(docs, mode="hybrid", vector_cache=False)


def test_missing_model_names_the_download_command(docs, monkeypatch, tmp_path):
    monkeypatch.setenv("DESIGN_INGEST_MODEL_DIR", str(tmp_path / "models"))

    def no_model(local_only):
        assert local_only                                       # hybrid must never ask for a download
        raise ValueError("model files not found")

    monkeypatch.setattr(embeddings, "_fastembed", no_model)
    with pytest.raises(EmbeddingsUnavailable, match="design-ingest download-model") as e:
        build_index(docs, mode="hybrid", vector_cache=False)
    assert str(tmp_path / "models") in str(e.value)


def test_model_dir_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("DESIGN_INGEST_MODEL_DIR", str(tmp_path / "m"))
    assert embeddings.model_dir() == tmp_path / "m"
    monkeypatch.delenv("DESIGN_INGEST_MODEL_DIR")
    monkeypatch.setenv("DESIGN_INGEST_CACHE", str(tmp_path / "c"))
    assert embeddings.model_dir() == tmp_path / "c" / "models"


def test_download_model_is_the_only_call_that_may_download(monkeypatch, tmp_path):
    monkeypatch.setenv("DESIGN_INGEST_MODEL_DIR", str(tmp_path / "m"))
    seen = []
    monkeypatch.setattr(embeddings, "_fastembed", lambda local_only: seen.append(local_only))
    assert embeddings.download_model() == tmp_path / "m"
    assert seen == [False]


def test_cli_download_model_and_hybrid_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DESIGN_INGEST_MODEL_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("DESIGN_INGEST_CACHE", str(tmp_path / "c"))
    monkeypatch.setattr(embeddings, "_fastembed", lambda local_only: None)
    assert main(["download-model"]) == 0
    assert "65 MB" in capsys.readouterr().out

    def no_model(local_only):
        raise ValueError("missing")

    monkeypatch.setattr(embeddings, "_fastembed", no_model)
    f = tmp_path / "t.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    assert main(["search", str(f), "fans", "--mode", "hybrid"]) == 1
    assert "download-model" in capsys.readouterr().err


def test_vectors_are_unit_length():
    v = embeddings.normalize([3.0, 4.0])
    assert math.isclose(sum(x * x for x in v), 1.0)
