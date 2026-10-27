from docqa.markdown_view import render_markdown, format_answer


def test_headings_bold_lists_tables_and_code_are_rendered():
    source = "## 核心机制\n\n**加粗结论** 与 *斜体*。\n\n- 第一项\n- 第二项\n\n1. 导入\n2. 检索\n\n| 模块 | 功能 |\n| --- | --- |\n| Agent | 问答 |\n\n> 原文引用\n\n`FE_HEART_Start`\n\n```python\nprint('hello')\n```\n\n---\n\n~~旧内容~~"
    html = render_markdown(source)
    for expected in ("<h2>核心机制</h2>","<strong>加粗结论</strong>","<em>斜体</em>","<ul>","<ol>","<table>","<th>模块</th>","<blockquote>","<code>FE_HEART_Start</code>",'<code class="language-python">',"<hr>","<s>旧内容</s>"):
        assert expected in html


def test_raw_html_and_dangerous_urls_cannot_execute():
    source = '<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">\n\n[危险](javascript:alert%281%29)\n\n[安全](https://example.com "示例")\n\n![跟踪图](https://evil.example/tracking.png)'
    html = render_markdown(source)
    assert "<script" not in html and "<img" not in html
    assert 'href="javascript:' not in html
    assert 'href="https://example.com"' in html
    assert 'rel="noopener noreferrer nofollow"' in html
    assert 'target="_blank"' in html


def test_code_and_citation_numbers_remain_literal():
    source = '结论见 [1][2]。\n\n```html\n<script>alert(1)</script>\n```'
    html = render_markdown(source)
    assert "[1][2]" in html
    assert "&lt;script&gt;" in html and "<script>" not in html


def test_format_answer_keeps_original_markdown_and_formats_sources():
    result = {"answer":"**结论** [1]", "mode":"model", "citations":[{"number":1,"excerpt":"# 来源\n\n**原文**"}]}
    formatted = format_answer(result)
    assert formatted["answer"] == result["answer"]
    assert formatted["answer_html"].startswith("<p><strong>结论</strong>")
    assert "<h1>来源</h1>" in formatted["citations"][0]["excerpt_html"]
    assert "answer_html" not in result


def test_escaped_and_obfuscated_protocols_are_not_allowed():
    for text in ('[x](javascript:alert(1))','[x](data:text/html;base64,PHNjcmlwdD4=)','[x](vbscript:test)','<iframe src="https://example.com"></iframe>'):
        html = render_markdown(text)
        assert '<iframe' not in html
        assert 'href="javascript:' not in html and 'href="data:' not in html and 'href="vbscript:' not in html
