# tests/agent/test_faithfulness_pg_sink.py
"""忠实度抽检 PG 形状汇集成测试（08 §7.4 / 10 篇缺口②；2026-09-28 接管收口增补）。

断言目标：组合根（build_chat_orchestrator）注入的采样命中汇 → audit_logs 一行结构化 JSON
（action=chat.faithfulness_sample、resource_id=run_id、digest 携对账标识与 llm_judge=pending），
且抽检留痕不改变事件流形状（RUN_FINISHED 收尾）。无 LLM 路径（FakeChatModel）；
本地 PG 不可达或 schema 未迁移自动跳过（tests/memory/conftest 同款纪律）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.business.chat_events import ChatCommand, ChatEventName, ChatPolicy
from services.agent.business.chat_orchestrator import build_chat_orchestrator
from services.iam.data.orm import Tenant as TenantORM
from services.memory.domain.model.l1 import L1Snapshot
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # psycopg 异步要求

pytestmark = pytest.mark.integration


class FakeL1Store:
    """L1 最小桩（test_faithfulness_sampling 同款面）。"""

    async def read(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> L1Snapshot:
        return L1Snapshot(tenant_id=tenant_id, session_id=session_id)

    async def write_blocks(self, tenant_id: uuid.UUID, session_id: uuid.UUID, blocks: list) -> int:
        return 0

    async def append_window(self, tenant_id: uuid.UUID, session_id: uuid.UUID, messages: list) -> int:
        return len(messages)

    async def write_state(self, tenant_id: uuid.UUID, session_id: uuid.UUID, state: dict) -> None:
        return None

    async def delete_all(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        return None


class FakeChatModel:
    """无 LLM 路径：固定回答的结构化生成桩。"""

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        return {"answer": "结论：雷击跳闸。"}


@pytest.fixture
async def faith_seed() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID]]:
    """PG 探测 + 单租户种子；audit_logs 行先于租户清理（FK 逆序，standards/01 §2.9）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM audit_logs LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 audit_logs 未迁移，跳过忠实度抽检汇集成用例")
    await probe.dispose()

    tenant_id = uuid.uuid4()
    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id,
                name=f"faith-it-租户-{uuid.uuid4().hex[:8]}",
                slug=f"faith-it-{uuid.uuid4().hex[:12]}",
                plan="free",
                status="active",
            )
        )
    yield factory, tenant_id
    async with factory() as db, db.begin():
        await db.execute(text("DELETE FROM audit_logs WHERE tenant_id = CAST(:tid AS uuid)"), {"tid": str(tenant_id)})
        await db.execute(text("DELETE FROM tenants WHERE id = CAST(:tid AS uuid)"), {"tid": str(tenant_id)})
    await engine.dispose()


async def test_采样命中_经组合根汇落audit_logs一行(faith_seed):
    """rate=1 恒抽中：完成路径 → audit_logs 一行（run 定位 + 判定上下文 + pending 标记）。"""
    factory, tenant_id = faith_seed
    user_id, session_id, task_id, run_id = (uuid.uuid4() for _ in range(4))
    orchestrator = build_chat_orchestrator(
        model_port=FakeChatModel(),  # type: ignore[arg-type]
        l1_store=FakeL1Store(),  # type: ignore[arg-type]
        session_factory=factory,
        ollama_base_url="http://127.0.0.1:9",  # 嵌入不可达 → kb 检索降级 BM25-only（不阻断对话）
        policy=ChatPolicy(faithfulness_sampling_enabled=True, faithfulness_sample_rate=1.0),
    )
    command = ChatCommand(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        task_id=task_id,
        run_id=run_id,
        message="线路A停电原因？",
        trace_id="trace-faith-pg-it",
    )
    # Act
    events = [event async for event in orchestrator.stream_chat(command)]
    # Assert：抽检留痕不改变事件流形状
    assert events[-1].name is ChatEventName.RUN_FINISHED
    # Assert：audit_logs 落一行结构化 JSON（evaluation 汇欠账期替代面）
    async with factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT resource_id, actor_type, result, "
                    "params_digest->>'task_id' AS task_id, params_digest->>'session_id' AS session_id, "
                    "params_digest->>'status' AS status, params_digest->>'benchmark_type' AS benchmark, "
                    "params_digest->>'llm_judge' AS judge "
                    "FROM audit_logs WHERE tenant_id = CAST(:tid AS uuid) "
                    "AND action = 'chat.faithfulness_sample'"
                ),
                {"tid": str(tenant_id)},
            )
        ).first()
    assert row is not None, "采样命中未落 audit_logs"
    assert row.resource_id == str(run_id)  # run 定位（resource 索引面）
    assert row.actor_type == "system" and row.result == "success"
    assert row.task_id == str(task_id) and row.session_id == str(session_id)
    assert row.status == "completed"
    assert row.benchmark == "faithfulness"  # 期望登记值（evaluation_runs CHECK 冻结，欠账待迁移）
    assert row.judge == "pending"  # LLM-as-judge 判定本体随评估批次接入（本批不做，登记欠账）


async def test_开关关闭_零留痕_流尾不阻断(faith_seed):
    """开关关（采样器不存在）→ 组合根汇零调用：audit_logs 零行，RUN_FINISHED 照常（零行为变化面）。"""
    factory, tenant_id = faith_seed
    orchestrator = build_chat_orchestrator(
        model_port=FakeChatModel(),  # type: ignore[arg-type]
        l1_store=FakeL1Store(),  # type: ignore[arg-type]
        session_factory=factory,
        ollama_base_url="http://127.0.0.1:9",
        policy=ChatPolicy(faithfulness_sampling_enabled=False, faithfulness_sample_rate=0.0),  # 开关关 → 零抽检
    )
    command = ChatCommand(
        tenant_id=tenant_id,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="开关关闭不抽样",
        trace_id="trace-faith-pg-off",
    )
    events = [event async for event in orchestrator.stream_chat(command)]
    assert events[-1].name is ChatEventName.RUN_FINISHED
    async with factory() as db:
        count = (
            await db.execute(
                text(
                    "SELECT count(*) FROM audit_logs WHERE tenant_id = CAST(:tid AS uuid) "
                    "AND action = 'chat.faithfulness_sample'"
                ),
                {"tid": str(tenant_id)},
            )
        ).scalar_one()
    assert count == 0  # 开关关：零留痕（零行为变化面）
