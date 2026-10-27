from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import pytest
from docqa.docbase import DocBase
from docqa.agent import answer_question
from docqa.doc2md import parse_document


def test_real_http_model_protocols(tmp_path):
    calls = []
    class Mock(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if self.path == "/embeddings":
                response = {"data":[{"index":i,"embedding":[1.,0.,.5]} for i in range(len(body["input"]))]}
            elif self.path == "/rerank":
                response = {"results":[{"index":i,"relevance_score":.9-i*.1} for i in range(len(body["documents"]))]}
            else:
                response = {"choices":[{"message":{"content":"资料中的接口用于心跳启动。[1]" if self.path == "/chat" else "图片有一个白色方形。"}}]}
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1",0),Mock)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    cfg = {"data_dir":str(tmp_path/"data"), **{role:{"url":root+path,"model":"test"} for role,path in [("embedding","/embeddings"),("chat","/chat"),("vision","/vision"),("reranker","/rerank")]}}
    try:
        file = tmp_path/"doc.md"
        file.write_text("FE_HEART_Start 接口用于心跳启动。",encoding="utf-8")
        base = DocBase(cfg)
        base.build(file)
        answer = answer_question(base,"FE_HEART_Start")
        assert answer["mode"] == "model"
        assert set(answer["routes"]) == {"BM25","dense"}
        from PIL import Image
        image = tmp_path/"image.png"
        Image.new("RGB",(10,10),"white").save(image)
        manifest = parse_document(image,tmp_path/"converted",cfg)
        assert "白色方形" in manifest["sections"][0]["text"]
        vision = next(body for path,body in calls if path == "/vision")
        assert vision["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert {p for p,b in calls} == {"/embeddings","/rerank","/chat","/vision"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_watcher_indexes_and_stops(tmp_path):
    from docqa.service import Watcher
    folder = tmp_path/"watched"
    folder.mkdir()
    (folder/"watch.md").write_text("WATCH_TOKEN 监控测试",encoding="utf-8")
    base = DocBase({"data_dir":str(tmp_path/"data"),"watch_dir":str(folder)})
    watcher = Watcher(base)
    # A single scan exits immediately after the first file's build, without timing races.
    original = base.build
    def build(*args,**kwargs):
        result = original(*args,**kwargs)
        watcher.stop.set()
        return result
    base.build = build
    watcher.run()
    assert watcher.events[0]["status"] == "indexed"
    assert base.query("WATCH_TOKEN")["results"]
    base.cfg["watch_dir"] = str(base.root)
    with pytest.raises(ValueError,match="互相包含"):
        Watcher(base).run()
