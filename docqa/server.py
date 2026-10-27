import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
import time
import uuid
import re
from urllib.parse import urlparse, parse_qs, unquote
from .agent import answer_question
from .doc2md import SUPPORTED
from .service import Watcher
from .models import configured
from .markdown_view import format_answer, render_markdown
from .streaming import CancelToken, GenerationCancelled


def serve(base, port=8765, config_path="config.json"):
    from .conversations import Conversations
    conversations = Conversations(base.root)
    lock = threading.Lock()
    job_lock = threading.Lock()
    active_requests = {}
    cancel_lock = threading.Lock()
    upgrade = {"status":"idle", "completed":0, "total":0}
    from .import_jobs import ImportJobs
    imports = ImportJobs(base)
    def configure_models(settings, test_only=False):
        from .model_settings import apply_settings
        def progress(done, total, result):
            with job_lock:
                upgrade.update(completed=done, total=total, last_document=result["name"])
        try:
            with lock:
                result = apply_settings(base, settings, config_path, progress, test_only)
            with job_lock:
                upgrade.update(status="complete" if result["status"] in {"activated", "tested"} else "failed", result=result)
        except ValueError as exc:
            with job_lock:
                upgrade.update(status="failed", error=str(exc))
        except Exception:
            with job_lock:
                upgrade.update(status="failed", error="操作失败，请检查接口、密钥或文件权限；原配置保持不变")
    def activate(settings):
        from .retrieval_setup import prepare_upgrade
        def progress(done,total,result):
            with job_lock:
                upgrade.update(completed=done,total=total,last_document=result["name"])
        try:
            with lock:
                result = prepare_upgrade(base,settings,config_path,progress)
            with job_lock:
                upgrade.update(status="complete" if result["status"] == "activated" else "failed",result=result)
        except Exception:
            with job_lock:
                upgrade.update(status="failed",error="升级失败，请检查接口配置与本机凭证；原有服务仍可使用")
    watcher = None
    if base.cfg.get("watch_dir"):
        watcher = Watcher(base)
        watcher.build_lock = lock
        def watch():
            try:
                watcher.run()
            except Exception as exc:
                watcher.events.append({"status":"error","error":str(exc)})
        threading.Thread(target=watch, daemon=True).start()
    class Handler(BaseHTTPRequestHandler):
        def send(self, code, data, mime="application/json; charset=utf-8", headers=None):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for k,v in (headers or {}).items():
                self.send_header(k,v)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (ConnectionError, OSError):
                # The job and committed index outlive a closed browser connection.
                pass

        def allowed(self):
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in {f"http://{h}" for h in hosts}

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error":"仅允许本地同源访问"})
            route = urlparse(self.path).path
            try:
                if route == "/api/import/status":
                    with job_lock:
                        state = imports.status()
                    return self.send(200, state)
                if route == "/api/health":
                    return self.send(200, {"status":"online", "import_status":imports.status()["status"]})
                if route == "/":
                    return self.send(200, Path(__file__).with_name("web").joinpath("index.html").read_bytes(), "text/html; charset=utf-8")
                if route == "/vendor/mermaid.min.js":
                    return self.send(200, Path(__file__).with_name("web").joinpath("vendor/mermaid.min.js").read_bytes(), "text/javascript; charset=utf-8")
                if route == "/api/conversations":
                    return self.send(200, {"conversations":conversations.list()})
                if route.startswith("/api/conversations/"):
                    item = conversations.get(route.rsplit("/",1)[-1])
                    for turn in item["turns"]:
                        turn["result"] = format_answer(turn["result"])
                    return self.send(200, item)
                if route.startswith("/api/pdf/") or route.startswith("/api/pdf-page/"):
                    from .pdf_view import locate, render_page
                    doc_id = route.rsplit("/",1)[-1]
                    query = parse_qs(urlparse(self.path).query)
                    number = int(query.get("page",["1"])[0])
                    if route.startswith("/api/pdf-page/"):
                        return self.send(200, render_page(base,doc_id,number), "image/png")
                    return self.send(200, locate(base,doc_id,number,query.get("chunk_id",[""])[0]))
                if route == "/api/status":
                    documents = base.documents()
                    from .visual_index import status
                    visual_status = status(base)
                    return self.send(200, {"visual_embedding":visual_status, "documents":documents, "chunks":sum(d["chunks"] for d in documents), "models":{k:configured(base.cfg,k) for k in ("chat","vision","embedding","reranker")}, "bge_dense":configured(base.cfg,"bge"), "bge_mode":"cloud_dense" if configured(base.cfg,"bge") else "local_sparse" if base.cfg.get("sparse_model_path") else "disabled", "remote_models_allowed":bool(base.cfg.get("allow_remote_models",False)), "sparse":bool(base.cfg.get("sparse_model_path")), "events":watcher.events if watcher else []})
                if route == "/api/model-settings":
                    from .model_settings import public_settings
                    return self.send(200, public_settings(base.cfg))
                if route == "/api/model-settings/job":
                    with job_lock:
                        return self.send(200, dict(upgrade))
                if route == "/api/retrieval/upgrade":
                    with job_lock:
                        return self.send(200,dict(upgrade))
                if route.startswith("/api/source/"):
                    doc_id = route.rsplit("/",1)[-1]
                    doc = next((d for d in base.documents() if d["id"] == doc_id), None)
                    if not doc:
                        return self.send(404, {"error":"文档不存在"})
                    source = Path(doc["source"])
                    # Active formats are downloaded, never executed within the app origin.
                    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
                    return self.send(200, source.read_bytes(), mime, {"Content-Disposition":"attachment"})
                if route.startswith("/api/assets/"):
                    target = (base.root / unquote(route[len("/api/assets/"):])).resolve()
                    if not target.is_relative_to(base.root / "parsed") or target.suffix != ".png":
                        return self.send(403, {"error":"无效资源路径"})
                    return self.send(200, target.read_bytes(), "image/png")
                self.send(404, {"error":"路径不存在"})
            except Exception as exc:
                self.send(400, {"error":str(exc)})

        def do_POST(self):
            if not self.allowed():
                return self.send(403, {"error":"仅允许本地同源访问"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415, {"error":"仅接受 application/json"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 40*1024*1024:
                    return self.send(413, {"error":"请求最大40MB，文件建议小于25MB"})
                payload = json.loads(self.rfile.read(length))
                route = urlparse(self.path).path
                if route == "/api/model-settings/reveal":
                    from .credentials import read_secret
                    from .model_settings import ROLES
                    role = payload.get("role") if isinstance(payload, dict) else None
                    if not isinstance(role, str) or role not in ROLES:
                        return self.send(400, {"error":"未知模型通道"})
                    try:
                        with lock:
                            key = read_secret(base.cfg.get(role, {}))
                    except Exception:
                        return self.send(400, {"error":"无法读取本机凭证，请重新填写密钥"})
                    return self.send(200, {"key":key})
                if route in {"/api/model-settings/save", "/api/model-settings/test"}:
                    if not isinstance(payload, dict):
                        raise ValueError("设置必须为 JSON 对象")
                    with job_lock:
                        if upgrade["status"] == "running" or imports.status()["status"] == "running":
                            return self.send(409, {"error":"已有配置任务进行中"})
                        upgrade.clear()
                        upgrade.update(status="running", completed=0, total=0)
                    threading.Thread(target=configure_models, args=(payload, route.endswith("/test")), daemon=True).start()
                    return self.send(202, {"status":"running"})
                if route == "/api/retrieval/setup":
                    if not isinstance(payload,dict):
                        raise ValueError("设置必须为 JSON 对象")
                    with job_lock:
                        if upgrade["status"] == "running" or imports.status()["status"] == "running":
                            return self.send(409,{"error":"已有重建任务进行中"})
                        upgrade.clear()
                        upgrade.update(status="running",completed=0,total=0)
                    threading.Thread(target=activate,args=(payload,),daemon=True).start()
                    return self.send(202,{"status":"running"})
                if route == "/api/retrieval/credential":
                    from .credentials import save_secret
                    name = "retrieval-" + uuid.uuid4().hex
                    value = str(payload.get("key", "")).strip()
                    if not value or len(value)>1024:
                        raise ValueError("请填写有效的服务密钥")
                    save_secret(name,value)
                    return self.send(200,{"status":"stored","name":name})
                if route == "/api/benchmark":
                    from .benchmark import compare
                    cases = payload.get("cases",[])
                    if not isinstance(cases,list) or not 1 <= len(cases) <= 100:
                        raise ValueError("评测样例数量必须介于1到100之间")
                    with lock:
                        return self.send(200,compare(base,cases,int(payload.get("top_k",5))))
                if route == "/api/diagnose":
                    from .diagnostics import diagnose
                    return self.send(200, diagnose(base.cfg))
                if route == "/api/upload":
                    name = payload["name"]
                    if Path(name).name != name or any(c in name for c in '/\\:') or name in {".",".."}:
                        raise ValueError("无效文件名")
                    if Path(name).suffix.lower() not in SUPPORTED:
                        raise ValueError("不支持该文件格式")
                    raw = base64.b64decode(payload["content"], validate=True)
                    if len(raw) > 25*1024*1024:
                        raise ValueError("文件最大25MB")
                    category = payload.get("category") or "默认"
                    with job_lock:
                        if upgrade["status"] == "running" or imports.status()["status"] == "running":
                            return self.send(409, {"error":"已有导入或配置任务，请等待当前任务完成"})
                        result = imports.start(name, raw, category)
                    if payload.get("background") is True:
                        return self.send(202, result)
                    while True:
                        with job_lock:
                            state = imports.status()
                        if state["status"] != "running":
                            if state["status"] == "failed":
                                raise ValueError(state["error"])
                            return self.send(200, state["result"])
                        time.sleep(0.1)
                if route == "/api/import/retry":
                    with job_lock:
                        if upgrade["status"] == "running":
                            return self.send(409, {"error":"已有配置任务进行中"})
                        result = imports.retry()
                    return self.send(202, result)
                if route == "/api/ask/cancel":
                    with cancel_lock:
                        token = active_requests.get(str(payload.get("request_id", "")))
                    if token:
                        token.cancel()
                    return self.send(200, {"cancelled":bool(token)})
                if route == "/api/conversations/delete":
                    with lock:
                        conversations.delete(str(payload.get("id", "")))
                    return self.send(200, {"status":"deleted"})
                if route in {"/api/ask", "/api/query"}:
                    question = str(payload.get("question", ""))[:8000]
                    if not question.strip():
                        raise ValueError("问题不能为空")
                    category = payload.get("category") or None
                    count = max(1,min(20,int(payload.get("top_k",5))))
                    if route.endswith("query"):
                        with lock:
                            return self.send(200, base.query(question,count,category))
                    conversation_id = payload.get("conversation_id") or None
                    if conversation_id is not None and not isinstance(conversation_id,str):
                        raise ValueError("会话编号格式无效")
                    # Resolve history again under the same lock used to append turns.
                    if conversation_id:
                        conversations.get(conversation_id)
                    streaming = payload.get("stream") is True
                    request_id = payload.get("request_id") or uuid.uuid4().hex
                    if not isinstance(request_id,str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,80}",request_id):
                        raise ValueError("请求编号格式无效")
                    token = CancelToken()
                    with cancel_lock:
                        if request_id in active_requests:
                            return self.send(409,{"error":"请求编号重复"})
                        active_requests[request_id] = token
                    partial = []
                    evidence = []
                    def stage(value):
                        token.check()
                        event({"type":"stage", "stage":value})
                    def delta(text):
                        token.check()
                        partial.append(text)
                        event({"type":"delta","text":text,"html":render_markdown("".join(partial))})
                    def sources(items):
                        evidence[:] = items
                        event({"type":"sources","citations":format_answer({"answer":"","citations":items})["citations"]})
                    disconnected = False
                    def event(data):
                        nonlocal disconnected
                        if disconnected:
                            return
                        try:
                            self.wfile.write((json.dumps(data,ensure_ascii=False)+"\n").encode())
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            disconnected = True
                            token.cancel()
                    if streaming:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        event({"type":"stage", "stage":"queued"})
                    try:
                        with lock:
                            token.check()
                            history = conversations.get(conversation_id)["turns"] if conversation_id else []
                            retry_turn = payload.get("retry_turn_id")
                            if retry_turn is not None:
                                index = next((i for i,t in enumerate(history) if str(t["id"])==str(retry_turn)),None)
                                if index is None:
                                    raise ValueError("重新回答的历史问题不存在")
                                history = history[:index]
                            result = answer_question(base,question,count,category,history=history,progress=stage if streaming else None,on_delta=delta if streaming else None,on_evidence=sources if streaming else None,cancel=token)
                            token.check()
                            result["request"] = {"question":question,"category":category,"top_k":count}
                            result.update(conversations.append(conversation_id,question,result))
                            result = format_answer(result)
                        if streaming:
                            event({"type":"result", "result":result})
                            return
                        return self.send(200, result)
                    except GenerationCancelled:
                        result = {"answer":"".join(partial) or "已停止，尚未生成回答。","mode":"interrupted","citations":evidence,"routes":[],"rounds_used":1,"warnings":["回答已停止，内容未完成且尚未通过引用检查。"],"request":{"question":question,"category":category,"top_k":count}}
                        with lock:
                            result.update(conversations.append(conversation_id,question,result))
                        result = format_answer(result)
                        if streaming:
                            event({"type":"result","result":result})
                            return
                        return self.send(200,result)
                    except Exception:
                        if streaming:
                            event({"type":"error", "error":"处理问题失败，请检查模型服务或索引后重试。"})
                            return
                        raise
                    finally:
                        with cancel_lock:
                            active_requests.pop(request_id,None)
                if route == "/api/delete":
                    with job_lock:
                        if imports.status()["status"] == "running":
                            return self.send(409, {"error":"导入期间请等待完成后再移除索引"})
                    with lock:
                        base.delete(payload["id"])
                    return self.send(200, {"status":"deleted"})
                return self.send(404, {"error":"路径不存在"})
            except Exception as exc:
                self.send(400, {"error":str(exc)})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"DocQA 已启动：http://127.0.0.1:{port}（Ctrl+C 停止）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if watcher:
            watcher.stop.set()
        httpd.server_close()
