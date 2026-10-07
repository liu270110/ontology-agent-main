"""L2 事实仓储 PG 实现（services.memory.domain.repo.fact_repo.L2FactRepository）。

租户作用域构造期绑定（04 §4）；仓储不提交——提交归调用方会话管理
（SessionDep 自动提交 / 后台任务显式 commit）。

裸 SQL 纪律（2026-09-28 接管收口修订）：本文件以 ORM 为主；文末「记忆审计执行层」段
为唯一裸 SQL 例外——audit_logs 归 iam.data 模块私有（import-linter 禁 memory import），
读写走 raw SQL + information_schema 列探测（前身为 data/audit.py，因「memory.data 模块
私有」契约仅豁免本模块与 data.l1 两条消费边而并入；data/audit.py 留迁移指针）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from services.memory.data.orm import MemoryL2Fact as MemoryL2FactORM
from services.memory.data.orm import MemoryL2FactInvalidation as MemoryL2FactInvalidationORM
from services.memory.data.vector import EMBED_MODEL, set_fact_embedding
from services.memory.domain.model.l2_fact import FactCategory, FactInvalidation, FactStatus, L2Fact


def _to_domain(row: MemoryL2FactORM) -> L2Fact:
    return L2Fact(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        agent_id=row.agent_id,
        content=row.content,
        category=FactCategory(row.category),
        confidence=float(row.confidence),
        decay_score=float(row.decay_score),
        embedding_ref=row.embedding_ref,
        source_session_id=row.source_session_id,
        source_message_ids=[UUID(item) for item in (row.source_message_ids or [])],
        status=FactStatus(row.status),
        supersedes_id=row.supersedes_id,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        fingerprint=row.fingerprint,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PgL2FactRepository:
    """memory_l2_facts 仓储（查询方法按用例定制，04 §4）。"""

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def get(self, fact_id: UUID) -> L2Fact | None:
        row = await self._get_orm(fact_id)
        return None if row is None else _to_domain(row)

    async def find_by_fingerprint(self, user_id: UUID, fingerprint: str) -> L2Fact | None:
        """幂等判重（memory §5.4：按指纹查既有事实，重复提交返回原 id）。

        墓碑语义（P3-3 文档化，2026-09-27）：本查询不过滤 status——invalidated/superseded
        的同指纹事实仍占据判重位，**永久抑制同指纹再写入**（防复活口径：失效/被取代的事实
        不得因重放/重提交而复活；更正须走新事实 + supersedes 版本链，见 domain 模型注释）。
        """
        row = (
            await self._session.execute(
                select(MemoryL2FactORM).where(
                    MemoryL2FactORM.tenant_id == self._tenant_id,
                    MemoryL2FactORM.user_id == user_id,
                    MemoryL2FactORM.fingerprint == fingerprint,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def add(self, fact: L2Fact) -> None:
        """新增事实（唯一索引 uk_memory_l2_facts_tenant_user_fingerprint 兜底并发判重）。

        并发同指纹（TOCTOU，P2-3/P3-4）：INSERT+flush 在 SAVEPOINT（begin_nested）内执行——
        唯一约束冲突抛 IntegrityError 时仅回滚保存点，不污染外层事务，调用方可捕获后按
        「另一写入者已落库」计 duplicates 继续写余量（consolidation writing 步消费口径）。
        """
        async with self._session.begin_nested():
            self._session.add(
                MemoryL2FactORM(
                    id=fact.id,
                    tenant_id=fact.tenant_id,
                    user_id=fact.user_id,
                    agent_id=fact.agent_id,
                    content=fact.content,
                    category=fact.category.value,
                    confidence=round(fact.confidence, 3),
                    decay_score=fact.decay_score,
                    embedding_ref=fact.embedding_ref,
                    source_session_id=fact.source_session_id,
                    source_message_ids=[str(m) for m in fact.source_message_ids],
                    status=fact.status.value,
                    supersedes_id=fact.supersedes_id,
                    valid_from=fact.valid_from,
                    valid_to=fact.valid_to,
                    keywords=[],
                    fingerprint=fact.fingerprint,
                    created_at=fact.created_at,
                    updated_at=fact.updated_at,
                )
            )
            await self._session.flush()

    async def save_state(self, fact: L2Fact) -> None:
        row = await self._get_orm(fact.id)
        if row is None:
            return
        row.status = fact.status.value
        row.supersedes_id = fact.supersedes_id
        row.valid_to = fact.valid_to
        row.decay_score = fact.decay_score
        row.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def archive_invalidated(self, fact: L2Fact, *, reason: str, invalidated_at: datetime) -> None:
        """失效即归档（K2-a §11.1）：影子行与 save_state 同会话同事务（本仓储不提交）。

        content=失效时事实文本快照（主表后续 UPDATE 不影响追溯）；reason 必填由领域层
        invalidate 与 API DTO 双层把守，此处兜底拒空（fail-closed）。
        """
        if not (reason and reason.strip()):
            raise ValueError("失效影子行 reason 必填（§11.1：无 reason 拒绝失效）")
        self._session.add(
            MemoryL2FactInvalidationORM(
                id=uuid.uuid4(),
                tenant_id=self._tenant_id,
                fact_id=fact.id,
                user_id=fact.user_id,
                content=fact.content,
                reason=reason,
                invalidated_at=invalidated_at,
                restored_at=None,
            )
        )
        await self._session.flush()

    async def list_invalidated(
        self,
        user_id: UUID,
        *,
        active_only: bool = True,
        offset: int = 0,
        limit: int = 50,
    ) -> list[FactInvalidation]:
        """用户失效归档分页（invalidated_at 倒序；active_only 仅 restored_at 为空的生效行）。"""
        stmt = select(MemoryL2FactInvalidationORM).where(
            MemoryL2FactInvalidationORM.tenant_id == self._tenant_id,
            MemoryL2FactInvalidationORM.user_id == user_id,
        )
        if active_only:
            stmt = stmt.where(MemoryL2FactInvalidationORM.restored_at.is_(None))
        stmt = stmt.order_by(MemoryL2FactInvalidationORM.invalidated_at.desc()).offset(offset).limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_invalidation_to_domain(row) for row in rows]

    async def restore(self, fact_id: UUID, *, now: datetime) -> FactInvalidation | None:
        """影子层可见性恢复（K2-a §11.1）：最新生效影子行 restored_at 回填；非复活——
        主表 fact 不动（INVALIDATED 终态）。无生效影子行（未失效过/已恢复）返回 None。
        """
        row = (
            await self._session.execute(
                select(MemoryL2FactInvalidationORM)
                .where(
                    MemoryL2FactInvalidationORM.tenant_id == self._tenant_id,
                    MemoryL2FactInvalidationORM.fact_id == fact_id,
                    MemoryL2FactInvalidationORM.restored_at.is_(None),
                )
                .order_by(MemoryL2FactInvalidationORM.invalidated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        shadow = _invalidation_to_domain(row)
        shadow.restore(now)  # 领域方法回填（幂等/时间倒置守卫）
        row.restored_at = shadow.restored_at
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return shadow

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        status: FactStatus | None = None,
        category: FactCategory | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[L2Fact]:
        stmt = select(MemoryL2FactORM).where(
            MemoryL2FactORM.tenant_id == self._tenant_id,
            MemoryL2FactORM.user_id == user_id,
        )
        if status is not None:
            stmt = stmt.where(MemoryL2FactORM.status == status.value)
        if category is not None:
            stmt = stmt.where(MemoryL2FactORM.category == category.value)
        stmt = stmt.order_by(MemoryL2FactORM.created_at.desc()).offset(offset).limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row) for row in rows]

    async def search_candidates(self, user_id: UUID, query: str, *, limit: int) -> list[L2Fact]:
        """关键词通道（ILIKE 顶片）；向量通道随 M3 嵌入接入（过渡方案，见报告欠账）。"""
        terms = _query_terms(query)
        if not terms:
            return []
        stmt = (
            select(MemoryL2FactORM)
            .where(
                MemoryL2FactORM.tenant_id == self._tenant_id,
                MemoryL2FactORM.user_id == user_id,
                MemoryL2FactORM.status == FactStatus.ACTIVE.value,
                or_(*(MemoryL2FactORM.content.ilike(f"%{term}%") for term in terms)),
            )
            .order_by(MemoryL2FactORM.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row) for row in rows]

    async def recent_candidates(self, user_id: UUID, *, limit: int) -> list[L2Fact]:
        stmt = (
            select(MemoryL2FactORM)
            .where(
                MemoryL2FactORM.tenant_id == self._tenant_id,
                MemoryL2FactORM.user_id == user_id,
                MemoryL2FactORM.status == FactStatus.ACTIVE.value,
            )
            .order_by(MemoryL2FactORM.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row) for row in rows]

    async def save_embedding(self, fact_id: UUID, vector: Sequence[float], *, model: str | None = None) -> bool:
        """向量回写（raw SQL 委托 data/vector.py——embedding 列不在 ORM 映射，embed.py 先例）。"""
        return await set_fact_embedding(self._session, fact_id=fact_id, vector=vector, model=model or EMBED_MODEL)

    async def chain_for_user(self, user_id: UUID, fact_id: UUID, *, limit: int = 50) -> list[L2Fact]:
        """版本链双向回放（timeline 数据面；协议注释=契约，此实现按代际从旧到新返回）。"""
        anchor = await self.get(fact_id)
        if anchor is None or anchor.user_id != user_id:
            return []
        older: list[L2Fact] = []
        current = anchor
        while current.supersedes_id is not None and len(older) < limit:
            prev = await self.get(current.supersedes_id)
            if prev is None or prev.user_id != user_id:  # 链断（跨用户/缺失）即止
                break
            older.append(prev)
            current = prev
        newer: list[L2Fact] = []
        current = anchor
        while len(older) + len(newer) < limit:
            rows = (
                (
                    await self._session.execute(
                        select(MemoryL2FactORM).where(
                            MemoryL2FactORM.tenant_id == self._tenant_id,
                            MemoryL2FactORM.user_id == user_id,
                            MemoryL2FactORM.supersedes_id == current.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                break
            successor = _to_domain(rows[0])
            newer.append(successor)
            current = successor
        return [*reversed(older), anchor, *newer]

    async def _get_orm(self, fact_id: UUID) -> MemoryL2FactORM | None:
        row = (
            await self._session.execute(
                select(MemoryL2FactORM).where(
                    MemoryL2FactORM.id == fact_id,
                    MemoryL2FactORM.tenant_id == self._tenant_id,
                )
            )
        ).scalar_one_or_none()
        return cast(MemoryL2FactORM | None, row)


def _invalidation_to_domain(row: MemoryL2FactInvalidationORM) -> FactInvalidation:
    """影子行 ORM → 领域对象（K2-a §11.1；列白名单与 ORM 精确相等）。"""
    return FactInvalidation(
        id=row.id,
        tenant_id=row.tenant_id,
        fact_id=row.fact_id,
        user_id=row.user_id,
        content=row.content,
        reason=row.reason,
        invalidated_at=row.invalidated_at,
        restored_at=row.restored_at,
    )


def _query_terms(query: str, *, min_len: int = 2, max_terms: int = 8) -> list[str]:
    """查询词顶片：按空白切分、去重、长度下限过滤（确定性规则，无 LLM）。"""
    seen: dict[str, None] = {}
    for token in query.split():
        token = token.strip("，。；、!?,.;:（）()\"'`")
        if len(token) >= min_len:
            seen.setdefault(token, None)
        if len(seen) >= max_terms:
            break
    return list(seen)


# ---------------------------------------------------------------- 记忆审计执行层（api/01 §5.5 ★ 端点）

# audit_logs 归 iam.data 模块私有（import-linter「iam.data 模块私有」禁 memory import），
# 读写统一走本段 raw SQL + information_schema 列探测（原 data/audit.py 全量并入，函数语义
# 不变）；表缺失（未迁移环境）→ 查询空列表 / 登记抛降级异常，调用方降级消化。
# 本段被 api/memory.py 与 business/runtime.py 消费——二者对 data 层仅本模块与 data.l1
# 两条豁免边（pyproject 冻结，新增消费面必须复用既有边，禁另开模块）。

_AUDIT_READY_SQL = "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'audit_logs')"

_MEMORY_ACTION_PREDICATE = "(action LIKE 'memory.%' OR action LIKE '%/memory%')"

_AUDIT_FIELDS = (
    "id, actor_type, actor_id, action, resource_type, resource_id, params_digest, result, trace_id, created_at"
)


class MemoryAuditUnavailableError(RuntimeError):
    """审计存储不可用（audit_logs 表缺失）——调用方据此降级。"""


async def memory_audit_ready(session: AsyncSession) -> bool:
    """audit_logs 表是否存在（未迁移环境 → 查询降级空集/登记 503）。"""
    row = await session.execute(text(_AUDIT_READY_SQL))
    return bool(row.scalar())


async def query_memory_audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    offset: int = 0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """记忆域审计回放（tenant 硬过滤；user/session 可选过滤，均缺省=全租户记忆动作）。

    会话过滤走 params_digest->>'session_id'（promotions 登记行携带；中间件路径行无该键
    自然不命中）。表缺失 → 空列表（降级非失败，L1 同款口径）。
    """
    if not await memory_audit_ready(session):
        return []
    predicates = [_MEMORY_ACTION_PREDICATE]
    params: dict[str, Any] = {"tenant_id": str(tenant_id), "offset": offset, "limit": limit}
    if user_id is not None:
        predicates.append("actor_id = CAST(:user_id AS uuid)")
        params["user_id"] = str(user_id)
    if session_id is not None:
        predicates.append("params_digest->>'session_id' = :session_id")
        params["session_id"] = str(session_id)
    rows = await session.execute(
        text(
            f"SELECT {_AUDIT_FIELDS} FROM audit_logs "
            f"WHERE tenant_id = CAST(:tenant_id AS uuid) AND {' AND '.join(predicates)} "
            "ORDER BY created_at DESC, id OFFSET :offset LIMIT :limit"
        ),
        params,
    )
    return [dict(r) for r in rows.mappings()]


async def promotion_exists(session: AsyncSession, *, tenant_id: uuid.UUID, fact_id: uuid.UUID) -> bool:
    """同事实是否已有升级登记行（POST /memory/promotions 409* 冲突判定面；表缺失=False 放行首登记）。"""
    if not await memory_audit_ready(session):
        return False
    row = await session.execute(
        text(
            "SELECT 1 FROM audit_logs WHERE tenant_id = CAST(:tenant_id AS uuid) "
            "AND action = 'memory.promotion' AND resource_id = :fact_id LIMIT 1"
        ),
        {"tenant_id": str(tenant_id), "fact_id": str(fact_id)},
    )
    return row.first() is not None


async def query_promotions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    offset: int = 0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """升级单登记行回放（GET /memory/promotions：audit_logs 投影，created_at 倒序分页）。

    M5 审核工作流接入前登记面=audit_logs 行本身（memory §1 裁决框），故记录即审计行投影；
    表缺失 → 空列表（占位面契约形状恒成立，audit 查询同款降级口径）。
    """
    if not await memory_audit_ready(session):
        return []
    predicates = ["action = 'memory.promotion'"]
    params: dict[str, Any] = {"tenant_id": str(tenant_id), "offset": offset, "limit": limit}
    if user_id is not None:
        predicates.append("actor_id = CAST(:user_id AS uuid)")
        params["user_id"] = str(user_id)
    rows = await session.execute(
        text(
            f"SELECT {_AUDIT_FIELDS} FROM audit_logs "
            f"WHERE tenant_id = CAST(:tenant_id AS uuid) AND {' AND '.join(predicates)} "
            "ORDER BY created_at DESC, id OFFSET :offset LIMIT :limit"
        ),
        params,
    )
    return [dict(r) for r in rows.mappings()]


async def record_memory_promotion(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    fact_id: uuid.UUID,
    promotion_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    reason: str | None = None,
    trace_id: str | None = None,
) -> None:
    """L2→L3 升级申请单登记（占位面：仅审计留痕，不写 L3、不建工单——memory §1 裁决框）。"""
    if not await memory_audit_ready(session):
        raise MemoryAuditUnavailableError("audit_logs 表不可用（迁移未应用），升级单无法登记")
    digest = {
        "promotion_id": str(promotion_id),
        "fact_id": str(fact_id),
        "from_layer": "l2",
        "to_layer": "l3",
        "session_id": str(session_id) if session_id else None,
        "reason": (reason or "")[:200],  # params_digest=脱敏摘要（08 §3）：截断入账
        "note": "L2→L3 升级单占位登记：审核工作流随 M5 接入（memory §1/§2）",
    }
    await session.execute(
        text(
            "INSERT INTO audit_logs (id, tenant_id, actor_type, actor_id, action, resource_type, "
            "resource_id, params_digest, result, trace_id, created_at) "
            "VALUES (CAST(:id AS uuid), CAST(:tenant_id AS uuid), 'user', CAST(:actor_id AS uuid), "
            "'memory.promotion', 'memory', :resource_id, CAST(:digest AS jsonb), 'success', :trace_id, now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "actor_id": str(actor_id),
            "resource_id": str(fact_id),
            "digest": _json_dumps(digest),
            "trace_id": trace_id,
        },
    )


async def record_faithfulness_sample(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    digest: dict[str, Any],
    trace_id: str | None = None,
) -> bool:
    """对话忠实度抽检命中留痕：audit_logs 一行结构化 JSON（evaluation 汇欠账期的替代面）。

    - actor_type='system'（平台采样动作）、actor_id=NULL（ChatOutcome 无 user_id；
      数据主体经 digest.session_id 关联 sessions 表回溯）；
    - digest 携带对账标识 + 判定待定标记（llm_judge=pending，随评估批次回填）；
    - 表缺失 → False（调用方降级为仅结构化日志，不阻断对话流尾）。
    """
    if not await memory_audit_ready(session):
        return False
    await session.execute(
        text(
            "INSERT INTO audit_logs (id, tenant_id, actor_type, actor_id, action, resource_type, "
            "resource_id, params_digest, result, trace_id, created_at) "
            "VALUES (CAST(:id AS uuid), CAST(:tenant_id AS uuid), 'system', NULL, "
            "'chat.faithfulness_sample', 'chat_run', :resource_id, CAST(:digest AS jsonb), "
            "'success', :trace_id, now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "resource_id": str(run_id),
            "digest": _json_dumps(digest),
            "trace_id": trace_id,
        },
    )
    return True


def _json_dumps(payload: dict[str, Any]) -> str:
    """dict → JSON 字符串（CAST(:digest AS jsonb) 入参；std lib 即可，无第三方序列化面）。"""
    return json.dumps(payload, ensure_ascii=False)
