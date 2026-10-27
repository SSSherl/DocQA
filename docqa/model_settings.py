"""Independent model settings, encrypted credentials and staged activation."""
import copy
import json
import hashlib
import time
import sqlite3
from contextlib import closing
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse
from .credentials import read_secret, save_secret, delete_secret
from .diagnostics import diagnose
from .docbase import DocBase
from .models import endpoint_url, configured

ROLES = ("chat", "vision", "embedding", "reranker", "bge", "visual_embedding")
FIELDS = {"enabled", "base_url", "url", "model", "api_key_env", "timeout", "dimensions"}


def public_settings(cfg):
    models = {}
    for role in ROLES:
        endpoint = cfg.get(role, {})
        item = {k: v for k, v in endpoint.items() if k in FIELDS}
        try:
            item["has_key"] = bool(read_secret(endpoint))
        except Exception:
            item["has_key"] = False
        item["default_enabled"] = bool(endpoint.get("enable_on_save", True))
        models[role] = item
    return {"models": models, "allow_remote_models": bool(cfg.get("allow_remote_models"))}


def candidate_settings(cfg, payload, created):
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), dict):
        raise ValueError("请提供模型配置")
    updates = payload["models"]
    if not updates or set(updates) - set(ROLES):
        raise ValueError("未知模型通道")
    candidate = copy.deepcopy(cfg)
    if "allow_remote_models" in payload:
        if not isinstance(payload["allow_remote_models"], bool):
            raise ValueError("远程模型开关格式无效")
        candidate["allow_remote_models"] = payload["allow_remote_models"]
    for role, values in updates.items():
        if not isinstance(values, dict) or set(values) - FIELDS - {"key", "clear_key"}:
            raise ValueError("模型字段无效")
        endpoint = candidate.setdefault(role, {})
        for field, value in values.items():
            if field not in FIELDS:
                continue
            if field == "enabled":
                if not isinstance(value, bool):
                    raise ValueError("启用开关格式无效")
            elif field in {"timeout", "dimensions"}:
                if field == "dimensions" and value in (None, ""):
                    endpoint.pop(field, None)
                    continue
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= (600 if field == "timeout" else 65536):
                    raise ValueError("超时或向量维度超出范围")
            elif not isinstance(value, str) or len(value) > 2048:
                raise ValueError("模型字段格式无效")
            else:
                value = value.strip()
            endpoint[field] = value
        if "enabled" in values:
            endpoint["enable_on_save"] = endpoint["enabled"]
        for field in ("base_url", "url"):
            if endpoint.get(field):
                parsed = urlparse(endpoint[field])
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("接口地址必须为不含密钥、查询参数的 HTTP(S) 地址")
        env = endpoint.get("api_key_env", "")
        if env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
            raise ValueError("环境变量名称无效")
        key = values.get("key", "")
        if not isinstance(key, str) or len(key) > 4096 or any(c in key for c in "\r\n"):
            raise ValueError("密钥格式无效")
        if values.get("clear_key"):
            endpoint.pop("api_key_store", None)
            endpoint["api_key_env"] = ""
        if key.strip():
            alias = "model-" + role + "-" + uuid.uuid4().hex
            save_secret(alias, key.strip())
            created.append(alias)
            endpoint["api_key_store"] = alias
            # An old environment value must not override the newly supplied key.
            endpoint["api_key_env"] = ""
        if endpoint.get("enabled"):
            if not endpoint.get("model") or not endpoint_url(candidate, role):
                raise ValueError("启用模型需要填写接口地址和模型名")
    # Do not allow disabling remote access underneath another active cloud role.
    if not candidate.get("allow_remote_models"):
        for role in ROLES:
            if configured(candidate, role) and urlparse(endpoint_url(candidate, role)).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("请先允许远程模型调用，或停用所有云端模型")
    return candidate, list(updates)


def validation_report(base, candidate, roles, fresh=False):
    cache = getattr(base, "_validation_cache", None)
    if cache is None:
        cache = base._validation_cache = {}
    keys = {}
    output = {}
    pending = []
    now = time.monotonic()
    for role in roles:
        ep = candidate.get(role, {})
        try:
            secret = read_secret(ep)
        except Exception:
            secret = ""
        identity = [role, endpoint_url(candidate,role), ep.get("model"), ep.get("dimensions"),
                    ep.get("request_options"), bool(candidate.get("allow_remote_models")),
                    hashlib.sha256((secret or ep.get("api_key_store", "")).encode()).hexdigest()]
        key = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        keys[role] = key
        previous = cache.get(key)
        if not fresh and configured(candidate,role) and previous and now-previous[0] < 300:
            output[role] = dict(previous[1], cached=True, message="复用5分钟内的成功连接验证；本次未重复发送测试请求")
        else:
            pending.append(role)
    note = "成功验证可在5分钟内用于保存；修改接口、模型或密钥会重新验证，点击测试连接始终重新请求。"
    if pending:
        report = diagnose(candidate,pending)
        note = report.get("note", "") + " " + note
        for item in report["models"]:
            output[item["role"]] = item
            if item["status"] == "available":
                cache[keys[item["role"]]] = (time.monotonic(),copy.deepcopy(item))
    base._validation_cache = {k:v for k,v in cache.items() if time.monotonic()-v[0]<300}
    return {"models":[output[r] for r in roles],"note":note}


