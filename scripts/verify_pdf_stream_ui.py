"""Local end-to-end PDF preview, token display, stop and regenerate; no paid calls."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import pymupdf
from playwright.sync_api import sync_playwright
from docqa.docbase import DocBase
import docqa.server as module
import docqa.agent as agent
from http.server import ThreadingHTTPServer


def main():
    with tempfile.TemporaryDirectory(prefix="docqa-pdf-ui-") as temporary:
        root=Path(temporary)
        path=root/"source.pdf"
        pdf=pymupdf.open()
        pdf.new_page().insert_text((70,100),"Client training workflow and server aggregation evidence.")
        pdf.new_page().insert_text((70,100),"Another page of the original document.")
        pdf.save(path)
        pdf.close()
        base=DocBase({"data_dir":str(root/"data"),"chat":{"url":"http://localhost/chat"}})
        base.build(path)
        requests=[]
        def stream(cfg,messages,on_delta,cancel):
            requests.append(json.loads(messages[-1]["content"]))
            fragments=["**Client ","training** ","workflow ","uses ","the ","original ","document. ","[1]\n"]
            if len(requests)==1:fragments*=8
            for fragment in fragments:
                cancel.check()
                on_delta(fragment)
                time.sleep(.08)
            return "".join(fragments)
        agent.stream_chat=stream
        with socket.socket() as probe:
            probe.bind(("127.0.0.1",0))
            port=probe.getsockname()[1]
        servers=[]
        def factory(*args):
            server=ThreadingHTTPServer(*args);servers.append(server);return server
        module.ThreadingHTTPServer=factory
        worker=threading.Thread(target=module.serve,args=(base,port),daemon=True);worker.start()
        for _ in range(100):
            if servers:break
            time.sleep(.01)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",headless=True)
                page=browser.new_page(viewport={"width":1440,"height":1000});errors=[]
                page.on("pageerror",lambda e:errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}",wait_until="networkidle")
                page.locator("#question").fill("client training workflow")
                page.locator("#ask-button").click()
                page.locator("#live-answer").wait_for()
                assert page.locator("#ask-button").is_disabled()
                page.locator("#question").fill("next draft")
                began=time.perf_counter()
                page.locator("#stop-generation").click()
                page.wait_for_function("document.querySelector('.badge')?.textContent==='已停止 · 未完成'")
                assert time.perf_counter()-began<2
                assert page.locator("#question").input_value()=="next draft"
                assert page.locator(".answer").inner_text()
                page.get_by_role("button",name="重新回答",exact=True).click()
                page.wait_for_function("document.querySelector('.badge')?.textContent==='模型综合'")
                page.wait_for_function("document.querySelector('#turn-navigation')?.textContent.includes('2 个问题')")
                assert requests[0]["question"]==requests[1]["question"]
                assert requests[1]["untrusted_history"]==[]
                assert page.locator("#question").input_value()=="next draft"
                assert page.locator(".answer strong").inner_text()=="Client training"
                page.get_by_role("button",name="查看来源 1",exact=True).first.click()
                page.wait_for_function("document.querySelector('#pdf-paper img').naturalWidth>0 && document.querySelector('#pdf-paper img').style.visibility==='visible'")
                assert page.locator("#pdf-dialog").is_visible()
                assert page.locator("#pdf-highlights span").count()>0
                assert page.get_by_label("PDF 页码",exact=True).input_value()=="1"
                page.screenshot(path="docs/pdf-citation-preview.png")
                page.get_by_role("button",name="下一页",exact=True).click()
                page.wait_for_function("document.querySelector('#pdf-dialog input').value==='2' && document.querySelector('#pdf-paper img').style.visibility==='visible'")
                assert page.locator("#pdf-highlights span").count()==0
                page.keyboard.press("Escape")
                assert not page.locator("#pdf-dialog").is_visible()
                page.set_viewport_size({"width":390,"height":844})
                assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
                assert not errors,errors
                browser.close()
        finally:
            servers[0].shutdown();servers[0].server_close();worker.join(5)
    print("PASS: live Markdown, stop/partial history, draft preservation, regenerate context, direct PDF/highlight/page turn, mobile")


if __name__=="__main__":main()
