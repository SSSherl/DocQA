import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from .doc2md import parse_document, SUPPORTED
from .models import embeddings, bge_embeddings, visual_embeddings, rerank, configured, endpoint_url, ModelError
from .visual_index import make_vectors, save_vectors, signature as visual_signature


def tokens(text):
    text = text.lower()
    words = re.findall(r"[a-z0-9_]+", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(run)
        words.extend(run[i:i+2] for i in range(len(run)-1))
    return words


def chunks(text, size=1000, overlap=150):
    if size < 100 or overlap < 0 or overlap >= size:
        raise ValueError("chunk_size 至少100，overlap 必须介于0和size之间")
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary
        if text[start:end].strip():
            yield text[start:end].strip()
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError("向量维度已变更，请重建索引")
    denom = math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b))/denom if denom else 0


class DocBase:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg.get("data_dir", "data")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.dbpath = self.root / "docqa.sqlite3"
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, name TEXT, category TEXT,
                source TEXT UNIQUE, hash TEXT, fingerprint TEXT, warnings TEXT);
            CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, doc_id TEXT, text TEXT,
                page INTEGER, title TEXT, figure TEXT, image TEXT, dense TEXT, sparse TEXT);
            CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(doc_id);
            CREATE TABLE IF NOT EXISTS image_vectors(image TEXT PRIMARY KEY, signature TEXT, vector TEXT);
            ''')
            if "bge_dense" not in {r["name"] for r in db.execute("PRAGMA table_info(chunks)")}:
                db.execute("ALTER TABLE chunks ADD COLUMN bge_dense TEXT NOT NULL DEFAULT '[]'")
        self._sparse = None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.dbpath, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def sparse(self, texts):
        path = self.cfg.get("sparse_model_path")
        if not path:
            return [{} for _ in texts]
        if not Path(path).is_dir():
            raise ValueError("sparse_model_path 必须是已下载的本地 BGE-M3 模型目录")
        if self._sparse is None:
            from FlagEmbedding import BGEM3FlagModel
            self._sparse = BGEM3FlagModel(path, use_fp16=False)
        rows = self._sparse.encode(texts, return_dense=False, return_sparse=True, return_colbert_vecs=False)["lexical_weights"]
        return [{str(k): float(v) for k, v in row.items()} for row in rows]

    def fingerprint(self):
        relevant = {k: self.cfg.get(k) for k in ("chunk_size", "chunk_overlap", "sparse_model_path")}
        relevant["parser_version"] = 2
        for role in ("embedding", "vision"):
            relevant[role] = {"url": endpoint_url(self.cfg, role), "model": self.cfg.get(role, {}).get("model", "")}
            if role == "embedding" and self.cfg.get(role,{}).get("dimensions") is not None:
                relevant[role]["dimensions"] = self.cfg[role]["dimensions"]
        if configured(self.cfg,"bge"):
            relevant["bge"] = {"url":endpoint_url(self.cfg,"bge"),"model":self.cfg["bge"].get("model"),"dimensions":self.cfg["bge"].get("dimensions")}
        return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()

    def build(self, source, category="默认", force=False, progress=None, input_path=None):
        from .ingestion import text_vectors, notify
        runtime_cfg = dict(self.cfg, _ingest_progress=progress)
        notify(runtime_cfg, "检查文档与现有索引")
        source = Path(source).resolve()
        incoming = Path(input_path).resolve() if input_path is not None else source
        if not incoming.is_file():
            raise ValueError("文件不存在")
        doc_id = hashlib.sha256(str(source).encode()).hexdigest()[:24]
        digest = hashlib.sha256(incoming.read_bytes()).hexdigest()
        fp = self.fingerprint()
        with self.connect() as db:
            old = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if old and old["hash"] == digest and old["fingerprint"] == fp and not force:
                db.execute("UPDATE documents SET category=? WHERE id=?", (category, doc_id))
                return {"id": doc_id, "status": "unchanged", "warnings": json.loads(old["warnings"])}
        # New assets go in a version directory. An unsuccessful parse never invalidates the old index.
        import uuid
        folder = self.root / "parsed" / doc_id / uuid.uuid4().hex
        try:
            notify(runtime_cfg, "解析文档与图片")
            manifest = parse_document(incoming, folder, runtime_cfg)
            rows = []
            for section in manifest["sections"]:
                for text in chunks(section["text"], self.cfg.get("chunk_size", 1000), self.cfg.get("chunk_overlap", 150)):
                    # Missing-vision notices are metadata, not searchable evidence.
                    if text.startswith("[图片未解读") and not configured(self.cfg, "visual_embedding"):
                        continue
                    rows.append(dict(section, text=text))
            texts = [r["text"] for r in rows]
            dense = []
            if texts and configured(self.cfg, "embedding"):
                dense = text_vectors(runtime_cfg, "embedding", texts, embeddings)
            else:
                dense = [[] for _ in rows]
            notify(runtime_cfg, "生成本地检索索引")
            sparse = self.sparse(texts) if texts else []
            bge = []
            if configured(self.cfg,"bge"):
                bge = text_vectors(runtime_cfg, "bge", texts, bge_embeddings)
            else:
                bge = [[] for _ in rows]
            image_vectors = []
            if configured(self.cfg, "visual_embedding"):
                images = {str((folder / r["image"]).relative_to(self.root)).replace("\\", "/"): folder / r["image"] for r in rows if r["image"]}
                image_vectors = make_vectors(runtime_cfg, images.items())
            notify(runtime_cfg, "保存索引")
            if input_path is not None:
                import os
                temporary_source = source.with_name(source.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    shutil.copy2(incoming, temporary_source)
                    os.replace(temporary_source, source)
                finally:
                    temporary_source.unlink(missing_ok=True)
            with self.connect() as db:
                db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
                db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?)", (doc_id, source.name, category, str(source), digest, fp, json.dumps(manifest["warnings"], ensure_ascii=False)))
                for i, (row, dv, sv, bv) in enumerate(zip(rows, dense, sparse, bge)):
                    image = str((folder / row["image"]).relative_to(self.root)).replace("\\", "/") if row["image"] else ""
                    db.execute("INSERT INTO chunks (id,doc_id,text,page,title,figure,image,dense,sparse,bge_dense) VALUES(?,?,?,?,?,?,?,?,?,?)", (f"{doc_id}-{i}", doc_id, row["text"], row["page"], row["title"], row["figure"], image, json.dumps(dv), json.dumps(sv),json.dumps(bv)))
                save_vectors(db, self.cfg, image_vectors)
                db.execute("DELETE FROM image_vectors WHERE image NOT IN (SELECT image FROM chunks WHERE image<>'')")
            return {"id": doc_id, "status": "indexed", "chunks": len(rows), "warnings": manifest["warnings"]}
        except Exception:
            if folder.resolve().is_relative_to((self.root / "parsed").resolve()):
                shutil.rmtree(folder, ignore_errors=True)
            raise

    def documents(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT d.*, COUNT(c.id) AS chunks FROM documents d LEFT JOIN chunks c ON d.id=c.doc_id GROUP BY d.id ORDER BY d.name")]

    def delete(self, doc_id):
        with self.connect() as db:
            db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            db.execute("DELETE FROM image_vectors WHERE image NOT IN (SELECT image FROM chunks WHERE image<>'')")

    def query(self, question, top_k=5, category=None, mode="configured", progress=None):
        started = time.perf_counter()
        if progress:
            progress("retrieval")
        if mode not in {"configured", "bm25", "hybrid", "reranked", "expanded"}:
            raise ValueError("未知检索模式")
        if mode in {"hybrid", "reranked", "expanded"} and not configured(self.cfg, "embedding"):
            raise ValueError("该模式需要先接入 Embedding 并重建索引")
        if mode in {"reranked","expanded"} and not configured(self.cfg, "reranker"):
            raise ValueError("该模式需要先接入 Reranker")
        if mode == "expanded" and not configured(self.cfg,"bge"):
            raise ValueError("该模式需要先接入 BGE-M3 云端补充向量")
        if not question.strip():
            raise ValueError("问题不能为空")
        top_k = max(1, min(int(top_k), 20))
        with self.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT c.*, d.name, d.category, d.source, d.fingerprint FROM chunks c JOIN documents d ON c.doc_id=d.id WHERE (? IS NULL OR d.category=?)", (category, category))]
        notices = []
        if not rows:
            return {"results": [], "routes": [], "warnings": ["当前范围没有可检索文本，请导入文档或配置视觉模型后重建。"], "reranked":False, "elapsed_ms":(time.perf_counter()-started)*1000, "mode":mode}
        if any(r["fingerprint"] != self.fingerprint() for r in rows):
            raise ValueError("解析/模型/分块配置发生变化，请运行 build 重建索引")
        identifier = question.strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9_]*",identifier) and ("_" in identifier or re.search(r"[a-z]\d|\d[a-z]",identifier)):
            if not any(identifier in set(tokens(row["text"])) for row in rows):
                return {"results":[],"routes":[],"warnings":["该精确标识符未在当前范围的文档中出现，未使用相似术语替代。"],"reranked":False,"elapsed_ms":(time.perf_counter()-started)*1000,"mode":mode,"literal_miss":True}
        terms = Counter(tokens(question))
        corpus = [Counter(tokens(r["text"])) if not r["text"].startswith("[图片未解读") else Counter() for r in rows]
        avg = sum(sum(c.values()) for c in corpus)/len(corpus) or 1
        df = Counter(t for c in corpus for t in c)
        scores = []
        for i, counts in enumerate(corpus):
            length = sum(counts.values())
            value = sum(math.log(1+(len(rows)-df[t]+.5)/(df[t]+.5))*counts[t]*2.5/(counts[t]+1.5*(.25+.75*length/avg)) for t in terms if counts[t])
            scores.append((i, value))
        settings = self.cfg.get("retrieval", {})
        recall_k = max(top_k, min(200, int(settings.get("recall_k", 50))))
        candidate_k = max(top_k, min(100, int(settings.get("candidate_k", 30))))
        rrf_k = float(settings.get("rrf_k", 60))
        weights = settings.get("weights", {})
        if not math.isfinite(rrf_k) or rrf_k <= 0:
            raise ValueError("rrf_k 必须为正数")
        rankings = {"BM25": sorted([(i,s) for i,s in scores if s>0], key=lambda p:p[1], reverse=True)[:recall_k]}
        if mode == "configured" and configured(self.cfg, "visual_embedding"):
            with self.connect() as db:
                image_vectors = {r["image"]: json.loads(r["vector"]) for r in db.execute("SELECT * FROM image_vectors WHERE signature=?", (visual_signature(self.cfg),))}
            if any(r["image"] in image_vectors for r in rows):
                try:
                    vector = visual_embeddings(self.cfg, [{"text": question}])[0]
                    # One candidate per original picture, even when its description spans chunks.
                    seen, values = set(), []
                    minimum = float(settings.get("visual_min_score", 0.3))
                    for i, row in enumerate(rows):
                        image = row["image"]
                        if image in image_vectors and image not in seen:
                            seen.add(image)
                            score = cosine(vector, image_vectors[image])
                            if score >= minimum:
                                values.append((i, score))
                    rankings["图文向量"] = sorted(values, key=lambda p:p[1], reverse=True)[:recall_k]
                except Exception:
                    notices.append("图文向量调用失败，已使用其余可用检索通道")
            if any(r["image"] and r["image"] not in image_vectors for r in rows):
                notices.append("部分图片尚未建立当前模型的图文向量，请运行 refresh-images")
        if mode != "bm25" and configured(self.cfg, "embedding"):
            if progress:
                progress("retrieval")
            try:
                vector = embeddings(self.cfg, [question])[0]
                values = [(i,cosine(vector,json.loads(r["dense"]))) for i,r in enumerate(rows) if json.loads(r["dense"])]
                minimum = float(settings.get("dense_min_score", -1))
                rankings["dense"] = sorted([(i,s) for i,s in values if s >= minimum], key=lambda p:p[1], reverse=True)[:recall_k]
            except Exception:
                notices.append("Embedding 调用失败，已仅使用其余可用召回通道")
        elif mode != "bm25":
            notices.append("未配置 Embedding：语义向量召回未启用")
        if mode in {"configured","expanded"} and configured(self.cfg,"bge"):
            if progress:
                progress("retrieval")
            try:
                vector = bge_embeddings(self.cfg,[question])[0]
                values = [(i,cosine(vector,json.loads(r["bge_dense"]))) for i,r in enumerate(rows) if json.loads(r["bge_dense"])]
                minimum = float(settings.get("bge_min_score",0.4))
                rankings["BGE-M3-dense"] = sorted([(i,s) for i,s in values if s >= minimum],key=lambda p:p[1],reverse=True)[:recall_k]
            except Exception:
                notices.append("BGE-M3 云端向量调用失败，已使用其他可用通道")
        if mode == "configured" and self.cfg.get("sparse_model_path"):
            try:
                vector = self.sparse([question])[0]
                values = [(i,sum(float(v)*vector.get(k,0) for k,v in json.loads(r["sparse"]).items())) for i,r in enumerate(rows)]
                rankings["BGE-M3"] = sorted([(i,s) for i,s in values if s>0], key=lambda p:p[1], reverse=True)[:recall_k]
            except Exception:
                notices.append("BGE-M3 调用失败，已仅使用其余可用召回通道")
        elif mode == "configured" and not configured(self.cfg,"bge"):
            notices.append("未配置本地 BGE-M3：学习型稀疏召回未启用")
        fused = Counter()
        for name, ranking in rankings.items():
            weight = float(weights.get(name, 1))
            if not math.isfinite(weight) or weight < 0:
                raise ValueError("召回通道权重必须是非负有限数")
            if weight == 0:
                continue
            for rank,(i,_) in enumerate(ranking,1):
                fused[i] += weight/(rrf_k+rank)
        selected = sorted(fused, key=fused.get, reverse=True)[:candidate_k]
        rerank_scores = {}
        reranker_status = {"status":"not_used", "candidates":len(selected)}
        if selected and mode in {"configured", "reranked", "expanded"} and configured(self.cfg, "reranker"):
            if progress:
                progress("rerank")
            rerank_started = time.perf_counter()
            try:
                order = rerank(self.cfg, question, [rows[i]["text"] for i in selected])
                rerank_scores = {selected[r["index"]]: r["relevance_score"] for r in order}
                selected.sort(key=rerank_scores.get, reverse=True)
                reranker_status["status"] = "completed"
            except Exception as exc:
                code = exc.status if isinstance(exc, ModelError) else "invalid_response"
                reasons = {"timeout":"请求超时", "unreachable":"连接失败", "service_unavailable":"平台服务暂不可用", "rate_limited":"平台限流，请稍后重试", "unauthorized":"密钥鉴权失败", "forbidden":"模型访问权限不足", "missing_key":"未配置密钥", "missing_resources":"账户无可用计费资源", "insufficient_balance":"账户余额不足", "not_found":"模型或接口不存在", "invalid_request":"请求参数不兼容", "invalid_response":"返回格式不兼容"}
                notices.append("Reranker 调用失败（" + reasons.get(code, "平台接口异常") + "），已回退到 RRF 排序")
                reranker_status.update(status="fallback", error_code=code)
            finally:
                reranker_status["elapsed_ms"] = (time.perf_counter()-rerank_started)*1000
        elif mode == "configured":
            notices.append("未启用 Reranker：结果按 RRF 融合排序")
        output = []
        for i in selected[:top_k]:
            row = {k:v for k,v in rows[i].items() if k not in {"dense", "sparse", "bge_dense", "fingerprint"}}
            row.update(score=fused[i], rerank_score=rerank_scores.get(i), routes=[name for name,ranking in rankings.items() if any(j==i for j,_ in ranking)])
            row["route_scores"] = {name:next((score for j,score in ranking if j == i),None) for name,ranking in rankings.items()}
            output.append(row)
        return {"results": output, "routes": list(rankings), "warnings": notices, "reranked":bool(rerank_scores), "reranker":reranker_status, "elapsed_ms":(time.perf_counter()-started)*1000, "mode":mode}

    def rebuild_all(self, progress=None):
        reports = []
        documents = self.documents()
        for i, doc in enumerate(documents, 1):
            try:
                result = self.build(doc["source"], doc["category"], force=True)
                reports.append({"name":doc["name"], **result})
            except Exception:
                reports.append({"name":doc["name"], "status":"failed", "error":"文档重建失败，旧索引已保留；请检查原文件和模型服务"})
            if progress:
                progress(i, len(documents), reports[-1])
        return {"total":len(documents), "failed":sum(r["status"] == "failed" for r in reports), "documents":reports}

    def refresh_vectors(self, progress=None):
        """Re-embed existing parsed chunks, preserving text, images and citation IDs."""
        reports = []
        documents = self.documents()
        for index, doc in enumerate(documents,1):
            try:
                with self.connect() as db:
                    rows = db.execute("SELECT id,text FROM chunks WHERE doc_id=? ORDER BY id",(doc["id"],)).fetchall()
                from .ingestion import text_vectors
                vectors = text_vectors(self.cfg,"embedding",[r["text"] for r in rows],embeddings) if configured(self.cfg,"embedding") else [[] for _ in rows]
                if len(vectors) != len(rows):
                    raise ValueError("向量返回数量不一致")
                with self.connect() as db:
                    for row,vector in zip(rows,vectors):
                        db.execute("UPDATE chunks SET dense=? WHERE id=?",(json.dumps(vector),row["id"]))
                    db.execute("UPDATE documents SET fingerprint=? WHERE id=?",(self.fingerprint(),doc["id"]))
                reports.append({"name":doc["name"],"status":"indexed","chunks":len(rows)})
            except Exception:
                reports.append({"name":doc["name"],"status":"failed","error":"向量重建失败，尚未替换正式索引"})
            if progress:
                progress(index,len(documents),reports[-1])
        return {"total":len(documents),"failed":sum(r["status"] == "failed" for r in reports),"documents":reports}

    def evaluate(self, cases, top_k=5):
        reports = []
        for case in cases:
            # Relevant chunk IDs or document IDs are explicit ground truth.
            relevant = set(case["relevant_ids"])
            hits = self.query(case["question"], top_k, case.get("category"))["results"]
            found = {key for hit in hits for key in (hit["id"], hit["doc_id"]) if key in relevant}
            rank = next((i for i,h in enumerate(hits,1) if h["id"] in relevant or h["doc_id"] in relevant), None)
            reports.append({"question": case["question"], "recall": len(found)/len(relevant) if relevant else 0, "hit_rate": int(bool(found)), "mrr": 1/rank if rank else 0})
        return {"count": len(reports), **{k: sum(r[k] for r in reports)/len(reports) if reports else 0 for k in ("recall", "hit_rate", "mrr")}, "cases": reports}

    def refresh_bge_vectors(self, progress=None):
        reports = []
        documents = self.documents()
        for index,doc in enumerate(documents,1):
            try:
                with self.connect() as db:
                    rows = db.execute("SELECT id,text FROM chunks WHERE doc_id=? ORDER BY id",(doc["id"],)).fetchall()
                from .ingestion import text_vectors
                vectors = text_vectors(self.cfg,"bge",[r["text"] for r in rows],bge_embeddings) if configured(self.cfg,"bge") else [[] for _ in rows]
                if len(vectors) != len(rows):
                    raise ValueError("BGE 向量数量不匹配")
                with self.connect() as db:
                    for row,vector in zip(rows,vectors):
                        db.execute("UPDATE chunks SET bge_dense=? WHERE id=?",(json.dumps(vector),row["id"]))
                    db.execute("UPDATE documents SET fingerprint=? WHERE id=?",(self.fingerprint(),doc["id"]))
                reports.append({"name":doc["name"],"status":"indexed","chunks":len(rows)})
            except Exception:
                reports.append({"name":doc["name"],"status":"failed","error":"BGE 向量生成失败，尚未切换正式索引"})
            if progress:
                progress(index,len(documents),reports[-1])
        return {"total":len(documents),"failed":sum(r["status"]=="failed" for r in reports),"documents":reports}
