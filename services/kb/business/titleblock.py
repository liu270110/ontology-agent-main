"""标题栏字段正则投影（kb v1.5 wedge 裁决卡 1/3：抽取文本 → meta.titleblock → 检索可命中字段行）。

九字段通用模式（图号/名称/材料/数量/比例/重量/幅面/版本/日期）：纯确定性正则、零 LLM、
零写死图号（值一律来自被投影文本，本模块样例/测试用 SAMPLE-* 占位）——符合宪法第 2 条
（确定性高频逻辑不走 LLM）。

三级匹配（同字段首中即得，字段按九字段序输出）：
- 分隔级（全别名）：``label [:：=] value``——值允许 CJK（材料: 铝合金）；
- 紧凑级（中文主标签+英文别名）：``label<空格>value``——值须字母/数字开头（SAMPLE-0001/
  Q235-B/A3/1:50 形态），压制散文误命中（「图号 见附录」不收——值首字符非字母数字）；
  英文别名对齐真实图纸标题栏常见标签：Drawing No/DWG No→图号、Title→名称、Material→
  材料、Qty/Pcs→数量、Scale→比例、Size→幅面、Rev→版本、Date→日期（同为九字段词表，
  不因英文标签扩字段）；
- 空间级（2026-10-05，主会话真机实测 F1=0 根因：真实矢量图纸标题栏为「标签列+值列」
  两栏版面，pdfium get_text_bounded 字符流把两栏交错打散——标签与值相隔数十 token，
  文本流两级双双配对失败）：``project_titleblock_spatial`` 在坐标空间找标签词位置，
  取其**右邻最近**（同行 y 重叠）与**下邻最近**的未分配值片段配对（距离阈值可配置，
  Settings.kb_titleblock_max_gap_pt）；输入=parsers.pdfium 产出的 run 级带坐标片段
  (:class:`TextFragment`)；
- 比例字段值模式额外允许 ``1:50`` 比值形态（值内冒号）。

已知边界（v1 雏形，候选仍入终审队列——宪法第 3 条兜底）：无分隔紧凑 CJK 值文本级不收
（空间级/片段内联可收）；PDF 文本抽取把相邻文本运行粘连时（如 pypdfium2 同行双 Tj）字段
可能残缺；prose 中「材料: 详见附表」会照收（值形态合法但语义非字段值）；紧凑/分隔级为
全文扫描、无标题栏区域定位，视图区同形态文本（如「比例 1:3」）会误捕——标题栏区域定位
属 v2 版面分析，本批留后续；空间级仅在前两级**整体零命中**时兜底（部分命中不补漏，
见 project_titleblock_spatial）。

span 契约：spans 为「含标签的匹配区间 [start, end)」，指向被投影原文（meta.content），
供 chunk 步额外语义块 span 指回原文（出处指针门禁同源）；投影函数为纯确定性——
preprocess 与 chunk 两处分别调用得到一致结果（不落盘 spans，单一事实源=本函数）。
空间级产物不持 content 偏移（片段坐标≠字符偏移）：chunk 步经
:func:`titleblock_anchor_span` 以「值原文定位→标签词正则定位」的次序回查 best-effort
锚点（出处指针门禁同源；值文本可能命中视图区同形文本，属可接受的有损溯源）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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
        return serialize_fields(self.fields)

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


# ---------------------------------------------------------------- 空间级（第三级：坐标配对）

# 空间级值片段形态（右邻/下邻/内联共用）：与 _VALUE 同字集（允许 CJK——中文回归场景），
# 仅按首字符过滤（字母/数字/CJK 起头），列间大间距已由片段聚合切开，散文误捕由
# 「标签词配对+距离阈值」双重门控压制。
_SPATIAL_VALUE_FIRST = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


@dataclass(frozen=True, slots=True)
class TextFragment:
    """run 级带坐标文本片段（parsers.pdfium 产出；PDF 页面坐标系，原点左下、y 向上）。

    text=几何聚合的片段文本（同行近邻字符合并，列间隙切开）；page=页码（1 起）；
    (x0, y0)=左下、(x1, y1)=右上。空间级唯一输入形态——与字符流顺序解耦（两栏交错
    打散不影响聚合正确性）。
    """

    text: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def _overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
    """一维区间重叠占较小区间长度的比例（行/列同轴判定；较小者长度 ≤0 时记 0）。"""
    smaller = min(a1 - a0, b1 - b0)
    if smaller <= 0.0:
        return 0.0
    return max(0.0, min(a1, b1) - max(a0, b0)) / smaller


def _label_regexes() -> tuple[tuple[str, re.Pattern[str], re.Pattern[str], re.Pattern[str]], ...]:
    """空间级标签识别三元编译（模块级一次）：字段 → (纯标签全匹配, 分隔内联, 紧凑内联)。

    纯标签=片段剥空白与尾部冒号后全等某别名（区分大小写不敏感——标题栏标签常全大写）；
    内联=同片段 ``标签[分隔符]值``（run 聚合粘连或单文本对象自含，值与 _VALUE 同字集——
    空间级不沿用紧凑级的字母数字首字符限制，中文值「材料 铝合金」在片段内联可收）。
    """
    compiled: list[tuple[str, re.Pattern[str], re.Pattern[str], re.Pattern[str]]] = []
    for name, aliases, _compact_label in _FIELDS:
        value, _strict = _ratio_value() if name == "比例" else (_VALUE, _VALUE_STRICT)
        compiled.append(
            (
                name,
                re.compile(rf"^(?:{aliases})$", re.IGNORECASE),
                re.compile(rf"^(?:{aliases})\s*[:：=]\s*({value})$", re.IGNORECASE),
                re.compile(rf"^(?:{aliases})[ \t]+({value})$", re.IGNORECASE),
            )
        )
    return tuple(compiled)


_SPATIAL_LABELS = _label_regexes()


def _classify_fragment(text: str) -> tuple[str, str] | None:
    """片段分类（纯标签/内联标签值）：返回 (字段, 值【纯标签为空串】) 或 None（值片段候选）。"""
    stripped = text.strip()
    if not stripped:
        return None
    normalized = stripped.rstrip(":：").strip()
    for name, pure, inline_sep, inline_compact in _SPATIAL_LABELS:
        if pure.fullmatch(normalized) or pure.fullmatch(stripped):
            return (name, "")
        inline = inline_sep.match(stripped) or inline_compact.match(stripped)
        if inline is not None:
            return (name, inline.group(1).strip())
    return None


def project_titleblock_spatial(
    fragments: Iterable[TextFragment],
    *,
    max_gap_pt: float = 150.0,
) -> TitleblockProjection:
    """空间级标题栏投影（纯确定性）：文本两级零命中后的第三级坐标配对。

    算法（2026-10-05 真机 F1=0 根因对策；两栏表版面）：
    ① 片段分类——纯标签片段 / 内联标签值片段（片段内自含，先落、等价文本级命中）/ 值池；
    ② 标签按阅读序遍历（页升序、页内 y 顶先、x 左先），对每个未饱和字段：
       同行（y 重叠 ≥50%）**右侧最近**未分配值片段优先，无则同列（x 重叠 ≥50%）
       **下方最近**未分配值片段；间距（右向间隙/垂直间隙）须 ≤ max_gap_pt；
    ③ 值片段一经配对即出池（「未分配」语义——多标签竞争时阅读序先者优先，同字段首中即得）。

    输出与文本级同构（fields 按 _FIELDS 序；spans 恒空——片段坐标≠content 字符偏移，
    出处锚点由 chunk 步经 titleblock_anchor_span 回查）。纯标签配不上值、间距超限、
    值池耗尽均静默放弃该标签（候选兜底仍走 LLM 通道/人工终审——宪法第 3 条）。
    """
    projection = TitleblockProjection()
    labels: list[tuple[int, TextFragment]] = []  # (字段序, 片段)
    pool: list[TextFragment] = []
    assigned: dict[str, str] = {}
    inline_hits: list[tuple[int, str]] = []  # 内联先落（自含片段，无需配对）
    for frag in fragments:
        classified = _classify_fragment(frag.text)
        if classified is None:
            text = frag.text.strip()
            if text and _SPATIAL_VALUE_FIRST.match(text):
                pool.append(frag)
            continue
        name, value = classified
        order = next(i for i, (field_name, _, _) in enumerate(_FIELDS) if field_name == name)
        if value:
            inline_hits.append((order, value))
        else:
            labels.append((order, frag))
    for order, value in inline_hits:  # 内联命中：同字段首中即得
        assigned.setdefault(_FIELDS[order][0], value)

    def _pick(label: TextFragment, *, right: bool) -> TextFragment | None:
        """右邻/下邻最近未分配值片段（近邻胜出；平局取阅读序靠前者）。"""
        best: tuple[tuple[float, float, float], TextFragment] | None = None
        for cand in pool:
            if right:
                if _overlap_ratio(label.y0, label.y1, cand.y0, cand.y1) < 0.5:
                    continue
                gap = cand.x0 - label.x1
            else:
                if _overlap_ratio(label.x0, label.x1, cand.x0, cand.x1) < 0.5:
                    continue
                gap = label.y0 - cand.y1  # PDF y 向上：下邻 = 值顶低于标签底
            if gap < -2.0 or gap > max_gap_pt:  # 容 -2pt 盒边微叠（PDF 常见），超距拒绝
                continue
            key = (abs(gap), cand.x0, -cand.y1)  # 近邻胜出；平局取阅读序靠前者
            if best is None or key < best[0]:
                best = (key, cand)
        return None if best is None else best[1]

    labels.sort(key=lambda item: (item[1].page, -item[1].y1, item[1].x0))  # 阅读序
    for order, label in labels:
        name = _FIELDS[order][0]
        if name in assigned:
            continue  # 同字段首中即得
        picked = _pick(label, right=True) or _pick(label, right=False)
        if picked is None:
            continue
        assigned[name] = picked.text.strip()
        pool.remove(picked)  # 值片段出池：多标签竞争下不二配
    for name, _, _ in _FIELDS:  # 九字段序输出
        if name in assigned:
            projection.fields[name] = assigned[name]
    return projection


def serialize_fields(fields: Mapping[str, str]) -> str:
    """``字段: 值`` 行集序列化（TitleblockProjection.block_text 与 chunk 步 meta 字段共用）。"""
    return "\n".join(f"{name}: {value}" for name, value in fields.items())


def titleblock_anchor_span(content: str, fields: Mapping[str, str]) -> tuple[int, int] | None:
    """空间级字段块的原文出处锚点（best-effort，[start, end) 指向 content）。

    次序：逐字段**值原文定位**（值来自 content 字符自身，命中率高）→ 逐字段**标签词
    正则定位**（分隔级全别名，词边界同文本级）。均不中返回 None（chunk 步据此跳过块）。
    """
    for _name, value in fields.items():
        text = str(value).strip()
        if text:
            index = content.find(text)
            if index >= 0:
                return (index, index + len(text))
    for name, _pattern, _compact in _PATTERNS:
        if name not in fields:
            continue
        aliases = next(a for n, a, _c in _FIELDS if n == name)
        match = re.search(rf"(?<![0-9A-Za-z])(?:{aliases})", content, re.IGNORECASE)
        if match is not None:
            return (match.start(), match.end())
    return None
