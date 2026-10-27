import io
import json
import urllib.error
import pytest
from docqa.models import post, embeddings, ModelError


def test_gitee_account_resource_error_is_classified_without_echoing_secret(monkeypatch):
    class Opener:
        def open(self,request,timeout):
            assert request.get_header("X-failover-enabled") == "false"
            body = json.dumps({"error":{"message":"当前账户没有可用的计费资源。secret-should-not-echo"}}).encode()
            raise urllib.error.HTTPError(request.full_url,400,"bad request",{},io.BytesIO(body))
    monkeypatch.setattr("docqa.models.read_secret",lambda ep:"dummy")
    monkeypatch.setattr("docqa.models.urllib.request.build_opener",lambda *args:Opener())
    cfg = {"allow_remote_models":True,"embedding":{"url":"https://ai.gitee.com/v1/embeddings"}}
    with pytest.raises(ModelError) as error:
        post(cfg,"embedding",{"input":["test"]})
    assert error.value.status == "missing_resources"
    assert "secret-should-not-echo" not in str(error.value)


def test_embedding_contract_explicit_float_and_dimensions(monkeypatch):
    captured = {}
    def fake(cfg,role,payload):
        captured.update(payload)
        return {"data":[{"index":0,"embedding":[1.,0.]}]}
    monkeypatch.setattr("docqa.models.post",fake)
    assert embeddings({"embedding":{"dimensions":2}},["test"]) == [[1.,0.]]
    assert captured == {"input":["test"],"encoding_format":"float","dimensions":2}
