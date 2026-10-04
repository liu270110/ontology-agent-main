"""B2 判据求值器（02 §2 B2：SuccessCriterion 在台账凭证上求值，只认外部回执，不认 agent 自写状态）。

M3 简化版语义（02 §2.3 C1 注）：凭证源=内核账本（PG 台账行），``externally_verified`` 先于
判据——账本无对应回执时判据「暂不可求值」（blocked_by_trust），回执到达即转可求值；
agent_attested 事实（工具输出/agent 自述完成）一律不参与求值（负向测试 test_kernel_b2_criteria.py）。
求值器为纯确定性函数：输入仅判据集 + 账本，无 agent 产物通道（结构性杜绝自证完成）。

E-4 投影补充分支（2026-10-05 K1 批，docs/Agent/13 §2 K1-c）::

    evaluate()                      同步纯回执口径（M3 不变；todo 能力面/既有测试消费）
    evaluate_with_projection()      全路径：回执优先；回执缺失且判据声明投影求值面且
                                    端口已注册 → focus_iri 作 focus node 走端口求值
                                    （确定性引擎，非 agent 自述通道）。

保守侧退化：端口缺失/判据未声明/端口超时或异常/evaluated=False 一律退回 blocked
（求值不可得≠求值失败，等待回执）；投影不满足（conforms=False）=确定性负结论
（satisfied=False 且非 blocked——不可判完成，A4 终止只认判据求值）。
"""

from __future__ import annotations

import asyncio

from services.agent.business.kernel.extensions import CriterionProjection
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.domain.model.kernel_context import TenantContext
from services.agent.domain.model.kernel_planning import CriterionReport, SuccessCriterion

# 投影端口求值钳制（组装面内超时按 blocked 退化，不中断运行；内核 wait_for 兜底）
_PROJECTION_TIMEOUT_S = 1.0


class CriterionEvaluator:
    """判据求值器：全部判据 satisfied ⇒ 运行方可判完成；任一 blocked ⇒ 不可自宣完成。"""

    def __init__(self, *, projection: CriterionProjection | None = None) -> None:
        # E-4 K1-c：投影端口经分发器注入（内核禁直连 pyshacl，A3 依赖倒置）；None=纯回执口径
        self._projection = projection

    def evaluate(self, criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger) -> tuple[CriterionReport, ...]:
        """纯回执口径求值（M3，同步不变口径；todo 能力面 CriteriaLink 消费）。"""
        return tuple(self._receipt_report(criterion, ledger) for criterion in criteria)

    async def evaluate_with_projection(
        self, criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger, ctx: TenantContext
    ) -> tuple[CriterionReport, ...]:
        """全路径求值（E-4 K1-c）：回执命中仍优先；缺失时按声明走投影补充分支。"""
        reports: list[CriterionReport] = []
        for criterion in criteria:
            report = self._receipt_report(criterion, ledger)
            if report.satisfied or criterion.projection is None or self._projection is None:
                reports.append(report)  # 回执命中 / 未声明投影面 / 端口未注册 → M3 口径原样
                continue
            reports.append(await self._projection_report(criterion, ctx))
        return tuple(reports)

    # ── 内部：回执口径（M3 原语义，逐字段未动）────────────────────────────
    @staticmethod
    def _receipt_report(criterion: SuccessCriterion, ledger: KernelLedger) -> CriterionReport:
        receipts = ledger.receipts(kind=criterion.required_receipt_kind, focus_iri=criterion.focus_iri)
        if receipts:
            # 账本回执即 externally_verified 凭证（M3 简化版，见模块 docstring）
            return CriterionReport(
                criterion_id=criterion.criterion_id,
                satisfied=True,
                blocked_by_trust=False,
                detail=f"外部回执 {receipts[0].receipt_id} 已落账（externally_verified）",
            )
        return CriterionReport(
            criterion_id=criterion.criterion_id,
            satisfied=False,
            blocked_by_trust=True,
            detail=(
                f"缺少 kind={criterion.required_receipt_kind} focus={criterion.focus_iri} "
                "的外部回执，判据暂不可求值（B2：agent_attested 不参与）"
            ),
        )

    # ── 内部：投影补充分支（E-4 K1-c）────────────────────────────────────
    async def _projection_report(self, criterion: SuccessCriterion, ctx: TenantContext) -> CriterionReport:
        assert criterion.projection is not None and self._projection is not None  # 调用方已窄化
        try:
            projection = await asyncio.wait_for(
                self._projection.evaluate(criterion, ctx),
                timeout=_PROJECTION_TIMEOUT_S,
            )
        except Exception as exc:  # 超时/端口异常 → 保守退回 blocked（等回执，不中断运行）
            return CriterionReport(
                criterion_id=criterion.criterion_id,
                satisfied=False,
                blocked_by_trust=True,
                detail=f"投影求值不可得（{type(exc).__name__}: {exc}），退回等待外部回执（B2 保守侧）",
            )
        if not projection.evaluated:  # 端口明示无法求值（形状/图缺失）→ 同保守退化
            return CriterionReport(
                criterion_id=criterion.criterion_id,
                satisfied=False,
                blocked_by_trust=True,
                detail=f"投影不可求值（{projection.detail or '端口未给出原因'}），退回等待外部回执（B2 保守侧）",
            )
        if projection.conforms:
            return CriterionReport(
                criterion_id=criterion.criterion_id,
                satisfied=True,
                blocked_by_trust=False,
                detail=(
                    f"投影求值满足：focus={criterion.focus_iri} conforms"
                    f"（shapes={criterion.projection.shapes_iri}；E-4 确定性投影分支）"
                ),
            )
        return CriterionReport(
            criterion_id=criterion.criterion_id,
            satisfied=False,
            blocked_by_trust=False,
            detail=(
                f"投影求值不满足：focus={criterion.focus_iri} 非 conforms"
                f"（shapes={criterion.projection.shapes_iri}"
                + (f"；{projection.detail}" if projection.detail else "")
                + "；E-4 确定性投影分支）"
            ),
        )
