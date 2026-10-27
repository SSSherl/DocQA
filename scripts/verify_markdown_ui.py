"""Check Markdown presentation with a synthetic response, without invoking cloud models."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from docqa.markdown_view import format_answer


def main():
    markdown = """**FedEAT 是面向联邦大语言模型的鲁棒性优化框架**，以下为排版演示。[1]

## 1. 核心机制

- **客户端侧**：进行本地训练与更新。
- **服务端侧**：聚合模型并检查更新。

## 2. 模块对比

| 模块 | 作用 |
| --- | --- |
| Embedding | 语义检索 |
| Agent | 综合回答 |

> 本页面仅用于验证 Markdown 展示效果。

使用 `FE_HEART_Start` 表示接口名。

```python
print("带格式的文档回答")
```
"""
    response = format_answer({"answer":markdown,"mode":"model","warnings":[],"routes":["BM25","dense"],"rounds_used":1,"citations":[{"number":1,"name":"排版演示.md","page":None,"title":"排版演示","figure":"","image":"","url":"/api/source/demo","excerpt":"# 原文片段\n\n**来源也支持加粗**。"}]})
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",headless=True)
        page = browser.new_page(viewport={"width":1440,"height":1100},device_scale_factor=1)
        page.route("**/api/ask",lambda route:route.fulfill(status=200,content_type="application/json",body=json.dumps(response,ensure_ascii=False)))
        page.goto("http://127.0.0.1:8765",wait_until="networkidle")
        page.locator("#question").fill("FedEAT 有什么作用？")
        page.locator("#ask-button").click()
        page.locator(".answer h2").first.wait_for()
        assert page.locator(".answer strong").count() == 3
        assert page.locator(".answer table").count() == 1
        assert page.locator(".answer pre code").count() == 1
        assert page.locator(".answer blockquote").count() == 1
        page.get_by_role("button",name="查看 Markdown 原文").click()
        assert page.locator(".answer-source").is_visible()
        page.get_by_role("button",name="返回排版视图").click()
        page.locator(".citation summary").click()
        assert page.locator(".citation strong").inner_text() == "来源也支持加粗"
        Path("docs").mkdir(exist_ok=True)
        page.screenshot(path="docs/markdown-preview.png",full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path="docs/markdown-preview-mobile.png",full_page=True)
        browser.close()
        print("Markdown browser checks passed: bold, headings, lists, table, code, quote, source toggle, source excerpts, mobile width.")


if __name__ == "__main__":
    main()
