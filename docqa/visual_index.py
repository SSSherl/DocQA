"""Independent image vectors; original chunks and citation locations stay intact."""
import base64
import hashlib
import json
from pathlib import Path
from .models import endpoint_url, visual_embeddings, configured, ModelError


def signature(cfg):
    ep = cfg.get("visual_embedding", {})
    value = [endpoint_url(cfg, "visual_embedding"), ep.get("model"), ep.get("dimensions")]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def image_input(path):
    path = Path(path)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("图片超过20MB，未发送至图文向量服务")
    return {"image": base64.b64encode(path.read_bytes()).decode("ascii")}


def make_vectors(cfg, images):
    from .ingestion import cached_call, notify, ordered_work, retry_call
    images = list(images)
    notify(cfg, "图文向量", 0, len(images))
    def run(item):
        key, path = item
        payload = image_input(path)
        def request():
            return retry_call(cfg, lambda:visual_embeddings(cfg, [payload])[0])
        return key, cached_call(cfg, "visual_embedding", payload, request)
    rows = []
    for result in ordered_work(images, run):
        rows.append(result)
        notify(cfg, "图文向量", len(rows), len(images))
    return rows


def save_vectors(db, cfg, rows):
    for image, vector in rows:
        db.execute("INSERT OR REPLACE INTO image_vectors VALUES(?,?,?)",
                   (image, signature(cfg), json.dumps(vector)))


def refresh_images(base, cfg=None, progress=None):
    """Compute first, then commit every vector together; failures retain the old index."""
    cfg = cfg or base.cfg
    if not configured(cfg, "visual_embedding"):
        raise ValueError("未配置图文向量模型")
    with base.connect() as db:
        images = [r[0] for r in db.execute("SELECT DISTINCT image FROM chunks WHERE image<>'' ORDER BY image")]
    computed = []
    hashes = set()
    for i, image in enumerate(images, 1):
        path = (base.root / image).resolve()
        if not path.is_relative_to(base.root / "parsed"):
            raise ValueError("无效图片资源路径")
        computed.extend(make_vectors(dict(cfg, _ingest_cache_bypass=True), [(image, path)]))
        hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        if progress:
            progress(i, len(images))
    if len(hashes) > 1 and len({tuple(vector) for _, vector in computed}) == 1:
        raise ValueError("不同图片返回完全相同的向量，图文模型未通过有效性验证；旧索引已保留")
    with base.connect() as db:
        db.execute("DELETE FROM image_vectors")
        save_vectors(db, cfg, computed)
    return {"images": len(computed), "model": cfg["visual_embedding"]["model"]}


def status(base):
    with base.connect() as db:
        total = db.execute("SELECT COUNT(DISTINCT image) FROM chunks WHERE image<>''").fetchone()[0]
        indexed = db.execute("SELECT COUNT(DISTINCT c.image) FROM chunks c JOIN image_vectors v ON c.image=v.image WHERE v.signature=?", (signature(base.cfg),)).fetchone()[0]
    return {"configured": configured(base.cfg, "visual_embedding"), "indexed": indexed, "total": total,
            "model": base.cfg.get("visual_embedding", {}).get("model", "")}
