"""语义分块（OntRAG 知识库GraphRAG设计 §2.1「分块策略」；L5 knowledge.preprocess 的算法本体）。

规则（2026-09-26 痛点优化口径，纯确定性函数、零 LLM）：
- 按标题层级 + 段落边界切分，绝不在句中硬切；目标 512±128 token
  （token 粗估 = len(text)//2，中英混排近似口径，随 PoC ③ 实测冻结）；
- 相邻块重叠 10%：预算溢出换页时把尾部完整小块（合计 ≤ 目标 10% token）带入下块，
  防边界事实两头丢失（整块搬运，不打断句子）；
- 表格整块不切；超长表格按行组切分且每段重复表头（含关键列上下文，块脱离原文自解释）；
- 代码块围栏整体保留不切；超预算整块入库并在 meta 打 oversized 告警标记，不硬拆；
- 每片带原文出处指针：meta.span = [start, end)（原文 content 的字符区间，门禁=无出处指针
  的 chunk 不得进入步骤 3）；meta.heading 为标题层级路径（跨块上下文）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TARGET_TOKENS = 512  # §2.1 起点值而非结论，随 PoC ③ 实测冻结
TOKEN_TOLERANCE = 128
OVERLAP_RATIO = 0.1

_HEADING_RE = re.compile(r"^(#{1,6})\s+")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def estimate_tokens(text: str) -> int:
    """token 粗估（len//2，任务口径）：空串为 0。"""
    return len(text) // 2 if text else 0


@dataclass(slots=True)
class Chunk:
    """分块产物：content 为原文切片（含重叠搬运段），span 指向原文覆盖区间 [start, end)。"""

    seq: int
    content: str
    token_count: int
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class _Block:
    kind: str  # heading | paragraph | table | code
    text: str
    start: int
    end: int
    level: int = 0
    header: str | None = None  # 表格：表头 + 分隔行（行组切分时每段重复）


def _parse_blocks(content: str) -> list[_Block]:
    """把 markdown 源切成块序列：标题 / 段落 / 表格（| 开头连续行）/ 代码围栏。"""
    blocks: list[_Block] = []
    lines = content.splitlines(keepends=True)
    offset = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        start = offset
        if _FENCE_RE.match(line):
            text = line
            i += 1
            offset += len(line)
            while i < n and not _FENCE_RE.match(lines[i]):  # 围栏整体保留
                text += lines[i]
                i += 1
                offset += len(lines[i - 1])
            if i < n:  # 收尾围栏
                text += lines[i]
                offset += len(lines[i])
                i += 1
            blocks.append(_Block("code", text, start, offset))
            continue
        if stripped.startswith("|"):
            text = line
            i += 1
            offset += len(line)
            while i < n and lines[i].strip().startswith("|"):
                text += lines[i]
                offset += len(lines[i])
                i += 1
            kept = [ln for ln in text.splitlines(keepends=True) if ln.strip().startswith("|")]
            header = "".join(kept[:2])  # 表头 + 分隔行（保留行尾换行，行组拼回不失真）
            blocks.append(_Block("table", text, start, offset, header=header))
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            blocks.append(_Block("heading", line, start, start + len(line), level=len(heading.group(1))))
            offset += len(line)
            i += 1
            continue
        if not stripped:  # 空行 = 段落边界
            offset += len(line)
            i += 1
            continue
        text = line  # 段落：连续非空行
        i += 1
        offset += len(line)
        while i < n and lines[i].strip() and not _FENCE_RE.match(lines[i]) and not lines[i].lstrip().startswith("|"):
            if _HEADING_RE.match(lines[i]):
                break
            text += lines[i]
            offset += len(lines[i])
            i += 1
        blocks.append(_Block("paragraph", text, start, offset))
    return blocks


def _split_table(block: _Block, *, budget_tokens: int) -> list[_Block]:
    """超长表格按行组切分，每段重复表头（§2.1 特殊块规则）。"""
    lines = [ln for ln in block.text.splitlines(keepends=True) if ln.strip().startswith("|")]
    header = block.header or ""
    data_rows = lines[2:] if len(lines) > 2 else []  # 前两行 = 表头 + 分隔行，不参与行组
    header_tokens = estimate_tokens(header)
    groups: list[_Block] = []
    current: list[str] = []
    current_tokens = header_tokens
    for row in data_rows:
        t = estimate_tokens(row)
        if current and current_tokens + t > budget_tokens:
            groups.append(_Block("table", header + "".join(current), block.start, block.end, header=header))
            current, current_tokens = [], header_tokens
        current.append(row)
        current_tokens += t
    if current:
        groups.append(_Block("table", header + "".join(current), block.start, block.end, header=header))
    return groups


