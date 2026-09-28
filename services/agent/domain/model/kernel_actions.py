"""L4 领域模型：行动/工具执行值对象（02 篇 §4.1 ④⑧ 扩展点载体）。

「值不经采样」：ToolCall.parameters 由调用方槽位填充（数字/ID/枚举值），实现不得改造为
自由文本（幻觉防线）；ToolResult 一律不可信外部输入（B3）：trust_level 由内核标界，
工具自称 externally_verified 被降权（负向测试 test_kernel_b3_trust_boundary.py）。
失败必须结构化返回（ok=False + 登记错误码），禁裸异常逃逸循环。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.agent.domain.model.kernel_context import TrustLevel


class ExecutionMode(StrEnum):
    """executionMode 行动分级（B1 分级审批 / B5 审批路由的判级依据）。"""

    READ = "read"  # 只读：基线门禁即可放行
    WRITE = "write"  # 平台内写：需 scope 覆盖
    EXTERNAL_WRITE = "external_write"  # 对外写（externalWrite）：需审批回执（B5）
    CODE = "code"  # 沙箱代码：需审批 + 执行后端（B4 出口控制硬编码）


class ActionDecision(BaseModel):
    """行动决定（值对象 frozen）：行动类 IRI + 槽位参数 + executionMode（PreGate 求值输入）。"""

    model_config = ConfigDict(frozen=True)

    action_iri: str  # 本体行动类 IRI（枚举合法性对账键）
    execution_mode: ExecutionMode = ExecutionMode.READ
    parameters: dict[str, Any] = Field(default_factory=dict)  # 槽位参数（值不经采样）
    step_seq: int = 0


class ToolCall(BaseModel):
    """工具调用（值对象 frozen）：行动类 IRI + 槽位参数 + 参数哈希（B5 参数哈希绑定）。"""

    model_config = ConfigDict(frozen=True)

    call_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    action_iri: str
    execution_mode: ExecutionMode = ExecutionMode.READ
    parameters: dict[str, Any] = Field(default_factory=dict)
    param_hash: str  # 参数哈希（sha256，内核计算；审批回执绑定键）
    step_seq: int = 0


class ApprovalTicket(BaseModel):
    """审批回执（值对象 frozen，B5 放行凭证）：参数哈希绑定，防审批后换参重放。

    expires_at（H-0b 2026-09-29 补）：运行中审批票时效；执行侧重放并入内核 approvals
    前校验（过期视同无回执，走 B5 默认拒绝）。缺省 None=不限时（预授权注入的既有形态）。
    """

    model_config = ConfigDict(frozen=True)

    ticket_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    param_hash: str  # 必须与 ToolCall.param_hash 一致，不一致即拒绝
    approved_by: uuid.UUID | None = None
    expires_at: datetime | None = None


class ToolResult(BaseModel):
    """工具结果（值对象 frozen，不可信外部输入）：失败必须结构化（登记错误码，禁裸异常）。

    trust_level 由内核标界注入：工具实现自称 externally_verified（claimed_trust_level）
    一律降为 agent_attested，不采信（B3，负向测试 test_kernel_b3_trust_boundary.py）。
    usage.total_tokens 供 A4 预算记账。
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)  # 结构化产物（值不经采样原样回传）
    error_code: int | None = None  # 02 篇 §7 登记错误码（失败时必填）
    error_message: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)  # {total_tokens: int, ...}（A4 记账）
    claimed_trust_level: TrustLevel | None = None  # 实现自报信任级（仅留痕，内核不采信）
    trust_level: TrustLevel = TrustLevel.AGENT_ATTESTED  # 内核标界后的实际信任级


class StepResult(BaseModel):
    """步产物（值对象 frozen）：PostGate 后验与判据求值的对象（不得由此改写 StepState，T2）。"""

    model_config = ConfigDict(frozen=True)

    step_id: uuid.UUID
    run_id: uuid.UUID
    step_seq: int
    action_iri: str
    ok: bool
    tool_result: ToolResult | None = None  # 工具原始结果（含回执指针语义）
    output: dict[str, Any] = Field(default_factory=dict)


class SandboxSpec(BaseModel):
    """沙箱规格（值对象 frozen）：镜像/配额/出口白名单——B4 硬编码项不可覆盖。"""

    model_config = ConfigDict(frozen=True)

    image: str
    network_enabled: bool = False  # v1 默认无网（DSec §6.5 降级承诺）；True 一律拒绝
    egress_whitelist: tuple[str, ...] = ()  # 白名单域名/IP（逐包放行，v1 演练契约面）
    cpu_limit: str = "1.0"
    memory_limit: str = "512m"


class SandboxLease(BaseModel):
    """沙箱租约（值对象 frozen）：取消清单第 3 步强制释放的登记凭据。"""

    model_config = ConfigDict(frozen=True)

    lease_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    backend_name: str
    sandbox_ref: str


class CodeAction(BaseModel):
    """沙箱代码行动（值对象 frozen）：executionMode=code 的行动类载体。"""

    model_config = ConfigDict(frozen=True)

    action_iri: str
    code: str
    entrypoint: str = "main"
    step_seq: int = 0


class ExecutionResult(BaseModel):
    """沙箱执行结果（值对象 frozen，不可信外部输入，B3 标界同 ToolResult）。"""

    model_config = ConfigDict(frozen=True)

    ok: bool
    exit_code: int | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: int | None = None
    trust_level: TrustLevel = TrustLevel.AGENT_ATTESTED
