"""L3 用例服务：插件市场全生命周期（M5-1 最小版；权威=docs/Skills §4 流水线 + api/01 §5.6）。

用例簇（一文件一用例函数簇）：
- :meth:`PluginMarketService.create_listing` —— 登记 draft 插件 + 首版本（submitted）；
- :meth:`PluginMarketService.submit_for_review` —— 服务端实跑门禁 1（schema 校验，宪法 3
  任何档位不可跳过）→ 版本 scan_passed → 插件 in_review → 候选进审（target_type=
  plugin_listing，复用 ReviewTicketService）；失败退回 draft（Skills §4 退回边）；
- :meth:`PluginMarketService.review_decision` —— 治理三档审批链（收敛于
  review.domain.approval_chain）+ 通过后发布联动（工单 published → 版本 published →
  插件 published → runtime 工具注册，08 §4 插件上架场景行）；
- :meth:`PluginMarketService.install/enable/disable` —— 租户安装与启停（tools.enabled
  持久真相；published↔suspended 域状态机在聚合侧联动）。

事务纪律：仓储方法只 flush 不 commit（SessionDep 提交）；审批侧 ReviewApprovalService 自持
短事务——发布联动为幂等补齐设计（ticket approved 但发布未完成时重入本用例续走）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from services.platform.kernel import DomainError
from services.plugin.domain.model.manifest import ToolTemplate, manifest_tools, validate_manifest
from services.plugin.domain.model.plugin import (
    Plugin,
    PluginKind,
    PluginStatus,
    PluginVersion,
    PluginVersionStatus,
    ToolBinding,
)
from services.plugin.domain.repo.plugin_repo import PluginRepository, ToolBindingRepository
from services.plugin.domain.repo.review_port import PluginReviewPort, ReviewDecisionPort
from services.plugin.runtime.registry import PluginRuntime

_TARGET_TYPE = "plugin_listing"


def _tier_label(value: Any) -> str:
    """档位枚举/字符串 → 标签（GovernanceTier 为 StrEnum；插件侧不依赖 review.domain）。"""
    return str(getattr(value, "value", value))


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    """上架审批决策结果（complete + published 双标记供 API 断言三档行为）。

    ``tier`` 为档位字符串值（solo/team/enterprise）——plugin 侧不 import review.domain
    （契约三：gateway 挂载边传递链禁触 review.domain，三档判定仍收敛在 review.domain）。
    """

    ticket_id: uuid.UUID
    ticket_status: str
    tier: str
    signatures_collected: int
    signatures_required: int
    published: bool


class PluginMarketService:
    """插件市场用例服务（组合根/路由装配；仓储租户面构造期绑定）。"""

    def __init__(
        self,
        repo: PluginRepository,
        bindings: ToolBindingRepository,
        review: PluginReviewPort,
        approvals: ReviewDecisionPort,
        runtime: PluginRuntime | None = None,
    ) -> None:
        self._repo = repo
        self._bindings = bindings
        self._review = review
        self._approvals = approvals
        self._runtime = runtime

    # ---- 登记（draft）----

    async def create_listing(
        self,
        *,
        tenant_id: uuid.UUID,
        publisher_id: uuid.UUID,
        slug: str,
        name: str,
        kind: PluginKind,
        version: str,
        server_json: dict[str, Any],
        artifact_key: str,
        checksum: str,
        scope_required: tuple[str, ...] = (),
        compat_mcp: str | None = None,
    ) -> tuple[Plugin, PluginVersion]:
        """上传登记：draft 插件 + submitted 版本（slug 全平台唯一，uk_plugins_slug 兜底）。"""
        if await self._repo.get_by_slug(slug) is not None:
            raise DomainError(f"4507 PLUGIN_SLUG_TAKEN: slug 已被占用: {slug}")
        plugin = Plugin(slug=slug, name=name, kind=kind, publisher_id=publisher_id, status=PluginStatus.DRAFT)
        ver = PluginVersion(
            plugin_id=plugin.id,
            version=version,
            server_json=server_json,
            compat_mcp=compat_mcp,
            artifact_key=artifact_key,
            checksum=checksum,
            scope_required=scope_required,
        )
        await self._repo.add(plugin)
        await self._repo.add_version(ver)
        return plugin, ver

    async def add_version(
        self,
        *,
        plugin_id: uuid.UUID,
        version: str,
        server_json: dict[str, Any],
        artifact_key: str,
        checksum: str,
        scope_required: tuple[str, ...] = (),
        compat_mcp: str | None = None,
    ) -> PluginVersion:
        """新增版本（版本发布后不可覆盖——同号重提 4508）。"""
        plugin = await self._require_plugin(plugin_id)
        if await self._repo.find_version(plugin_id, version) is not None:
            raise DomainError(f"4508 PLUGIN_VERSION_EXISTS: 版本已存在: {plugin.slug}@{version}")
        if plugin.status is PluginStatus.DEPRECATED:
            raise DomainError("4501 PLUGIN_ILLEGAL_TRANSITION: 已弃用插件不可新增版本（终态）")
        ver = PluginVersion(
            plugin_id=plugin.id,
            version=version,
            server_json=server_json,
            compat_mcp=compat_mcp,
            artifact_key=artifact_key,
            checksum=checksum,
            scope_required=scope_required,
        )
        await self._repo.add_version(ver)
        return ver

    # ---- 提交上架（门禁 1 服务端实跑 → 候选进审）----

    async def submit_for_review(
        self,
        *,
        tenant_id: uuid.UUID,
        plugin_id: uuid.UUID,
        version_id: uuid.UUID,
        submitter_id: uuid.UUID,
        trace_id: str = "",
    ) -> uuid.UUID:
        """提交上架审核：门禁 1 通过 → in_review + 工单 pending_review；失败退回 draft。

        幂等口径：同版本已有 open 工单 → 4701（uk_review_one_open 的用例侧前置检查）。
        """
        plugin = await self._require_plugin(plugin_id)
        ver = await self._repo.get_version(version_id)
        if ver is None or ver.plugin_id != plugin.id:
            raise LookupError(f"插件版本不存在: {version_id}")
        if (
            await self._review.get_open_ticket(tenant_id=tenant_id, target_type=_TARGET_TYPE, target_id=ver.id)
            is not None
        ):
            raise DomainError("4701 OBJECT_ALREADY_IN_REVIEW: 该版本已有在审工单")

        # 门禁 1（服务端实跑，不采信客户端自报——宪法 3）；2~6 关 M5 未建（scan_report 如实标注）
        try:
            report = validate_manifest(ver.server_json)
        except DomainError:
            # 失败留痕（Skills §4 submitted→draft 退回边）：报告随版本落库，插件保持/退回 draft
            ver.scan_report = {"gate": "schema_v1", "passed": False, "trace_id": trace_id}
            if plugin.status is not PluginStatus.DRAFT:
                plugin.reject_back_to_draft()
            await self._repo.save_version(ver)
            await self._repo.save(plugin)
            raise
        report = {**report, "trace_id": trace_id}

        if plugin.status is PluginStatus.DRAFT:
            plugin.submit()
        plugin.pass_auto_gates()  # submitted → in_review（非法迁移即 4501，负向测试锚点）
        if ver.status is PluginVersionStatus.SUBMITTED:
            ver.record_scan(report)  # submitted → scan_passed
        else:
            ver.scan_report = report  # scan_passed 版本复提交仅刷新报告（状态机不回退——04 篇不变式）

        ticket_id = await self._review.submit_candidate(
            tenant_id=tenant_id,
            target_type=_TARGET_TYPE,
            target_id=ver.id,
            payload={
                "envelope_version": 1,
                "candidate_type": _TARGET_TYPE,
                "plugin_id": str(plugin.id),
                "plugin_slug": plugin.slug,
                "version": ver.version,
                "checksum": ver.checksum,
                "scan_report": report,
            },
            submitter_id=submitter_id,
        )
        await self._repo.save(plugin)
        await self._repo.save_version(ver)
        return ticket_id

    # ---- 审批决策（治理三档）+ 发布联动 ----

    async def review_decision(
        self,
        *,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        action: str,
        approver_id: uuid.UUID,
        note: str = "",
    ) -> DecisionOutcome:
        """审批决策（三档审批链）+ 通过后发布联动（08 §4：工单 published → 版本 published → 工具注册）。"""
        ticket = await self._review.get_ticket(tenant_id=tenant_id, ticket_id=ticket_id)
        if ticket is None:
            raise LookupError(f"审核单不存在: {ticket_id}")
        ver = await self._repo.get_version(uuid.UUID(str(ticket["target_id"])))
        if ver is None:
            raise LookupError(f"审核单目标版本不存在: {ticket['target_id']}")
        plugin = await self._require_plugin(ver.plugin_id)

        if ticket["status"] == "approved" and action == "approve":
            # 幂等补齐：决策已落、发布联动中断后重入（续走发布，不重复签名）
            outcome = DecisionOutcome(
                ticket_id=ticket_id,
                ticket_status="approved",
                tier=_tier_label(await self._approvals.tier(tenant_id)),
                signatures_collected=1,
                signatures_required=1,
                published=False,
            )
        else:
            result = await self._approvals.decide(
                tenant_id=tenant_id, ticket_id=ticket_id, action=action, approver_id=approver_id, note=note
            )
            outcome = DecisionOutcome(
                ticket_id=ticket_id,
                ticket_status=result.status,
                tier=_tier_label(result.tier),
                signatures_collected=result.decision.signatures_collected,
                signatures_required=result.decision.signatures_required,
                published=False,
            )
            if not result.decision.complete:
                return outcome  # enterprise 第一签：pending_review 续等
            if action == "reject":
                plugin.reject_back_to_draft()  # REJ 回边：回 draft 修改后可再提交
                await self._repo.save(plugin)
                return outcome

        # 发布联动：版本 published → 插件 published → 工单 published → runtime 注册
        ver.publish()  # scan_passed → published（非法迁移 4501）
        plugin.publish()  # in_review → published
        plugin.latest_version = ver.version
        plugin.signature = f"platform-ed25519:{ver.checksum[:32]}"  # 平台签占位（两级签名随市场全量批次）
        await self._repo.save_version(ver)
        await self._repo.save(plugin)
        await self._review.mark_published(tenant_id=tenant_id, ticket_id=ticket_id, note=note)
        if self._runtime is not None:
            await self._runtime.load(plugin, ver)  # 已发布才可见（候选非成品在 runtime 的落点）
            self._runtime.start(plugin.id)
        return DecisionOutcome(
            ticket_id=ticket_id,
            ticket_status="published",
            tier=outcome.tier,
            signatures_collected=outcome.signatures_collected,
            signatures_required=outcome.signatures_required,
            published=True,
        )

    # ---- 租户安装与启停 ----

    async def install(
        self, *, tenant_id: uuid.UUID, plugin_id: uuid.UUID, version: str | None = None
    ) -> list[ToolBinding]:
        """安装已发布版本为租户工具绑定（enabled=false，api/01 §5.6 install；重复安装 4504）。"""
        plugin = await self._require_plugin(plugin_id)
        if version is not None:
            ver = await self._repo.find_version(plugin_id, version)
        else:  # 缺省装最新版本（版本树倒序首件；latest_version 仅发布后回填）
            versions = await self._repo.list_versions(plugin_id)
            ver = versions[0] if versions else None
        if ver is None:
            raise LookupError(f"插件版本不存在: {version if version is not None else 'latest'}")
        if ver.status.value != "published":
            raise DomainError(f"4502 PLUGIN_NOT_PUBLISHED: 仅已发布版本可安装（当前 {ver.status.value}）")
        existing = await self._bindings.list_by_plugin(plugin_id)
        if existing:
            raise DomainError(f"4504 PLUGIN_ALREADY_INSTALLED: 插件已安装: {plugin.slug}")
        templates = manifest_tools(ver.server_json)
        if not templates:
            templates = [ToolTemplate(name=plugin.slug, description=plugin.name)]
        created: list[ToolBinding] = []
        for tpl in templates:
            if await self._bindings.get_by_name(tpl.name) is not None:
                raise DomainError(f"4504 PLUGIN_ALREADY_INSTALLED: 工具名冲突: {tpl.name}")
            binding = ToolBinding(
                tenant_id=tenant_id,
                name=tpl.name,
                kind="plugin",
                provider_ref={"plugin_id": str(plugin.id), "version": ver.version, "plugin_slug": plugin.slug},
                input_schema=dict(tpl.input_schema),
                annotations=dict(tpl.annotations),
                ontology_action_iri=tpl.ontology_action_iri,
                scope_required=tuple(tpl.required_scopes),
                enabled=False,  # DDL 默认 false；enable 端点显式开启（默认不可信）
            )
            await self._bindings.add(binding)
            created.append(binding)
        return created

    async def enable(self, *, tenant_id: uuid.UUID, plugin_id: uuid.UUID) -> list[ToolBinding]:
        """启用（published↔suspended 的 enable 向；tools.enabled 持久真相 + runtime start）。"""
        plugin = await self._require_plugin(plugin_id)
        if plugin.status is not PluginStatus.PUBLISHED:
            raise DomainError(f"4502 PLUGIN_NOT_PUBLISHED: 仅已上架插件可启用（当前 {plugin.status.value}）")
        bindings = await self._toggle(plugin_id, True)
        if self._runtime is not None and self._runtime.get(plugin_id) is not None:
            self._runtime.start(plugin_id)
        return bindings

    async def disable(self, *, tenant_id: uuid.UUID, plugin_id: uuid.UUID) -> list[ToolBinding]:
        """停用（suspend 向：聚合 published→suspended；存储三值无 suspended 位——
        持久真相=tools.enabled=false + runtime stop + 审计，见模块报告漂移节）。"""
        plugin = await self._require_plugin(plugin_id)
        if plugin.status is PluginStatus.PUBLISHED:
            plugin.suspend()
            await self._repo.save(plugin)
        bindings = await self._toggle(plugin_id, False)
        if self._runtime is not None:
            self._runtime.stop(plugin_id)
        return bindings

    async def _toggle(self, plugin_id: uuid.UUID, enabled: bool) -> list[ToolBinding]:
        bindings = await self._bindings.list_by_plugin(plugin_id)
        if not bindings:
            raise DomainError("4502 PLUGIN_NOT_INSTALLED: 插件未安装，无启停对象")
        for binding in bindings:
            binding.enable() if enabled else binding.disable()
            await self._bindings.save_enabled(binding)
        return bindings

    async def get_detail(self, plugin_id: uuid.UUID) -> tuple[Plugin, list[PluginVersion]]:
        """详情（含版本树，api/01 §5.6 GET /plugins/{id}）。"""
        plugin = await self._require_plugin(plugin_id)
        return plugin, await self._repo.list_versions(plugin.id)

    async def list_market(
        self,
        *,
        status: PluginStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Plugin]:
        """市场列表（默认视图排除 delisted）。"""
        return await self._repo.list_market(status=status, offset=offset, limit=limit)

    async def _require_plugin(self, plugin_id: uuid.UUID) -> Plugin:
        plugin = await self._repo.get(plugin_id)
        if plugin is None:
            raise LookupError(f"插件不存在: {plugin_id}")
        return plugin
