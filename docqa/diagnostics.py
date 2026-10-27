"""Small opt-in probes with bounded latency and explicit visual validity checks."""
import base64
import copy
import io
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from urllib.parse import urlparse
from .models import configured, endpoint_url, embeddings, bge_embeddings, visual_embeddings, rerank, chat, ModelError


def diagnose(cfg, roles=None):
    roles = list(roles or ("chat", "vision", "embedding", "reranker", "bge", "visual_embedding"))
    gate = BoundedSemaphore(3)
    def invoke(fn, *args):
        with gate:
            return fn(*args)
    def check(role):
        item = {"role":role,"status":"disabled","message":"未配置或已停用，未发送请求"}
        if not configured(cfg, role):
            return item
        candidate = copy.deepcopy(cfg)
        try:
            endpoint = candidate[role]
            # Connection tests should not inherit long answer generation limits or retries.
            endpoint["timeout"] = min(15, endpoint.get("timeout", 15))
            if role in {"chat", "vision"}:
                options = dict(endpoint.get("request_options", {}))
                options["max_tokens"] = 16 if role == "chat" else 64
                if urlparse(endpoint_url(candidate, role)).hostname == "api.deepseek.com":
                    options["thinking"] = {"type":"disabled"}
                endpoint["request_options"] = options
            if role == "reranker":
                endpoint["retries"] = 0
            if role == "visual_embedding":
                from PIL import Image, ImageDraw
                inputs = []
                for color in ("red", "blue"):
                    raw = io.BytesIO()
                    image = Image.new("RGB", (128,128), "white")
                    draw = ImageDraw.Draw(image)
                    if color == "red": draw.ellipse((16,16,112,112), fill=color)
                    else: draw.rectangle((16,16,112,112), fill=color)
                    image.save(raw, format="PNG")
                    inputs.append({"image":base64.b64encode(raw.getvalue()).decode()})
                inputs.append({"text":"red circle"})
                with ThreadPoolExecutor(max_workers=2) as pool:
                    vectors = list(pool.map(lambda value:invoke(visual_embeddings,candidate,[value])[0], inputs))
                if vectors[0] == vectors[1]:
                    raise ModelError("invalid_response", "不同测试图片返回相同向量，图文检索未通过有效性验证")
                if len({len(v) for v in vectors}) != 1:
                    raise ModelError("invalid_response", "图片与文本的向量维度不一致")
                item["dimension"] = len(vectors[0])
            elif role in {"embedding", "bge"}:
                vector = invoke(bge_embeddings if role == "bge" else embeddings, candidate, ["DocQA connection test"])[0]
                item["dimension"] = len(vector)
            elif role == "reranker":
                invoke(rerank,candidate,"connection test",["connection test","unrelated"])
            elif role == "vision":
                from PIL import Image
                raw = io.BytesIO()
                Image.new("RGB",(32,32),"white").save(raw,format="PNG")
                data = "data:image/png;base64," + base64.b64encode(raw.getvalue()).decode()
                invoke(chat,candidate,role,[{"role":"user","content":[{"type":"text","text":"Describe this image in at most eight words."},{"type":"image_url","image_url":{"url":data}}]}])
            else:
                invoke(chat,candidate,role,[{"role":"user","content":"Reply OK."}])
            item.update(status="available",message="该模型接口已通过最小请求验证；仅证明此模型当前可调用")
        except ModelError as exc:
            item.update(status=exc.status,message="连接测试在15秒内未响应，可稍后重试；未修改正式调用超时" if exc.status=="timeout" else str(exc))
        except Exception:
            item.update(status="unavailable",message="配置、服务响应或模型格式不兼容，未启用")
        return item
    with ThreadPoolExecutor(max_workers=3) as pool:
        output = list(pool.map(check,roles))
    return {"models":output,"note":"使用最小输出测试，最多3个请求并发，每个请求超时至多15秒；不修改正式调用参数，不发送用户文档。401通常与鉴权有关，403也可能是权限限制。"}
