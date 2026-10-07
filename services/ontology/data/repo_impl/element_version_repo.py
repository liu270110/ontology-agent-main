"""OntologyElementVersionRepo：元素定义快照（ONT-1.1/1.2/1.6，append-only 机制单点实现）。

写入时机（契约 06 篇 §ONT-1.1）：元素创建/定义变化时**同事务**插入新快照并作废旧快照
（作废=UPDATE superseded_at/superseded_by——append-only 指行不删，允许这一次定向 UPDATE，
永不 DELETE）；同 definition_hash 判等跳过（不产生新快照行）。

元素定义突变三件套（§ONT-1.6）：①新定义快照（定义变时）②审计 definition.superseded
③criterion.changed 对账（仅 rule/axiom 定义变化时）——三笔写共享调用方会话事务。

白名单（§ONT-1.2）单点定义=services.ontology.domain.model.ontology_read_model.DEFINITION_FIELDS；
hash 口径单点=sha256_canonical（canonical_json：键递归排序+紧凑分隔符）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils.compat import uuid7

from services.ontology.data.audit_actions import record_ontology_audit
from services.ontology.data.orm import OntologyElementVersion as OntologyElementVersionORM
from services.ontology.domain.model.audit_actions import OntologyAuditAction
from services.ontology.domain.model.ontology_read_model import (
    project_definition,
    sha256_canonical,
)

_CRITERION_ELEMENT_TYPES = ("rule", "axiom")  # 三件套③仅规则/公理定义变化需要对账（§ONT-1.6）


def _now() -> datetime:
    return datetime.now(UTC)


class OntologyElementVersionRepo:
    """ontology_element_versions 仓储（租户作用域构造期绑定；须在调用方会话事务内使用）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get_active_snapshot(
        self, ontology_id: uuid.UUID, *, element_type: str, element_key: str
    ) -> OntologyElementVersionORM | None:
        """当前活跃定义快照（superseded_at IS NULL；部分唯一索引保证至多一行）。"""
        stmt = select(OntologyElementVersionORM).where(
            OntologyElementVersionORM.tenant_id == self._tenant_id,
            OntologyElementVersionORM.ontology_id == ontology_id,
            OntologyElementVersionORM.element_type == element_type,
            OntologyElementVersionORM.element_key == element_key,
            OntologyElementVersionORM.superseded_at.is_(None),
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_snapshots(
        self, ontology_id: uuid.UUID, *, element_type: str, element_key: str
    ) -> list[OntologyElementVersionORM]:
        """全历史快照链（created_at 升序；作废链测试/追溯面，只读不筛选 status）。"""
        stmt = (
            select(OntologyElementVersionORM)
            .where(
                OntologyElementVersionORM.tenant_id == self._tenant_id,
                OntologyElementVersionORM.ontology_id == ontology_id,
                OntologyElementVersionORM.element_type == element_type,
                OntologyElementVersionORM.element_key == element_key,
            )
            .order_by(OntologyElementVersionORM.created_at.asc(), OntologyElementVersionORM.id.asc())
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def snapshot(
        self,
        ontology_id: uuid.UUID,
        element_type: str,
        element_key: str,
        definition_fields: dict,
        *,
        created_by: uuid.UUID | None = None,
        trace_id: str | None = None,
    ) -> OntologyElementVersionORM:
        """定义快照写入（ONT-1.1）：白名单投影 → hash 判等 → 同 hash 跳过 / 变 hash 作废旧+插新。

        返回操作后的**活跃**快照行（跳过时即既有活跃行；变更时即新插入行）。白名单外键
        静默剔除（project_definition，ONT-1.2「不算改定义」）；未知元素类型 ValueError 拒绝。
        突变三件套（§ONT-1.6）随本调用同事务落库：新快照 + definition.superseded 审计 +
        （rule/axiom）criterion.changed 对账。
        """
        definition = project_definition(element_type, definition_fields)  # 未知类型在此拒绝
        definition_hash = sha256_canonical(definition)
        active = await self.get_active_snapshot(ontology_id, element_type=element_type, element_key=element_key)
        if active is not None:
            if active.tenant_id != self._tenant_id:  # 防御：禁止跨租户写（06 篇 §1 repo_impl 纪律）
                raise ValueError("租户不匹配：拒绝读写他租户定义快照")
            if active.definition_hash == definition_hash:
                return active  # 同定义判等：跳过，不产生新快照行
        # 显式 uuid7 主键；作废/插入分三步 flush（FK 时序 + 部分唯一双赢）：
        # ① 旧快照摘除活跃（superseded_at 先行——部分唯一索引随即放行新活跃行）
        # ② INSERT 新快照（行落地，superseded_by 才有可指目标）
        # ③ 回填 superseded_by 指针（append-only 的唯一一次定向 UPDATE 完成）
        new_row = OntologyElementVersionORM(
            id=uuid7(),
            tenant_id=self._tenant_id,
            ontology_id=ontology_id,
            element_type=element_type,
            element_key=element_key,
            definition=definition,
            definition_hash=definition_hash,
            created_by=created_by,
        )
        if active is not None:
            active.superseded_at = _now()
            await self._db.flush()
        self._db.add(new_row)
        await self._db.flush()
        if active is not None:
            active.superseded_by = new_row.id
            await self._db.flush()
        if active is not None:
            digest = {
                "element_type": element_type,
                "element_key": element_key,
                "old_hash": active.definition_hash,
                "new_hash": definition_hash,
                "superseded_by": str(new_row.id),
            }
            await record_ontology_audit(
                self._db,
                tenant_id=self._tenant_id,
                action=OntologyAuditAction.DEFINITION_SUPERSEDED,
                actor_id=created_by,
                ontology_id=ontology_id,
                digest=digest,
                trace_id=trace_id,
            )
            if element_type in _CRITERION_ELEMENT_TYPES:  # 三件套③：rule/axiom 定义变 → 对账审计
                await record_ontology_audit(
                    self._db,
                    tenant_id=self._tenant_id,
                    action=OntologyAuditAction.CRITERION_CHANGED,
                    actor_id=created_by,
                    ontology_id=ontology_id,
                    digest={**digest, "reason": "definition_superseded"},
                    trace_id=trace_id,
                )
        return new_row
