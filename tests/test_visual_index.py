import json
from pathlib import Path
import pytest
from PIL import Image
from docqa.docbase import DocBase
from docqa.models import visual_embeddings
from docqa.visual_index import refresh_images, status
from docqa.retrieval_setup import prepare_visual_upgrade


def endpoint():
    return {"url": "http://localhost/embeddings", "model": "visual", "dimensions": 2}


def test_multimodal_payload_uses_separate_model(monkeypatch):
    observed = []
    def post(cfg, role, payload):
        observed.append((cfg["embedding"]["model"], payload))
        return {"data": [{"index": 0, "embedding": [0., 1.]}]}
    monkeypatch.setattr("docqa.models.post", post)
    cfg = {"embedding": {"model": "text"}, "visual_embedding": endpoint()}
    visual_embeddings(cfg, [{"image": "base64"}])
    assert observed[0][0] == "visual"
    assert observed[0][1]["input"] == [{"image": "base64"}]
    assert cfg["embedding"]["model"] == "text"


def test_visual_only_recall_keeps_original_image_and_page(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "data"), "visual_embedding": endpoint()}
    monkeypatch.setattr("docqa.visual_index.visual_embeddings", lambda c, t: [[0., 1.] for _ in t])
    monkeypatch.setattr("docqa.docbase.visual_embeddings", lambda c, t: [[0., 1.] for _ in t])
    source = tmp_path / "figure.png"
    Image.new("RGB", (64,64), "red").save(source)
    base = DocBase(cfg)
    base.build(source, "pictures")
    result = base.query("红色图片", category="pictures")
    assert result["results"][0]["routes"] == ["图文向量"]
    assert result["results"][0]["page"] == 1
    assert result["results"][0]["figure"] == "Figure 1"
    assert (base.root / result["results"][0]["image"]).is_file()
    assert not base.query("红色图片", category="other")["results"]
    assert status(base)["indexed"] == 1
    # Failure must leave previously computed vectors usable.
    def fail(*a): raise RuntimeError("network unavailable")
    monkeypatch.setattr("docqa.visual_index.visual_embeddings", fail)
    with pytest.raises(RuntimeError): refresh_images(base)
    assert status(base)["indexed"] == 1
    # Old model vectors must never be compared to a different model's query.
    base.cfg["visual_embedding"]["model"] = "different"
    assert status(base)["indexed"] == 0
    assert "图文向量" not in base.query("红色图片")["routes"]


@pytest.mark.parametrize("failure", [False, True])
def test_visual_upgrade_preserves_all_original_chunks(tmp_path, monkeypatch, failure):
    base = DocBase({"data_dir": str(tmp_path / "data")})
    source = tmp_path / "note.md"
    source.write_text("original text", encoding="utf8")
    base.build(source)
    image = base.root / "parsed" / "picture.png"
    image.parent.mkdir(exist_ok=True)
    Image.new("RGB", (64,64), "red").save(image)
    with base.connect() as db:
        db.execute("UPDATE chunks SET image='parsed/picture.png'")
        original = [tuple(r) for r in db.execute("SELECT * FROM chunks")]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base.cfg), encoding="utf8")
    previous = path.read_bytes()
    monkeypatch.setattr("docqa.models.visual_embeddings", lambda *a: [[0.,1.]])
    def embed(*a):
        if failure: raise RuntimeError("failed")
        return [[0.,1.]]
    monkeypatch.setattr("docqa.visual_index.visual_embeddings", embed)
    if failure:
        with pytest.raises(RuntimeError): prepare_visual_upgrade(base, endpoint(), path)
        assert path.read_bytes() == previous
        assert "visual_embedding" not in base.cfg
    else:
        assert prepare_visual_upgrade(base, endpoint(), path)["images"] == 1
        assert status(base)["indexed"] == 1
    with base.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM chunks")] == original


def test_reject_constant_image_vectors_before_commit(tmp_path, monkeypatch):
    base = DocBase({"data_dir": str(tmp_path / "data"), "visual_embedding": endpoint()})
    monkeypatch.setattr("docqa.visual_index.visual_embeddings", lambda c,t: [[0.,1.]])
    for i, color in enumerate(("red", "blue")):
        path = tmp_path / f"{i}.png"
        Image.new("RGB", (64,64), color).save(path)
        base.build(path)
    with base.connect() as db:
        original = [tuple(r) for r in db.execute("SELECT * FROM image_vectors ORDER BY image")]
    with pytest.raises(ValueError, match="相同"):
        refresh_images(base)
    with base.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM image_vectors ORDER BY image")] == original
