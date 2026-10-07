"""L4 领域模型：StepState 步状态机 + 七阶段主循环枚举 + 预算水位值对象。

权威：docs/Agent/02 §2 A1（agent loop 七阶段）/A2（Run/Step 状态机，迁移合法性内核断言）
与 docs/architecture/04 §3 Step 状态机（唯一版本，此处代码化）：
planned→gated→executing（⇄waiting_approval）→validated/failed；终态不可逆。
cancelled 为内核取消完整性终态（02 §2.4）：任何非终态可入，资源清单执行完毕才落。
非法迁移一律抛 :class:`StepStateError`（负向测试 tests/agent/test_kernel_a2_step_state.py）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StepStateError(Exception):
    """步状态机领域错误（02 §2 A2：非法迁移被拒绝；错误码经内核映射 02 篇 §7 登记段）。"""


class LoopStage(StrEnum):
    """agent loop 七阶段（02 §2 A1）。

    术语对照：感知/检索=装载 Grounding+组装；观察=后验+写回+漂移检测；沉淀=闭环落账。
    """

    GROUNDING = "grounding"  # ① 感知＝装载（Grounding+确认）：TaskRef 校验、供给器取材
    RETRIEVAL = "retrieval"  # ② 检索＝上下文组装：ContextBlock 绝对预算裁剪（A1 组装断点）
    PLANNING = "planning"  # ③ 规划：计划图候选三层校验（含分级预判：确定性优先）
    GATE = "gate"  # ④ 门禁：基线 B1 先、包 gate 后（只增不替）
    EXECUTION = "execution"  # ⑤ 执行：tools.bindings / execution.backends
    OBSERVATION = "observation"  # ⑥ 观察：后验 gates.post＋判据求值＋漂移检测＋写回
    SETTLEMENT = "settlement"  # ⑦ 沉淀：闭环落账（账本/事件汇/审计，C2）


class StepStatus(StrEnum):
    """步状态（04 §3 权威状态机 + 02 §2.4 cancelled）。"""

    PLANNED = "planned"
    GATED = "gated"
    EXECUTING = "executing"
    WAITING_APPROVAL = "waiting_approval"
    VALIDATED = "validated"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STEP_STATUSES = frozenset({StepStatus.VALIDATED, StepStatus.FAILED, StepStatus.CANCELLED})

# 迁移表（04 §3 状态图代码化；cancelled 入口=02 §2.4 取消完整性，非终态皆可入）
# M4.5-A 增补（docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1.3）：planned→validated
# 为 **resume 计划对账特批迁移**——仅内核对账路径可走（前序 Run 的 step_validated 锚点
# (seq,action_iri,param_hash) 全等且 execution_mode=READ），非执行旁路：终态可追溯性由
# kernel.step_resumed_validated 审计事件（resumed=true）承载。
_VALID_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PLANNED: frozenset({StepStatus.GATED, StepStatus.VALIDATED, StepStatus.CANCELLED}),
    StepStatus.GATED: frozenset({StepStatus.EXECUTING, StepStatus.FAILED, StepStatus.CANCELLED}),
    StepStatus.EXECUTING: frozenset(
        {StepStatus.WAITING_APPROVAL, StepStatus.VALIDATED, StepStatus.FAILED, StepStatus.CANCELLED}
    ),
    StepStatus.WAITING_APPROVAL: frozenset({StepStatus.EXECUTING, StepStatus.FAILED, StepStatus.CANCELLED}),
    StepStatus.VALIDATED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
}


class BudgetWatermark(BaseModel):
    """预算水位快照（值对象，frozen）：步状态携带，终态可追溯（A4 预算终止凭据）。

    tokens_used/steps_done 单调累计；duration_elapsed_s 由内核时钟回填；三预算口径
    token/步数/时长（成本归因 C2 随 M4 台账接入）。

    M4.5-B（docs/Agent/12 §2 批次 B）增 ``estimated`` 标注（additive，缺省 False 零行为
    变化）：True=该水位的 token 口径含未锚定估算（真实 usage 回执缺位，预算检查按
    组装估算计入）——水位消费方据此区分实测/估算（口径标注，非计量值变更）。
    """

    model_config = ConfigDict(frozen=True)

    tokens_used: int = 0
    steps_done: int = 0
    duration_elapsed_s: float = 0.0
    estimated: bool = False  # M4.5-B：token 口径含未锚定估算时为 True（见类注释）

    def merged(self, *, tokens: int, steps: int, elapsed_s: float) -> BudgetWatermark:
        """返回累计后的新水位（frozen 值对象不可原地改，替换式更新）。

        estimated 口径随水位传递（M4.5-B）：聚合含估算口径的水位不得回落默认 False
        （否则估算被误报为实测，水位消费方口径标注失真）。
        """
        return BudgetWatermark(
            tokens_used=self.tokens_used + tokens,
            steps_done=self.steps_done + steps,
            duration_elapsed_s=max(self.duration_elapsed_s, elapsed_s),
            estimated=self.estimated,
        )


class StepState(BaseModel):
    """步状态（task 聚合内步实体的执行态投影，04 §3）：迁移合法性内核断言（A2）。

    字段对应交付物②：步序号 seq／阶段 stage（产出该状态的循环阶段）／状态 status／
    预算水位 budget_watermark／门禁结论 gate_verdict／可恢复点 resumable。
    聚合纪律：validate_assignment=True；迁移只能走 :meth:`transition`。
    """

    model_config = ConfigDict(validate_assignment=True)

    step_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    run_id: uuid.UUID
    seq: int  # 步序号（Run 内严格递增，04 §2 seq 纪律同源）
    stage: LoopStage
    status: StepStatus = StepStatus.PLANNED
    action_iri: str | None = None  # 计划绑定的行动类 IRI（漂移检测对账键）
    budget_watermark: BudgetWatermark = Field(default_factory=BudgetWatermark)
    gate_verdict: str | None = None  # 门禁结论（GateVerdict 值；未过门禁为 None）
    error: str | None = None  # 终态原因（结构化描述，审计可读）
    resumable: bool = False  # 可恢复点：True=可从本步对账续跑（C1 重连续跑锚点）
    updated_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STEP_STATUSES

    @property
    def is_gate_passed(self) -> bool:
        """门禁已放行（gated 之后诸态）；未过门禁一律不得执行（A1 负向断言依据）。"""
        return self.status in {
            StepStatus.GATED,
            StepStatus.EXECUTING,
            StepStatus.WAITING_APPROVAL,
            StepStatus.VALIDATED,
        }

    def transition(self, to: StepStatus, *, stage: LoopStage | None = None) -> None:
        """状态迁移（非法迁移抛 StepStateError，02 §2 A2 内核断言）。stage 记录产出阶段。"""
        if to not in _VALID_STEP_TRANSITIONS[self.status]:
            raise StepStateError(f"非法步状态迁移 {self.status} → {to}（04 篇 §3 状态机）")
        self.status = to
        if stage is not None:
            self.stage = stage

    def cancel(self, *, reason: str | None = None) -> None:
        """取消完整性入口（02 §2.4）：非终态皆可入 cancelled；终态再取消被拒。"""
        self.transition(StepStatus.CANCELLED)
        if reason is not None:
            self.error = reason
