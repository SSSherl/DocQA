"""Bounded model work and reusable successful inference results during import."""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from .models import endpoint_url, ModelError


def notify(cfg, stage, done=0, total=0):
    callback = cfg.get("_ingest_progress")
    if callback:
        callback(stage, done, total)


def ordered_work(items, call, workers=2):
    """Keep at most two tasks in flight; never queue a whole failed document."""
    iterator = iter(items)
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("ingestion.workers 必须为1到4")
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = deque()
    try:
        for _ in range(workers):
            try:
                pending.append(pool.submit(call, next(iterator)))
            except StopIteration:
                break
        while pending:
            yield pending.popleft().result()
            try:
                pending.append(pool.submit(call, next(iterator)))
            except StopIteration:
                pass
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


def cached_call(cfg, role, payload, call):
    endpoint = cfg.get(role, {})
    identity = [2, role, endpoint_url(cfg, role), endpoint.get("model"),
                endpoint.get("dimensions"), endpoint.get("request_options"), payload]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    root = Path(cfg.get("data_dir", "data"))
    root.mkdir(parents=True, exist_ok=True)
    path = root / "ingest-cache.sqlite3"
    # Each worker owns its connections; no DB lock is held over a network call.
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS results(key TEXT PRIMARY KEY, value TEXT)")
        found = db.execute("SELECT value FROM results WHERE key=?", (key,)).fetchone()
    if found and not cfg.get("_ingest_cache_bypass"):
        try:
            return json.loads(found[0])
        except (ValueError, TypeError):
            pass
    value = call()
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("INSERT OR REPLACE INTO results VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))
    return value


def retry_call(cfg, call, on_retry=None):
    """Retry transient transport failures only; never retry authentication/rate limits."""
    opts = cfg.get("ingestion", {})
    retries = opts.get("retries", 1)
    delay = opts.get("retry_backoff", 0.5)
    if type(retries) is not int or not 0 <= retries <= 3:
        raise ValueError("ingestion.retries 必须为0到3")
    if not isinstance(delay, (int, float)) or not 0 <= delay <= 10:
        raise ValueError("ingestion.retry_backoff 必须为0到10秒")
    for attempt in range(retries + 1):
        try:
            return call()
        except ModelError as exc:
            if attempt == retries or exc.status not in {"timeout", "unreachable", "service_unavailable"}:
                raise
            if on_retry:
                on_retry(attempt + 1)
            time.sleep(delay * 2 ** attempt)


def text_vectors(cfg, role, texts, call):
    """Small batches, two workers, one retry for transient errors, stable ordering."""
    opts = cfg.get("ingestion", {})
    size = opts.get("batch_size", 8)
    workers = opts.get("workers", 2)
    if type(size) is not int or not 1 <= size <= 32:
        raise ValueError("ingestion.batch_size 必须为1到32")
    batches = [texts[i:i+size] for i in range(0, len(texts), size)]
    label = "Qwen 文本向量" if role == "embedding" else "BGE-M3 补充向量"
    notify(cfg, label, 0, len(texts))
    def run(batch):
        def request():
            return retry_call(cfg, lambda:call(cfg, batch),
                              lambda n:notify(cfg, label + f"：连接暂时失败，第{n}次重试"))
        return cached_call(cfg, role, batch, request)
    vectors = []
    for result in ordered_work(batches, run, workers):
        vectors.extend(result)
        notify(cfg, label, len(vectors), len(texts))
    return vectors
