import json
from types import SimpleNamespace

import pytest

from design_ingest import extract
from design_ingest.llm import BudgetExceeded, ExternalCallBlocked, read_log, summarize, tokens_today
from design_ingest.schemas import SCHEMAS, InverterDatasheet


class FakeClient:
    def __init__(self, payload, in_tokens=1000, out_tokens=200):
        self.payload, self.calls = payload, []
        self.in_tokens, self.out_tokens = in_tokens, out_tokens
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        block = SimpleNamespace(type="tool_use", input=self.payload)
        usage = SimpleNamespace(input_tokens=self.in_tokens, output_tokens=self.out_tokens)
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)


PAYLOAD = {"manufacturer": "TestInv", "model": "TI-320K", "ac_nominal_power_kw": 300, "n_mppt": 6}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Cache and LLM log in a temp folder; no permission and no budget from the environment."""
    monkeypatch.setenv("DESIGN_INGEST_CACHE", str(tmp_path))
    monkeypatch.setenv("DESIGN_INGEST_LLM_LOG", str(tmp_path / "llm_log.jsonl"))
    monkeypatch.delenv("DESIGN_INGEST_ALLOW_EXTERNAL", raising=False)
    monkeypatch.delenv("DESIGN_INGEST_TOKEN_BUDGET", raising=False)
    return tmp_path


def test_extract_with_fake_client(samples, isolated):
    fake = FakeClient(PAYLOAD)
    res = extract(samples["ond"], InverterDatasheet, client=fake, allow_external=True)
    assert res.ac_nominal_power_kw == 300 and res.n_mppt == 6
    call = fake.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record"}
    assert "TI-320K" in call["messages"][0]["content"][0]["text"]
    # second call is served from cache: nothing is sent, so nothing needs permission
    again = extract(samples["ond"], InverterDatasheet, client=fake)
    assert again == res and len(fake.calls) == 1


def test_default_model_is_current():
    from design_ingest.extract import DEFAULT_MODEL

    assert DEFAULT_MODEL == "claude-haiku-5-5"


def test_external_call_blocked_by_default(samples, isolated):
    fake = FakeClient(PAYLOAD)
    with pytest.raises(ExternalCallBlocked, match="blocked by default"):
        extract(samples["ond"], InverterDatasheet, client=fake)
    assert fake.calls == [] and read_log() == []  # nothing sent, nothing logged


def test_permission_from_environment(samples, isolated, monkeypatch):
    monkeypatch.setenv("DESIGN_INGEST_ALLOW_EXTERNAL", "1")
    fake = FakeClient(PAYLOAD)
    assert extract(samples["ond"], InverterDatasheet, client=fake).n_mppt == 6
    # an explicit False wins over the environment
    with pytest.raises(ExternalCallBlocked):
        extract(samples["pan"], InverterDatasheet, client=fake, allow_external=False)


def test_call_is_logged_without_content(samples, isolated):
    extract(samples["ond"], InverterDatasheet, client=FakeClient(PAYLOAD, 1234, 56), allow_external=True)
    (rec,) = read_log()
    assert rec["purpose"] == "extract:InverterDatasheet" and rec["model"] == "claude-haiku-5-5"
    assert rec["file"] == "inverter.ond" and len(rec["sha256"]) == 12
    assert rec["input_tokens"] == 1234 and rec["output_tokens"] == 56 and rec["sent"]["text"] > 100
    raw = (isolated / "llm_log.jsonl").read_text(encoding="utf-8")
    assert "TI-320K" not in raw and "TestInv" not in raw  # the document text is never written to the log
    s = summarize()
    assert s["calls"] == 1 and s["input_tokens"] == 1234 and s["files"] == ["inverter.ond"]
    assert tokens_today() == 1290


def test_daily_token_budget(samples, isolated, monkeypatch):
    monkeypatch.setenv("DESIGN_INGEST_TOKEN_BUDGET", "10000")
    fake = FakeClient(PAYLOAD, 5000, 4000)
    extract(samples["ond"], InverterDatasheet, client=fake, allow_external=True, max_tokens=500)
    # 9000 used today; the next call needs at least its max_tokens more
    with pytest.raises(BudgetExceeded, match="9000 used today"):
        extract(samples["pan"], InverterDatasheet, client=fake, allow_external=True, max_tokens=2000)
    assert len(fake.calls) == 1


def test_cli_blocks_without_flag_and_shows_log(samples, isolated, capsys):
    from design_ingest.cli import main

    assert main(["extract", str(samples["ond"]), "--schema", "inverter"]) == 1
    assert "blocked by default" in capsys.readouterr().err
    assert main(["llm-log"]) == 0
    assert "0 calls" in capsys.readouterr().out


def test_schemas_are_valid_json_schema():
    for s in SCHEMAS.values():
        js = s.model_json_schema()
        assert js["type"] == "object"
