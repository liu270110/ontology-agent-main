"""HTML→正文抽取（标准库 html.parser 手写，零新依赖；docs/Agent/06 #3 + 研究整理/08 C3）。

取舍（hermes web_extract 思路的最小够用版，设计宪法 4）：

- 剔除 script/style/noscript/template/svg 内容与注释（注入面最大头）；
- 块级标签落换行边界、行内标签只留文本；convert_charrefs=True 由解析器解实体；
- 空白行折叠为单换行；``<title>`` 单独产出（供 B3 标界元数据）。
- **不做** readability 级正文/导航智能剔除 → P1；抓取正文本就 B3 不可信输入，
  模型侧还有截断与审核队列兜底，此处过度加工收益低。
"""

from __future__ import annotations

from html.parser import HTMLParser

# 内容整体剔除的标签（含子孙文本）
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
# 块级标签：出现即落换行边界（闭合再落一次，防行内粘连）
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)


class _TextExtractor(HTMLParser):
    """流式抽取器：skip 深度计数 + title 捕获 + 文本分段。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._parts: list[str] = []
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += " ".join(data.split())
            return
        self._parts.append(data)

    def text(self) -> str:
        lines = ("".join(self._parts)).splitlines()
        collapsed = (" ".join(line.split()) for line in lines)
        return "\n".join(line for line in collapsed if line)


def extract_html_text(html: str) -> tuple[str, str]:
    """HTML → (正文, title)；畸形输入降级为原样文本（不可信输入禁异常逃逸）。"""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # noqa: BLE001 —— 恶意/畸形 HTML 降级不中断（B3：正文本就不可信）
        return html, ""
    return extractor.text(), extractor.title
