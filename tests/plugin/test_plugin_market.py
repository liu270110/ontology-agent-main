"""插件市场全链测试（上架→审核→发布，治理三档各一例；进程内 Fake 装配，AAA+中文命名）。

覆盖：三档全链（solo 自批/team 禁自批+他人批/enterprise 双签四眼）、门禁失败退回、
重复提交 4701、发布后 runtime 注册联动、安装与启停。
"""

from __future__ import annotations

import uuid

import pytest
from helpers import CHECKSUM, VALID_SERVER_JSON, FakeReviewPort, FakeTierReader

from services.platform.kernel import DomainError
from services.plugin.business.lifecycle import PluginMarketService
from services.plugin.domain.model.plugin import Plugin, PluginKind, PluginStatus, PluginVersion, ToolBinding
from services.plugin.runtime.registry import PluginRuntime
from services.review.domain.approval_chain import GovernanceTier


class FakePluginRepository:
    def __init__(self) -> None:
        self.plugins: dict[uuid.UUID, Plugin] = {}
        self.versions: dict[uuid.UUID, PluginVersion] = {}

    async def get(self, plugin_id: uuid.UUID) -> Plugin | None:
        return self.plugins.get(plugin_id)

    async def get_by_slug(self, slug: str) -> Plugin | None:
        return next((p for p in self.plugins.values() if p.slug == slug), None)

    async def add(self, plugin: Plugin) -> None:
        self.plugins[plugin.id] = plugin

    async def save(self, plugin: Plugin) -> None:
        self.plugins[plugin.id] = plugin

    async def list_market(self, *, status=None, offset: int = 0, limit: int = 20) -> list[Plugin]:
        if status is not None:
            items = [p for p in self.plugins.values() if p.status is status]
        else:
            items = [p for p in self.plugins.values() if p.status is not PluginStatus.DEPRECATED]
        return items[offset : offset + limit]

    async def add_version(self, version: PluginVersion) -> None:
        self.versions[version.id] = version

    async def get_version(self, version_id: uuid.UUID) -> PluginVersion | None:
        return self.versions.get(version_id)

    async def find_version(self, plugin_id: uuid.UUID, version: str) -> PluginVersion | None:
        return next((v for v in self.versions.values() if v.plugin_id == plugin_id and v.version == version), None)

    async def list_versions(self, plugin_id: uuid.UUID) -> list[PluginVersion]:
        return [v for v in self.versions.values() if v.plugin_id == plugin_id]

    async def save_version(self, version: PluginVersion) -> None:
        self.versions[version.id] = version


