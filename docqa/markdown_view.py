"""Render document text as a sanitized HTML fragment, keeping raw Markdown available."""
from markdown_it import MarkdownIt
import nh3


def render_markdown(text):
    parser = MarkdownIt("js-default", {"html":False, "breaks":True})
    # Model-supplied remote images must not trigger third-party requests on page load.
    parser.disable("image")
    html = parser.render(text)
    return nh3.clean(
        html,
        tags={"p","br","h1","h2","h3","h4","h5","h6","strong","em","s","del","blockquote","ul","ol","li","hr","pre","code","table","thead","tbody","tr","th","td","a"},
        attributes={"a":{"href","title"},"ol":{"start"},"code":{"class"}},
        url_schemes={"http","https","mailto"},
        link_rel="noopener noreferrer nofollow",
        set_tag_attribute_values={"a":{"target":"_blank"}},
    )


def format_answer(result):
    formatted = dict(result, answer_html=render_markdown(result["answer"]))
    formatted["citations"] = [dict(c, excerpt_html=render_markdown(c["excerpt"])) for c in result["citations"]]
    return formatted
