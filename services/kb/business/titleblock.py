"""标题栏字段正则投影（kb v1.5 wedge 裁决卡 1/3：抽取文本 → meta.titleblock → 检索可命中字段行）。

九字段通用模式（图号/名称/材料/数量/比例/重量/幅面/版本/日期）：纯确定性正则、零 LLM、
零写死图号（值一律来自被投影文本，本模块样例/测试用 SAMPLE-* 占位）——符合宪法第 2 条
（确定性高频逻辑不走 LLM）。

两级匹配（同字段首中即得，字段按九字段序输出）：
- 分隔级（全别名）：``label [:：=] value``——值允许 CJK（材料: 铝合金）；
- 紧凑级（中文主标签+英文别名）：``label<空格>value``——值须字母/数字开头（SAMPLE-0001/
  Q235-B/A3/1:50 形态），压制散文误命中（「图号 见附录」不收——值首字符非字母数字）；
  英文别名对齐真实图纸标题栏常见标签：Drawing No/DWG No→图号、Title→名称、Material→
  材料、Qty/Pcs→数量、Scale→比例、Size→幅面、Rev→版本、Date→日期（同为九字段词表，
  不因英文标签扩字段）；
- 比例字段值模式额外允许 ``1:50`` 比值形态（值内冒号）。

已知边界（v1 雏形，候选仍入终审队列——宪法第 3 条兜底）：无分隔紧凑 CJK 值不收
（需版面分析，随 v2）；PDF 文本抽取把相邻文本运行粘连时（如 pypdfium2 同行双 Tj）字段
可能残缺；prose 中「材料: 详见附表」会照收（值形态合法但语义非字段值）；紧凑/分隔级为
全文扫描、无标题栏区域定位，视图区同形态文本（如「比例 1:3」）会误捕——标题栏区域定位
属 v2 版面分析，本批留后续。

span 契约：spans 为「含标签的匹配区间 [start, end)」，指向被投影原文（meta.content），
供 chunk 步额外语义块 span 指回原文（出处指针门禁同源）；投影函数为纯确定性——
preprocess 与 chunk 两处分别调用得到一致结果（不落盘 spans，单一事实源=本函数）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 值字符集：字母/数字/工程符号（- _ . / × ° ± Φ φ Ω）+ CJK；止于空白/分隔符（| , ; : 等）。
_VALUE = r"[0-9A-Za-z_\-./×xX*°±ΦφΩ\u4e00-\u9fff]+"
# 紧凑级值：字母/数字开头（压制散文误命中，见模块头）。
_VALUE_STRICT = r"[0-9A-Za-z][0-9A-Za-z_\-./]*"

# 九字段：字段名 → (分隔级全别名, 紧凑级标签别名=中文主标签+英文别名)。别名内一律非捕获
# 组（捕获组会抢占 value 的 group(1) 编号）。更新此处即扩字段。
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("图号", r"图号|图纸编号|Drawing\s*No\.?|DWG\s*No\.?", r"图号|Drawing\s*No\.?|DWG\s*No\.?"),
    ("名称", r"名称|图名|Part\s*Name|Title", r"名称|Title"),
    ("材料", r"材料|材质|Material", r"材料|Material"),
    ("数量", r"数量|Qty\.?|Quantity|Pcs\.?", r"数量|Qty\.?|Pcs\.?"),
    ("比例", r"比例|Scale", r"比例|Scale"),
    ("重量", r"重量|Weight", r"重量"),
    ("幅面", r"幅面|图幅|Sheet(?:\s*Size)?|Size", r"幅面|图幅|Size"),
    ("版本", r"版本|版次|Rev(?:ision)?|Version", r"版本|版次|Rev\.?"),
    ("日期", r"日期|Date", r"日期|Date"),
)


def _ratio_value() -> tuple[str, str]:
    """比例字段值模式：分隔级/紧凑级都额外允许 ``1:50`` 比值形态（值内冒号）。"""
    return rf"(?:\d+\s*[:：]\s*\d+|{_VALUE})", rf"(?:\d+\s*[:：]\s*\d+|{_VALUE_STRICT})"


def _build_patterns() -> tuple[tuple[str, re.Pattern[str], re.Pattern[str] | None], ...]:
    """(字段名, 分隔级 pattern, 紧凑级 pattern|None) 三元组集（模块级编译一次）。

    两 pattern 前缀定宽左词边界 ``(?<![0-9A-Za-z])``（ocr 评审条目）：英文别名不得作为
    单词子串命中（Update 里的 date / Subtitle 里的 Title / Prev 里的 Rev / Downscale 里的
    Scale / FileSize: 1024 的 Size）——CJK 不在字符类内，中文别名行为不变。
    """
    compiled: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = []
    for name, aliases, compact_label in _FIELDS:
        value, strict = _ratio_value() if name == "比例" else (_VALUE, _VALUE_STRICT)
        compiled.append(
            (
                name,
                re.compile(rf"(?<![0-9A-Za-z])(?:{aliases})\s*[:：=]\s*({value})", re.IGNORECASE),
                re.compile(rf"(?<![0-9A-Za-z])(?:{compact_label})[ \t]+({strict})", re.IGNORECASE),
            )
        )
    return tuple(compiled)


_PATTERNS = _build_patterns()


@dataclass(slots=True)
class TitleblockProjection:
    """投影产物：fields=字段→值（首中即得，按九字段序）；spans=字段→匹配区间（含标签，
    指回被投影原文 [start, end)）。"""

    fields: dict[str, str] = field(default_factory=dict)
    spans: dict[str, tuple[int, int]] = field(default_factory=dict)

    def block_text(self) -> str:
        """序列化为额外语义块文本（``字段: 值`` 行集，chunk 步直接入库可检索）。"""
        return "\n".join(f"{name}: {value}" for name, value in self.fields.items())

    def block_span(self) -> tuple[int, int] | None:
        """额外语义块的原文出处指针：最早命中字段的 [start, end)（无命中为 None）。"""
        if not self.spans:
            return None
        first = min(self.spans, key=lambda name: self.spans[name][0])
        return self.spans[first]


def project_titleblock(text: str) -> TitleblockProjection:
    """对文本跑九字段标题栏投影（纯确定性；preprocess 与 chunk 共用同一事实源）。"""
    projection = TitleblockProjection()
    for name, pattern, compact in _PATTERNS:
        match = pattern.search(text)
        if match is None and compact is not None:
            match = compact.search(text)
        if match is not None:
            projection.fields[name] = match.group(1).strip()
            projection.spans[name] = (match.start(), match.end())
    return projection
