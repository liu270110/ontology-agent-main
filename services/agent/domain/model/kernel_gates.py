"""L4 领域模型：门禁/后验报告与运行结果值对象（02 篇 §4.1 ③ 与 A4 终态载体）。

GateReport 必须结构化（focus + 规则 IRI + severity + message），供「错误即反馈」回流与
审计；合成顺序固定「基线先、包后」，包 gate 无权改写基线 GateReport（铁律 1 只增不替）。
错误码一律取 02 篇 §7 已登记段（platform/errors.ErrorCode），禁随手编。
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from services.agent.domain.model.kernel_planning import CriterionReport
from services.agent.domain.model.step_state import StepState


class GateVerdict(StrEnum):
    """门禁结论（值语义）：allow/reject；包 gate 只能附违例，不能改基线结论。"""

    ALLOW = "allow"
    REJECT = "reject"


class GateFinding(BaseModel):
    """单条违例（值对象 frozen）：结构化违例（focus + 规则 + severity + message）。"""

    model_config = ConfigDict(frozen=True)

    focus: str  # focus node（行动类 IRI / 参数槽位）
    rule_iri: str  # 规则/shape IRI（基线规则用内核保留段前缀）
    severity: str = "error"  # error | warning
    code: int  # 02 篇 §7 登记错误码
    message: str


class GateReport(BaseModel):
    """门禁报告（值对象 frozen）：is_baseline 标记基线/包来源（合成顺序审计）。"""

    model_config = ConfigDict(frozen=True)

    verdict: GateVerdict
    is_baseline: bool  # True=内核基线 B1（只增不替的「基」）
    findings: tuple[GateFinding, ...] = ()
    reporter: str = "kernel.baseline"  # 报告方（基线固定；包 gate 用 meta.name）

    @property
    def passed(self) -> bool:
        return self.verdict is GateVerdict.ALLOW


class ValidationReport(BaseModel):
    """后验校验报告（值对象 frozen）：错误供重生成与降级安全回答分支消费；不改写 StepState。"""

    model_config = ConfigDict(frozen=True)

    ok: bool
    findings: tuple[GateFinding, ...] = ()
    validator: str = ""


class RunOutcome(BaseModel):
    """运行终局（值对象 frozen）：A4 预算终止/取消完整性产出的终态载体（不可卡死、可追溯）。

    status 复用 RunStatus（04 §3 权威）；terminal_states=全部步终态快照；
    reason_code 取登记错误码（预算耗尽=5005 RETRY_BUDGET_EXHAUSTED 段内唯一预算码）。
    """

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    status: str  # RunStatus 值（completed/failed/timeout/cancelled）
    reason_code: int | None = None  # 登记错误码；正常完成 None
    reason: str = ""
    terminal_states: tuple[StepState, ...] = ()
    criteria: tuple[CriterionReport, ...] = ()  # B2 判据求值报告（终态可追溯）
