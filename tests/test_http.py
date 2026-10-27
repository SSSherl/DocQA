import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error


def test_http_import_ask_and_access_boundaries(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port = sock.getsockname()[1]
    cfg = tmp_path/"config.json"
    cfg.write_text(json.dumps({"data_dir":str(tmp_path/"data"),"chat":{"api_key_env":"DOCQA_TEST_REVEAL_KEY"}}),encoding="utf-8")
    proc = subprocess.Popen([sys.executable,"-m","docqa","--config",str(cfg),"serve","--port",str(port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={**os.environ,"DOCQA_TEST_REVEAL_KEY":"dummy-http-secret"})
    origin = f"http://127.0.0.1:{port}"
    def request(path,body=None,headers=None):
        hs = {"Content-Type":"application/json",**(headers or {})}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(origin+path,data=data,headers=hs)
        try:
            with urllib.request.urlopen(req,timeout=10) as r:
                return r.status,r.read()
        except urllib.error.HTTPError as exc:
            return exc.code,exc.read()
    try:
        for _ in range(80):
            try:
                if request("/api/status")[0] == 200:
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError("服务未启动")
        assert request("/")[0] == 200
        view = json.loads(request("/api/model-settings")[1])
        assert len(view["models"]) == 6
        assert view["models"]["chat"]["has_key"]
        assert all(not item["has_key"] for role,item in view["models"].items() if role!="chat")
        assert b"dummy-http-secret" not in request("/api/model-settings")[1]
        assert request("/api/model-settings/reveal")[0] == 404
        assert request("/api/model-settings/reveal", {"role":"chat"}, {"Origin":"https://untrusted.example"})[0] == 403
        assert request("/api/model-settings/reveal", {"role":"unknown"})[0] == 400
        assert request("/api/model-settings/reveal", {"role":[]})[0] == 400
        status,revealed=request("/api/model-settings/reveal", {"role":"chat"})
        assert status==200 and json.loads(revealed)["key"]=="dummy-http-secret"
        assert json.loads(request("/api/model-settings/reveal", {"role":"embedding"})[1])["key"]==""
        assert request("/api/model-settings/save", {"models":{"chat":{"enabled":False}}})[0] == 202
        for _ in range(80):
            job = json.loads(request("/api/model-settings/job")[1])
            if job["status"] != "running":
                break
            time.sleep(.05)
        assert job["status"] == "complete", job
        status, diagnostic = request("/api/diagnose", {})
        assert status == 200
        assert all(item["status"] == "disabled" for item in json.loads(diagnostic)["models"])
        raw = base64.b64encode("# 本地架构\nFE_HEART_Start 是心跳接口。".encode()).decode()
        status,body = request("/api/upload",{"name":"示例.md","content":raw,"category":"技术"})
        assert status == 200, body
        doc_id = json.loads(body)["id"]
        assert request("/api/upload",{"name":"示例.md","content":raw,"category":"技术","background":True})[0] == 202
        for _ in range(80):
            status, raw_job = request("/api/import/status")
            assert status == 200, raw_job
            job=json.loads(raw_job)
            if job["status"]!="running":break
            time.sleep(.05)
        assert job["status"]=="complete" and job["result"]["status"]=="unchanged",job
        assert request("/api/upload",{"name":"示例.md","content":base64.b64encode(b"\xff").decode(),"background":True})[0] == 202
        for _ in range(80):
            status, raw_job = request("/api/import/status")
            assert status == 200, raw_job
            job=json.loads(raw_job)
            if job["status"]!="running":break
            time.sleep(.05)
        assert job["status"]=="failed" and "旧索引已保留" in job["error"]
        assert request("/api/source/"+doc_id)[1]==base64.b64decode(raw)
        status,body = request("/api/benchmark",{"cases":[{"question":"FE_HEART_Start","relevant_ids":[doc_id]}],"top_k":1})
        comparison = json.loads(body)
        assert status == 200 and comparison["modes"]["bm25"]["recall"] == 1
        assert comparison["modes"]["hybrid"]["status"] == "not_configured"
        assert request("/api/retrieval/upgrade")[0] == 200
        assert request("/api/retrieval/setup",{"embedding":{}})[0] == 202
        for _ in range(40):
            job = json.loads(request("/api/retrieval/upgrade")[1])
            if job["status"] != "running":
                break
            time.sleep(.05)
        assert job["status"] == "failed"
        status,body = request("/api/ask",{"question":"FE_HEART_Start"})
        result = json.loads(body)
        assert status == 200 and result["mode"] == "extractive"
        assert "answer_html" in result and "<p>" in result["answer_html"]
        assert "excerpt_html" in result["citations"][0]
        assert "<h1>" in result["citations"][0]["excerpt_html"]
        assert result["citations"][0]["doc_id"] == doc_id
        assert request("/api/source/"+doc_id)[0] == 200
        assert request("/api/upload",{"name":"../escape.md","content":raw})[0] == 400
        assert request("/api/assets/../../config.json")[0] == 403
        assert request("/api/status",headers={"Origin":"https://evil.example"})[0] == 403
        assert request("/api/status",headers={"Host":"evil.example"})[0] == 403
        assert request("/api/delete",{"id":doc_id})[0] == 200
        assert request("/api/source/"+doc_id)[0] == 404
    finally:
        proc.terminate()
        proc.communicate(timeout=10)
