"""K2-a 失效即归档影子表集成测试（契约=memory 权威篇 §11.1；本地 PG 不可达自动跳过）。

覆盖：invalidate 写影子行（快照+reason，与主表同事务）、幂等不重复归档、reason 必填双层拒绝、
restore 回填 restored_at 且主表保持 INVALIDATED（非复活红线）、治理档（他人 403/admin 放行）、
list_invalidated 过滤、未命中 404。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.memory.api.memory import invalidate_fact, restore_fact, write_memory
from services.memory.api.schemas.memory import FactInvalidateIn, MemoryWriteIn, SourceRefsIn
from services.memory.data.l1 import RedisL1Store
from services.memory.domain.model.l2_fact import FactCategory, FactStatus, L2Fact

if TYPE_CHECKING:
    from tests.memory.conftest import MemorySeed

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _request(seed: MemorySeed) -> StarletteRequest:
    """携带 app（settings）的最小 Request（端点直调，tests/memory 惯例）。"""
    app = create_app(seed.settings)
    scope: dict = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
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
    request.state.trace_id = "k2-shadow-trace"
    return request


def _store(seed: MemorySeed) -> RedisL1Store:
    return RedisL1Store(seed.redis, ttl_seconds=3600)


async def test_invalidate_写影子行快照与reason_幂等不重复归档(mem_seed):
    from services.memory.data.orm import MemoryL2FactInvalidation as ShadowORM

    async with mem_seed.factory() as db:
        written = await write_memory(
            MemoryWriteIn(
                level="l2",
                content="城东馈线甲常载 80%",
                category=FactCategory.FACT,
                source_refs=SourceRefsIn(session_id=mem_seed.session_id, message_ids=[]),
            ),
            principal=mem_seed.principal,
            db=db,
            l1=_store(mem_seed),
        )
        await db.commit()
        # Act：失效（K2-a：reason 必填 + 影子行同事务归档）
        out = await invalidate_fact(
            written.fact_id, FactInvalidateIn(reason="台账已更正"), principal=mem_seed.principal, db=db
        )
        await db.commit()
        # Assert：主表墓碑 + 影子行（content 快照/reason/restored_at 空）
        assert out.status == FactStatus.INVALIDATED.value
        rows = (
            (await db.execute(ShadowORM.__table__.select().where(ShadowORM.fact_id == written.fact_id)))
            .mappings()
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["content"] == "城东馈线甲常载 80%"  # 失效时文本快照
        assert row["reason"] == "台账已更正" and row["restored_at"] is None
        assert row["tenant_id"] == mem_seed.tenant_id and row["user_id"] == mem_seed.user_id
        assert row["invalidated_at"] is not None
        # Act / Assert：重复失效幂等——原样返回且不重复归档
        again = await invalidate_fact(
            written.fact_id, FactInvalidateIn(reason="二次提交"), principal=mem_seed.principal, db=db
        )
        assert again.status == FactStatus.INVALIDATED.value
        rows2 = (
            (await db.execute(ShadowORM.__table__.select().where(ShadowORM.fact_id == written.fact_id)))
            .mappings()
            .all()
        )
        assert len(rows2) == 1 and rows2[0]["reason"] == "台账已更正"  # 幂等路径不写第二行
        await db.commit()


async def test_invalidate_reason必填_领域与DTO双层拒绝(mem_seed):
    fact = L2Fact(
        id=uuid4(),
        tenant_id=mem_seed.tenant_id,
        user_id=mem_seed.user_id,
        content="待失效事实",
        category=FactCategory.FACT,
    )
    # Act / Assert：领域层空 reason 拒绝（§11.1 无 reason 拒绝失效）
    with pytest.raises(ValueError, match="reason"):
        fact.invalidate(NOW, reason="")
    with pytest.raises(ValueError, match="reason"):
        fact.invalidate(NOW, reason="  ")
    assert fact.status is FactStatus.ACTIVE  # 拒绝即无状态副作用
    # Act / Assert：DTO 层空白 reason 拒绝（min_length=1 把守 → 422 的直接来源）
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FactInvalidateIn(reason="")


async def test_restore_影子行回填restored_at_主表保持INVALIDATED_重复409(mem_seed):
    from services.platform.errors import GatewayError

    async with mem_seed.factory() as db:
        written = await write_memory(
            MemoryWriteIn(
                level="l2",
                content="将恢复的失效事实",
                category=FactCategory.FACT,
                source_refs=SourceRefsIn(session_id=mem_seed.session_id, message_ids=[]),
            ),
            principal=mem_seed.principal,
            db=db,
            l1=_store(mem_seed),
        )
        await db.commit()
        await invalidate_fact(written.fact_id, FactInvalidateIn(reason="误失效"), principal=mem_seed.principal, db=db)
        await db.commit()
        # Act：restore（影子层可见性恢复）
        out = await restore_fact(written.fact_id, principal=mem_seed.principal, db=db)
        # Assert：restored_at 回填 + 主表保持 INVALIDATED（非复活红线）
        assert out.fact_id == written.fact_id and out.restored_at is not None
        assert out.status == "invalidated" and out.reason == "误失效"
        fact = await mem_seed.repo(db).get(written.fact_id)
        assert fact is not None and fact.status is FactStatus.INVALIDATED  # 主表终态不动
        # Act / Assert：重复 restore → 409（无生效中影子行）
        with pytest.raises(GatewayError) as ei:
            await restore_fact(written.fact_id, principal=mem_seed.principal, db=db)
        assert ei.value.status_code == 409
        await db.commit()


async def test_restore_治理档_他人403_管理员放行_未知404(mem_seed):
    from services.platform.deps import Principal
    from services.platform.errors import GatewayError

    stranger = Principal(
        {
            "sub": str(uuid4()),
            "tenant_id": str(mem_seed.tenant_id),
            "roles": ["member"],
            "scopes": ["memory:read", "memory:write"],
            "typ": "access",
            "jti": uuid4().hex,
        }
    )
    admin = Principal(
        {
            "sub": str(uuid4()),
            "tenant_id": str(mem_seed.tenant_id),
            "roles": ["admin"],
            "scopes": ["memory:read", "memory:write"],
            "typ": "access",
            "jti": uuid4().hex,
        }
    )
    async with mem_seed.factory() as db:
        written = await write_memory(
            MemoryWriteIn(
                level="l2",
                content="治理档恢复对象事实",
                category=FactCategory.FACT,
                source_refs=SourceRefsIn(session_id=mem_seed.session_id, message_ids=[]),
            ),
            principal=mem_seed.principal,
            db=db,
            l1=_store(mem_seed),
        )
        await db.commit()
        await invalidate_fact(
            written.fact_id, FactInvalidateIn(reason="治理流程演示"), principal=mem_seed.principal, db=db
        )
        await db.commit()
        # Act / Assert：他人（非 admin）→ 403+2002
        with pytest.raises(GatewayError) as ei:
            await restore_fact(written.fact_id, principal=stranger, db=db)
        assert (ei.value.code, ei.value.status_code) == (2002, 403)
        # Act / Assert：admin（同租户）→ 放行（治理档 v1：本人或 admin 可 restore）
        out = await restore_fact(written.fact_id, principal=admin, db=db)
        assert out.restored_at is not None
        # Act / Assert：未知事实 → 404（不泄露存在性）
        with pytest.raises(GatewayError) as ei2:
            await restore_fact(uuid4(), principal=mem_seed.principal, db=db)
        assert ei2.value.status_code == 404
        await db.commit()


async def test_list_invalidated_active过滤与倒序(mem_seed):
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        f1 = L2Fact(
            id=uuid4(),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            content="归档甲",
            category=FactCategory.FACT,
        )
        f2 = L2Fact(
            id=uuid4(),
            tenant_id=mem_seed.tenant_id,
            user_id=mem_seed.user_id,
            content="归档乙",
            category=FactCategory.FACT,
        )
        await repo.add(f1)
        await repo.add(f2)
        await db.commit()
        await invalidate_fact(f1.id, FactInvalidateIn(reason="旧"), principal=mem_seed.principal, db=db)
        await invalidate_fact(f2.id, FactInvalidateIn(reason="新"), principal=mem_seed.principal, db=db)
        await db.commit()
        # Act / Assert：active（生效中）= 2 条，invalidated_at 倒序（f2 先）
        rows = await repo.list_invalidated(mem_seed.user_id)
        assert [r.reason for r in rows] == ["新", "旧"] and all(r.active for r in rows)
        # Act / Assert：f1 restore 后 active_only 只剩 f2；全量仍 2 条
        await repo.restore(f1.id, now=datetime.now(UTC))
        await db.commit()
        active = await repo.list_invalidated(mem_seed.user_id, active_only=True)
        assert [r.reason for r in active] == ["新"]
        allrows = await repo.list_invalidated(mem_seed.user_id, active_only=False)
        assert {r.reason for r in allrows} == {"新", "旧"}
        assert all(r.restored_at is not None for r in allrows if r.reason == "旧")


async def test_restore_从未失效_409(mem_seed):
    from services.platform.errors import GatewayError

    async with mem_seed.factory() as db:
        written = await write_memory(
            MemoryWriteIn(
                level="l2",
                content="从未失效的健康事实",
                category=FactCategory.FACT,
                source_refs=SourceRefsIn(session_id=mem_seed.session_id, message_ids=[]),
            ),
            principal=mem_seed.principal,
            db=db,
            l1=_store(mem_seed),
        )
        await db.commit()
        with pytest.raises(GatewayError) as ei:
            await restore_fact(written.fact_id, principal=mem_seed.principal, db=db)
        assert ei.value.status_code == 409
