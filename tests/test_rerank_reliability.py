import urllib.error
import pytest
from docqa.models import rerank, post, ModelError
from docqa.docbase import DocBase
from docqa.agent import answer_question


@pytest.mark.parametrize("code", ["timeout", "unreachable", "service_unavailable"])
def test_transient_rerank_recovers_once(monkeypatch, code):
    calls = []
    def request(*args):
        calls.append(args)
        if len(calls) == 1:
            raise ModelError(code, "transient")
        return {"results": [{"index": 0, "score": .8}]}
    monkeypatch.setattr("docqa.models.post", request)
    monkeypatch.setattr("docqa.models.time.sleep", lambda _: None)
    assert rerank({}, "question", ["evidence"])[0]["relevance_score"] == .8
    assert len(calls) == 2


@pytest.mark.parametrize("code,count", [("timeout",2),("unauthorized",1),("rate_limited",1),("invalid_response",1)])
def test_retry_is_bounded_and_permanent_errors_are_not_retried(monkeypatch,code,count):
    calls=[]
    def request(*args):
        calls.append(1)
        raise ModelError(code,"NEVER_SHOW_PROVIDER_SECRET")
    monkeypatch.setattr("docqa.models.post",request)
    monkeypatch.setattr("docqa.models.time.sleep",lambda _:None)
    with pytest.raises(ModelError): rerank({},"q",["doc"])
    assert len(calls)==count


@pytest.mark.parametrize("wrapped", [True, False])
def test_transport_timeouts_are_distinct_from_connection_errors(monkeypatch,wrapped):
    class Opener:
        def open(self,*args,**kwargs):
            raise urllib.error.URLError(TimeoutError()) if wrapped else TimeoutError()
    monkeypatch.setattr("urllib.request.build_opener",lambda *a:Opener())
    with pytest.raises(ModelError) as error:
        post({"reranker":{"url":"http://localhost/rerank"}},"reranker",{})
    assert error.value.status=="timeout"


def test_fallback_reports_reason_and_keeps_rrf_evidence(tmp_path,monkeypatch):
    base=DocBase({"data_dir":str(tmp_path/"data"),"reranker":{"url":"http://localhost/rerank"}})
    source=tmp_path/"note.md"
    source.write_text("technical evidence for ranking",encoding="utf8")
    base.build(source)
    def fail(*a): raise ModelError("timeout","NEVER_SHOW_PROVIDER_SECRET")
    monkeypatch.setattr("docqa.docbase.rerank",fail)
    result=base.query("evidence")
    assert result["results"] and not result["reranked"]
    assert result["reranker"]["error_code"]=="timeout"
    assert any("超时" in w for w in result["warnings"])
    assert all("SECRET" not in w for w in result["warnings"])
    answer=answer_question(base,"evidence")
    assert answer["citations"] and answer["reranker"]["status"]=="fallback"
