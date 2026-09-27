"""memory 六端点集成测试（api/01 §5.5；本地 PG 不可达自动跳过；端点函数直调=tests/gateway 同款）。

覆盖：l1/l2 写入（指纹幂等）、读过滤、检索、invalidate 墓碑 404/幂等、context 形状与来源标注、
consolidate 202 受理、跨用户 403（授权矩阵验收）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.memory.api.memory import (
    consolidate_memory,
    invalidate_fact,
    memory_context,
    read_memory,
    search_memory,
    write_memory,
)
from services.memory.api.schemas.memory import (
    BlockIn,
    ConsolidateIn,
    MemorySearchIn,
    MemoryWriteIn,
    SourceRefsIn,
    WindowMessageIn,
)
from services.memory.data.l1 import RedisL1Store
from services.memory.domain.model.l2_fact import FactCategory, FactStatus, L2Fact

if TYPE_CHECKING:
    from tests.memory.conftest import MemorySeed

pytestmark = pytest.mark.integration


def _request(seed: MemorySeed) -> StarletteRequest:
    """携带 app（settings）的最小 Request（端点直调用，tests/gateway 直调模式的补充件）。"""
    app = create_app(seed.settings)  # 不跑 lifespan：app.state.settings 已就绪
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/memory",
        "raw_path": b"/api/v1/memory",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "mem-it-trace"
    return request


def _store(seed: MemorySeed) -> RedisL1Store:
    return RedisL1Store(seed.redis, ttl_seconds=3600)


def _write_l2_body(content: str, seed: MemorySeed, **kw) -> MemoryWriteIn:
    return MemoryWriteIn(
        level="l2",
        content=content,
        category=kw.get("category", FactCategory.FACT),
        source_refs=SourceRefsIn(session_id=seed.session_id, message_ids=[]),
    )


async def test_POST_memory写L2事实_201_重复提交幂等返回原fact_id(mem_seed):
    # Arrange
    async with mem_seed.factory() as db:
        body = _write_l2_body("用户偏好：工单摘要先给结论", mem_seed)
        # Act：两次同文提交（第二次仅空白差异）
        first = await write_memory(body, principal=mem_seed.principal, db=db, l1=_store(mem_seed))
        second = await write_memory(
            _write_l2_body("  用户偏好：工单摘要先给结论  ", mem_seed),
            principal=mem_seed.principal,
            db=db,
            l1=_store(mem_seed),
        )
        await db.commit()
    # Assert：幂等（§5.4 重复提交返回原 fact_id）
    assert first.duplicate is False and second.duplicate is True
    assert first.fact_id == second.fact_id


async def test_POST_memory写L1_块窗口草稿_读回一致(mem_seed):
    # Arrange
    body = MemoryWriteIn(
        level="l1",
        session_id=mem_seed.session_id,
        blocks=[BlockIn(key="task", title="任务", content="停电归因分析")],
        window=[WindowMessageIn(role="user", content="查一下城东停电原因")],
        state={"step": "grounding"},
    )
    # Act
    async with mem_seed.factory() as db:
        result = await write_memory(body, principal=mem_seed.principal, db=db, l1=_store(mem_seed))
        snapshot = await read_memory(
            principal=mem_seed.principal,
            db=db,
            l1=_store(mem_seed),
            layer="l1",
            session_id=mem_seed.session_id,
        )
    # Assert
    assert result["level"] == "l1"
    assert snapshot.blocks["task"].content == "停电归因分析"
    assert snapshot.window[0].content == "查一下城东停电原因"
    assert snapshot.state == {"step": "grounding"}


async def test_GET_memory_layer_l2_分页与过滤(mem_seed):
    # Arrange：经仓储直写 2 条不同类别
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        f1 = L2Fact(
            id=uuid4(),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            content="事实甲",
            category=FactCategory.FACT,
            confidence=0.9,
            decay_score=0.9,
        )
        f2 = L2Fact(
            id=uuid4(),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            content="偏好乙",
            category=FactCategory.PREFERENCE,
            confidence=0.9,
            decay_score=0.9,
        )
        await repo.add(f1)
        await repo.add(f2)
        await db.commit()
        # Act / Assert：category 过滤
        page = await read_memory(
            principal=mem_seed.principal,
            db=db,
            l1=_store(mem_seed),
            status_filter=FactStatus.ACTIVE,
            category=FactCategory.PREFERENCE,
        )
        assert [i.fact_id for i in page.items] == [f2.id]
        # Act / Assert：全量分页形状
        all_page = await read_memory(principal=mem_seed.principal, db=db, l1=_store(mem_seed))
        assert all_page.limit == 20 and {i.fact_id for i in all_page.items} == {f1.id, f2.id}


async def test_GET_memory_跨用户403_授权矩阵(mem_seed):
    # Act / Assert：显式他人 user_id → 403+2002（memory §8 越权用例）
    stranger = uuid4()
    async with mem_seed.factory() as db:
        from services.platform.errors import GatewayError

        with pytest.raises(GatewayError) as ei:
            await read_memory(principal=mem_seed.principal, db=db, l1=_store(mem_seed), user_id=stranger)
    assert (ei.value.code, ei.value.status_code) == (2002, 403)


async def test_POST_memory_search_命中带来源标注(mem_seed):
    # Arrange
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        fact = L2Fact(
            id=uuid4(),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            content="停电分析先核对变压器台账",
            category=FactCategory.FACT,
            confidence=0.9,
            decay_score=0.9,
        )
        await repo.add(fact)
        await db.commit()
        # Act
        out = await search_memory(
            MemorySearchIn(query="变压器 台账"),
            request=_request(mem_seed),
            principal=mem_seed.principal,
            db=db,
        )
    # Assert：命中 + 逐条来源层标注（api/01 §6.6）
    assert [i.fact_id for i in out.items] == [fact.id]
    assert out.items[0].source == "l2"


async def test_POST_invalidate_202墓碑_幂等与404(mem_seed):
    from services.platform.errors import GatewayError

    async with mem_seed.factory() as db:
        # Arrange：先写一条
        written = await write_memory(
            _write_l2_body("将失效的事实", mem_seed), principal=mem_seed.principal, db=db, l1=_store(mem_seed)
        )
        await db.commit()
        # Act：失效标记
        out = await invalidate_fact(written.fact_id, principal=mem_seed.principal, db=db)
        # Assert：墓碑式软删（无物理删除）
        assert out.status == FactStatus.INVALIDATED.value and out.valid_to is not None
        # Act / Assert：重复失效幂等（202 原样返回）
        again = await invalidate_fact(written.fact_id, principal=mem_seed.principal, db=db)
        assert again.status == FactStatus.INVALIDATED.value
        # Act / Assert：未知 id → 404
        with pytest.raises(GatewayError) as ei:
            await invalidate_fact(uuid4(), principal=mem_seed.principal, db=db)
        assert ei.value.status_code == 404
        await db.commit()


async def test_POST_consolidate_202受理并登记后台任务(mem_seed):
    # Act
    from starlette.background import BackgroundTasks

    background = BackgroundTasks()
    out = await consolidate_memory(
        ConsolidateIn(session_id=mem_seed.session_id),
        request=_request(mem_seed),
        principal=mem_seed.principal,
        l1=_store(mem_seed),
        background=background,
    )
    # Assert：202 受理 + 后台任务登记（执行体由 test_consolidation.py 直调覆盖）
    assert out.session_id == mem_seed.session_id and out.status == "accepted"
    assert len(background.tasks) == 1


async def test_GET_memory_context_full与light_形状与降级标注(mem_seed):
    # Arrange：L1 窗口（含 query）+ L2 事实
    store = _store(mem_seed)
    window_msg = WindowMessageIn(role="user", content="变压器 台账 怎么核对").to_domain()
    await store.append_window(mem_seed.tenant_id, mem_seed.session_id, [window_msg])
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        fact = L2Fact(
            id=uuid4(),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            content="停电分析先核对变压器台账",
            category=FactCategory.FACT,
            confidence=0.9,
            decay_score=0.9,
            valid_from=datetime.now(UTC),
        )
        await repo.add(fact)
        await db.commit()
        # Act：full（L1 全量 + 双通道融合）
        full = await memory_context(
            request=_request(mem_seed),
            principal=mem_seed.principal,
            db=db,
            l1=store,
            session_id=mem_seed.session_id,
            mode="full",
        )
        # Act：light（轻检索降级：仅 L1 全量 + L2 top-k）
        light = await memory_context(
            request=_request(mem_seed),
            principal=mem_seed.principal,
            db=db,
            l1=store,
            session_id=mem_seed.session_id,
            mode="light",
        )
    # Assert：api/01 §6.6 形状
    assert full.meta == {"mode": "full", "degraded": False}
    assert [w["content"] for w in full.data["l1"]["window"]] == ["变压器 台账 怎么核对"]
    assert [h["fact_id"] for h in full.data["l2"]] == [str(fact.id)]
    assert full.data["l2"][0]["source"] == "l2"
    assert full.data["l3"] == [] and full.data["l4"] == []  # L3 延后 M5 / L4 走 knowledge.search
    # Assert：light 恒标 degraded（轻检索降级观测位）
    assert light.meta["mode"] == "light" and light.meta["degraded"] is True
    assert [h["fact_id"] for h in light.data["l2"]] == [str(fact.id)]
