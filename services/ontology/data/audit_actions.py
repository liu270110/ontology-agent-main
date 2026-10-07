"""ontology 审计写入助手（ONT-1.6）：平台审计通道 audit_logs 的 repo 级同事务写入。

动作词汇单点=services.ontology.domain.model.audit_actions.OntologyAuditAction（L4 纯枚举）；
写入通道循 tools 先例（services/tools/data/repo_impl/tool_repo.py record_audit）——repo 级
同事务裸 SQL INSERT，created_at 取 clock_timestamp()（同一事务内多笔审计时间戳严格递增，
动作序可复原）；ontology 元素突变三件套（新快照/审计/criterion 对账）必须与业务写同事务，
故不走网关审计中间件通道（中间件 action=METHOD /路由模板，与本动作族互不重叠）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.ontology.domain.model.audit_actions import OntologyAuditAction

__all__ = ["OntologyAuditAction", "record_ontology_audit"]

# ONT-1.4 引用完整性第 4 档不变式（06 篇）：平台审计对本体对象只存标识/摘要，不设 FK。
_RESOURCE_TYPES: dict[str, str] = {
    "changeset": "ontology_changeset",
    "element": "ontology_element",
    "ontology": "ontology",
}


async def record_ontology_audit(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: OntologyAuditAction | str,
    actor_id: uuid.UUID | None,
    ontology_id: uuid.UUID,
    digest: dict[str, Any],
    trace_id: str | None = None,
    subject: str = "ontology",
    subject_id: uuid.UUID | None = None,
) -> None:
    """同事务写一条 ontology.* 审计行（audit_logs；resource 无 FK=ONT-1.4 第 4 档不变式）。

    `subject`：changeset（resource_id=变更单 id）/ element / ontology（resource_id=本体 id，
    元素身份入 digest——元素 IRI 最长 256 超 resource_id String(64) 容量，故恒以本体 id 落列）。
    必须在调用方会话事务内调用（随调用方提交；业务写回滚即随审计一并消失——三件套同事务语义）。
    """
    action_str = action.value if isinstance(action, OntologyAuditAction) else str(action)
    resource_type = _RESOURCE_TYPES.get(subject, subject)
    resource_id = str(subject_id) if (subject == "changeset" and subject_id) else str(ontology_id)
    await db.execute(
        text(
            "INSERT INTO audit_logs (id, tenant_id, actor_type, actor_id, action, resource_type, "
            "resource_id, params_digest, result, trace_id, created_at) "
            "VALUES (CAST(:id AS uuid), CAST(:tenant_id AS uuid), 'user', CAST(:actor_id AS uuid), "
            ":action, :resource_type, :resource_id, CAST(:digest AS jsonb), 'success', :trace_id, "
            "clock_timestamp())"
        ),
        {
            "id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "actor_id": str(actor_id) if actor_id is not None else None,
            "action": action_str,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "digest": json.dumps(digest, ensure_ascii=False, sort_keys=True, default=str),
            "trace_id": trace_id,
        },
    )
