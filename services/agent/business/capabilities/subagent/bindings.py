"""subagent 能力绑定工厂（docs/Agent/06 #4：三工具经 ToolPort 进内核，B1 门禁链照常生效）。

组合根（gateway/task bootstrap 等）接线片段：

    from services.agent.business.capabilities.subagent import build_subagent_bindings

    spawn, wait, interrupt = build_subagent_bindings(
        dispatcher.agent_slot(),                         # 内核 AgentSlot（L3 唯一派生路径）
        task_resolver=lambda ctx: current_run_task(ctx),  # 当前 Run 的 TaskRef（父归因/分账键）
        derivable_agents=("researcher", "analyst"),       # 派生白名单（deny-by-default）
        budget_probe=lambda: tracker.remaining_tokens,    # 父剩余预算探针（A4 份额上限）
    )
    for tool in (spawn, wait, interrupt):
        dispatcher.register_tool(tool)

缺省语义（fail-closed）：``derivable_agents=()`` 时 spawn 全拒（安全边界而非功能开关，
与 web 出口白名单同款）；``budget_probe=None`` 时不设份额上限，仅受申请额与内核 A4
分账断言（≤父剩余）约束。
"""

from __future__ import annotations

from collections.abc import Iterable

from services.agent.business.capabilities.subagent.audit import AuditSink, default_audit_sink
from services.agent.business.capabilities.subagent.guards import (
    BUDGET_SHARE_DEFAULT,
    MAX_CONCURRENT_SUBAGENTS_DEFAULT,
    MAX_DERIVATION_DEPTH_DEFAULT,
    BudgetSharePolicy,
    DerivationWhitelist,
)
from services.agent.business.capabilities.subagent.tools import (
    ParentBudgetProbe,
    SpawnGroupRegistry,
    SubagentInterruptTool,
    SubagentSlotPort,
    SubagentSpawnTool,
    SubagentWaitTool,
    TaskScopeResolver,
)


def build_subagent_bindings(
    slot: SubagentSlotPort,
    task_resolver: TaskScopeResolver,
    derivable_agents: DerivationWhitelist | Iterable[str],
    *,
    budget_probe: ParentBudgetProbe | None = None,
    budget_share: float = BUDGET_SHARE_DEFAULT,
    max_depth: int = MAX_DERIVATION_DEPTH_DEFAULT,
    max_concurrent: int = MAX_CONCURRENT_SUBAGENTS_DEFAULT,
    audit_sink: AuditSink | None = None,
) -> tuple[SubagentSpawnTool, SubagentWaitTool, SubagentInterruptTool]:
    """装配 spawn/wait/interrupt 三工具绑定（一能力一目录，共享句柄组登记表与审计汇）。

    - ``slot``：内核 AgentSlot（kernel/subagent.py BuiltinAgentSlot；唯一派生路径）；
    - ``task_resolver``：当前 Run 的 TaskRef 解析器（spawn_sub 以其 run_id 作父归因与
      分账/级联键；组合根按运行上下文解析，tests 注入静态值）；
    - ``derivable_agents``：派生白名单（DerivationWhitelist 或类型可迭代；空白名单=全拒）；
    - ``budget_probe``：父剩余 token 预算探针（None=缺省不设份额上限，内核 A4 兜底）；
    - ``budget_share``：预算继承份额 ∈ (0, 1]（缺省 0.5）；
    - ``max_depth`` / ``max_concurrent``：递归深度与并发子代理上限（缺省 2 / 4）；
    - ``audit_sink``：派生审计汇（缺省标准日志行；正文永不落审计）。
    """
    whitelist = (
        derivable_agents if isinstance(derivable_agents, DerivationWhitelist) else DerivationWhitelist(derivable_agents)
    )
    registry = SpawnGroupRegistry()
    sink = audit_sink if audit_sink is not None else default_audit_sink
    spawn = SubagentSpawnTool(
        slot,
        task_resolver,
        whitelist,
        BudgetSharePolicy(budget_share),
        budget_probe if budget_probe is not None else (lambda: None),
        registry,
        max_depth=max_depth,
        max_concurrent=max_concurrent,
        audit_sink=sink,
    )
    wait = SubagentWaitTool(slot, registry)
    interrupt = SubagentInterruptTool(registry, audit_sink=sink)
    return spawn, wait, interrupt
