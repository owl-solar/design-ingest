"""Search and ask on generated documents; the model is a fake client."""
from types import SimpleNamespace

import pytest

from design_ingest.ask import ask
from design_ingest.llm import ExternalCallBlocked, read_log
from design_ingest.schema import Chunk, Document
from design_ingest.search import build_index, passages, search, tokenize


def _doc(name, chunks=(), data=None, doc_type=None):
    d = dict(data or {})
    if doc_type:
        d["doc_info"] = {"doc_type": doc_type}
    return Document(source=f"/x/{name}", filename=name, file_type="pdf", sha256="ab" * 32, size_bytes=1,
                    parser="t", parser_version="1", chunks=list(chunks), data=d)


@pytest.fixture
def docs():
    protocol = _doc(
        "Device_Protocol_V1.pdf",
        [Chunk(text="Default setting\nBaud rate 9600 bit/s\nParity none", kind="text", page=6)],
        {"registers": [
            {"name": "Daily power yields", "address": 5003, "address_end": None, "n_registers": 1,
             "data_type": "U16", "scale": 0.1, "unit": "kWh", "page": 8},
            {"name": "Total running time", "address": 5006, "address_end": 5007, "n_registers": 2,
             "data_type": "U32", "scale": None, "unit": "h", "page": 8},
        ]}, "protocol")
    sheet = _doc(
        "DS_X1_Datasheet.pdf",
        [Chunk(text="X1 multi-MPPT inverter", kind="text", page=1)],
        {"specs": {"X1": {"Input (DC) > Max. PV input voltage": {"raw": "1500 V"},
                          "General Data > Degree of protection": {"raw": "IP66"}}}}, "datasheet")
    manual = _doc("UM_Logger_Manual.pdf", [
        Chunk(text="The data logger collects data from inverters.\nDefault IP address 12.12.12.12", kind="text", page=3),
        Chunk(text="| Parameter | Value |\n|---|---|\n" + "\n".join(f"| row {i} | {i * 7} |" for i in range(200)),
              kind="table", page=4)], doc_type="manual")
    return [protocol, sheet, manual]


def test_tokenize_keeps_numbers_and_decimals():
    assert tokenize("Register 5003 scale 0,1 kWh") == ["register", "5003", "scale", "0.1", "kwh"]


def test_registers_and_specs_become_short_passages(docs):
    ps = passages(docs[0])
    assert "Register 5003 (U16): Daily power yields, scale 0.1, unit kWh" in [p["text"] for p in ps]
    assert any(p["text"].startswith("Register 5006-5007 (U32)") and p["page"] == 8 for p in ps)
    spec = [p["text"] for p in passages(docs[1]) if p["kind"] == "spec"]
    assert "X1: Input (DC) > Max. PV input voltage: 1500 V" in spec


def test_long_tables_are_split_with_the_header_repeated(docs):
    ps = [p for p in passages(docs[2], max_chars=600) if p["kind"] == "table"]
    assert len(ps) > 3
    assert all(p["text"].startswith("| Parameter | Value |\n|---|---|") for p in ps)
    assert all(len(p["text"]) < 800 for p in ps)


def test_search_ranks_the_exact_value_first(docs):
    top = search(docs, "daily power yield register", top_k=3)[0]
    assert top["kind"] == "register" and "5003" in top["text"] and top["page"] == 8
    top = search(docs, "max PV input voltage", top_k=1)[0]
    assert top["kind"] == "spec" and top["file"] == "DS_X1_Datasheet.pdf"
    top = search(docs, "default baud rate", top_k=1)[0]
    assert "9600" in top["text"] and top["page"] == 6
    assert search(docs, "zzzz qqqq") == [] and search(docs, "   ") == []


def test_search_filters_and_file_name_counts(docs):
    assert {h["file"] for h in search(docs, "default", top_k=10, doc_types=["manual"])} == {"UM_Logger_Manual.pdf"}
    assert {h["file"] for h in search(docs, "default", top_k=10, files=["device_protocol_v1.pdf"])} == {"Device_Protocol_V1.pdf"}
    assert search(docs, "datasheet protection", top_k=1)[0]["file"] == "DS_X1_Datasheet.pdf"  # word from the file name
    idx = build_index(docs)
    assert idx.search("ip address", top_k=1)[0]["page"] == 3


# ------------------------------------------------------------------- ask
class FakeClient:
    def __init__(self, reply, in_tokens=900, out_tokens=60):
        self.reply, self.calls = reply, []
        self.in_tokens, self.out_tokens = in_tokens, out_tokens
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        block = SimpleNamespace(type="tool_use", input=self.reply)
        return SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=self.in_tokens, output_tokens=self.out_tokens))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DESIGN_INGEST_CACHE", str(tmp_path))
    monkeypatch.setenv("DESIGN_INGEST_LLM_LOG", str(tmp_path / "llm_log.jsonl"))
    monkeypatch.delenv("DESIGN_INGEST_ALLOW_EXTERNAL", raising=False)
    monkeypatch.delenv("DESIGN_INGEST_TOKEN_BUDGET", raising=False)
    return tmp_path