def chunk_document(
    content: str,
    *,
    target_tokens: int = TARGET_TOKENS,
    tolerance: int = TOKEN_TOLERANCE,
    overlap_ratio: float = OVERLAP_RATIO,
) -> list[Chunk]:
    """语义分块主入口：标题开新节、段落聚攒、表格行组、代码整块；产出按 seq 编号。"""
    if not content.strip():
        return []
    budget = target_tokens + tolerance
    overlap_budget = max(1, int(target_tokens * overlap_ratio))
    blocks = _parse_blocks(content)

    chunks: list[Chunk] = []
    parts: list[_Block] = []  # 当前聚攒（含带入的重叠尾块）
    carry: list[_Block] = []  # 换片时留给下一片的重叠尾块
    heading_path: list[tuple[int, str]] = []

    def heading_label() -> str:
        return " > ".join(t for _, t in heading_path)

    def _pure_carry() -> bool:
        return bool(parts) and bool(carry) and len(parts) == len(carry)

    def flush(carry_over: bool) -> None:
        """聚攒块落片；carry_over=True 时按 10% 预算把尾部完整小块留给下一片。"""
        nonlocal parts, carry
        if not parts:
            carry = []
            return
        body = "".join(p.text for p in parts)
        chunks.append(
            Chunk(
                seq=len(chunks),
                content=body,
                token_count=estimate_tokens(body),
                meta={
                    "kind": parts[0].kind if len(parts) == 1 else "section",
                    "heading": heading_label(),
                    "span": [parts[0].start, parts[-1].end],
                },
            )
        )
        if carry_over:  # 重叠：尾部完整小块（不打断句子）
            tail: list[_Block] = []
            tail_tokens = 0
            candidates = parts[:-1] if parts[-1].kind == "heading" else parts
            for blk in reversed(candidates):
                blk_tokens = estimate_tokens(blk.text)
                if tail and tail_tokens + blk_tokens > overlap_budget:
                    break
                if not tail and blk_tokens > overlap_budget:
                    break  # 单块即超重叠预算：不硬拆，放弃重叠
                tail.insert(0, blk)
                tail_tokens += blk_tokens
            carry = tail
        else:
            carry = []
        parts = []

    def append_block(block: _Block) -> None:
        nonlocal parts, carry
        if not parts and carry:
            parts = list(carry)  # 新片以重叠尾块开头
        parts.append(block)

    for block in blocks:
        if block.kind == "heading":
            if _pure_carry():  # 纯重叠尾片撞上新节：丢弃不成片
                parts = []
            else:
                flush(carry_over=False)  # 标题开新节，不跨节重叠
            while heading_path and heading_path[-1][0] >= block.level:
                heading_path.pop()
            heading_path.append((block.level, _HEADING_RE.sub("", block.text).strip()))
            parts.append(block)
            continue
        if block.kind == "table" and estimate_tokens(block.text) > budget:
            flush(carry_over=False)
            for group in _split_table(block, budget_tokens=budget):  # 整表超预算 → 行组切分
                chunks.append(
                    Chunk(
                        seq=len(chunks),
                        content=group.text,
                        token_count=estimate_tokens(group.text),
                        meta={
                            "kind": "table",
                            "heading": heading_label(),
                            "span": [block.start, block.end],
                            "table_header": (group.header or "").splitlines()[0] if group.header else None,
                            "row_group": True,
                        },
                    )
                )
            continue
        if block.kind == "code" and estimate_tokens(block.text) > budget:
            flush(carry_over=False)  # 代码超预算：整块入库并告警，不硬拆（§2.1）
            chunks.append(
                Chunk(
                    seq=len(chunks),
                    content=block.text,
                    token_count=estimate_tokens(block.text),
                    meta={
                        "kind": "code",
                        "heading": heading_label(),
                        "span": [block.start, block.end],
                        "oversized": True,
                    },
                )
            )
            continue
        append_block(block)
        if sum(estimate_tokens(p.text) for p in parts) > budget:
            flush(carry_over=True)
    if _pure_carry():
        parts = []  # 文档收尾不得吐纯重叠尾片
    flush(carry_over=False)
    return chunks
