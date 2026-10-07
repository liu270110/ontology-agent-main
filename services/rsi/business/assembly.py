"""贡献者命名空间热装配原型（architecture/09 §14.3 步 4~5；批次 A）。

**物理分表铁律（§14.3 步 4）**：装配/升级/回滚只操作 rsi_contributor_bindings 表
（``contributor:<id>`` 命名空间绑定集，contributor_id 外键+版本号+shadow 标记）——既有
任何 agent 的绑定（内核 tools.bindings 注册表/内置能力绑定面）零触碰。

版本化（步 4）：每次装配产版本号（命名空间内自增）；一键回滚=克隆目标版本为**新版本行**
（append-only，全程可追溯宪法 5；不删不改历史行）。幂等（步 5）：重复配置载荷未变=不重复
建行（返回当前行）；载荷有变=版本升级（差异装配）。

**无侵扰断言（步 5，本批核心）**：每个装配动作前后对**全表绑定快照**做 diff——目标贡献者
命名空间之外的任何行变化=装配事故，机械拒绝（AssemblyIntrusionError）。该断言同时进
tests/rsi/test_contributor.py CI（装配-升级-回滚三动作 × 既有绑定快照 diff 全空）。

红线：一切装配默认 shadow（步 6 灰度；批次 A 无转正路径，领域构造期拒 shadow=False）；
apply 恒拒绝红线不触碰（装配≠生效）。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from services.rsi.audit import AuditTrail, InMemoryAuditTrail, RsiAuditRecord
from services.rsi.domain.contributor import (
    AssemblyIntrusionError,
    ContributorBinding,
    ContributorError,
)
from services.rsi.domain.repo.contributor import BindingFilter, ContributorBindingRepository
from services.rsi.surfaces import EvolutionSurface

# 审计动作（装配面；rsi.assembly 前缀=命名空间装配，非进化动作——apply 恒拒红线不涉）
ACTION_ASSEMBLY_INSTALL = "rsi.assembly.install"
ACTION_ASSEMBLY_UPGRADE = "rsi.assembly.upgrade"
ACTION_ASSEMBLY_ROLLBACK = "rsi.assembly.rollback"
ACTION_ASSEMBLY_INTRUSION = "rsi.assembly.intrusion"  # 安全审计（装配事故=拒绝留痕）


class ContributorBindingAssembler:
    """贡献者命名空间装配器：install/upgrade/rollback + 无侵扰断言（仅操作分表）。

    repo 经构造注入（PG 实现=rsi_contributor_bindings 唯一操作面；测试用内存假体）。
    """

    def __init__(self, *, repo: ContributorBindingRepository, audit_trail: AuditTrail | None = None) -> None:
        self._repo = repo
        self.audit_trail = audit_trail or InMemoryAuditTrail()

    # ------------------------------------------------------------- 无侵扰断言（步 5）

    async def _snapshot(self) -> list[ContributorBinding]:
        return await self._repo.snapshot()

    @staticmethod
    def _diff_excluding(
        before: list[ContributorBinding], after: list[ContributorBinding], contributor_id: str
    ) -> list[ContributorBinding]:
        """快照 diff：目标贡献者命名空间之外的行变化集（空=无侵扰）。

        比对键=(id)；集合对称差+同 id 行字段全等双检——行内容被改也算侵扰（物理分表纪律：
        本装配器对命名空间外行**无任何写路径**，此断言是结构性保证的机械化验证）。
        """

        def _key(row: ContributorBinding) -> tuple:
            payload_canonical = json.dumps(row.payload, sort_keys=True, ensure_ascii=False, default=str)
            return (row.contributor_id, row.surface.value, row.version, row.shadow, payload_canonical)

        before_map = {row.id: row for row in before}
        after_map = {row.id: row for row in after}
        intrusions: list[ContributorBinding] = []
        for row_id in set(before_map) ^ set(after_map):
            row = before_map.get(row_id) or after_map[row_id]
            if row.contributor_id != contributor_id:
                intrusions.append(row)
        for row_id in set(before_map) & set(after_map):
            b, a = before_map[row_id], after_map[row_id]
            if b.contributor_id != contributor_id and _key(b) != _key(a):
                intrusions.append(a)
        return intrusions

    async def _guarded(
        self,
        contributor_id: str,
        action: str,
        apply_action: Callable[[], Awaitable[None]],
        *,
        trace_id: str | None = None,
    ) -> list[ContributorBinding]:
        """动作包装：前快照 → 执行 → 后快照 → 命名空间外 diff 非空即装配事故拒绝。"""
        before = await self._snapshot()
        await apply_action()
        after = await self._snapshot()
        intrusions = self._diff_excluding(before, after, contributor_id)
        if intrusions:
            await self.audit_trail.record(
                RsiAuditRecord(
                    action=ACTION_ASSEMBLY_INTRUSION,
                    outcome="rejected",
                    detail={
                        "contributor_id": contributor_id,
                        "action": action,
                        "intruding_rows": len(intrusions),
                        "intruding_contributors": sorted({r.contributor_id for r in intrusions}),
                    },
                    trace_id=trace_id,
                )
            )
            raise AssemblyIntrusionError(
                f"装配事故：命名空间外 {len(intrusions)} 行绑定被触碰"
                f"（contributor={contributor_id} action={action}）——无侵扰断言拒绝，§14.3 步 5"
            )
        return after

    # ------------------------------------------------------------- 装配/升级（步 4/5）

    async def _current_rows(self, contributor_id: str, surface: EvolutionSurface) -> list[ContributorBinding]:
        rows = await self._repo.list(BindingFilter(contributor_id=contributor_id, surface=surface))
        return sorted(rows, key=lambda r: r.version, reverse=True)  # version 倒序：current 在前

    async def install(
        self,
        *,
        tenant_id: Any,
        contributor_id: str,
        surface: EvolutionSurface | str,
        payload: dict[str, Any],
        trace_id: str | None = None,
    ) -> ContributorBinding:
        """装配（绿项/已转正适配产物；幂等：同载荷不重复建行；新行恒 shadow）。

        版本号=命名空间内 (contributor_id, surface) 当前最大版本+1（首装=1）。
        """
        face = surface if isinstance(surface, EvolutionSurface) else EvolutionSurface(str(surface).strip().upper())
        current = await self._current_rows(contributor_id, face)
        if current and current[0].payload == payload:
            return current[0]  # 幂等：重复配置=不重复建（§14.3 步 5）
        binding = ContributorBinding(
            tenant_id=tenant_id,
            contributor_id=contributor_id,
            surface=face,
            version=(current[0].version + 1) if current else 1,
            payload=payload,
            shadow=True,  # 恒 shadow（领域构造期拒 False——转正随灰度批次）
        )
        _ = await self._guarded(
            contributor_id,
            ACTION_ASSEMBLY_INSTALL,
            lambda: self._repo.add(binding),
            trace_id=trace_id,
        )
        await self.audit_trail.record(
            RsiAuditRecord(
                action=ACTION_ASSEMBLY_UPGRADE if current else ACTION_ASSEMBLY_INSTALL,
                outcome="ok",
                detail={
                    "contributor_id": contributor_id,
                    "surface": face.value,
                    "version": binding.version,
                    "shadow": True,
                    "namespace": f"contributor:{contributor_id}",
                },
                trace_id=trace_id,
            )
        )
        return binding

    async def rollback(
        self,
        *,
        tenant_id: Any,
        contributor_id: str,
        surface: EvolutionSurface | str,
        to_version: int,
        trace_id: str | None = None,
    ) -> ContributorBinding:
        """一键回滚到任意前版（§14.3 步 4）：克隆目标版本为新版本行（append-only）。

        目标版本即当前版本 → 幂等无操作（返回当前行）；目标版本不存在 → ContributorError。
        """
        face = surface if isinstance(surface, EvolutionSurface) else EvolutionSurface(str(surface).strip().upper())
        current = await self._current_rows(contributor_id, face)
        if not current:
            raise ContributorError(f"回滚失败：{contributor_id}/{face.value} 命名空间无绑定行")
        if current[0].version == to_version:
            return current[0]  # 幂等：已在该版本
        target = next((r for r in current if r.version == to_version), None)
        if target is None:
            existing = sorted((r.version for r in current), reverse=True)
            raise ContributorError(
                f"回滚失败：目标版本不存在（{contributor_id}/{face.value} 已有版本 {existing}，目标 {to_version}）"
            )
        binding = ContributorBinding(
            tenant_id=tenant_id,
            contributor_id=contributor_id,
            surface=face,
            version=current[0].version + 1,
            payload=dict(target.payload),  # 克隆目标版本载荷（历史行零触碰）
            shadow=True,
        )
        _ = await self._guarded(
            contributor_id,
            ACTION_ASSEMBLY_ROLLBACK,
            lambda: self._repo.add(binding),
            trace_id=trace_id,
        )
        await self.audit_trail.record(
            RsiAuditRecord(
                action=ACTION_ASSEMBLY_ROLLBACK,
                outcome="ok",
                detail={
                    "contributor_id": contributor_id,
                    "surface": face.value,
                    "from_version": current[0].version,
                    "to_version": to_version,
                    "new_version": binding.version,
                    "namespace": f"contributor:{contributor_id}",
                },
                trace_id=trace_id,
            )
        )
        return binding
