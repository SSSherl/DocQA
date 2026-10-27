"""Validate an extracted release, including credential UI and optional real providers.
Never log credentials. Real provider keys exist only in child environment variables.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import zipfile
from playwright.sync_api import sync_playwright


def verify(archive, live_config=None):
    report={'archive':str(Path(archive).resolve()),'checks':{},'real_models':[]}
    checks=report['checks']
    provider_calls=[]
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            provider_calls.append(body.get('model'))
            good=self.headers.get('Authorization')=='Bearer dummy-good-key'
            code=401 if not good else 404 if body.get('model')!='test-chat' else 200
            data={'choices':[{'message':{'content':'OK'}}]} if code==200 else {'error':{'message':'synthetic provider error'}}
            raw=json.dumps(data).encode()
            self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    model=ThreadingHTTPServer(('127.0.0.1',0),Model)
    threading.Thread(target=model.serve_forever,daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='docqa-release-acceptance-') as temp:
            root=Path(temp)
            with zipfile.ZipFile(archive) as zipped:
                assert zipped.testzip() is None
                for name in zipped.namelist():
                    target=(root/name).resolve()
                    assert target.is_relative_to(root.resolve()),'Unsafe archive path'
                    assert '/data/' not in name and not name.endswith('.dpapi') and name!='DocQA/config.json'
                zipped.extractall(root)
            app=root/'DocQA'
            checks['archive_integrity_and_private_file_exclusion']=True
            assert (app/'examples/项目说明.md').is_file()
            template=json.loads((app/'config.example.json').read_text(encoding='utf-8'))
            roles=('chat','vision','embedding','reranker','bge','visual_embedding')
            assert all(not template[r]['enabled'] and not template[r].get('api_key_store') and not template[r].get('api_key') for r in roles)
            cfg=copy.deepcopy(template)
            cfg['chat'].update(base_url='http://127.0.0.1:'+str(model.server_port),url='',model='test-chat',api_key_env='')
            (app/'config.json').write_text(json.dumps(cfg),encoding='utf-8')
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env=dict(os.environ,LOCALAPPDATA=str(root/'profile'),PYTHONIOENCODING='utf-8')
            proc=subprocess.Popen([sys.executable,'-m','docqa','serve','--port',str(port)],cwd=app,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            origin='http://127.0.0.1:'+str(port)
            def api(route,data=None):
                req=urllib.request.Request(origin+route,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=10) as response:return json.load(response)
            try:
                for _ in range(80):
                    try:api('/api/status');break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Release did not start')
                checks['extracted_app_startup']=True
                settings=api('/api/model-settings')
                assert not settings['allow_remote_models']
                assert all(not ep['has_key'] and not ep['enabled'] for ep in settings['models'].values())
                checks['fresh_release_all_six_channels_have_no_keys']=True
                with sync_playwright() as playwright:
                    executable=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
                    browser=playwright.chromium.launch(**({'executable_path':str(executable)} if executable.exists() else {}),headless=True)
                    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(origin,wait_until='networkidle')
                    page.locator('[data-tab=library]').click()
                    page.locator('#file').set_input_files(str(app/'examples/项目说明.md'))
                    page.locator('#upload').click()
                    page.wait_for_function("document.querySelector('#import-progress').textContent.includes('入库完成')")
                    assert page.locator('#file').input_value()==''
                    checks['sample_import_and_input_reset']=True
                    for question in ('FE_HEART_Start','DocQA 由哪些模块组成？','文档和索引保存在哪里？'):
                        answer=api('/api/ask',{'question':question})
                        assert answer['mode']=='extractive' and answer['citations']
                        assert any(c['name']=='项目说明.md' for c in answer['citations'])
                    assert not api('/api/query',{'question':'UNKNOWN_RELEASE_INTERFACE_999'})['results']
                    checks['offline_questions_citations_and_unknown_identifier']=True
                    page.locator('[data-tab=settings]').click()
                    page.wait_for_function("document.querySelectorAll('.model-config').length===6")
                    assert page.locator('.model-config [name=enabled]:checked').count()==6
                    form=page.locator('#config-chat')
                    card=page.locator('.model-option').filter(has=form)
                    card.locator(':scope > summary').click()
                    key=form.locator('[name=key]')
                    requests=[]
                    page.on('request',lambda r:requests.append(r.url) if '/api/model-settings/test' in r.url or '/api/model-settings/save' in r.url else None)
                    form.locator('[name=base_url]').fill('https://api.deepseek.com')
                    key.fill('dummy-bad-key');form.locator('button[type=submit]').click()
                    assert '允许远程模型调用' in form.locator('.model-feedback').inner_text()
                    assert key.input_value()=='dummy-bad-key' and not requests
                    checks['remote_consent_preflight_retains_key_without_requests']=True
                    form.locator('[name=base_url]').fill(cfg['chat']['base_url'])
                    form.locator('button[type=submit]').click()
                    page.wait_for_function("!document.querySelector('#config-chat button[type=submit]').disabled")
                    assert api('/api/model-settings/job')['status']=='failed'
                    assert key.input_value()=='dummy-bad-key' and not api('/api/model-settings')['models']['chat']['has_key']
                    assert '输入已保留' in form.locator('.model-feedback').inner_text()
                    assert not form.locator('.validation-check').is_visible()
                    checks['failed_save_retains_key_and_does_not_activate']=True
                    key.fill('dummy-good-key');form.locator('[name=model]').fill('wrong-model')
                    form.locator('.test-button').click()
                    page.wait_for_function("!document.querySelector('#config-chat button[type=submit]').disabled")
                    assert api('/api/model-settings/job')['status']=='failed' and key.input_value()=='dummy-good-key'
                    checks['wrong_model_retains_key']=True
                    form.locator('[name=model]').fill('test-chat')
                    form.locator('.test-button').click()
                    page.wait_for_function("!document.querySelector('#config-chat .validation-check').classList.contains('hidden')")
                    assert key.input_value()=='dummy-good-key' and not api('/api/model-settings')['models']['chat']['has_key']
                    assert '未保存' in form.locator('.model-feedback').inner_text()
                    checks['successful_test_retains_unsaved_key']=True
                    count=len(provider_calls)
                    form.locator('button[type=submit]').click()
                    page.wait_for_function("!document.querySelector('#config-chat button[type=submit]').disabled")
                    assert api('/api/model-settings')['models']['chat']['has_key']
                    assert key.input_value()=='' and key.get_attribute('placeholder')=='********'
                    assert '密钥已保存' in form.locator('.credential-status').inner_text()
                    assert form.locator('.validation-check').is_visible() and len(provider_calls)==count
                    assert form.locator('.model-actions').evaluate("e=>e.children[1].textContent==='验证并保存'&&e.children[2].classList.contains('validation-check')")
                    checks['successful_save_stars_status_cached_validation_and_tick_position']=True
                    raw=(app/'config.json').read_bytes()
                    assert b'dummy-good-key' not in raw
                    assert len(list((root/'profile/DocQA/credentials').glob('*.dpapi')))==1
                    page.reload(wait_until='networkidle');page.locator('[data-tab=settings]').click()
                    page.wait_for_function("document.querySelector('#config-chat')?.dataset.hasKey==='true'")
                    form=page.locator('#config-chat');card=page.locator('.model-option').filter(has=form)
                    card.locator(':scope > summary').click();key=form.locator('[name=key]')
                    assert key.input_value()=='' and key.get_attribute('placeholder')=='********'
                    form.locator('.key-toggle').click()
                    page.wait_for_function("document.querySelector('#config-chat [name=key]').type==='text'")
                    assert key.input_value()=='dummy-good-key'
                    form.locator('.key-toggle').click();assert key.input_value()==''
                    checks['reload_and_encrypted_credential_reveal']=True
                    key.fill('dummy-bad-key');form.locator('button[type=submit]').click()
                    page.wait_for_function("!document.querySelector('#config-chat button[type=submit]').disabled")
                    assert key.input_value()=='dummy-bad-key' and (app/'config.json').read_bytes()==raw
                    assert api('/api/model-settings')['models']['chat']['has_key']
                    checks['failed_replacement_preserves_previous_credential']=True
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert not errors,errors
                    checks['mobile_and_javascript']=True
                    browser.close()
            finally:
                proc.terminate();proc.wait(timeout=10)
            print('PASS: extracted-release browser and credential workflow.',flush=True)
            if live_config:
                # Read provided credentials from the caller profile, not the isolated test profile.
                from docqa.credentials import read_secret
                from urllib.parse import urlparse
                original=json.loads(Path(live_config).read_text(encoding='utf-8'))
                known={};by_origin={}
                def endpoint_origin(ep):return urlparse(ep.get('url') or ep.get('base_url') or '').netloc.lower()
                for role in roles:
                    ep=original.get(role,{})
                    try:value=read_secret(ep)
                    except Exception:value=''
                    if value:known[role]=value;by_origin[endpoint_origin(ep)]=value
                real=copy.deepcopy(template);real['allow_remote_models']=True;real['data_dir']=str(root/'real-data')
                real_env=dict(env)
                for role in roles:
                    ep=real[role];origin_key=endpoint_origin(ep)
                    value=known.get(role) if endpoint_origin(original.get(role,{}))==origin_key else None
                    value=value or by_origin.get(origin_key)
                    ep.pop('api_key_store',None)
                    if value:
                        variable='DOCQA_VERIFY_'+role.upper()+'_KEY';real_env[variable]=value;ep.update(enabled=True,api_key_env=variable)
                    else:ep.update(enabled=False,api_key_env='')
                path=root/'real-config.json';path.write_text(json.dumps(real),encoding='utf-8')
                def cli(*args):
                    command=[sys.executable,'-m','docqa','--config',str(path),*args]
                    done=subprocess.run(command,cwd=app,env=real_env,capture_output=True,timeout=180)
                    assert done.returncode==0,'Real provider check failed (details withheld to avoid credential logging)'
                    return json.loads(done.stdout.decode('utf-8'))
                real_report=cli('doctor')
                for item in real_report['models']:
                    if item['status'] in {'timeout','unreachable','service_unavailable'}:
                        first=item['status'];item=cli('doctor','--role',item['role'])['models'][0];item['first_attempt']=first
                    report['real_models'].append({k:v for k,v in item.items() if k in {'role','status','dimension','first_attempt'}})
                    print('Real model: '+item['role']+' '+item['status'],flush=True)
                tested=[m for m in report['real_models'] if m['status']!='disabled']
                assert tested and all(m['status']=='available' for m in tested),'One or more real models failed; inspect the sanitized report'
                checks['real_provider_connections']=True
                if real['chat']['enabled']:
                    built=cli('build',str(app/'examples/项目说明.md'))
                    for question in ('DocQA 由哪些模块组成？','文档和索引保存在哪里？'):
                        answer=cli('ask',question)
                        assert answer['mode']=='model' and answer['citations'],'Real cited answer check failed'
                    checks['real_sample_import_and_two_cited_answers']=True
                    print('PASS: real sample import and two cited answers.',flush=True)
            else:
                report['real_model_test_note']='Not run: no explicit private credential configuration provided. Browser tests used a local mock provider.'
    finally:
        model.shutdown();model.server_close()
    return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--archive',required=True);parser.add_argument('--live-config');parser.add_argument('--report',required=True)
    args=parser.parse_args()
    try:
        report=verify(args.archive,args.live_config)
        report['passed']=True
    except Exception as exc:
        report={'passed':False,'error':type(exc).__name__+': '+str(exc)}
        Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        raise
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: release acceptance report written.',flush=True)

if __name__=='__main__':main()
