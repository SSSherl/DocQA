import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import pytest
from docqa.models import endpoint_url, configured, config, rerank, post, ModelError
from docqa.diagnostics import diagnose
from docqa.diagrams import graph_description
from docqa.docbase import DocBase
from docqa.agent import answer_question


def test_base_url_enabled_and_nested_config(tmp_path):
    cfg = {"chat":{"base_url":"https://ai.gitee.com/v1/"},"embedding":{"base_url":"https://ai.gitee.com/v1"}}
    assert endpoint_url(cfg,"chat") == "https://ai.gitee.com/v1/chat/completions"
    assert endpoint_url(cfg,"embedding") == "https://ai.gitee.com/v1/embeddings"
    cfg["chat"]["enabled"] = False
    assert not configured(cfg,"chat")
    cfg["embedding"]["url"] = "http://localhost:9999/custom"
    assert endpoint_url(cfg,"embedding") == "http://localhost:9999/custom"
    path = tmp_path/"config.json"
    path.write_text(json.dumps({"chat":{"enabled":False}}),encoding="utf-8")
    assert config(path)["chat"]["api_key_env"] == ""
    assert config(path)["chat"]["timeout"] == 120


@pytest.mark.parametrize("response",[
    [{"index":0,"score":.9},{"index":1,"score":.3}],
    {"data":[{"index":0,"score":.9},{"index":1,"score":.3}]},
    {"results":[{"index":0,"relevance_score":.9},{"index":1,"relevance_score":.3}]}
])
def test_reranker_response_adapters(monkeypatch,response):
    monkeypatch.setattr("docqa.models.post",lambda *args:response)
    assert rerank({},"question",["a","b"])[0] == {"index":0,"relevance_score":.9}


def test_invalid_reranker_is_rejected(monkeypatch):
    monkeypatch.setattr("docqa.models.post",lambda *args:[{"index":0,"score":float('nan')}])
    with pytest.raises(ModelError):
        rerank({},"q",["a"])


@pytest.mark.parametrize("code,status",[(401,"unauthorized"),(403,"forbidden"),(429,"rate_limited")])
def test_doctor_error_classification_and_redaction(code,status):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'NEVER_ECHO_PROVIDER_CREDENTIAL')
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    t = threading.Thread(target=server.serve_forever,daemon=True)
    t.start()
    try:
        cfg = {"chat":{"base_url":f"http://127.0.0.1:{server.server_port}"}}
        report = diagnose(cfg,["chat"])
        assert report["models"][0]["status"] == status
        assert "NEVER_ECHO" not in json.dumps(report)
    finally:
        server.shutdown()
        server.server_close()
        t.join(5)


def test_missing_owned_key_does_not_call_remote(monkeypatch):
    monkeypatch.delenv("DOCQA_TEST_ABSENT_KEY",raising=False)
    def forbidden(*args,**kwargs):
        raise AssertionError("不应该发送网络请求")
    monkeypatch.setattr("urllib.request.build_opener",forbidden)
    cfg = {"allow_remote_models":True,"chat":{"base_url":"https://ai.gitee.com/v1","api_key_env":"DOCQA_TEST_ABSENT_KEY"}}
    assert diagnose(cfg,["chat"])["models"][0]["status"] == "missing_key"


def test_graph_json_mermaid_and_structural_validation():
    graph = {"description":"流程描述", "nodes":[{"id":"A","label":"开始"},{"id":"B","label":"结束"}],"edges":[{"source":"A","target":"B","label":"成功"}]}
    text, result = graph_description(json.dumps(graph))
    assert 'n0 -->|"成功"| n1' in text
    assert result["structure_checked"] and not result["human_verified"]
    graph["edges"][0]["target"] = "UNKNOWN"
    with pytest.raises(ValueError,match="不存在"):
        graph_description(json.dumps(graph))
    graph["edges"] = []
    graph["nodes"][1]["id"] = "A"
    with pytest.raises(ValueError,match="重复"):
        graph_description(json.dumps(graph))
    assert graph_description("普通图片文字")[1] is None


def test_failed_chat_and_reranker_fall_back_to_evidence(tmp_path,monkeypatch):
    cfg = {"data_dir":str(tmp_path/"data"),"chat":{"url":"http://localhost/chat"},"reranker":{"url":"http://localhost/rerank"}}
    base = DocBase(cfg)
    file = tmp_path/"evidence.md"
    file.write_text("FE_HEART_Start 是心跳接口。",encoding="utf-8")
    base.build(file)
    def fail(*args,**kwargs):
        raise ModelError("unauthorized","模拟401")
    monkeypatch.setattr("docqa.docbase.rerank",fail)
    monkeypatch.setattr("docqa.agent.chat",fail)
    report = answer_question(base,"FE_HEART_Start")
    assert report["mode"] == "extractive"
    assert report["citations"]
    assert any("Reranker" in w for w in report["warnings"])
    assert any("Chat" in w for w in report["warnings"])


def test_fingerprint_ignores_timeout_and_secret_variable_changes(tmp_path):
    cfg = {"data_dir":str(tmp_path/"data"),"embedding":{"url":"http://localhost/embed","model":"test"}}
    base = DocBase(cfg)
    before = base.fingerprint()
    cfg["embedding"].update(timeout=5,api_key_env="NEW_KEY")
    assert base.fingerprint() == before
