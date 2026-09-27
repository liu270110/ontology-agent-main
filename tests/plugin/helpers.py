"""plugin 测试共享件（进程内 Fake 与构造器；经 pytest prepend 导入 `import helpers`）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from services.plugin.domain.model.manifest import validate_manifest
from services.plugin.domain.model.plugin import Plugin, PluginKind, PluginVersion, ToolBinding
from services.review.domain.approval_chain import GovernanceTier

CHECKSUM = "b" * 64

VALID_SERVER_JSON: dict[str, Any] = {
    "name": "io.ontology-agent/weather",
    "display_name": "天气查询",
    "version": "1.0.0",
    "description": "按城市查天气",
    "transport": {"type": "streamable_http", "url": "https://weather.example.com/mcp"},
    "x-platform": {
        "schema_version": "1",
        "category": "data-tools",
        "required_scopes": ["weather:read"],
        "tools": [
            {
                "name": "weather.query",
                "description": "查询天气",
                "input_schema": {"type": "object"},
                "required_scopes": ["weather:read"],
            }
        ],
    },
}


@dataclass
class FakeTicket:
    id: uuid.UUID
    tenant_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    submitter_id: uuid.UUID | None
    status: str = "pending_review"
    payload: dict[str, Any] = field(default_factory=dict)


class FakeReviewPort:
    """review_tickets 进程内 Fake（uk_review_one_open 唯一 open 单同口径）。"""

    def __init__(self) -> None:
        self.tickets: dict[uuid.UUID, FakeTicket] = {}

    async def submit_candidate(
        self,
        *,
        tenant_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        payload: dict[str, Any],
        status: str = "pending_review",
        submitter_id: uuid.UUID | None = None,
        sla_deadline: Any = None,
    ) -> uuid.UUID:
        for t in self.tickets.values():
            if (
                t.tenant_id == tenant_id
                and t.target_type == target_type
                and t.target_id == target_id
                and t.status in ("draft", "pending_review")
            ):
                return t.id
        ticket = FakeTicket(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            target_type=target_type,
            target_id=target_id,
            submitter_id=submitter_id,
            status=status,
            payload=payload,
        )
        self.tickets[ticket.id] = ticket
        return ticket.id

    async def get_open_ticket(
        self, *, tenant_id: uuid.UUID, target_type: str, target_id: uuid.UUID
    ) -> dict[str, Any] | None:
        for t in self.tickets.values():
            if (
                t.tenant_id == tenant_id
                and t.target_type == target_type
                and t.target_id == target_id
                and t.status in ("draft", "pending_review")
            ):
                return {"id": t.id, "status": t.status, "submitter_id": t.submitter_id, "payload": dict(t.payload)}
        return None

    async def get_ticket(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID) -> dict[str, Any] | None:
        t = self.tickets.get(ticket_id)
        if t is None or t.tenant_id != tenant_id:
            return None
        return {
            "id": t.id,
            "status": t.status,
            "target_type": t.target_type,
            "target_id": t.target_id,
            "submitter_id": t.submitter_id,
            "payload": dict(t.payload),
        }

    async def mark_published(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID, note: str = "") -> None:
        t = self.tickets[ticket_id]
        assert t.status == "approved"
        t.status = "published"


@dataclass
class FakeTierReader:
    """档位读取 Fake（档位判定收敛点仍是 review.domain.approval_chain）。"""

    tier: GovernanceTier = GovernanceTier.SOLO

    async def get_tier(self, tenant_id: uuid.UUID) -> GovernanceTier:
        return self.tier


def published_plugin(
    slug: str = "weather", *, server_json: dict[str, Any] | None = None
) -> tuple[Plugin, PluginVersion]:
    """构造已走完审批链的发布态聚合（runtime 用例 Arrange 件）。"""
    p = Plugin(slug=slug, name=slug, kind=PluginKind.MCP_SERVER)
    v = PluginVersion(
        plugin_id=p.id,
        version="1.0.0",
        server_json=server_json or VALID_SERVER_JSON,
        artifact_key=f"plugin-packages/{p.id}/1.0.0/package.zip",
        checksum=CHECKSUM,
    )
    validate_manifest(v.server_json)
    v.record_scan({"gate": "schema_v1"})
    v.publish()
    p.submit()
    p.pass_auto_gates()
    p.publish()
    return p, v


def new_binding(tenant_id: uuid.UUID, name: str, plugin_id: uuid.UUID) -> ToolBinding:
    return ToolBinding(
        tenant_id=tenant_id,
        name=name,
        kind="plugin",
        provider_ref={"plugin_id": str(plugin_id)},
    )
