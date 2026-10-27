import json
import threading
import time
from pathlib import Path
import pytest
from docqa.docbase import DocBase
from docqa.ingestion import text_vectors
from docqa.models import ModelError


def cfg(tmp_path):
    return {'data_dir':str(tmp_path/'data'),'embedding':{'enabled':True,'url':'http://localhost/embeddings','model':'test','dimensions':2}}


def test_batches_order_concurrency_and_success_cache(tmp_path):
    config=cfg(tmp_path)
    counts={'calls':0,'active':0,'max':0}
    lock=threading.Lock()
    def embed(c,texts):
        with lock:
            counts['calls']+=1;counts['active']+=1
            counts['max']=max(counts['max'],counts['active'])
        assert len(texts)<=8
        time.sleep(.03)
        with lock: counts['active']-=1
        return [[float(t),1.] for t in texts]
    texts=[str(i) for i in range(25)]
    result=text_vectors(config,'embedding',texts,embed)
    assert result==[[float(i),1.] for i in range(25)]
    assert counts['calls']==4 and counts['max']==2
    assert text_vectors(config,'embedding',texts,embed)==result and counts['calls']==4
    config['embedding']['model']='changed'
    text_vectors(config,'embedding',texts,embed)
    assert counts['calls']==8


@pytest.mark.parametrize('status,expected',[('timeout',2),('service_unavailable',2),('unauthorized',1),('rate_limited',1)])
def test_retry_bounded_and_failed_results_not_cached(tmp_path,status,expected):
    config=cfg(tmp_path);calls=[]
    def fail(*args):
        calls.append(1)
        raise ModelError(status,'test failure')
    with pytest.raises(ModelError):text_vectors(config,'embedding',['a'],fail)
    assert len(calls)==expected
    result=text_vectors(config,'embedding',['a'],lambda *a:[[1.,0.]])
    assert result==[[1.,0.]]


def test_retry_reuses_vision_after_embedding_failure(tmp_path,monkeypatch):
    import pymupdf
    config=cfg(tmp_path)
    config['vision']={'enabled':True,'url':'http://localhost/vision','model':'vision'}
    base=DocBase(config)
    source=tmp_path/'diagram.pdf'
    with pymupdf.open() as pdf:
        for i in range(2):
            page=pdf.new_page()
            page.insert_text((40,40),'Diagram '+str(i+1))
            page.draw_rect(pymupdf.Rect(40+i*20,60,140,140))
        pdf.save(source)
    barrier=threading.Barrier(2);calls=[]
    def vision(*a):
        calls.append(1)
        barrier.wait(timeout=5)
        return json.dumps({'description':'Diagram evidence','nodes':[],'edges':[]})
    monkeypatch.setattr('docqa.doc2md.chat',vision)
    monkeypatch.setattr('docqa.docbase.embeddings',lambda *a:(_ for _ in ()).throw(ModelError('timeout','timeout')))
    with pytest.raises(ModelError):base.build(source)
    assert len(calls)==2 and not base.documents()
    monkeypatch.setattr('docqa.docbase.embeddings',lambda c,t:[[1.,0.] for _ in t])
    phases=[]
    result=base.build(source,progress=lambda stage,d,t:phases.append(stage))
    assert result['status']=='indexed' and len(calls)==2
    assert '解析图片' in phases and 'Qwen 文本向量' in phases and '保存索引' in phases
    with base.connect() as db:
        pictured=list(db.execute("select page,image from chunks where image<>'' order by page"))
    assert [r['page'] for r in pictured]==[1,2]
    assert all((base.root/r['image']).is_file() for r in pictured)
    # No open cache handles prevent a later filesystem move on Windows.
    cache=base.root/'ingest-cache.sqlite3'
    cache.rename(cache.with_suffix('.moved'))


def test_bad_credentials_do_not_submit_entire_document(tmp_path):
    calls=[]
    def fail(*args):
        calls.append(1)
        raise ModelError('unauthorized','bad key')
    with pytest.raises(ModelError):
        text_vectors(cfg(tmp_path),'embedding',[str(i) for i in range(80)],fail)
    assert len(calls)<=2
