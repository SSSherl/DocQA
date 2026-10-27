from dataclasses import dataclass
from pathlib import Path
import http.client
import ipaddress
import json
import os
import math
import time
import urllib.request
import urllib.parse
import urllib.error
from .credentials import read_secret


@dataclass
class Section:
    text: str
    page: int | None = None
    title: str = ""
    figure: str = ""
    image: str = ""


def config(path="config.json"):
    defaults = json.loads((Path(__file__).parent.parent / "config.example.json").read_text(encoding="utf-8")) if (Path(__file__).parent.parent / "config.example.json").exists() else {"data_dir": "data", "chunk_size": 1000, "chunk_overlap": 150}
    if Path(path).exists():
        overrides = json.loads(Path(path).read_text(encoding="utf-8"))
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(defaults.get(key), dict):
                defaults[key] = dict(defaults[key], **value)
            else:
                defaults[key] = value
    return defaults


PATHS = {"chat": "chat/completions", "vision": "chat/completions", "embedding": "embeddings", "reranker": "rerank", "bge":"embeddings", "visual_embedding":"embeddings"}


def endpoint_url(cfg, role):
    endpoint = cfg.get(role, {})
    if endpoint.get("enabled", True) is False:
        return ""
    if endpoint.get("url"):
        return endpoint["url"].rstrip("/")
    base = endpoint.get("base_url", "").rstrip("/")
    return base + "/" + PATHS[role] if base else ""


def configured(cfg, role):
    return bool(endpoint_url(cfg, role))


class ModelError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def _request(cfg, role, payload):
    endpoint = cfg.get(role, {})
    url = endpoint_url(cfg, role)
    if not url:
        raise ValueError(f"未配置 {role} 模型服务")
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    local = host == "localhost"
    try:
        local = local or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parsed.scheme not in ("http", "https") or not local and not cfg.get("allow_remote_models", False):
        raise ValueError("默认仅允许回环地址模型；使用远程服务需显式设置 allow_remote_models=true")
    options = endpoint.get("request_options", {})
    if not isinstance(options, dict) or set(options) - {"max_tokens", "thinking", "response_format"}:
        raise ValueError("request_options 仅支持 max_tokens、thinking 和 response_format")
    body = {**options, **payload, "model": endpoint.get("model", "")}
    headers = {"Content-Type": "application/json"}
    if host == "ai.gitee.com":
        headers["X-Failover-Enabled"] = "false"
    key = read_secret(endpoint)
    if not local and not key:
        raise ModelError("missing_key", f"未设置 {role} 的密钥环境变量或本机凭证；已跳过远程调用")
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    return endpoint, req


def post(cfg, role, payload):
    endpoint, req = _request(cfg, role, payload)
    # Do not follow redirects: a local model must not silently forward documents elsewhere.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(req, timeout=endpoint.get("timeout", 120)) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        status = {400:"invalid_request",401: "unauthorized",402:"insufficient_balance",403: "forbidden",404: "not_found",429: "rate_limited",502:"service_unavailable",503:"service_unavailable",504:"service_unavailable"}.get(exc.code, "http_error")
        # Inspect only enough of a structured error to classify resource gating; never echo its contents.
        try:
            detail = json.loads(exc.read(4096)).get("error",{})
            message = detail.get("message", "") if isinstance(detail,dict) else ""
            if "计费资源" in message or "no available billing resource" in message.lower():
                raise ModelError("missing_resources",f"{role} 服务要求账户先开通可用资源；当前未启用，也未购买或充值")
        except (ValueError, TypeError, AttributeError, http.client.HTTPException, OSError):
            pass
        # Never echo provider response bodies, URLs, or headers which may contain credentials.
        raise ModelError(status, f"{role} 服务返回 HTTP {exc.code}；请检查凭证、模型权限或接口地址") from None
    except http.client.HTTPException:
        raise ModelError("unreachable", f"{role} 服务响应中断，请重试") from None
    except TimeoutError:
        raise ModelError("timeout", f"{role} 服务请求超时") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ModelError("timeout", f"{role} 服务请求超时") from None
        raise ModelError("unreachable", f"{role} 服务连接失败") from None
    except OSError:
        raise ModelError("unreachable", f"{role} 服务连接失败") from None
    except (ValueError, TypeError):
        raise ModelError("invalid_response", f"{role} 服务未返回有效 JSON") from None


def chat(cfg, role, messages):
    result = post(cfg, role, {"messages": messages, "temperature": 0})
    try:
        content = result["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError()
        return content
    except (KeyError, IndexError, TypeError, ValueError):
        raise ModelError("invalid_response", f"{role} 服务返回的回答为空或格式不兼容") from None


def embeddings(cfg, texts):
    payload = {"input":texts, "encoding_format":"float"}
    dimension = cfg.get("embedding",{}).get("dimensions")
    if dimension is not None:
        if type(dimension) is not int or dimension <= 0:
            raise ValueError("dimensions 必须是正整数")
        payload["dimensions"] = dimension
    result = post(cfg, "embedding", payload)
    rows = sorted(result["data"], key=lambda r: r["index"])
    if [r["index"] for r in rows] != list(range(len(texts))):
        raise ValueError("Embedding 返回索引或数量不一致")
    vectors = [r["embedding"] for r in rows]
    if not vectors or not vectors[0] or any(len(v) != len(vectors[0]) for v in vectors):
        raise ValueError("Embedding 返回维度不一致或为空")
    if dimension is not None and any(len(v) != dimension for v in vectors):
        raise ValueError("Embedding 返回维度与配置的 dimensions 不一致")
    if any(not isinstance(x, (int, float)) or not math.isfinite(x) for v in vectors for x in v):
        raise ValueError("Embedding 返回非有限数值")
    return vectors


def rerank(cfg, question, documents):
    # Retry only transient transport/server failures. Never retry bad credentials,
    # malformed results or rate limits (which may require a provider cooldown).
    retries = cfg.get("reranker", {}).get("retries", 1)
    if type(retries) is not int or retries not in (0, 1):
        raise ValueError("reranker.retries 必须为0或1")
    for attempt in range(retries + 1):
        try:
            return _rerank_once(cfg, question, documents)
        except ModelError as exc:
            if attempt == retries or exc.status not in {"timeout", "unreachable", "service_unavailable"}:
                raise
            time.sleep(0.5)


def _rerank_once(cfg, question, documents):
    result = post(cfg, "reranker", {"query": question, "documents": documents, "top_n": len(documents)})
    if isinstance(result, dict):
        result = result.get("results", result.get("data"))
    if not isinstance(result, list) or len(result) != len(documents):
        raise ModelError("invalid_response", "Reranker 必须返回所有候选项")
    output = []
    for row in result:
        if not isinstance(row, dict):
            raise ModelError("invalid_response", "Reranker 候选格式错误")
        index = row.get("index")
        score = row.get("relevance_score", row.get("score"))
        if type(index) is not int or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ModelError("invalid_response", "Reranker 返回无效索引或分数")
        output.append({"index": index, "relevance_score": float(score)})
    if sorted(r["index"] for r in output) != list(range(len(documents))):
        raise ModelError("invalid_response", "Reranker 索引越界或重复")
    return output


def bge_embeddings(cfg, texts):
    # Reuse the validated embeddings protocol with its own model, endpoint and credentials.
    return embeddings(dict(cfg,embedding=cfg.get("bge",{})),texts)


def visual_embeddings(cfg, inputs):
    """Gitee multimodal embeddings: [{text: ...}, {image: base64 ...}]."""
    return embeddings(dict(cfg, embedding=cfg.get("visual_embedding", {})), inputs)
