import json
import socket
import threading
import time
import urllib.request
import urllib.error
import pytest
from docqa.docbase import DocBase
from docqa.conversations import Conversations


def test_conversations_persist_raw_and_delete_only_history(tmp_path):
    store = Conversations(tmp_path)
    result = {"answer":"**answer**", "answer_html":"<unsafe>", "citations":[{"excerpt":"raw", "excerpt_html":"<unsafe>"}]}
    first = store.append(None,"first question",result)
    store.append(first["conversation_id"],"follow-up",result)
    loaded = Conversations(tmp_path).get(first["conversation_id"])
    assert len(loaded["turns"]) == 2
    assert "answer_html" not in loaded["turns"][0]["result"]
    assert "excerpt_html" not in loaded["turns"][0]["result"]["citations"][0]
    assert store.list()[0]["turns"] == 2
    store.delete(first["conversation_id"])
    assert store.list() == []
    with pytest.raises(ValueError): store.get(first["conversation_id"])


def test_http_stream_stages_history_context_and_restore(tmp_path,monkeypatch):
    import docqa.server as module
    from http.server import ThreadingHTTPServer
    base = DocBase({"data_dir":str(tmp_path/"data"),"chat":{"url":"http://localhost/chat"},"reranker":{"url":"http://localhost/rerank"}})
    source = tmp_path/"evidence.md"
    source.write_text("evidence technical document",encoding="utf8")
    base.build(source)
    observed = []
    def chat(cfg,role,messages):
        observed.append(json.loads(messages[-1]["content"]))
        return "**Evidence** [1]"
    monkeypatch.setattr("docqa.agent.chat",chat)
    def stream(cfg,messages,on_delta,cancel):
        text=chat(cfg,"chat",messages)
        on_delta(text)
        return text
    monkeypatch.setattr("docqa.agent.stream_chat",stream)
    monkeypatch.setattr("docqa.docbase.rerank",lambda cfg,q,docs:[{"index":i,"relevance_score":1.} for i in range(len(docs))])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1",0))
        port = probe.getsockname()[1]
    servers=[]
    def factory(*args):
        server = ThreadingHTTPServer(*args)
        servers.append(server)
        return server
    monkeypatch.setattr(module,"ThreadingHTTPServer",factory)
    worker = threading.Thread(target=module.serve,args=(base,port),daemon=True)
    worker.start()
    for _ in range(100):
        if servers: break
        time.sleep(.01)
    root=f"http://127.0.0.1:{port}"
    def request(path,payload=None):
        req=urllib.request.Request(root+path,data=json.dumps(payload).encode() if payload is not None else None,headers={"Content-Type":"application/json"})
        return urllib.request.urlopen(req,timeout=10)
    try:
        with request("/api/ask",{"question":"evidence","stream":True}) as response:
            assert "ndjson" in response.headers["Content-Type"]
            events=[json.loads(line) for line in response]
        assert [e["stage"] for e in events if e["type"]=="stage"]==["queued","retrieval","rerank","generation"]
        result=events[-1]["result"]
        assert result["mode"]=="model" and result["conversation_id"]
        with request("/api/ask",{"question":"继续解释它","conversation_id":result["conversation_id"]}) as response:
            followup=json.load(response)
        assert followup["mode"]=="model"
        assert observed[-1]["untrusted_history"][0]["question"]=="evidence"
        with request("/api/conversations/"+result["conversation_id"]) as response:
            saved=json.load(response)
        assert len(saved["turns"])==2
        assert "<strong>Evidence</strong>" in saved["turns"][0]["result"]["answer_html"]
        with request("/vendor/mermaid.min.js") as response:
            assert "javascript" in response.headers["Content-Type"]
        with request("/api/conversations/delete",{"id":result["conversation_id"]}) as response:
            assert json.load(response)["status"]=="deleted"
        assert len(base.documents())==1
    finally:
        servers[0].shutdown()
        servers[0].server_close()
        worker.join(5)
