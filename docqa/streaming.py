"""Cancellable SSE transport for Chat; closing the upstream socket stops local consumption."""
import http.client
import json
import socket
import threading
from urllib.parse import urlsplit
from .models import _request, ModelError


class GenerationCancelled(RuntimeError):
    pass


class CancelToken:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.interrupt = None

    def check(self):
        if self.event.is_set():
            raise GenerationCancelled("已停止生成")

    def bind(self, interrupt):
        with self.lock:
            self.interrupt = interrupt
            cancelled = self.event.is_set()
        if cancelled:
            interrupt()
            self.check()

    def cancel(self):
        self.event.set()
        with self.lock:
            interrupt = self.interrupt
        if interrupt:
            interrupt()

    def unbind(self):
        with self.lock:
            self.interrupt = None


def stream_chat(cfg, messages, on_delta, cancel):
    cancel.check()
    endpoint, request = _request(cfg, "chat", {"messages":messages,"temperature":0,"stream":True})
    url = urlsplit(request.full_url)
    kind = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = kind(url.hostname, url.port, timeout=endpoint.get("timeout",120))
    transport_socket = None
    response = None
    def interrupt():
        active_socket = transport_socket or connection.sock
        if active_socket and active_socket.fileno() >= 0:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            # A buffered HTTPResponse keeps socket.close() deferred. Detach the
            # actual handle and close it so Windows cancels a blocking recv too.
            try:
                socket.close(active_socket.detach())
            except OSError:
                pass
    fragments = []
    try:
        cancel.check()
        cancel.bind(interrupt)
        connection.connect()
        cancel.check()
        transport_socket = connection.sock
        cancel.bind(interrupt)
        connection.request("POST", url.path + ("?"+url.query if url.query else ""), body=request.data, headers=dict(request.header_items()))
        response = connection.getresponse()
        if response.status != 200:
            codes={401:"unauthorized",402:"insufficient_balance",403:"forbidden",429:"rate_limited"}
            raise ModelError(codes.get(response.status,"http_error"),f"Chat 流式接口返回 HTTP {response.status}")
        if "text/event-stream" not in response.getheader("Content-Type", ""):
            raise ModelError("invalid_response","Chat 接口未返回 SSE 流")
        completed = False
        for line in response:
            cancel.check()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                completed = True
                break
            event = json.loads(data)
            if event.get("error"):
                raise ModelError("invalid_response","Chat 流式接口报告生成失败")
            choices = event.get("choices", [])
            if not choices:
                continue
            text = choices[0].get("delta", {}).get("content")
            if isinstance(text, str) and text:
                fragments.append(text)
                on_delta(text)
        cancel.check()
        if not completed or not fragments:
            raise ModelError("invalid_response","Chat 流提前结束或回答为空")
        return "".join(fragments)
    except GenerationCancelled:
        raise
    except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError):
        cancel.check()
        raise ModelError("unreachable","Chat 流式连接中断或响应不兼容") from None
    finally:
        cancel.unbind()
        if response is not None:
            response.close()
        connection.close()
        if transport_socket is not None:
            transport_socket.close()
