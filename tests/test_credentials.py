import json
import os
import pytest
from docqa.credentials import read_secret, save_secret


def test_environment_key_has_precedence(monkeypatch):
    monkeypatch.setenv("DOCQA_DUMMY_KEY", "dummy-value")
    assert read_secret({"api_key_env":"DOCQA_DUMMY_KEY", "api_key_store":"unused"}) == "dummy-value"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_dpapi_round_trip_without_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    save_secret("test", "dummy-secret-not-a-real-key")
    encrypted = (tmp_path/"DocQA"/"credentials"/"test.dpapi").read_bytes()
    assert b"dummy-secret-not-a-real-key" not in encrypted
    assert read_secret({"api_key_store":"test"}) == "dummy-secret-not-a-real-key"
    assert read_secret({"api_key_store":"missing"}) == ""
    with pytest.raises(ValueError):
        save_secret("../bad", "dummy")


def test_request_options_and_credentials(monkeypatch):
    import docqa.models as models
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"choices":[]}'
    class Opener:
        def open(self, req, timeout):
            captured.update(body=json.loads(req.data), authorization=req.get_header("Authorization"))
            return Response()
    monkeypatch.setattr(models, "read_secret", lambda ep: "dummy-key")
    monkeypatch.setattr(models.urllib.request, "build_opener", lambda *args:Opener())
    cfg = {"chat":{"url":"http://localhost/chat", "model":"test", "request_options":{"max_tokens":64,"thinking":{"type":"disabled"}}}}
    models.post(cfg, "chat", {"messages":[]})
    assert captured["body"]["max_tokens"] == 64
    assert captured["body"]["thinking"]["type"] == "disabled"
    assert captured["authorization"] == "Bearer dummy-key"
