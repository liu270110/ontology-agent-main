"""沉淀编排集成测试（memory §2 写入管线 M3 过渡；本地 PG 不可达自动跳过）。

覆盖：确定性物化（无 LLM 兜底路径）、LLM 抽取路径（桩）、置信度防线丢弃、
指纹幂等（重跑零重复——§5.4 冲突判定幂等）、来源指针必填。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from services.memory.business.consolidation import consolidate_session
from services.memory.domain.model.l1 import MemoryBlock, WindowMessage
from services.memory.domain.model.l2_fact import FactCategory, FactStatus

if TYPE_CHECKING:
    from tests.memory.conftest import MemorySeed

pytestmark = pytest.mark.integration


class StubExtractPort:
    """抽取桩：恒返回注入的 facts（签名与 ModelPort 对齐，含 num_ctx 增参）。"""

    provider = "stub"

    def __init__(self, facts: list[dict[str, Any]]) -> None:
        self.facts = facts
        self.calls = 0

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        timeout_s: float = 60.0,
        trace_id: str | None = None,
        num_ctx: int | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        return {"facts": self.facts}


async def _put_blocks(seed: MemorySeed, contents: list[str]) -> None:
    blocks = [MemoryBlock(key=f"b{i}", content=c) for i, c in enumerate(contents)]
    from services.memory.data.l1 import RedisL1Store

    store = RedisL1Store(seed.redis, ttl_seconds=3600)
    await store.write_blocks(seed.tenant_id, seed.session_id, blocks)


async def test_沉淀_无LLM时自编辑块确定性物化为事实(mem_seed):
    # Arrange：L1 两个自编辑块
    await _put_blocks(mem_seed, ["用户偏好：结论先行", "负责城东片区巡检"])
    async with mem_seed.factory() as db:
        # Act
        result = await consolidate_session(
            l1_store=_store(mem_seed),
            repo=mem_seed.repo(db),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            session_id=mem_seed.session_id,
        )
        await db.commit()
        # Assert：双写且来源=blocks、来源指针=会话（§9.2 来源必填）
        assert result.written == 2 and result.duplicates == 0 and result.source == "blocks"
        facts = await mem_seed.repo(db).list_for_user(mem_seed.user_id)
        assert len(facts) == 2
        assert all(f.source_session_id == mem_seed.session_id for f in facts)
        assert all(f.status is FactStatus.ACTIVE for f in facts)


async def test_沉淀_LLM抽取路径_低置信候选被防线丢弃(mem_seed):
    # Arrange：L1 有窗口消息（触发 LLM 路径）+ 抽取桩（一高一低置信）
    from services.memory.data.l1 import RedisL1Store

    store = RedisL1Store(mem_seed.redis, ttl_seconds=3600)
    await store.append_window(
        mem_seed.tenant_id, mem_seed.session_id, [WindowMessage(role="user", content="我只看结论，别给我过程")]
    )
    stub = StubExtractPort(
        [
            {"content": "用户偏好：只看结论", "category": "preference", "confidence": 0.9},
            {"content": "噪声候选：低置信", "category": "fact", "confidence": 0.1},
        ]
    )
    async with mem_seed.factory() as db:
        # Act
        result = await consolidate_session(
            l1_store=store,
            repo=mem_seed.repo(db),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            session_id=mem_seed.session_id,
            model_port=stub,  # type: ignore[arg-type]  # 测试桩
        )
        await db.commit()
        # Assert：LLM 路径生效 + 低于阈值（0.3）候选丢弃（§9.2 写入侧防线）
        assert stub.calls == 1 and result.source == "llm"
        facts = await mem_seed.repo(db).list_for_user(mem_seed.user_id)
        assert [f.content for f in facts] == ["用户偏好：只看结论"]
        assert facts[0].category is FactCategory.PREFERENCE


async def test_沉淀_幂等_重跑不产生重复事实(mem_seed):
    # Arrange
    await _put_blocks(mem_seed, ["巡检记录：3 号变压器已换"])
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        first = await consolidate_session(
            l1_store=_store(mem_seed),
            repo=repo,
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            session_id=mem_seed.session_id,
        )
        await db.commit()
        # Act：重跑（checkpoint 断点续跑/事件重放语义）
        second = await consolidate_session(
            l1_store=_store(mem_seed),
            repo=repo,
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            session_id=mem_seed.session_id,
        )
        await db.commit()
        # Assert：零重复（§5.4 指纹判重兜底）
        assert first.written == 1
        assert second.written == 0 and second.duplicates == 1
        facts = await mem_seed.repo(db).list_for_user(mem_seed.user_id)
        assert len(facts) == 1


async def test_沉淀_空会话_零写入不报错(mem_seed):
    async with mem_seed.factory() as db:
        # Act：L1 全空
        result = await consolidate_session(
            l1_store=_store(mem_seed),
            repo=mem_seed.repo(db),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            session_id=mem_seed.session_id,
        )
        # Assert
        assert result.source == "empty" and result.extracted == 0 and result.written == 0


def _store(seed: MemorySeed):
    from services.memory.data.l1 import RedisL1Store

    return RedisL1Store(seed.redis, ttl_seconds=3600)
