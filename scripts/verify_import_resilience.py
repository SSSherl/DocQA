"""Extracted-release regression: 147-page PDF, unresponsive proxy, truncated/slow models and restart/retry."""
import argparse
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error
import zipfile
import pymupdf


def verify(archive):
    checks={};gate=threading.Event();entered=threading.Event()
    mode={'unauthorized':False,'truncated':False,'calls':0}
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            mode['calls']+=1;entered.set();gate.wait(20)
            if mode['unauthorized']:
                self.send_response(401);self.send_header('Content-Length','0');self.end_headers();return
            raw=json.dumps({'data':[{'index':i,'embedding':[1.,0.]}for i in range(len(payload['input']))]}).encode()
            truncated=not mode['truncated'];mode['truncated']=True
            self.send_response(200);self.send_header('Content-Length',str(len(raw)+100 if truncated else len(raw)));self.end_headers()
            self.wfile.write(raw[:10] if truncated else raw);self.close_connection=True
    model=ThreadingHTTPServer(('127.0.0.1',0),Model)
    threading.Thread(target=model.serve_forever,daemon=True).start()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with tempfile.TemporaryDirectory(prefix='docqa-import-resilience-')as temp:
            root=Path(temp)
            with zipfile.ZipFile(archive)as z:z.extractall(root)
            app=root/'DocQA';data=app/'data'
            cfg=json.loads((app/'config.example.json').read_text(encoding='utf-8'))
            cfg['embedding'].update(enabled=True,base_url=f'http://127.0.0.1:{model.server_port}',model='local-test',dimensions=2,timeout=5)
            cfg['ingestion']={'batch_size':8,'workers':2,'retries':1,'retry_backoff':.05}
            (app/'config.json').write_text(json.dumps(cfg),encoding='utf-8')
            env=dict(os.environ,HTTP_PROXY='http://127.0.0.1:1',HTTPS_PROXY='http://127.0.0.1:1',ALL_PROXY='http://127.0.0.1:1',NO_PROXY='',LOCALAPPDATA=str(root/'profile'))
            with socket.socket()as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            origin=f'http://127.0.0.1:{port}'
            def api(path,body=None,timeout=2):
                request=urllib.request.Request(origin+path,data=json.dumps(body).encode()if body is not None else None,headers={'Content-Type':'application/json'})
                try:
                    with opener.open(request,timeout=timeout)as r:
                        raw=r.read();return raw if path=='/' else json.loads(raw)
                except urllib.error.HTTPError as exc:
                    raise AssertionError(path + ': ' + exc.read().decode('utf-8')) from None
            def start():
                proc=subprocess.Popen([sys.executable,'-m','docqa','serve','--port',str(port)],cwd=app,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                for _ in range(100):
                    try:api('/api/health');return proc
                    except OSError:time.sleep(.1)
                proc.terminate();proc.wait();raise AssertionError('App failed to start')
            def wait_job():
                for _ in range(300):
                    job=api('/api/import/status')
                    if job['status']!='running':return job
                    time.sleep(.1)
                raise AssertionError('Import did not finish')
            pdf=root/'large.pdf'
            with pymupdf.open()as doc:
                for n in range(147):
                    page=doc.new_page(width=250,height=210)
                    page.insert_text((20,30),f'Page {n+1}: FE_HEART_Start evidence and pipeline notes.')
                    if n%9==0:page.draw_rect(pymupdf.Rect(20,55,130,100))
                doc.save(pdf)
            raw=pdf.read_bytes();proc=start()
            try:
                assert api('/api/upload',{'name':pdf.name,'content':base64.b64encode(raw).decode(),'background':True})['status']=='running'
                assert entered.wait(15),'Model call not reached'
                times=[]
                for _ in range(4):
                    for path in ('/','/api/status','/api/health','/api/import/status'):
                        before=time.monotonic();api(path);times.append(time.monotonic()-before)
                assert max(times)<2
                checks['web_responsive_during_slow_import']={'requests':len(times),'max_seconds':round(max(times),3)}
                proc.terminate();proc.wait(timeout=10);proc=start()
                assert api('/api/import/status')['status']=='running'
                checks['restart_preserves_running_worker_and_progress']=True
                gate.set();job=wait_job()
                assert job['status']=='complete',job
                assert mode['truncated'] and api('/api/status')['documents']
                checks['147_page_pdf_with_17_diagrams_imported']=True
                checks['truncated_response_retried_under_broken_proxy_environment']=True
                assert (data/'uploads'/pdf.name).read_bytes()==raw
                with pymupdf.open(stream=raw,filetype='pdf')as changed_doc:
                    changed_doc[0].insert_text((20,160),'Updated evidence for failure and retry test.')
                    changed=changed_doc.tobytes()
                mode['unauthorized']=True
                assert api('/api/upload',{'name':pdf.name,'content':base64.b64encode(changed).decode(),'background':True})['status']=='running'
                job=wait_job();assert job['status']=='failed' and job['source_retained'],job
                assert (data/'uploads'/pdf.name).read_bytes()==raw
                assert len(api('/api/status')['documents'])==1
                checks['failed_update_retains_old_source_index_and_incoming_file']=True
                proc.terminate();proc.wait(timeout=10);proc=start()
                assert api('/api/import/status')['status']=='failed'
                mode['unauthorized']=False
                assert api('/api/import/retry',{})['status']=='running'
                job=wait_job();assert job['status']=='complete',job
                assert (data/'uploads'/pdf.name).read_bytes()==changed
                checks['retry_after_restart_without_reupload']=True
                query=api('/api/query',{'question':'FE_HEART_Start'})
                assert query['results'] and all(item['page']for item in query['results'])
                checks['real_pdf_page_citations']=True
            finally:
                gate.set()
                # Workers intentionally survive the web service. Wait before deleting their test directory.
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    pending=[]
                    for statefile in data.glob('import-jobs/*/state.json'):
                        from docqa.import_jobs import alive, read_json
                        state=read_json(statefile)
                        if alive(state.get('pid')):pending.append(statefile)
                    if not pending:break
                    time.sleep(.1)
                proc.terminate();proc.wait(timeout=10)
        return {'passed':True,'checks':checks,'real_cloud_api_used':False}
    finally:
        gate.set();model.shutdown();model.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--archive',required=True);parser.add_argument('--report',required=True);args=parser.parse_args()
    try:report=verify(args.archive)
    except Exception as exc:
        Path(args.report).write_text(json.dumps({'passed':False,'error':type(exc).__name__+': '+str(exc)},ensure_ascii=False,indent=2),encoding='utf-8');raise
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: extracted-release 147-page import, proxy bypass, responsive web, restart and retry.',flush=True)
