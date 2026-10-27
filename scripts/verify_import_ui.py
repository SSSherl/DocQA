"""Local import regression: progress, reload, atomic failure and cached retry. No cloud API."""
from contextlib import closing
import base64
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pymupdf
from playwright.sync_api import sync_playwright


def main():
    gate=threading.Event(); state={'vision':0,'embedding':0,'fail':True}
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/chat/completions':
                state['vision']+=1
                time.sleep(.15)
                body={'choices':[{'message':{'content':json.dumps({'description':'Local diagram evidence','nodes':[],'edges':[]})}}]}
                code=200
            else:
                state['embedding']+=1
                gate.wait(20)
                code=401 if state['fail'] else 200
                body={'error':{'message':'synthetic failure'}} if state['fail'] else {'data':[{'index':i,'embedding':[1.,0.]} for i in range(len(payload['input']))]}
            raw=json.dumps(body).encode()
            self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    model=ThreadingHTTPServer(('127.0.0.1',0),Model)
    threading.Thread(target=model.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='docqa-import-ui-') as temp:
        root=Path(temp)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        address='http://127.0.0.1:'+str(model.server_port)
        config={'data_dir':str(root/'data'),'vision':{'enabled':True,'base_url':address,'model':'local-vision'},'embedding':{'enabled':True,'base_url':address,'model':'local-embedding','dimensions':2}}
        path=root/'config.json';path.write_text(json.dumps(config),encoding='utf-8')
        pdfpath=root/'diagrams.pdf'
        with pymupdf.open() as pdf:
            for i in range(2):
                pg=pdf.new_page();pg.insert_text((40,40),'Diagram '+str(i));pg.draw_rect(pymupdf.Rect(40+i*10,80,180,180))
            pdf.save(pdfpath)
        proc=subprocess.Popen([sys.executable,'-m','docqa','--config',str(path),'serve','--port',str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                page=browser.new_page(viewport={'width':1400,'height':1000});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                for _ in range(80):
                    try:page.goto('http://127.0.0.1:'+str(port),wait_until='networkidle');break
                    except Exception:time.sleep(.1)
                page.locator('[data-tab=library]').click()
                page.locator('#file').set_input_files(str(pdfpath));page.locator('#upload').click()
                page.wait_for_function("document.querySelector('#import-progress').textContent.includes('Qwen 文本向量')")
                assert page.locator('#upload').is_disabled()
                assert state['vision']==2
                page.reload(wait_until='networkidle');page.locator('[data-tab=library]').click()
                page.wait_for_function("document.querySelector('#import-progress').textContent.includes('Qwen 文本向量')")
                assert page.locator('#upload').is_disabled()
                gate.set()
                page.wait_for_function("document.querySelector('#import-progress').textContent.includes('旧索引已保留')")
                with closing(sqlite3.connect(root/'data/docqa.sqlite3')) as db:assert db.execute('select count(*) from documents').fetchone()[0]==0
                assert not page.locator('#upload').is_disabled()
                state['fail']=False
                assert page.locator('#retry-import').is_visible()
                page.locator('#retry-import').click()
                page.wait_for_function("document.querySelector('#import-progress').textContent.includes('入库完成')")
                assert page.locator('#file').input_value()=='' and page.locator('#upload-category').input_value()==''
                page.locator('#file').set_input_files(str(pdfpath));page.locator('#upload-category').fill('Test category');page.locator('#upload').click()
                page.wait_for_function("document.querySelector('#import-progress').textContent.includes('文档未变化')")
                assert page.locator('#file').input_value()=='' and page.locator('#upload-category').input_value()==''
                # Failed submissions retain the selection and category for retry.
                page.route('**/api/upload',lambda route:route.fulfill(status=400,json={'error':'Synthetic submission failure'}))
                page.locator('#file').set_input_files(str(pdfpath));page.locator('#upload-category').fill('Keep on failure');page.locator('#upload').click()
                page.wait_for_function("document.querySelector('#import-progress').textContent.includes('Synthetic submission failure')")
                assert page.locator('#file').input_value() and page.locator('#upload-category').input_value()=='Keep on failure'
                assert state['vision']==2, 'Retry repeated successful image analysis'
                with closing(sqlite3.connect(root/'data/docqa.sqlite3')) as db:
                    images=db.execute("select page,image from chunks where image<>'' order by page").fetchall()
                assert [x[0] for x in images]==[1,2]
                assert all((root/'data'/x[1]).is_file() for x in images)
                assert not errors,errors
                browser.close()
                print('PASS: background import, phase progress, reload recovery, failed index isolation, retry with zero repeated vision calls, page/image citations.')
        finally:
            gate.set();proc.terminate();proc.wait(timeout=10)
    model.shutdown();model.server_close()

if __name__=='__main__':main()
