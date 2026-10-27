import copy
import threading
import time
from docqa.diagnostics import diagnose


def test_minimal_probe_limits_and_concurrent_role_checks(monkeypatch):
    cfg={'chat':{'enabled':True,'url':'https://api.deepseek.com/chat/completions','model':'flash','timeout':120,'request_options':{'max_tokens':4096}},'vision':{'enabled':True,'url':'https://api.deepseek.com/chat/completions','model':'flash','timeout':120},'reranker':{'enabled':True,'url':'http://localhost/rerank','model':'rank','timeout':20,'retries':1}}
    before=copy.deepcopy(cfg);active=0;maximum=0;lock=threading.Lock();observed=[]
    def work(config,role):
        nonlocal active,maximum
        with lock:active+=1;maximum=max(maximum,active);observed.append((role,config[role]))
        time.sleep(.06)
        with lock:active-=1
        return 'OK'
    monkeypatch.setattr('docqa.diagnostics.chat',lambda c,r,m:work(c,r))
    monkeypatch.setattr('docqa.diagnostics.rerank',lambda c,q,d:work(c,'reranker'))
    report=diagnose(cfg,['chat','vision','reranker'])
    assert all(m['status']=='available' for m in report['models'])
    assert maximum==3 and cfg==before
    for role,ep in observed:
        assert ep['timeout']<=15
        if role=='reranker':assert ep['retries']==0
        else:
            assert ep['request_options']['max_tokens']<=64
            assert ep['request_options']['thinking']=={'type':'disabled'}


def test_fast_visual_probe_keeps_nonconstant_vector_check(monkeypatch):
    cfg={'visual_embedding':{'enabled':True,'url':'http://localhost/embeddings','model':'visual'}}
    monkeypatch.setattr('docqa.diagnostics.visual_embeddings',lambda *a:[[1.,0.]])
    assert diagnose(cfg,['visual_embedding'])['models'][0]['status']=='invalid_response'
