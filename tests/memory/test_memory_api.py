"""memory 六端点 + ★ 端点集成测试（api/01 §5.5；本地 PG 不可达自动跳过；端点函数直调=tests/gateway 同款）。

覆盖：l1/l2 写入（指纹幂等）、读过滤、检索、invalidate 墓碑 404/幂等、context 形状与来源标注、
consolidate 202 受理、跨用户 403（授权矩阵验收）；
★ 补齐（2026-09-28）：GET /memory/l1/{sid} 快照、GET /memory/facts 分页过滤、
GET /memory/facts/{id}/timeline 留痕、promotions 登记与记录面、GET /memory/audit 回放。
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
    create_promotion,
    fact_timeline,
    invalidate_fact,
    list_facts,
    list_promotions,
    memory_audit,
    memory_context,
    read_l1_snapshot,
    read_memory,
    search_memory,
    write_memory,
)
from services.memory.api.schemas.memory import (
    BlockIn,
    ConsolidateIn,
    MemorySearchIn,
    MemoryWriteIn,
    PromotionIn,
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
        assert [i.fact_id for i in page.data] == [f2.id]
        # Act / Assert：全量分页形状（信封 {data, meta:{page,page_size,total}}，M4.6-D3）
        all_page = await read_memory(principal=mem_seed.principal, db=db, l1=_store(mem_seed))
        assert all_page.meta.page_size == 20 and {i.fact_id for i in all_page.data} == {f1.id, f2.id}


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


# ── ★ 端点（api/01 §5.5 补齐，2026-09-28）────────────────────────────────


async def test_GET_memory_l1_sessionid_快照读写一致(mem_seed):
    # Arrange：L1 三件套写入
    body = MemoryWriteIn(
        level="l1",
        session_id=mem_seed.session_id,
        blocks=[BlockIn(key="task", title="任务", content="停电归因分析")],
        window=[WindowMessageIn(role="user", content="查一下城东停电原因")],
        state={"step": "grounding"},
    )
    async with mem_seed.factory() as db:
        await write_memory(body, principal=mem_seed.principal, db=db, l1=_store(mem_seed))
        # Act：★ 专用快照端点
        snap = await read_l1_snapshot(principal=mem_seed.principal, l1=_store(mem_seed), session_id=mem_seed.session_id)
    # Assert：blocks/window/state 与写入一致；键缺失=新会话空快照非 404（degraded=False）
    assert snap.session_id == mem_seed.session_id and snap.degraded is False
    assert snap.blocks["task"].content == "停电归因分析"
    assert snap.window[0].content == "查一下城东停电原因"
    assert snap.state == {"step": "grounding"}


async def test_GET_memory_facts_分页过滤_跨用户403(mem_seed):
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
        # Act / Assert：category 过滤 + 分页形状
        page = await list_facts(
            principal=mem_seed.principal, db=db, status_filter=FactStatus.ACTIVE, category=FactCategory.PREFERENCE
        )
        assert [i.fact_id for i in page.data] == [f2.id] and page.meta.page == 1
        one = await list_facts(principal=mem_seed.principal, db=db, limit=1)
        assert one.meta.page_size == 1 and len(one.data) == 1
        # Act / Assert：显式他人 user_id → 403+2002（授权矩阵）
        from services.platform.errors import GatewayError

        with pytest.raises(GatewayError) as ei:
            await list_facts(principal=mem_seed.principal, db=db, user_id=uuid4())
    assert (ei.value.code, ei.value.status_code) == (2002, 403)


async def test_GET_memory_facts_timeline_产生失效留痕_未知404(mem_seed):
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        fact = L2Fact(
            id=uuid4(),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            content="将走时间线的事实",
            category=FactCategory.FACT,
            confidence=0.9,
            decay_score=0.9,
            valid_from=datetime.now(UTC),
        )
        await repo.add(fact)
        await db.commit()
        # Act / Assert：新事实 → 单 created 事件，chain=自身
        tl = await fact_timeline(fact.id, principal=mem_seed.principal, db=db)
        assert tl.fact_id == fact.id and tl.chain == [fact.id]
        assert [e.type for e in tl.events] == ["created"]
        # Act / Assert：失效后事件追加（FR-MEM-06 全程留痕）
        fact.invalidate(datetime.now(UTC))
        await repo.save_state(fact)
        await db.commit()
        tl2 = await fact_timeline(fact.id, principal=mem_seed.principal, db=db)
        assert [e.type for e in tl2.events] == ["created", "invalidated"]
        assert tl2.events[1].at is not None
        # Act / Assert：未知 id → 404（不泄露存在性）
        from services.platform.errors import GatewayError

        with pytest.raises(GatewayError) as ei:
            await fact_timeline(uuid4(), principal=mem_seed.principal, db=db)
    assert ei.value.status_code == 404


async def test_POST_memory_promotions_202登记_重复409_未知404(mem_seed):
    from services.platform.errors import GatewayError

    async with mem_seed.factory() as db:
        # Arrange：先写一条 L2 事实
        written = await write_memory(
            _write_l2_body("可升级事实：城东馈线甲常载 80%", mem_seed),
            principal=mem_seed.principal,
            db=db,
            l1=_store(mem_seed),
        )
        await db.commit()
        # Act：发起升级单（202 占位登记=audit_logs 一行）
        out = await create_promotion(
            PromotionIn(fact_id=written.fact_id, reason="高频复用", session_id=mem_seed.session_id),
            request=_request(mem_seed),
            principal=mem_seed.principal,
            db=db,
        )
        await db.commit()
        # Assert：registered 占位态
        assert out.fact_id == written.fact_id and out.status == "registered"
        # Act / Assert：同事实重复申请 → 409*
        with pytest.raises(GatewayError) as ei:
            await create_promotion(
                PromotionIn(fact_id=written.fact_id),
                request=_request(mem_seed),
                principal=mem_seed.principal,
                db=db,
            )
        assert ei.value.status_code == 409
        # Act / Assert：未知事实 → 404
        with pytest.raises(GatewayError) as ei2:
            await create_promotion(
                PromotionIn(fact_id=uuid4()), request=_request(mem_seed), principal=mem_seed.principal, db=db
            )
    assert ei2.value.status_code == 404


async def test_GET_memory_promotions_登记行投影(mem_seed):
    async with mem_seed.factory() as db:
        written = await write_memory(
            _write_l2_body("投影面事实", mem_seed), principal=mem_seed.principal, db=db, l1=_store(mem_seed)
        )
        await db.commit()
        out = await create_promotion(
            PromotionIn(fact_id=written.fact_id, reason="审计回放依据", session_id=mem_seed.session_id),
            request=_request(mem_seed),
            principal=mem_seed.principal,
            db=db,
        )
        await db.commit()
        # Act：★ 记录面（M5 前占位：audit_logs 登记行投影）
        page = await list_promotions(principal=mem_seed.principal, db=db)
        # Assert：登记行 → 契约形状（信封 {data, meta}，M4.6-D3；total=len(data) v1 口径）
        assert page.meta.page_size == 20 and page.meta.total == 1 and len(page.data) == 1
        rec = page.data[0]
        assert rec.promotion_id == out.promotion_id
        assert rec.fact_id == written.fact_id and rec.status == "registered"
        assert rec.reason == "审计回放依据" and rec.session_id == mem_seed.session_id
        assert rec.requested_by == mem_seed.user_id and rec.created_at is not None


async def test_GET_memory_audit_升级单回放_会话过滤_跨用户403(mem_seed):
    from services.platform.errors import GatewayError

    async with mem_seed.factory() as db:
        written = await write_memory(
            _write_l2_body("审计回放事实", mem_seed), principal=mem_seed.principal, db=db, l1=_store(mem_seed)
        )
        await db.commit()
        await create_promotion(
            PromotionIn(fact_id=written.fact_id, session_id=mem_seed.session_id),
            request=_request(mem_seed),
            principal=mem_seed.principal,
            db=db,
        )
        await db.commit()
        # Act：按 session 回放（digest 携 session_id）
        page = await memory_audit(principal=mem_seed.principal, db=db, session_id=mem_seed.session_id)
        # Assert：memory.* 动作可见（升级单登记行；信封 {data, meta}，M4.6-D3）
        assert len(page.data) == 1
        entry = page.data[0]
        assert entry.action == "memory.promotion" and entry.resource_id == str(written.fact_id)
        assert entry.result == "success" and entry.actor_id == mem_seed.user_id
        # Act / Assert：跨用户回放 → 403+2002
        with pytest.raises(GatewayError) as ei:
            await memory_audit(principal=mem_seed.principal, db=db, user_id=uuid4())
    assert (ei.value.code, ei.value.status_code) == (2002, 403)
