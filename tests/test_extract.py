from types import SimpleNamespace

from design_ingest import extract
from design_ingest.schemas import SCHEMAS, InverterDatasheet


class FakeClient:
    def __init__(self, payload):
        self.payload, self.calls = payload, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        block = SimpleNamespace(type="tool_use", input=self.payload)
        return SimpleNamespace(content=[block], stop_reason="tool_use")


def test_extract_with_fake_client(samples, tmp_path, monkeypatch):
    monkeypatch.setenv("DESIGN_INGEST_CACHE", str(tmp_path))
    fake = FakeClient({"manufacturer": "TestInv", "model": "TI-320K", "ac_nominal_power_kw": 300, "n_mppt": 6})
    res = extract(samples["ond"], InverterDatasheet, client=fake)
    assert res.ac_nominal_power_kw == 300 and res.n_mppt == 6
    call = fake.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record"}
    assert "TI-320K" in call["messages"][0]["content"][0]["text"]
    # second call is served from cache
    again = extract(samples["ond"], InverterDatasheet, client=fake)
    assert again == res and len(fake.calls) == 1


def test_schemas_are_valid_json_schema():
    for s in SCHEMAS.values():
        js = s.model_json_schema()
        assert js["type"] == "object"