class FakeBindingRepository:
    def __init__(self, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.items: dict[uuid.UUID, ToolBinding] = {}

    async def add(self, binding: ToolBinding) -> None:
        self.items[binding.id] = binding

    async def get(self, tool_id: uuid.UUID) -> ToolBinding | None:
        return self.items.get(tool_id)

    async def get_by_name(self, name: str) -> ToolBinding | None:
        return next((b for b in self.items.values() if b.name == name and b.tenant_id == self.tenant_id), None)

    async def list_by_plugin(self, plugin_id: uuid.UUID) -> list[ToolBinding]:
        return [
            b
            for b in self.items.values()
            if b.tenant_id == self.tenant_id and b.provider_ref.get("plugin_id") == str(plugin_id)
        ]

    async def save_enabled(self, binding: ToolBinding) -> None:
        self.items[binding.id] = binding


def _market(
    tier: GovernanceTier, runtime: PluginRuntime | None = None
) -> tuple[PluginMarketService, FakeReviewPort, uuid.UUID, PluginRuntime | None]:
    repo = FakePluginRepository()
    tenant_id = uuid.uuid4()
    review = FakeReviewPort()
    service = PluginMarketService(
        repo=repo,
        bindings=FakeBindingRepository(tenant_id),
        review=review,
        approvals=FakeApprovalServiceShim(review, FakeTierReader(tier)),
        runtime=runtime,
    )
    return service, review, tenant_id, runtime


class FakeApprovalServiceShim:
    """以 FakeReviewPort 复刻 ReviewApprovalService 决策面（判定仍走收敛纯函数）。"""

    def __init__(self, review: FakeReviewPort, tier_reader: FakeTierReader) -> None:
        self._review = review
        self._tier_reader = tier_reader
        from services.review.business.candidates import DecisionResult
        from services.review.domain.approval_chain import resolve_decision

        self._resolve = resolve_decision
        self._result_type = DecisionResult

    async def tier(self, tenant_id: uuid.UUID) -> GovernanceTier:
        return await self._tier_reader.get_tier(tenant_id)

    async def decide(
        self,
        *,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        action: str,
        approver_id: uuid.UUID,
        note: str = "",
    ):
        ticket = await self._review.get_ticket(tenant_id=tenant_id, ticket_id=ticket_id)
        if ticket is None:
            raise LookupError(f"审核单不存在: {ticket_id}")
        if ticket["status"] != "pending_review":
            raise DomainError(f"4703 REVIEW_TICKET_NOT_OPEN: 单据非 pending_review 状态（当前 {ticket['status']}）")
        tier = await self._tier_reader.get_tier(tenant_id)
        prior: list[uuid.UUID] = []
        for item in ticket["payload"].get("approvals") or []:
            prior.append(uuid.UUID(str(item["approver_id"])))
        decision = self._resolve(
            tier,
            action=action,  # type: ignore[arg-type]
            submitter_id=ticket["submitter_id"],
            approver_id=approver_id,
            prior_approvers=tuple(prior),
        )
        if not decision.allowed:
            raise DomainError(f"4702 APPROVER_NOT_ALLOWED: {decision.reason}")
        payload = dict(ticket["payload"])
        approvals = list(payload.get("approvals") or [])
        approvals.append({"action": action, "approver_id": str(approver_id), "governance_tier": tier.value})
        payload["approvals"] = approvals
        t = self._review.tickets[ticket_id]
        t.payload = payload
        if decision.complete:
            t.status = "approved" if action == "approve" else "rejected"
        return self._result_type(
            ticket_id=ticket_id,
            status=("approved" if action == "approve" else "rejected") if decision.complete else "pending_review",
            tier=tier,
            decision=decision,
        )


async def _submit_listing(
    service: PluginMarketService, tenant_id: uuid.UUID, submitter_id: uuid.UUID
) -> tuple[Plugin, PluginVersion, uuid.UUID]:
    plugin, ver = await service.create_listing(
        tenant_id=tenant_id,
        publisher_id=submitter_id,
        slug="weather",
        name="天气插件",
        kind=PluginKind.MCP_SERVER,
        version="1.0.0",
        server_json=VALID_SERVER_JSON,
        artifact_key=f"plugin-packages/x/{CHECKSUM[:8]}/package.zip",
        checksum=CHECKSUM,
    )
    ticket_id = await service.submit_for_review(
        tenant_id=tenant_id, plugin_id=plugin.id, version_id=ver.id, submitter_id=submitter_id, trace_id="t1"
    )
    return plugin, ver, ticket_id


async def test_solo档全链_提交人自批_上架发布并进runtime():
    # Arrange
    runtime = PluginRuntime()
    service, _review, tenant_id, runtime = _market(GovernanceTier.SOLO, runtime)
    plugin, ver, ticket_id = await _submit_listing(service, tenant_id, uuid.uuid4())
    submitter = plugin.publisher_id
    assert submitter is not None
    # Act：solo 提交人即审批人（留痕自批）
    outcome = await service.review_decision(
        tenant_id=tenant_id, ticket_id=ticket_id, action="approve", approver_id=submitter
    )
    # Assert：全链发布（工单 published → 版本 published → 插件 published）
    assert outcome.published and outcome.signatures_required == 1
    loaded_plugin = (await service.get_detail(plugin.id))[0]
    assert loaded_plugin.status is PluginStatus.PUBLISHED
    assert loaded_plugin.latest_version == "1.0.0"
    assert runtime is not None and runtime.get(plugin.id) is not None  # 发布联动：runtime 注册


async def test_team档全链_自批4702拒绝_他人审批通过():
    # Arrange
    service, _review, tenant_id, _rt = _market(GovernanceTier.TEAM)
    submitter, approver = uuid.uuid4(), uuid.uuid4()
    plugin, ver, ticket_id = await _submit_listing(service, tenant_id, submitter)
    # Act / Assert：自批 → 4702（禁自批，负向）
    with pytest.raises(DomainError) as exc:
        await service.review_decision(tenant_id=tenant_id, ticket_id=ticket_id, action="approve", approver_id=submitter)
    assert int(str(exc.value)[:4]) == 4702
    # Act：他人审批
    outcome = await service.review_decision(
        tenant_id=tenant_id, ticket_id=ticket_id, action="approve", approver_id=approver
    )
    # Assert
    assert outcome.published and outcome.ticket_status == "published"


async def test_enterprise档全链_双负责人四眼_第一签未生效第二签发布():
    # Arrange
    service, review, tenant_id, _rt = _market(GovernanceTier.ENTERPRISE)
    submitter, owner_a, owner_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    plugin, ver, ticket_id = await _submit_listing(service, tenant_id, submitter)
    # Act：第一负责人签（owner_business 语义位）
    first = await service.review_decision(
        tenant_id=tenant_id, ticket_id=ticket_id, action="approve", approver_id=owner_a
    )
    # Assert：签 1/2 —— 单据 pending_review、插件未发布（四眼未齐不得生效）
    assert not first.published and first.ticket_status == "pending_review"
    detail = await service.get_detail(plugin.id)
    assert detail[0].status is PluginStatus.IN_REVIEW
    # Act：同签人重复签 → 4702（负向）
    with pytest.raises(DomainError) as exc:
        await service.review_decision(tenant_id=tenant_id, ticket_id=ticket_id, action="approve", approver_id=owner_a)
    assert int(str(exc.value)[:4]) == 4702
    # Act：第二负责人签（owner_engineer 语义位）→ 发布
    second = await service.review_decision(
        tenant_id=tenant_id, ticket_id=ticket_id, action="approve", approver_id=owner_b
    )
    # Assert
    assert second.published and second.signatures_collected == 2
    assert review.tickets[ticket_id].status == "published"


async def test_门禁1失败_退回draft_修复后可再提交():
    # Arrange：非法清单（缺 transport）
    service, _review, tenant_id, _rt = _market(GovernanceTier.SOLO)
    publisher = uuid.uuid4()
    bad_json = {**VALID_SERVER_JSON}
    bad_json.pop("transport")
    plugin, ver = await service.create_listing(
        tenant_id=tenant_id,
        publisher_id=publisher,
        slug="bad",
        name="坏插件",
        kind=PluginKind.MCP_SERVER,
        version="0.1.0",
        server_json=bad_json,
        artifact_key="plugin-packages/x/0.1.0/package.zip",
        checksum=CHECKSUM,
    )
    # Act / Assert：提交 → 4503 门禁失败
    with pytest.raises(DomainError) as exc:
        await service.submit_for_review(
            tenant_id=tenant_id, plugin_id=plugin.id, version_id=ver.id, submitter_id=publisher
        )
    assert int(str(exc.value)[:4]) == 4503
    # Assert：仍在 draft（门禁失败退回边）
    assert (await service.get_detail(plugin.id))[0].status is PluginStatus.DRAFT


async def test_重复提交同版本_4701_同对象唯一open单():
    # Arrange
    service, _review, tenant_id, _rt = _market(GovernanceTier.SOLO)
    publisher = uuid.uuid4()
    plugin, ver, _ticket = await _submit_listing(service, tenant_id, publisher)
    # Act / Assert：同版本再提交 → 4701
    with pytest.raises(DomainError) as exc:
        await service.submit_for_review(
            tenant_id=tenant_id, plugin_id=plugin.id, version_id=ver.id, submitter_id=publisher
        )
    assert int(str(exc.value)[:4]) == 4701


async def test_驳回回draft_附理由_修改后可重新提交():
    # Arrange
    service, review, tenant_id, _rt = _market(GovernanceTier.TEAM)
    submitter, approver = uuid.uuid4(), uuid.uuid4()
    plugin, ver, ticket_id = await _submit_listing(service, tenant_id, submitter)
    # Act：驳回
    outcome = await service.review_decision(
        tenant_id=tenant_id, ticket_id=ticket_id, action="reject", approver_id=approver, note="扫描报告不足"
    )
    # Assert：插件回 draft（REJ 回边），工单 rejected
    assert outcome.ticket_status == "rejected"
    assert (await service.get_detail(plugin.id))[0].status is PluginStatus.DRAFT
    # Act：修复后重新提交 → 新 open 工单
    new_ticket = await service.submit_for_review(
        tenant_id=tenant_id, plugin_id=plugin.id, version_id=ver.id, submitter_id=submitter
    )
    assert new_ticket != ticket_id


async def test_安装与启停_未发布版本拒绝_安装默认停用_enable开启():
    # Arrange
    service, _review, tenant_id, _rt = _market(GovernanceTier.SOLO)
    publisher = uuid.uuid4()
    plugin, ver, ticket_id = await _submit_listing(service, tenant_id, publisher)
    # Act / Assert：未发布先安装 → 4502（负向）
    with pytest.raises(DomainError) as exc:
        await service.install(tenant_id=tenant_id, plugin_id=plugin.id)
    assert int(str(exc.value)[:4]) == 4502
    # Act：发布 → 安装
    await service.review_decision(tenant_id=tenant_id, ticket_id=ticket_id, action="approve", approver_id=publisher)
    bindings = await service.install(tenant_id=tenant_id, plugin_id=plugin.id)
    # Assert：weather.query 绑定默认停用（tools.enabled=false，外部默认不可信）
    assert [b.name for b in bindings] == ["weather.query"]
    assert bindings[0].enabled is False
    # Act：enable → enabled；disable → 停用（published↔suspended 启停面）
    enabled = await service.enable(tenant_id=tenant_id, plugin_id=plugin.id)
    assert all(b.enabled for b in enabled)
    disabled = await service.disable(tenant_id=tenant_id, plugin_id=plugin.id)
    assert all(not b.enabled for b in disabled)
    # Assert：聚合域真值 suspended（PG 存储投影回读为 published——三值 CHECK 见报告漂移节）
    assert (await service.get_detail(plugin.id))[0].status is PluginStatus.SUSPENDED
