"""Exercise real local history/stream endpoints with model calls replaced locally."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from playwright.sync_api import sync_playwright
from docqa.docbase import DocBase
import docqa.server as server_module
import docqa.agent as agent_module
import docqa.docbase as docbase_module
from http.server import ThreadingHTTPServer


def main():
    with tempfile.TemporaryDirectory(prefix="docqa-ui-") as temporary:
        root = Path(temporary)
        base = DocBase({"data_dir": str(root / "data"), "chat": {"url":"http://localhost/test"}, "reranker":{"url":"http://localhost/rerank"}})
        document = root / "example.md"
        document.write_text("# FedEAT\nFedEAT is a robustness framework. Client training and server aggregation.", encoding="utf8")
        base.build(document)
        def chat(cfg, role, messages):
            time.sleep(.25)
            return "## Framework\n\n**Client and server workflow** [1]\n\n```mermaid\nflowchart LR\n A[Client] --> B[Server]\n B --> C[Aggregate]\n```\n\nInvalid diagrams retain their code:\n\n```mermaid\nnot a diagram at all\n```"
        def rerank(cfg, question, documents):
            time.sleep(.15)
            return [{"index":i,"relevance_score":1.} for i in range(len(documents))]
        agent_module.chat = chat
        def stream(cfg,messages,on_delta,cancel):
            text=chat(cfg,"chat",messages)
            on_delta(text)
            return text
        agent_module.stream_chat=stream
        docbase_module.rerank = rerank
        with socket.socket() as probe:
            probe.bind(("127.0.0.1",0))
            port=probe.getsockname()[1]
        servers=[]
        def factory(*args):
            server=ThreadingHTTPServer(*args)
            servers.append(server)
            return server
        server_module.ThreadingHTTPServer=factory
        worker=threading.Thread(target=server_module.serve,args=(base,port),daemon=True)
        worker.start()
        for _ in range(100):
            if servers: break
            time.sleep(.01)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",headless=True)
                page=browser.new_page(viewport={"width":1520,"height":1000})
                errors,external=[],[]
                page.on("pageerror",lambda e:errors.append(str(e)))
                page.on("request",lambda r:external.append(r.url) if not r.url.startswith((f"http://127.0.0.1:{port}",f"blob:http://127.0.0.1:{port}")) else None)
                page.goto(f"http://127.0.0.1:{port}",wait_until="networkidle")
                page.locator("#question").fill("FedEAT workflow")
                page.locator("#question").press("Enter")
                page.locator(".busy").wait_for()
                page.locator(".diagram-view svg").wait_for()
                page.wait_for_function("document.querySelector('#turn-navigation')!==null")
                assert "Client" in page.locator(".diagram-view svg").first.text_content()
                assert "Server" in page.locator(".diagram-view svg").first.text_content()
                assert "图表暂未渲染" in page.locator("#conversation").inner_text()
                diagram=page.locator(".diagram-card").first
                diagram.get_by_role("button",name="查看代码",exact=True).click()
                assert diagram.locator("pre").is_visible()
                diagram.get_by_role("button",name="返回图形",exact=True).click()
                with page.expect_download() as download:
                    diagram.get_by_role("button",name="下载 SVG",exact=True).click()
                assert download.value.suggested_filename.endswith(".svg")
                diagram.get_by_role("button",name="放大",exact=True).click()
                assert page.locator("#image-dialog").is_visible()
                page.keyboard.press("Escape")
                page.locator("#question").fill("继续解释它")
                page.locator("#ask-button").click()
                page.wait_for_function("document.querySelector('#turn-navigation')?.textContent.includes('2 个问题')")
                assert page.locator("#history-list .history-row").count()==1
                page.locator("#turn-navigation select").select_option(index=0)
                assert page.locator(".question").inner_text()=="FedEAT workflow"
                page.reload(wait_until="networkidle")
                page.locator(".history-entry").first.click()
                page.wait_for_function("document.querySelector('#turn-navigation')?.textContent.includes('2 个问题')")
                assert page.locator(".question").inner_text()=="继续解释它"
                page.locator(".diagram-view svg").first.wait_for()
                page.screenshot(path="docs/conversation-diagram-preview.png",full_page=True)
                page.set_viewport_size({"width":390,"height":844})
                assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
                page.once("dialog",lambda d:d.accept())
                page.locator(".history-delete").first.click()
                page.wait_for_function("document.querySelectorAll('.history-row').length===0")
                assert len(base.documents())==1
                assert not external, external
                assert not errors, errors
                browser.close()
        finally:
            servers[0].shutdown()
            servers[0].server_close()
            worker.join(5)
    print("PASS: streaming, diagrams/source/fallback/export/zoom, history follow-up/restore/delete, mobile, no external calls")


if __name__ == "__main__":
    main()
