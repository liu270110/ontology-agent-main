"""subagent 能力（P0 #4：子 agent 工具面 spawn/wait/interrupt；docs/Agent/06 路线 #4）。

落点=services/agent/business/capabilities/subagent/（一能力一目录）；三工具经
:func:`build_subagent_bindings` 工厂产出（tools.bindings，通道 L0），**派生只走内核
AgentSlot**（kernel/subagent.py，L3 通道不可替换——子 Run 的预算分账 A4、Artifact
回传契约（02 §4.2）、窗口隔离、级联取消全部由内核裁决，本能力只做参数适配与护栏）。

护栏（guards.py，deny-by-default）：递归深度上限（默认 2 层，ContextVar 随内核派生
调用任务下传）、并发子代理上限（默认 4，批量 all-or-nothing）、派生白名单（空白名单=
全拒）、预算继承（子预算=min(申请额, ⌊父剩余×份额⌋)，份额可调，内核 A4 断言兜底）。
审计（audit.py）：spawn（注册+落定）/interrupt 结构化留痕（父 run_id/子 run_id/预算
份额）；objective 与 Artifact 正文不落审计（不可信内容红线）。
"""

from __future__ import annotations

from services.agent.business.capabilities.subagent.audit import AuditSink, default_audit_sink, emit_audit
from services.agent.business.capabilities.subagent.bindings import build_subagent_bindings
from services.agent.business.capabilities.subagent.guards import (
    BUDGET_SHARE_DEFAULT,
    DEFAULT_WAIT_TIMEOUT_MS,
    DERIVATION_DEPTH,
    MAX_BATCH_SIZE,
    MAX_CONCURRENT_SUBAGENTS_DEFAULT,
    MAX_DERIVATION_DEPTH_DEFAULT,
    BudgetSharePolicy,
    DerivationWhitelist,
    SpawnTaskSpec,
    SubagentGuardError,
    current_depth,
    parse_spawn_tasks,
)
from services.agent.business.capabilities.subagent.tools import (
    SUBAGENT_INTERRUPT_ACTION_IRI,
    SUBAGENT_SPAWN_ACTION_IRI,
    SUBAGENT_TOOL_VERSION,
    SUBAGENT_WAIT_ACTION_IRI,
    ParentBudgetProbe,
    SpawnGroupRegistry,
    SubagentInterruptTool,
    SubagentSlotPort,
    SubagentSpawnTool,
    SubagentWaitTool,
    TaskScopeResolver,
)

__all__ = [
    "BUDGET_SHARE_DEFAULT",
    "AuditSink",
    "BudgetSharePolicy",
    "DEFAULT_WAIT_TIMEOUT_MS",
    "DERIVATION_DEPTH",
    "MAX_BATCH_SIZE",
    "MAX_CONCURRENT_SUBAGENTS_DEFAULT",
    "MAX_DERIVATION_DEPTH_DEFAULT",
    "DerivationWhitelist",
    "ParentBudgetProbe",
    "SpawnGroupRegistry",
    "SpawnTaskSpec",
    "SUBAGENT_INTERRUPT_ACTION_IRI",
    "SUBAGENT_SPAWN_ACTION_IRI",
    "SUBAGENT_TOOL_VERSION",
    "SUBAGENT_WAIT_ACTION_IRI",
    "SubagentGuardError",
    "SubagentInterruptTool",
    "SubagentSlotPort",
    "SubagentSpawnTool",
    "SubagentWaitTool",
    "TaskScopeResolver",
    "build_subagent_bindings",
    "current_depth",
    "default_audit_sink",
    "emit_audit",
    "parse_spawn_tasks",
]
