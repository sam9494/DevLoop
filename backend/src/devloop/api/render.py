"""報告內文的 markdown 渲染。

`html=False` 是刻意的：內文是 LLM 產的，不讓它直接注入原始 HTML。
"""

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")


def to_html(markdown: str) -> str:
    html: str = _md.render(markdown or "")
    return html
