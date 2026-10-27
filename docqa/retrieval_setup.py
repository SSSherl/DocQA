"""Validate both retrieval services, stage reindexing, then activate the upgrade."""
import copy
import json
from pathlib import Path
import tempfile
from .docbase import DocBase
from .diagnostics import diagnose


def prepare_upgrade(base, settings, config_path, progress=None):
    candidate = copy.deepcopy(base.cfg)
    candidate["allow_remote_models"] = bool(candidate.get("allow_remote_models",False) or settings.get("allow_remote_models",False))
    roles = ["embedding"] + (["reranker"] if settings.get("enable_reranker",True) else [])
    for role in roles:
        endpoint = settings.get(role)
        if not isinstance(endpoint,dict) or not endpoint.get("model") or not (endpoint.get("url") or endpoint.get("base_url")):
            raise ValueError(f"请填写 {role} 模型名和接口地址")
        allowed = {"url","base_url","model","api_key_env","api_key_store","timeout","dimensions"}
        if set(endpoint) - allowed:
            raise ValueError("模型设置仅允许接口、模型名、凭证别名、环境变量名和超时；不接受明文密钥")
        candidate[role] = dict(endpoint, enabled=True)
    if "reranker" not in roles:
        candidate["reranker"] = dict(candidate.get("reranker",{}),enabled=False)
    report = diagnose(candidate,roles)
    if any(m["status"] != "available" for m in report["models"]):
        return {"status":"not_activated","diagnostics":report,"message":"模型验证未全部通过，当前配置和索引保持不变"}
    if any(d["fingerprint"] != base.fingerprint() for d in base.documents()):
        return {"status":"not_activated","diagnostics":report,"message":"现有解析配置与索引不一致，请先使用原配置重建，再升级向量服务"}
    current_root = base.root
    # Stage complete replacement in a separate local directory; old data stays queryable on failure.
    with tempfile.TemporaryDirectory(prefix="docqa-upgrade-") as temporary:
        stage_cfg = copy.deepcopy(candidate)
        stage_cfg["data_dir"] = temporary
        stage = DocBase(stage_cfg)
        with base.connect() as source, stage.connect() as dest:
            source.backup(dest)
        rebuilt = stage.refresh_vectors(progress)
        if rebuilt["failed"]:
            return {"status":"not_activated","diagnostics":report,"rebuild":rebuilt,"message":"重建失败，当前配置和索引保持不变"}
        # Vector-only refresh retains the original relative image paths and citation IDs.
        target = Path(config_path)
        previous = target.read_bytes() if target.exists() else None
        # data_dir in the live config must retain its original value, never the staging path.
        try:
            target.write_text(json.dumps(candidate,ensure_ascii=False,indent=2),encoding="utf-8")
            with stage.connect() as source, base.connect() as dest:
                source.backup(dest)
        except Exception:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
            raise
    base.cfg.clear()
    base.cfg.update(candidate)
    message = "Embedding＋BM25 混合检索已启用，已有文档已重建" + ("；Reranker 已启用" if "reranker" in roles else "；精排保持停用")
    return {"status":"activated","diagnostics":report,"rebuild":rebuilt,"message":message}


def prepare_bge_upgrade(base, endpoint, config_path, progress=None):
    candidate = copy.deepcopy(base.cfg)
    allowed = {"url","base_url","model","api_key_env","api_key_store","timeout","dimensions"}
    if not isinstance(endpoint,dict) or set(endpoint)-allowed or not endpoint.get("model") or not (endpoint.get("url") or endpoint.get("base_url")):
        raise ValueError("BGE 配置需提供服务地址、模型名及本机凭证别名，不能包含明文密钥")
    candidate["bge"] = dict(endpoint,enabled=True)
    report = diagnose(candidate,["bge"])
    if any(m["status"] != "available" for m in report["models"]):
        return {"status":"not_activated","diagnostics":report}
    if any(d["fingerprint"] != base.fingerprint() for d in base.documents()):
        raise ValueError("已有索引与配置不一致，请先修复原索引再升级")
    with tempfile.TemporaryDirectory(prefix="docqa-bge-upgrade-") as temporary:
        stage = DocBase(dict(candidate,data_dir=temporary))
        with base.connect() as source,stage.connect() as dest:
            source.backup(dest)
        built = stage.refresh_bge_vectors(progress)
        if built["failed"]:
            return {"status":"not_activated","diagnostics":report,"rebuild":built}
        target = Path(config_path)
        previous = target.read_bytes() if target.exists() else None
        try:
            target.write_text(json.dumps(candidate,ensure_ascii=False,indent=2),encoding="utf-8")
            with stage.connect() as source,base.connect() as dest:
                source.backup(dest)
        except Exception:
            if previous is not None:
                target.write_bytes(previous)
            else:
                target.unlink(missing_ok=True)
            raise
    base.cfg.clear()
    base.cfg.update(candidate)
    return {"status":"activated","diagnostics":report,"rebuild":built,"mode":"cloud_dense","message":"BGE-M3 云端补充稠密通道已启用；不是学习型稀疏通道"}


def prepare_visual_upgrade(base, endpoint, config_path, progress=None):
    from .visual_index import refresh_images
    from .models import visual_embeddings
    allowed = {"url", "base_url", "model", "api_key_env", "api_key_store", "timeout", "dimensions"}
    if not isinstance(endpoint, dict) or set(endpoint)-allowed or not endpoint.get("model") or not (endpoint.get("url") or endpoint.get("base_url")):
        raise ValueError("图文向量配置必须提供接口、模型和凭证别名，不能包含明文密钥")
    candidate = copy.deepcopy(base.cfg)
    candidate["visual_embedding"] = dict(endpoint, enabled=True)
    visual_embeddings(candidate, [{"text": "image retrieval connection test"}])
    with tempfile.TemporaryDirectory(prefix="docqa-visual-") as temporary:
        stage = DocBase(dict(candidate, data_dir=temporary))
        with base.connect() as source, stage.connect() as dest:
            source.backup(dest)
        # Read original assets while writing only to the temporary database.
        stage.root = base.root
        rebuilt = refresh_images(stage, candidate, progress)
        target = Path(config_path)
        previous = target.read_bytes() if target.exists() else None
        try:
            target.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
            with stage.connect() as source, base.connect() as dest:
                source.backup(dest)
        except Exception:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
            raise
    base.cfg.clear()
    base.cfg.update(candidate)
    return {"status": "activated", **rebuilt}
