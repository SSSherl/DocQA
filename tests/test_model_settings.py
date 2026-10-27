import copy
import json
from pathlib import Path
import pytest
from docqa.docbase import DocBase
from docqa.model_settings import apply_settings, public_settings, ROLES


def setup(tmp_path):
    base=DocBase({'data_dir':str(tmp_path/'data')})
    source=tmp_path/'sample.md'
    source.write_text('# Evidence\nA password reset uses email.',encoding='utf-8')
    base.build(source,'demo')
    path=tmp_path/'config.json'
    path.write_text(json.dumps(base.cfg),encoding='utf-8')
    return base,path


def update(role='chat',**extra):
    return {'models':{role:dict(enabled=True,base_url='http://localhost:9999/v1',model='test',**extra)}}


def ok(cfg,roles):
    return {'models':[{'role':r,'status':'available' if cfg[r]['enabled'] else 'disabled','message':'ok'} for r in roles]}


def test_all_roles_public_never_return_credentials(monkeypatch):
    monkeypatch.setattr('docqa.model_settings.read_secret',lambda e:'private-value')
    cfg={r:{'api_key_store':'private-alias','api_key':'private-raw','model':r} for r in ROLES}
    view=public_settings(cfg)
    assert len(view['models'])==6
    assert all(e['has_key'] for e in view['models'].values())
    assert 'private' not in json.dumps(view)


def test_secret_replacement_and_test_cleanup(tmp_path,monkeypatch):
    base,path=setup(tmp_path)
    secrets={}
    monkeypatch.setattr('docqa.model_settings.save_secret',lambda k,v:secrets.update({k:v}))
    monkeypatch.setattr('docqa.model_settings.delete_secret',lambda k:secrets.pop(k))
    monkeypatch.setattr('docqa.model_settings.diagnose',ok)
    before=path.read_bytes()
    result=apply_settings(base,update(key='dummy-value'),path,test_only=True)
    assert result['status']=='tested' and not secrets and path.read_bytes()==before
    apply_settings(base,update(key='dummy-value'),path)
    assert len(secrets)==1 and 'dummy-value' not in path.read_text()
    alias=base.cfg['chat']['api_key_store']
    apply_settings(base,update(),path)
    assert base.cfg['chat']['api_key_store']==alias
    apply_settings(base,{'models':{'chat':{'enabled':False,'clear_key':True}}},path)
    assert not base.cfg['chat'].get('api_key_store') and base.cfg['chat']['api_key_env']==''


def test_failed_model_and_build_preserve_config_index(tmp_path,monkeypatch):
    base,path=setup(tmp_path)
    before=path.read_bytes(); docs=base.documents()
    monkeypatch.setattr('docqa.model_settings.diagnose',lambda *a:{'models':[{'role':'chat','status':'unauthorized'}]})
    assert apply_settings(base,update(),path)['status']=='not_activated'
    assert path.read_bytes()==before
    monkeypatch.setattr('docqa.model_settings.diagnose',ok)
    monkeypatch.setattr('docqa.docbase.embeddings',lambda *a:(_ for _ in ()).throw(RuntimeError('offline')))
    assert apply_settings(base,update('embedding'),path)['status']=='not_activated'
    assert path.read_bytes()==before and base.documents()==docs
    assert base.query('password')['results']


def test_staged_vectors_and_disable_keep_citations(tmp_path,monkeypatch):
    base,path=setup(tmp_path)
    monkeypatch.setattr('docqa.model_settings.diagnose',ok)
    monkeypatch.setattr('docqa.docbase.embeddings',lambda cfg,texts:[[1.,0.] for t in texts])
    old_id=base.documents()[0]['id']
    assert apply_settings(base,update('embedding',dimensions=2),path)['status']=='activated'
    assert base.documents()[0]['id']==old_id
    assert base.query('password')['routes']==['BM25','dense']
    with base.connect() as db:
        assert json.loads(db.execute('select dense from chunks').fetchone()[0])==[1.,0.]
    assert apply_settings(base,{'models':{'embedding':{'enabled':False}}},path)['status']=='activated'
    assert base.query('password')['routes']==['BM25']
    with base.connect() as db:
        assert json.loads(db.execute('select dense from chunks').fetchone()[0])==[]


@pytest.mark.parametrize('values',[
    {'base_url':'https://user:secret@example.com/v1'},
    {'base_url':'https://example.com/v1?key=secret'},
    {'enabled':'true'}, {'dimensions':-2}, {'api_key_store':'injected'},
])
def test_reject_bad_fields(tmp_path,values):
    base,path=setup(tmp_path)
    with pytest.raises(ValueError):
        apply_settings(base,{'models':{'chat':values}},path)
    assert base.query('password')['results']


def test_cloud_requires_consent(tmp_path):
    base,path=setup(tmp_path)
    with pytest.raises(ValueError,match='远程'):
        apply_settings(base,{'models':{'chat':{'enabled':True,'base_url':'https://example.com/v1','model':'test'}}},path)