def test_ask_blocked_by_default_and_reads_nothing(docs, isolated):
    fake = FakeClient({})
    with pytest.raises(ExternalCallBlocked):
        ask("daily yield register?", docs, client=fake)
    assert fake.calls == [] and read_log() == []


def test_ask_sends_only_the_best_passages_and_checks_quotes(docs, isolated):
    reply = {"answer": "Register 5003, unit kWh, scale 0.1.", "found": True, "citations": [
        {"n": 1, "quote": "Register 5003 (U16): Daily power yields, scale 0.1, unit kWh"},   # verbatim
        {"n": 1, "quote": "scale  0.1,   unit KWH"},                                           # whitespace / case only
        {"n": 1, "quote": "the yield is stored in register 4999"},                            # invented
        {"n": 99, "quote": "a passage that was never sent"},                                  # unknown passage
    ]}
    fake = FakeClient(reply)
    a = ask("Which register holds the daily power yield?", docs, top_k=3, client=fake, allow_external=True)
    sent = fake.calls[0]["messages"][0]["content"]
    assert "Register 5003" in sent and "[1]" in sent and "[3]" in sent and "[4]" not in sent
    assert "row 150" not in sent  # the long table of the manual is not sent
    assert fake.calls[0]["tool_choice"] == {"type": "tool", "name": "reply"}
    assert a.found and a.verified and len(a.citations) == 3  # the unknown passage is dropped
    assert [c.verified for c in a.citations] == [True, True, False]
    assert a.citations[0].file == "Device_Protocol_V1.pdf" and a.citations[0].page == 8
    assert a.passages_sent[0]["file"] == "Device_Protocol_V1.pdf" and "text" not in a.passages_sent[0]
    # logged without any document text
    (rec,) = read_log()
    assert rec["purpose"] == "ask" and rec["input_tokens"] == 900 and rec["file"].startswith("Device_Protocol_V1.pdf")
    assert "5003" not in (isolated / "llm_log.jsonl").read_text(encoding="utf-8")


def test_ask_answer_without_verified_citation_is_flagged(docs, isolated):
    fake = FakeClient({"answer": "It is register 1234.", "found": True,
                       "citations": [{"n": 1, "quote": "register 1234 holds the yield"}]})
    a = ask("daily power yield register", docs, client=fake, allow_external=True)
    assert a.found and not a.verified and not a.citations[0].verified


def test_ask_not_found_and_no_hits(docs, isolated):
    fake = FakeClient({"answer": "The passages do not say.", "found": False, "citations": []})
    a = ask("what is the warranty period of the battery", docs, client=fake, allow_external=True)
    assert not a.found and not a.verified
    # nothing matches at all: no call is made and nothing is sent
    fake2 = FakeClient({})
    b = ask("qqqq zzzz", docs, client=fake2, allow_external=True)
    assert not b.found and fake2.calls == [] and "No passage" in b.answer


def test_cli_search_and_ask(docs, isolated, capsys, tmp_path, monkeypatch):
    from design_ingest import cli

    d = tmp_path / "docs"
    d.mkdir()
    (d / "m.pan").write_text("PVObject_=pvModule\n  Version=7.1\n  PVObject_Commercial=pvCommercial\n"
                             "    Manufacturer=TestSolar\n    Model=TS-1\n  End of PVObject pvCommercial\n"
                             "  Technol=mtSiMono\n  PNom=550.0\nEnd of PVObject pvModule\n", encoding="utf-8")
    assert cli.main(["search", str(d), "TestSolar manufacturer"]) == 0
    assert "m.pan" in capsys.readouterr().out
    assert cli.main(["ask", str(d), "who makes the module?"]) == 1  # blocked: no --allow-external
    assert "blocked by default" in capsys.readouterr().err


def test_ask_tolerates_missing_found_field_and_bad_answers(docs, isolated):
    # a model that leaves out `found` and `citations` for an unanswerable question
    a = ask("daily power yield register", docs, allow_external=True,
            client=FakeClient({"answer": "The passages do not say."}))
    assert a.found is False and a.citations == [] and not a.verified
    # `found` missing but a citation given: read as found
    b = ask("daily power yield register", docs, allow_external=True,
            client=FakeClient({"answer": "5003", "citations": [{"n": 1, "quote": "Register 5003"}]}))
    assert b.found is True and b.verified
    # unreadable answer: a clear error, not a traceback from pydantic
    with pytest.raises(RuntimeError, match="could not be read"):
        ask("daily power yield register", docs, allow_external=True,
            client=FakeClient({"answer": ["not", "a", "string"]}))
