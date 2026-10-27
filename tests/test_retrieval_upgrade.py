import copy
import json
from pathlib import Path
import pytest
from docqa.docbase import DocBase
from docqa.benchmark import compare
from docqa.retrieval_setup import prepare_upgrade


def fixture_base(tmp_path):
    cfg = {"data_dir":str(tmp_path/"live"),"embedding":{},"reranker":{}}
    base = DocBase(cfg)
    source = tmp_path/"account.md"
    source.write_text("# Account\nForgot password? Reset using the registered email.",encoding="utf-8")
    base.build(source,"demo")
    config_file = tmp_path/"config.json"
    config_file.write_text(json.dumps(cfg),encoding="utf-8")
    return base,config_file


def settings():
    return {"embedding":{"base_url":"http://localhost/test","model":"embed"},"reranker":{"base_url":"http://localhost/test","model":"rerank"}}


def successful_diagnosis(*args):
    return {"models":[{"role":r,"status":"available"} for r in ("embedding","reranker")]}


def test_failed_probe_changes_nothing(tmp_path,monkeypatch):
    base,path = fixture_base(tmp_path)
    previous = path.read_bytes()
    fingerprint = base.fingerprint()
    monkeypatch.setattr("docqa.retrieval_setup.diagnose",lambda *args:{"models":[{"status":"unauthorized"}]})
    result = prepare_upgrade(base,settings(),path)
    assert result["status"] == "not_activated"
    assert path.read_bytes() == previous and base.fingerprint() == fingerprint
    assert base.query("password")["results"]