def copy_inference_cache(source, destination):
    source = Path(source) / "ingest-cache.sqlite3"
    if source.exists():
        with closing(sqlite3.connect(source,timeout=30)) as src, closing(sqlite3.connect(Path(destination)/source.name,timeout=30)) as dst:
            src.backup(dst)


def model_identity(cfg,role):
    ep = cfg.get(role,{})
    return (endpoint_url(cfg,role),ep.get("model"),ep.get("dimensions"),ep.get("request_options"))


def apply_settings(base, payload, config_path, progress=None, test_only=False):
    created = []
    activated = False
    try:
        candidate, roles = candidate_settings(base.cfg, payload, created)
        if progress:
            progress(0, 0, {"name": "验证模型连接"})
        report = validation_report(base, candidate, roles, fresh=test_only)
        if any(m["status"] not in {"available", "disabled"} for m in report["models"]):
            return {"status": "not_activated", "diagnostics": report, "message": "连接验证失败，配置和索引保持不变"}
        if test_only:
            return {"status": "tested", "diagnostics": report, "message": "测试完成，未保存配置"}
        # Full parse is staged when parser/vector identity changes, preserving source citations.
        with tempfile.TemporaryDirectory(prefix="docqa-models-") as temporary:
            stage = DocBase(dict(candidate, data_dir=temporary))
            needs_build = stage.fingerprint() != base.fingerprint()
            visual_changed = any(candidate.get("visual_embedding", {}).get(k) != base.cfg.get("visual_embedding", {}).get(k) for k in ("enabled", "url", "base_url", "model", "dimensions"))
            needs_build = needs_build or visual_changed
            if needs_build:
                with base.connect() as source, stage.connect() as dest:
                    source.backup(dest)
                copy_inference_cache(base.root, temporary)
                reparse = (configured(candidate,"vision") and model_identity(candidate,"vision") != model_identity(base.cfg,"vision")) or any(d["fingerprint"]!=base.fingerprint() for d in base.documents())
                if reparse:
                    if progress: progress(0,0,{"name":"连接已验证，正在更新文档解析与索引"})
                    rebuilt = stage.rebuild_all(progress)
                else:
                    # Preserve parsed text, image provenance and chunk IDs for vector-only edits.
                    stage.root = base.root
                    rebuilt = {"failed":0}
                    for role,refresh in (("embedding",stage.refresh_vectors),("bge",stage.refresh_bge_vectors)):
                        if model_identity(candidate,role) != model_identity(base.cfg,role):
                            if progress: progress(0,0,{"name":"连接已验证，正在更新 " + role + " 向量"})
                            report_part = refresh(progress)
                            rebuilt["failed"] += report_part["failed"]
                    if visual_changed:
                        from .visual_index import refresh_images
                        if configured(candidate,"visual_embedding"):
                            if progress: progress(0,0,{"name":"连接已验证，正在更新图文向量"})
                            refresh_images(stage,candidate)
                        else:
                            with stage.connect() as db: db.execute("DELETE FROM image_vectors")
                    if not rebuilt["failed"]:
                        with stage.connect() as db: db.execute("UPDATE documents SET fingerprint=?",(stage.fingerprint(),))
                if rebuilt["failed"]:
                    return {"status":"not_activated","diagnostics":report,"message":"索引更新失败，配置和原索引保持不变"}
                parsed = Path(temporary) / "parsed"
                if parsed.exists():
                    shutil.copytree(parsed, base.root / "parsed", dirs_exist_ok=True)
            target = Path(config_path)
            previous = target.read_bytes() if target.exists() else None
            pending = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                pending.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
                pending.replace(target)
                if needs_build:
                    copy_inference_cache(temporary, base.root)
                    with stage.connect() as source, base.connect() as dest:
                        source.backup(dest)
            except Exception:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(previous)
                raise
            finally:
                pending.unlink(missing_ok=True)
        base.cfg.clear()
        base.cfg.update(candidate)
        activated = True
        return {"status": "activated", "diagnostics": report, "message": "配置已保存并生效" + ("，已有文档已重建索引" if needs_build else "")}
    finally:
        if not activated:
            for alias in created:
                delete_secret(alias)