def test_vision_rebuild_copies_assets_before_activation(tmp_path,monkeypatch):
    from PIL import Image
    base,path=setup(tmp_path)
    image=tmp_path/'figure.png'
    Image.new('RGB',(40,40),'red').save(image)
    base.build(image,'images')
    monkeypatch.setattr('docqa.model_settings.diagnose',ok)
    monkeypatch.setattr('docqa.doc2md.chat',lambda *a:'A red diagram with a password reset flow.')
    assert apply_settings(base,update('vision'),path)['status']=='activated'
    results=base.query('diagram')['results']
    pictured=[r for r in results if r['image']]
    assert pictured and (base.root/pictured[0]['image']).is_file()
    assert pictured[0]['page']==1 and pictured[0]['figure']=='Figure 1'


def test_real_encrypted_settings_and_failed_key_cleanup(tmp_path,monkeypatch):
    import os
    if os.name!='nt':
        pytest.skip('Windows DPAPI integration')
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'profile'))
    base,path=setup(tmp_path)
    monkeypatch.setattr('docqa.model_settings.diagnose',ok)
    result=apply_settings(base,update(key='dummy-only-not-a-real-key'),path)
    assert result['status']=='activated'
    encrypted=list((tmp_path/'profile/DocQA/credentials').glob('*.dpapi'))
    assert len(encrypted)==1 and b'dummy-only' not in encrypted[0].read_bytes()
    assert public_settings(base.cfg)['models']['chat']['has_key']
    before=path.read_bytes()
    monkeypatch.setattr('docqa.model_settings.diagnose',lambda *a:{'models':[{'role':'chat','status':'unauthorized'}]})
    assert apply_settings(base,update(key='dummy-invalid-key'),path)['status']=='not_activated'
    assert path.read_bytes()==before
    assert len(list(encrypted[0].parent.glob('*.dpapi')))==1


def test_test_then_save_reuses_success_but_explicit_test_is_fresh(tmp_path,monkeypatch):
    base,path=setup(tmp_path);calls=[]
    def probe(c,roles):
        calls.append(roles)
        return ok(c,roles)
    monkeypatch.setattr('docqa.model_settings.diagnose',probe)
    assert apply_settings(base,update(),path,test_only=True)['status']=='tested'
    assert len(calls)==1
    result=apply_settings(base,update(),path)
    assert result['status']=='activated' and result['diagnostics']['models'][0]['cached']
    assert len(calls)==1
    apply_settings(base,update(),path,test_only=True)
    assert len(calls)==2
    changed=update();changed['models']['chat']['model']='different'
    apply_settings(base,changed,path)
    assert len(calls)==3


def test_validation_cache_expires_and_cannot_hide_new_key_failure(tmp_path,monkeypatch):
    import time
    base,path=setup(tmp_path);calls=[]
    monkeypatch.setattr('docqa.model_settings.diagnose',lambda c,r:(calls.append(r) or ok(c,r)))
    apply_settings(base,update(),path)
    base._validation_cache={k:(time.monotonic()-301,v[1]) for k,v in base._validation_cache.items()}
    apply_settings(base,update(),path)
    assert len(calls)==2
    monkeypatch.setattr('docqa.model_settings.read_secret',lambda e:'changed-key')
    monkeypatch.setattr('docqa.model_settings.diagnose',lambda *a:{'models':[{'role':'chat','status':'unauthorized'}]})
    before=path.read_bytes()
    assert apply_settings(base,update(),path)['status']=='not_activated'
    assert path.read_bytes()==before


def test_vector_only_change_never_reparses_document(tmp_path,monkeypatch):
    base,path=setup(tmp_path)
    monkeypatch.setattr('docqa.model_settings.diagnose',ok)
    monkeypatch.setattr('docqa.docbase.parse_document',lambda *a:(_ for _ in ()).throw(AssertionError('Unexpected reparse')))
    monkeypatch.setattr('docqa.docbase.embeddings',lambda c,t:[[1.,0.] for _ in t])
    with base.connect() as db:before=[tuple(r) for r in db.execute('select id,text,page,title,figure,image from chunks')]
    assert apply_settings(base,update('embedding',dimensions=2),path)['status']=='activated'
    with base.connect() as db:after=[tuple(r) for r in db.execute('select id,text,page,title,figure,image from chunks')]
    assert before==after and base.query('password')['results']


def test_form_defaults_pending_enabled_and_remembers_explicit_disable(tmp_path):
    base,path=setup(tmp_path)
    assert public_settings(base.cfg)['models']['chat']['default_enabled']
    assert not public_settings(base.cfg)['models']['chat'].get('enabled')
    apply_settings(base,{'models':{'chat':{'enabled':False}}},path)
    assert not public_settings(base.cfg)['models']['chat']['default_enabled']
