import json
import sqlite3
import pytest
from docqa.docbase import DocBase
from docqa.models import bge_embeddings
from docqa.retrieval_setup import prepare_bge_upgrade


def endpoint():
    return {"base_url":"http://localhost/test","model":"bge-m3","dimensions":2}


def test_bge_model_has_separate_configuration(monkeypatch):
    cfg={"embedding":{"model":"qwen"},"bge":endpoint()}
    observed=[]
    monkeypatch.setattr("docqa.models.embeddings",lambda c,t:observed.append(c["embedding"]["model"]) or [[0.,1.]])
    assert bge_embeddings(cfg,["sample"])==[[0.,1.]]
    assert observed==["bge-m3"] and cfg["embedding"]["model"]=="qwen"


def test_three_cloud_routes_use_distinct_dense_columns(tmp_path,monkeypatch):
    cfg={"data_dir":str(tmp_path/"data"),"embedding":{"url":"http://localhost/embed"},"bge":endpoint()}
    monkeypatch.setattr("docqa.docbase.embeddings",lambda c,t:[[1.,0.] for _ in t])
    monkeypatch.setattr("docqa.docbase.bge_embeddings",lambda c,t:[[0.,1.] for _ in t])
    base=DocBase(cfg)
    source=tmp_path/"sample.md"
    source.write_text("sample technical document",encoding="utf-8")
    base.build(source)
    result=base.query("sample")
    assert set(result["routes"])=={"BM25","dense","BGE-M3-dense"}
    assert not any("未配置本地" in warning for warning in result["warnings"])
    assert "bge_dense" not in result["results"][0]
    with base.connect() as db:
        row=db.execute("SELECT dense,bge_dense,sparse FROM chunks").fetchone()
    assert json.loads(row["dense"])==[1.,0.]
    assert json.loads(row["bge_dense"])==[0.,1.]
    assert json.loads(row["sparse"])=={}


@pytest.mark.parametrize("failure",[False,True])
def test_bge_upgrade_preserves_qwen_and_original_chunks(tmp_path,monkeypatch,failure):
    base=DocBase({"data_dir":str(tmp_path/"data")})
    source=tmp_path/"sample.md"
    source.write_text("sample text",encoding="utf-8")
    base.build(source)
    config_path=tmp_path/"config.json"
    config_path.write_text(json.dumps(base.cfg),encoding="utf-8")
    old=config_path.read_bytes()
    with base.connect() as db:
        original=[tuple(r) for r in db.execute("SELECT id,text,dense,image FROM chunks")]
    monkeypatch.setattr("docqa.retrieval_setup.diagnose",lambda *args:{"models":[{"role":"bge","status":"available"}]})
    def vectors(cfg,texts):
        if failure: raise RuntimeError("offline")
        return [[0.,1.] for _ in texts]
    monkeypatch.setattr("docqa.docbase.bge_embeddings",vectors)
    def no_qwen(*args): raise AssertionError("不应重新计算原有Qwen向量")
    monkeypatch.setattr("docqa.docbase.embeddings",no_qwen)
    monkeypatch.setattr("docqa.docbase.parse_document",no_qwen)
    report=prepare_bge_upgrade(base,endpoint(),config_path)
    assert report["status"]==("not_activated" if failure else "activated")
    with base.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT id,text,dense,image FROM chunks")]==original
    if failure:
        assert config_path.read_bytes()==old and "bge" not in base.cfg
    else:
        assert "BGE-M3-dense" in base.query("sample")["routes"]


def test_old_nine_column_database_is_migrated_without_losing_rows(tmp_path):
    with sqlite3.connect(tmp_path/"docqa.sqlite3") as db:
        db.execute("CREATE TABLE chunks(id TEXT PRIMARY KEY,doc_id TEXT,text TEXT,page INTEGER,title TEXT,figure TEXT,image TEXT,dense TEXT,sparse TEXT)")
        db.execute("INSERT INTO chunks VALUES('old','doc','kept',NULL,'','','','[]','{}')")
    base=DocBase({"data_dir":str(tmp_path)})
    with base.connect() as db:
        row=db.execute("SELECT text,bge_dense FROM chunks WHERE id='old'").fetchone()
    assert row["text"]=="kept" and json.loads(row["bge_dense"])==[]
