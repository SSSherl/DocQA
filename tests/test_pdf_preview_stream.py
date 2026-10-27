import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pymupdf
import pytest
from docqa.docbase import DocBase
from docqa.pdf_view import locate, render_page
from docqa.streaming import stream_chat, CancelToken, GenerationCancelled


@pytest.mark.parametrize("rotation",[0,90])
def test_pdf_verbatim_highlight_page_bounds_and_changed_original(tmp_path,rotation):
    path=tmp_path/"example.pdf"
    pdf=pymupdf.open()
    page=pdf.new_page()
    page.insert_text((70,100),"Document evidence about the client training workflow.")
    page.set_rotation(rotation)
    pdf.new_page().insert_text((70,100),"Another source page with different text.")
    pdf.save(path)
    pdf.close()
    base=DocBase({"data_dir":str(tmp_path/"data")})
    doc=base.build(path)
    with base.connect() as db:
        chunk=db.execute("SELECT id FROM chunks WHERE page=1 AND image='' LIMIT 1").fetchone()[0]
    result=locate(base,doc["id"],1,chunk)
    assert result["pages"]==2 and result["highlights"]
    assert all(0<=value<=1 for rect in result["highlights"] for value in rect)
    assert render_page(base,doc["id"],1).startswith(b"\x89PNG")
    assert not locate(base,doc["id"],2,chunk)["highlights"]
    with pytest.raises(ValueError): locate(base,doc["id"],3,chunk)
    with pytest.raises(ValueError): locate(base,doc["id"],1,"wrong-chunk")
    with base.connect() as db:
        db.execute("UPDATE chunks SET image='parsed/diagram.png' WHERE id=?",(chunk,))
    assert not locate(base,doc["id"],1,chunk)["highlights"]
    path.write_bytes(path.read_bytes()+b"\nchanged")
    with pytest.raises(ValueError,match="变化"): locate(base,doc["id"],1,chunk)


def test_sse_emits_real_deltas_and_cancel_interrupts_waiting_socket():
    release=threading.Event()
    first=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["stream"] is True
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"hello "}}]}\n\n')
                self.wfile.flush()
                release.wait(5)
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"world"}}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
            except OSError:
                pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    cfg={"chat":{"url":f"http://127.0.0.1:{server.server_port}/chat","model":"test","timeout":10}}
    errors=[]
    cancel=CancelToken()
    def delta(text):
        assert text=="hello "
        first.set()
    def run():
        try: stream_chat(cfg,[],delta,cancel)
        except Exception as e: errors.append(e)
    worker=threading.Thread(target=run,daemon=True)
    try:
        worker.start()
        assert first.wait(2)
        started=time.perf_counter()
        cancel.cancel()
        worker.join(1)
        assert not worker.is_alive()
        assert isinstance(errors[0],GenerationCancelled)
        assert time.perf_counter()-started<1
        release.set()
        parts=[]
        assert stream_chat(cfg,[],parts.append,CancelToken())=="hello world"
        assert parts==["hello ","world"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
