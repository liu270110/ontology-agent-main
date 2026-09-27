"""B2 判据求值器（02 §2 B2：SuccessCriterion 在台账凭证上求值，只认外部回执，不认 agent 自写状态）。

M3 简化版语义（02 §2.3 C1 注）：凭证源=内核账本（PG 台账行），``externally_verified`` 先于
判据——账本无对应回执时判据「暂不可求值」（blocked_by_trust），回执到达即转可求值；
agent_attested 事实（工具输出/agent 自述完成）一律不参与求值（负向测试 test_kernel_b2_criteria.py）。
求值器为纯确定性函数：输入仅判据集 + 账本，无 agent 产物通道（结构性杜绝自证完成）。
"""

from __future__ import annotations

from services.agent.business.kernel.ledger import KernelLedger
from services.agent.domain.model.kernel_planning import CriterionReport, SuccessCriterion


class CriterionEvaluator:
    """判据求值器：全部判据 satisfied ⇒ 运行方可判完成；任一 blocked ⇒ 不可自宣完成。"""

    def evaluate(self, criteria: tuple[SuccessCriterion, ...], ledger: KernelLedger) -> tuple[CriterionReport, ...]:
        reports: list[CriterionReport] = []
        for criterion in criteria:
            receipts = ledger.receipts(kind=criterion.required_receipt_kind, focus_iri=criterion.focus_iri)
            if receipts:
                # 账本回执即 externally_verified 凭证（M3 简化版，见模块 docstring）
                reports.append(
                    CriterionReport(
                        criterion_id=criterion.criterion_id,
                        satisfied=True,
                        blocked_by_trust=False,
                        detail=f"外部回执 {receipts[0].receipt_id} 已落账（externally_verified）",
                    )
                )
            else:
                reports.append(
                    CriterionReport(
                        criterion_id=criterion.criterion_id,
                        satisfied=False,
                        blocked_by_trust=True,
                        detail=(
                            f"缺少 kind={criterion.required_receipt_kind} focus={criterion.focus_iri} "
                            "的外部回执，判据暂不可求值（B2：agent_attested 不参与）"
                        ),
                    )
                )
        return tuple(reports)
