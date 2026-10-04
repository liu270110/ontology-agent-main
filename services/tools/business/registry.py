"""L3 用例服务：工具集市注册/列表/生命周期（docs/Agent/14 §2/§3）。

用例簇：
- :meth:`ToolRegistryService.register` —— 注册上架：清单校验（name/action_iri 非空、
  semantic_annotation 非空——对齐 agent 内核 ExtensionMeta 上架纪律「无语义标注不上架」
  §7.4）→ v1 直通 listed（静态扫描+清单校验两项门禁先行，人工与签名挂接点预留 plugin
  先例，14 §2）+ 域级审计行；
- :meth:`ToolRegistryService.list_tools` —— 市场列表（query/class 通道过滤 + count）；
- :meth:`ToolRegistryService.lifecycle` —— 生命周期动作（下架/恢复/撤销；迁移合法性由
  聚合硬编码迁移表保证，非法迁移 4603）+ 域级审计行。

事务纪律：仓储方法只 flush 不 commit（SessionDep 提交）；审计行与主写同会话同事务。
"""

from __future__ import annotations

import uuid
from typing import Any

from services.platform.kernel import DomainError
from services.tools.domain.model.tool_entry import SourceChannel, ToolEntry, ToolStatus
from services.tools.domain.repo.tool_repo import ToolRepository

# v1 生命周期动作词汇（14 §2 lifecycle {action, reason?}：下架/恢复/撤销）
_ACTION_TARGETS: dict[str, ToolStatus] = {
    "delist": ToolStatus.DEPRECATED,  # 下架/废弃：listed → deprecated
    "restore": ToolStatus.LISTED,  # 恢复：deprecated → listed
    "revoke": ToolStatus.REVOKED,  # 撤销：listed|deprecated → revoked
}


class ToolRegistryService:
    """工具集市用例服务（组合根/路由装配；仓储租户面构造期绑定）。"""

    def __init__(self, repo: ToolRepository) -> None:
        self._repo = repo

    # ---- 注册（清单校验 → 直通 listed）----

    async def register(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        action_iri: str,
        source_channel: SourceChannel,
        semantic_annotation: dict[str, Any],
        version: str,
        health_hint: str | None = None,
        evidence_uri: str | None = None,
        registrant_id: uuid.UUID | None = None,
        trace_id: str = "",
    ) -> ToolEntry:
        """注册工具条目：清单校验通过即直通 listed（v1 直通边，14 §2）；同名 4602。"""
        # 清单校验（ExtensionMeta 上架纪律对齐；DTO 长度约束外的语义校验收敛于此）
        if not name or not name.strip():
            raise DomainError("4601 TOOL_LISTING_INVALID: name 非空必填（14 §3 注册清单）")
        if not action_iri or not action_iri.strip():
            raise DomainError("4601 TOOL_LISTING_INVALID: action_iri 非空必填（行动类对账键，ExtensionMeta 口径）")
        if not semantic_annotation:
            raise DomainError("4601 TOOL_LISTING_INVALID: semantic_annotation 非空必填（无语义标注不上架，§7.4）")
        annotation_iri = semantic_annotation.get("action_iri")
        if isinstance(annotation_iri, str) and annotation_iri and annotation_iri != action_iri:
            raise DomainError(
                "4601 TOOL_LISTING_INVALID: semantic_annotation.action_iri 与顶层 action_iri 不一致"
                "（行动类对账键唯一，ExtensionMeta 口径）"
            )
        if await self._repo.get_by_name(name) is not None:
            raise DomainError(f"4602 TOOL_NAME_TAKEN: 同名工具已登记: {name}")

        entry = ToolEntry(
            tenant_id=tenant_id,
            name=name,
            action_iri=action_iri,
            source_channel=source_channel,
            semantic_annotation=dict(semantic_annotation),
            version=version,
            health_hint=health_hint,
            evidence_uri=evidence_uri,
            status=ToolStatus.DRAFT,
        )
        entry.apply_transition(ToolStatus.LISTED)  # v1 直通边：draft → listed（14 §2）
        await self._repo.add(entry)
        await self._repo.record_audit(
            actor_id=registrant_id,
            action="tools.register",
            resource_id=str(entry.id),
            digest={"name": name, "action_iri": action_iri, "source_channel": source_channel.value, "to": "listed"},
            trace_id=trace_id,
        )
        return entry

    # ---- 列表/详情 ----

    async def list_tools(
        self,
        *,
        query: str | None = None,
        source_channel: SourceChannel | None = None,
        status: ToolStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ToolEntry], int]:
        """市场列表（query 模糊匹配 + 来源通道/状态过滤 + count；软删行不可见）。"""
        offset = (page - 1) * page_size
        return await self._repo.list_page(
            query=query, source_channel=source_channel, status=status, offset=offset, limit=page_size
        )

    async def get(self, tool_id: uuid.UUID) -> ToolEntry:
        """详情（含语义标注/来源通道/版本/健康提示；不存在 LookupError → api 404）。"""
        entry = await self._repo.get(tool_id)
        if entry is None:
            raise LookupError(f"工具不存在: {tool_id}")
        return entry

    # ---- 生命周期（迁移合法 + 审计行）----

    async def lifecycle(
        self,
        *,
        tool_id: uuid.UUID,
        action: str,
        reason: str = "",
        operator_id: uuid.UUID | None = None,
        trace_id: str = "",
    ) -> ToolEntry:
        """生命周期动作（14 §2 POST /tools/{id}/lifecycle {action, reason?}）。

        迁移合法性由聚合硬编码迁移表保证（非法迁移 4603 → api 409）；动作与理由随
        域级审计行留痕（全程可追溯）。
        """
        target = _ACTION_TARGETS.get(action)
        if target is None:  # 词汇表由 DTO Literal 前置；此为服务直调防御
            raise DomainError(f"4601 TOOL_LISTING_INVALID: 未知生命周期动作: {action}")
        entry = await self.get(tool_id)
        from_status = entry.status
        entry.apply_transition(target)  # 非法迁移 4603（负向测试锚点）
        await self._repo.save(entry)
        await self._repo.record_audit(
            actor_id=operator_id,
            action=f"tools.lifecycle.{action}",
            resource_id=str(entry.id),
            digest={"from": from_status.value, "to": target.value, "reason": reason},
            trace_id=trace_id,
        )
        return entry
