import http.client
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import pytest
from docqa.models import post, ModelError


def test_model_calls_ignore_environment_and_system_proxy(monkeypatch):
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            raw=b'{"ok": true}'
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Model)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    original=socket.getaddrinfo
    def resolve(host,*args,**kwargs):
        return original('127.0.0.1' if host=='model.invalid' else host,*args,**kwargs)
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    monkeypatch.setattr(urllib.request,'getproxies',lambda:{'http':'http://127.0.0.1:1'})
    monkeypatch.setattr(urllib.request,'proxy_bypass',lambda host:False)
    monkeypatch.setenv('DOCQA_TEST_DIRECT_KEY','dummy-key')
    cfg={'allow_remote_models':True,'chat':{'url':f'http://model.invalid:{server.server_port}/chat','api_key_env':'DOCQA_TEST_DIRECT_KEY','timeout':2}}
    try:
        assert post(cfg,'chat',{})=={'ok':True}
    finally:
        server.shutdown();server.server_close()


@pytest.mark.parametrize('error',[http.client.IncompleteRead(b'partial',10),http.client.RemoteDisconnected('closed')])
def test_incomplete_response_is_sanitized_transient_error(monkeypatch,error):
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):raise error
    class Opener:
        def open(self,*args,**kwargs):return Response()
    monkeypatch.setattr(urllib.request,'build_opener',lambda *args:Opener())
    with pytest.raises(ModelError)as caught:
        post({'embedding':{'url':'http://localhost/embeddings'}},'embedding',{})
    assert caught.value.status=='unreachable' and 'partial' not in str(caught.value)


def test_ingest_options_backoff_and_worker_limit(tmp_path,monkeypatch):
    from docqa.ingestion import text_vectors
    sleeps=[];calls=[]
    monkeypatch.setattr('docqa.ingestion.time.sleep',sleeps.append)
    cfg={'data_dir':str(tmp_path),'ingestion':{'batch_size':2,'workers':1,'retries':2,'retry_backoff':.25}}
    def embed(cfg,texts):
        calls.append(list(texts))
        if len(calls)<3:raise ModelError('unreachable','truncated')
        return [[float(text),1.]for text in texts]
    assert text_vectors(cfg,'embedding',['1','2','3'],embed)==[[1.,1.],[2.,1.],[3.,1.]]
    assert sleeps==[.25,.5] and len(calls)==4


def test_atomic_job_state_read_retries_windows_sharing_violation(tmp_path,monkeypatch):
    from pathlib import Path
    from docqa.import_jobs import read_json
    path=tmp_path/'state.json'
    path.write_text('{"status":"running"}',encoding='utf-8')
    original=Path.read_text
    attempts=[];sleeps=[]
    def read(self,*args,**kwargs):
        attempts.append(1)
        if len(attempts)<3:raise PermissionError('Synthetic Windows sharing violation')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    monkeypatch.setattr('docqa.import_jobs.time.sleep',sleeps.append)
    assert read_json(path)=={'status':'running'}
    assert len(attempts)==3 and sleeps==[.02,.02]
