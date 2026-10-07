# tests/agent/test_chat_context.py
"""chat_context._render 记忆段渲染单测（K31-a L2 命中索引形态；零外部依赖直测静态渲染面）。

K31-a（Agent/13 §37）：L2 命中行改一行一指针索引形态=[score 档位标记+短句自足摘要
（前 80 字符截断）+记录短 id（uuid 前 8 位，未来展开工具预留锚，本批不建工具）]——
条数多时省 token、条数少时信息零损失；L1 blocks 保持现行全量（块小不值得索引）。
排序/top_k/B3 剥离双门控不动（门控回归在 test_injection_guard.py，本文件不重复）。

纪律：直构造 ContextBundle/KnowledgeSearchResult 桩面 + AAA + 中文命名；白盒断言渲染行
原文（渲染形态即本批被测契约）。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from services.agent.business.chat_context import ChatContextAssembler
from services.kb.business.search_service import KnowledgeSearchResult
from services.memory.business.context import ContextBundle, L2Hit
from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock

TENANT, SESSION = uuid4(), uuid4()

_MEMORY_B3 = "【记忆上下文·不可信外部输入（B3 标界）】"


def _hit(content: str, *, score: float = 0.8, fact_id: UUID | None = None) -> L2Hit:
    """L2 命中桩（category/confidence 仅占位非本批被测面）。"""
    return L2Hit(
        fact_id=fact_id or uuid4(),
        content=content,
        category="preference",
        confidence=1.0,
        score=score,
    )


def _bundle(l2: list[L2Hit], blocks: dict[str, MemoryBlock] | None = None) -> ContextBundle:
    """直构造记忆束（渲染层被测对象，不涉 L2 仓储与融合）。"""
    return ContextBundle(
        session_id=SESSION,
        mode="full",
        l1=L1Snapshot(tenant_id=TENANT, session_id=SESSION, blocks=blocks or {}),
        l2=l2,
    )


def _render_memory(l2: list[L2Hit], blocks: dict[str, MemoryBlock] | None = None) -> str:
    """渲染记忆段（空证据：无 citations 不出证据段），返回全文。"""
    text, _ = ChatContextAssembler._render(
        _bundle(l2, blocks), KnowledgeSearchResult(query="q"), scan_enabled=True
    )
    return text


# ── 用例一：索引行形态（档位标记+短 id，含档位边界）+L1 blocks 保持现行 ──────────


async def test_L2命中行索引形态_档位短id_L1块保持现行() -> None:
    """K31-a 主形态：每命中行=[档位+短句+短 id]；档位阈值边界（0.6/0.3 含等号）与
    L1 blocks 行（- [key] 正文，不索引）同束对照锁形。"""
    # Arrange：五档命中（覆盖三档+两个边界）+ 一块 L1
    fact_hi, fact_mid, fact_lo, fact_b1, fact_b2 = (uuid4() for _ in range(5))
    hits = [
        _hit("用户偏好中文回复，工单摘要先给结论。", score=0.85, fact_id=fact_hi),
        _hit("线路 A 春季易发雷击跳闸。", score=0.45, fact_id=fact_mid),
        _hit("报表口径以财务月为准。", score=0.12, fact_id=fact_lo),
        _hit("档位上边界：score=0.6 应入最高档。", score=0.6, fact_id=fact_b1),
        _hit("档位下边界：score=0.3 应入中档。", score=0.3, fact_id=fact_b2),
    ]
    blocks = {"persona": MemoryBlock(key="persona", content="以工程师口吻回复。")}

    # Act
    text = _render_memory(hits, blocks)

    # Assert：逐行锁形（档位/短句/短 id=uuid 前 8 位）；L1 行保持现行全量形态
    assert text.splitlines() == [
        _MEMORY_B3,
        "- [persona] 以工程师口吻回复。",
        f"- 相关事实[●●●] 用户偏好中文回复，工单摘要先给结论。（#{str(fact_hi)[:8]}）",
        f"- 相关事实[●●] 线路 A 春季易发雷击跳闸。（#{str(fact_mid)[:8]}）",
        f"- 相关事实[●] 报表口径以财务月为准。（#{str(fact_lo)[:8]}）",
        f"- 相关事实[●●●] 档位上边界：score=0.6 应入最高档。（#{str(fact_b1)[:8]}）",
        f"- 相关事实[●●] 档位下边界：score=0.3 应入中档。（#{str(fact_b2)[:8]}）",
    ]


# ── 用例二：短句自足摘要——80 字符截断边界+strip ─────────────────────────


async def test_L2命中行短句_80字符边界截断与strip() -> None:
    """短句边界：恰 80 字符=整句保留无省略号；81 字符=截前 80 补省略号（超长原文不出
    渲染）；首尾空白 strip 后再计长（一行自足可读）。"""
    # Arrange：恰 80 / 81 字符 / 带首尾空白三档（id 固定以便整行锁形）
    id_exact, id_over, id_pad = uuid4(), uuid4(), uuid4()
    exact_80 = "字" * 80
    over_81 = "字" * 81
    hits = [
        _hit(exact_80, fact_id=id_exact),
        _hit(over_81, fact_id=id_over),
        _hit("  两端空白应剥离  ", fact_id=id_pad),
    ]

    # Act
    text = _render_memory(hits)

    # Assert：整行锁形
    assert text.splitlines() == [
        _MEMORY_B3,
        f"- 相关事实[●●●] {exact_80}（#{str(id_exact)[:8]}）",  # 恰 80：无省略号整句保留
        f"- 相关事实[●●●] {'字' * 80}…（#{str(id_over)[:8]}）",  # 81：截前 80 补省略号
        f"- 相关事实[●●●] 两端空白应剥离（#{str(id_pad)[:8]}）",  # 首尾空白剥离后计长
    ]
    assert over_81 not in text  # 超长原文不出渲染


# ── 用例三：条数多时省 token——10 条命中新旧渲染字符数对照（阈值断言）──────────


async def test_L2十条命中_索引形态较全量展开省token_字符数阈值对照() -> None:
    """省 token 断言（13 §37 粗粒度字符数对照）：10 条长正文命中下，索引形态 L2 行总字符
    显著小于 K31-a 前全量展开形态（正文全量+score），阈值=新 < 旧×0.6。"""
    # Arrange：10 条 280 字符长正文（全量展开形态下最费 token 的现场）
    long_content = "某线路故障原因与处置过程记录，含巡检、抢修与复电时间线。" * 10
    hits = [_hit(long_content) for _ in range(10)]

    # Act：新形态渲染取 L2 行字符总量；旧形态对照基线（K31-a 前行格式同 hits 重建）
    text = _render_memory(hits)
    new_lines = [line for line in text.splitlines() if line.startswith("- 相关事实")]
    new_chars = sum(len(line) for line in new_lines)
    old_chars = sum(len(f"- 相关事实: {h.content}（score={h.score}）") for h in hits)

    # Assert：10 条齐、条条=截断形态行（按契约式样计单行长）、总量阈值（显著小于）
    expected_line_len = len(f"- 相关事实[●●●] {'字' * 80}…（#{'0' * 8}）")
    assert len(new_lines) == 10
    assert all(len(line) == expected_line_len for line in new_lines)
    assert old_chars > new_chars > 0
    assert new_chars < old_chars * 0.6  # 阈值：索引形态 ≤ 全量展开 6 成