def test_failed_staging_preserves_live_data(tmp_path,monkeypatch):
    base,path = fixture_base(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr("docqa.retrieval_setup.diagnose",successful_diagnosis)
    def fail(*args): raise RuntimeError("model offline")
    monkeypatch.setattr("docqa.docbase.embeddings",fail)
    report = prepare_upgrade(base,settings(),path)
    assert report["status"] == "not_activated"
    assert path.read_bytes() == before
    assert not base.cfg["embedding"]
    assert base.query("password")["results"]


def test_valid_upgrade_activates_vectors_and_assets(tmp_path,monkeypatch):
    base,path = fixture_base(tmp_path)
    monkeypatch.setattr("docqa.retrieval_setup.diagnose",successful_diagnosis)
    monkeypatch.setattr("docqa.docbase.embeddings",lambda cfg,texts:[[1.,0.] for _ in texts])
    monkeypatch.setattr("docqa.docbase.rerank",lambda cfg,q,docs:[{"index":i,"relevance_score":1.-i*.1} for i in range(len(docs))])
    report = prepare_upgrade(base,settings(),path)
    assert report["status"] == "activated"
    assert json.loads(path.read_text())["embedding"]["enabled"]
    assert Path(base.cfg["data_dir"]) == base.root
    result = base.query("password",mode="reranked")
    assert result["reranked"] and "dense" in result["routes"]
    assert list((base.root/"parsed").rglob("document.md"))
    assert base.documents()[0]["category"] == "demo"


def test_benchmark_does_not_invent_unconfigured_modes(tmp_path):
    base,_ = fixture_base(tmp_path)
    cases = [{"question":"password","relevant_sources":["account.md"]},{"question":"ZXQ_NONEXISTENT_999","expected_empty":True}]
    result = compare(base,cases,1)
    assert result["modes"]["bm25"]["recall"] == 1
    assert result["modes"]["bm25"]["no_hit_accuracy"] == 1
    assert result["modes"]["hybrid"]["status"] == "not_configured"
    assert "recall" not in result["modes"]["hybrid"]
    with pytest.raises(ValueError,match="不存在"):
        compare(base,[{"question":"q","relevant_sources":["missing.md"]}])


def test_benchmark_marks_runtime_fallback_as_incomplete(tmp_path,monkeypatch):
    base,path = fixture_base(tmp_path)
    monkeypatch.setattr("docqa.retrieval_setup.diagnose",successful_diagnosis)
    monkeypatch.setattr("docqa.docbase.embeddings",lambda cfg,texts:[[1.,0.] for _ in texts])
    prepare_upgrade(base,settings(),path)
    def fail(*args): raise RuntimeError("offline")
    monkeypatch.setattr("docqa.docbase.embeddings",fail)
    result = compare(base,[{"question":"password","relevant_sources":["account.md"]}])
    assert result["modes"]["bm25"]["status"] == "complete"
    assert result["modes"]["hybrid"]["status"] == "incomplete"
    assert result["modes"]["hybrid"]["recall"] is None


def test_bm25_mode_does_not_call_model_or_mutate_config(tmp_path,monkeypatch):
    base,_ = fixture_base(tmp_path)
    before = copy.deepcopy(base.cfg)
    def fail(*args): raise AssertionError("unexpected model call")
    monkeypatch.setattr("docqa.docbase.embeddings",fail)
    monkeypatch.setattr("docqa.docbase.rerank",fail)
    result = base.query("password",mode="bm25")
    assert result["routes"] == ["BM25"] and not result["reranked"]
    assert base.cfg == before


def test_retrieval_settings_reject_raw_key(tmp_path):
    base,path = fixture_base(tmp_path)
    payload = settings()
    payload["embedding"]["api_key"] = "dummy-not-real"
    with pytest.raises(ValueError,match="明文"):
        prepare_upgrade(base,payload,path)


def test_embedding_only_upgrade_does_not_require_reranker_or_reparse(tmp_path,monkeypatch):
    base,path = fixture_base(tmp_path)
    original_ids = [r["id"] for r in base.query("password")["results"]]
    probed = []
    def diagnose(cfg,roles):
        probed.extend(roles)
        return {"models":[{"role":"embedding","status":"available"}]}
    monkeypatch.setattr("docqa.retrieval_setup.diagnose",diagnose)
    monkeypatch.setattr("docqa.docbase.embeddings",lambda cfg,texts:[[1.,0.] for _ in texts])
    def no_parse(*args): raise AssertionError("不应重复调用视觉解析")
    monkeypatch.setattr("docqa.docbase.parse_document",no_parse)
    payload = {"enable_reranker":False,"embedding":settings()["embedding"]}
    report = prepare_upgrade(base,payload,path)
    assert report["status"] == "activated" and probed == ["embedding"]
    assert base.cfg["reranker"]["enabled"] is False
    assert [r["id"] for r in base.query("password")["results"]] == original_ids


def test_dense_threshold_rejects_weak_evidence_without_losing_keyword_hits(tmp_path,monkeypatch):
    cfg = {"data_dir":str(tmp_path/"threshold"),"embedding":{"url":"http://localhost/embed"},"retrieval":{"dense_min_score":0.4}}
    monkeypatch.setattr("docqa.docbase.embeddings",lambda cfg,texts:[[1.,0.] for _ in texts])
    base = DocBase(cfg)
    source = tmp_path/"notes.md"
    source.write_text("FE_HEART_Start heartbeat interface",encoding="utf-8")
    base.build(source)
    monkeypatch.setattr("docqa.docbase.embeddings",lambda cfg,texts:[[0.,1.] for _ in texts])
    assert base.query("FE_HEART_Start")["results"][0]["routes"] == ["BM25"]
    assert not base.query("UNRELATED_98765")["results"]


def test_unknown_exact_identifier_is_not_replaced_by_semantic_similarity(tmp_path,monkeypatch):
    cfg = {"data_dir":str(tmp_path/"literal"),"embedding":{"url":"http://localhost/embed"}}
    monkeypatch.setattr("docqa.docbase.embeddings",lambda cfg,texts:[[1.,0.] for _ in texts])
    base = DocBase(cfg)
    source = tmp_path/"interface.md"
    source.write_text("FE_HEART_Start starts heartbeat",encoding="utf-8")
    base.build(source)
    assert base.query("FE_HEART_Start")["results"]
    result = base.query("UNKNOWN_INTERFACE_123")
    assert not result["results"] and result["literal_miss"]
