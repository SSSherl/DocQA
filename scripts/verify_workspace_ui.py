"""Verify workspace interactions using a local response; no cloud model calls."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from docqa.markdown_view import format_answer


def main():
    response_path = Path("data/visual-answer.json")
    if response_path.exists():
        result = json.loads(response_path.read_text(encoding="utf8"))
    else:
        result = {"answer": "## Answer\n\n**Document evidence** [1]", "mode": "extractive", "warnings": [], "routes": ["BM25"], "rounds_used": 1, "citations": [{"number": 1, "name": "example.md", "page": None, "title": "Example", "figure": "", "image": "", "url": "/api/source/example", "excerpt": "Evidence"}]}
    result["warnings"] = []
    response = format_answer(result)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe", headless=True)
        page = browser.new_page(viewport={"width": 1520, "height": 1000})
        errors, pending = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/api/ask", lambda route: pending.append(route))
        page.goto("http://127.0.0.1:8765", wait_until="networkidle")
        page.locator("#question").fill("   ")
        page.locator("#ask-button").click()
        assert not pending
        page.locator("#question").fill("Find the document diagram")
        page.locator("#question").press("Enter")
        page.wait_for_timeout(100)
        assert len(pending) == 1 and page.locator("#ask-button").is_disabled()
        assert page.locator("#question").input_value() == ""
        page.locator("#question").fill("draft")
        page.locator("#question").press("Control+Enter")
        page.locator("#question").type("second line")
        pending[0].fulfill(status=200, content_type="application/json", body=json.dumps(response, ensure_ascii=False))
        page.locator(".ref-link").first.wait_for()
        assert page.locator("#question").input_value() == "draft\nsecond line"
        number = next((c["number"] for c in result["citations"] if c["image"]), result["citations"][0]["number"])
        page.get_by_role("button", name="查看来源 " + str(number), exact=True).first.click()
        source = page.locator("#citation-" + str(number))
        assert source.evaluate("e=>e.open")
        if page.locator("#pdf-dialog").is_visible():
            page.keyboard.press("Escape")
        before = page.locator("#ask-form").bounding_box()["y"]
        page.locator("#conversation").evaluate("e=>{e.style.scrollBehavior='auto';e.scrollTop=e.scrollHeight}")
        assert abs(page.locator("#ask-form").bounding_box()["y"] - before) < 1
        page.locator("#conversation").evaluate("e=>e.scrollTop=0")
        page.wait_for_timeout(300)
        page.screenshot(path="docs/ui-workspace-desktop.png", full_page=True)
        if source.locator(".image-preview").count():
            source.locator(".image-preview").click()
            page.wait_for_function("document.querySelector('#zoom-image').naturalWidth>0")
            width = page.locator("#zoom-image").bounding_box()["width"]
            page.get_by_role("button", name="放大图片", exact=True).click()
            assert page.locator("#zoom-image").bounding_box()["width"] > width
            page.screenshot(path="docs/ui-image-viewer.png")
            page.keyboard.press("Escape")
            assert not page.locator("#image-dialog").is_visible()
            assert source.locator(".image-preview").evaluate("e=>e===document.activeElement")
        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate("window.scrollTo(0,0)")
        page.locator("#conversation").evaluate("e=>e.scrollTop=0")
        page.wait_for_timeout(200)
        assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
        page.screenshot(path="docs/ui-workspace-mobile.png", full_page=True)
        page.get_by_role("button", name="服务状态", exact=False).click()
        assert page.locator("#settings").is_visible()
        assert not errors, errors
        browser.close()
    print("Workspace browser checks passed: references, image zoom, keyboard, drafts, fixed composer, mobile, settings.")


if __name__ == "__main__":
    main()
