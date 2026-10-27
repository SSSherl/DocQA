import json
from pathlib import Path
import zipfile
import pytest
from scripts.build_release import build


def source_tree(tmp_path):
    root=tmp_path/"source"
    root.mkdir()
    for name in (".gitignore","AGENTS.md","README.md","pyproject.toml","start.ps1"):
        (root/name).write_text("example",encoding="utf8")
    for folder in ("docqa","examples","scripts","tests","docs","data",".venv"):
        (root/folder).mkdir()
    for name in ("需求规格说明书.md","发布说明.md","部署手册.md","验收测试清单.md"):
        (root/"docs"/name).write_text("public documentation",encoding="utf8")
    cfg={"allow_remote_models":False,**{role:{"enabled":False,"base_url":"","url":"","model":""} for role in ("chat","vision","embedding","reranker","bge","visual_embedding")}}
    (root/"config.example.json").write_text(json.dumps(cfg),encoding="utf8")
    return root


def test_release_excludes_local_data_and_configuration(tmp_path):
    root=source_tree(tmp_path)
    (root/"config.json").write_text("private settings",encoding="utf8")
    (root/"data"/"conversation.sqlite3").write_bytes(b"private history")
    (root/".venv"/"secret.txt").write_text("private environment",encoding="utf8")
    (root/"docs"/"private.png").write_bytes(b"private screenshot")
    result=build(root,tmp_path/"release")
    with zipfile.ZipFile(result["archive"]) as archive:
        names=archive.namelist()
        assert "DocQA/config.example.json" in names
        assert not any("/data/" in name or "/.venv/" in name or name.endswith(("config.json","private.png")) for name in names)


def test_release_rejects_secret_in_source(tmp_path):
    root=source_tree(tmp_path)
    (root/"docqa"/"leak.py").write_text("sk-"+"x"*32,encoding="utf8")
    with pytest.raises(ValueError,match="密钥"):
        build(root,tmp_path/"release")


def test_release_requires_models_disabled_by_default(tmp_path):
    root=source_tree(tmp_path)
    cfg=json.loads((root/"config.example.json").read_text(encoding="utf8"))
    cfg["chat"]["enabled"]=True
    (root/"config.example.json").write_text(json.dumps(cfg),encoding="utf8")
    with pytest.raises(ValueError,match="关闭模型"):
        build(root,tmp_path/"release")


def test_release_keeps_public_endpoints_and_models(tmp_path):
    root=source_tree(tmp_path)
    path=root/"config.example.json"
    cfg=json.loads(path.read_text(encoding="utf8"))
    cfg["chat"].update(base_url="https://api.deepseek.com",model="deepseek-flash")
    path.write_text(json.dumps(cfg),encoding="utf8")
    result=build(root,tmp_path/"release")
    with zipfile.ZipFile(result["archive"]) as archive:
        released=json.loads(archive.read("DocQA/config.example.json"))
        assert released["chat"]["base_url"]=="https://api.deepseek.com"
        assert released["chat"]["model"]=="deepseek-flash"
        assert "DocQA/docs/部署手册.md" in archive.namelist()


@pytest.mark.parametrize("field,value",[("api_key","private"),("api_key_store","private-alias"),("base_url","https://user:secret@example.com/v1"),("url","https://example.com/v1?key=private")])
def test_release_rejects_credential_fields_and_secret_urls(tmp_path,field,value):
    root=source_tree(tmp_path)
    path=root/"config.example.json"
    cfg=json.loads(path.read_text(encoding="utf8"));cfg["chat"][field]=value
    path.write_text(json.dumps(cfg),encoding="utf8")
    with pytest.raises(ValueError):build(root,tmp_path/"release")


@pytest.mark.parametrize("field", ["api_key", "api_key_store", "api_key_env", "access_token"])
def test_release_rejects_bindings_in_alternate_templates(tmp_path, field):
    root = source_tree(tmp_path)
    (root/"config.other.example.json").write_text(json.dumps({"chat": {field: "private-binding"}}), encoding="utf8")
    with pytest.raises(ValueError, match="凭证绑定"):
        build(root, tmp_path/"release")
