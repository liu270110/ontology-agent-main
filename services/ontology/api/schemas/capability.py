"""L2 网关 DTO：能力读模型（ONT-2.2/2.3；api/01 登记册风格，本批只读面）。

铁律：extra="forbid"、snake_case、只数据无行为；路由层只与 DTO 打交道（ontology 同款）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class CapabilityOut(BaseModel):
    """能力行（capabilities 读模型直映；withdrawn 双标记随行留痕，ONT-1.3 同口径）。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    ontology_id: uuid.UUID
    version_id: uuid.UUID
    iri: str
    kind: str
    name: str
    label: str | None = None
    description: str | None = None
    requires: list[Any] = []
    produces: list[Any] = []
    grants: list[Any] = []
    constrained_by: str | None = None
    execution: dict[str, Any]
    serves_task: list[Any] = []
    binds_action: str | None = None
    current_definition_version_id: uuid.UUID | None = None
    source: str
    withdrawn_at: datetime | None = None
    withdrawn_reason: str | None = None
    created_at: datetime | None = None


class CapabilityListOut(BaseModel):
    """GET /ontologies/{id}/capabilities 清单（offset/limit 分页 + total）。"""

    model_config = ConfigDict(extra="forbid")

    ontology_id: uuid.UUID
    items: list[CapabilityOut]
    total: int
    offset: int
    limit: int


def capability_from_domain(row: Any) -> CapabilityOut:
    """ORM 行 → L2 DTO（机械转换，保持层界；schemas/ontology.from_domain 同款）。"""
    return CapabilityOut(
        id=row.id,
        ontology_id=row.ontology_id,
        version_id=row.version_id,
        iri=row.iri,
        kind=row.kind,
        name=row.name,
        label=row.label,
        description=row.description,
        requires=list(row.requires or []),
        produces=list(row.produces or []),
        grants=list(row.grants or []),
        constrained_by=row.constrained_by,
        execution=dict(row.execution or {}),
        serves_task=list(row.serves_task or []),
        binds_action=row.binds_action,
        current_definition_version_id=row.current_definition_version_id,
        source=row.source,
        withdrawn_at=row.withdrawn_at,
        withdrawn_reason=row.withdrawn_reason,
        created_at=row.created_at,
    )
