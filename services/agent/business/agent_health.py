"""适配器健康记账服务（H-0c ③，2026-09-29 批；04 篇 §10 degraded 裁决的业务侧落点）。

health-check 端点（api/agents.py）探活后调用 :func:`record_adapter_health_outcome`：

- 失败：``agents.adapter_failure_count`` +1；连续失败 ≥ ``degrade_threshold``
  （Settings.agent_degrade_threshold，默认 3）→ 聚合 ``degrade()``（enabled→degraded）；
- 成功：计数清零；degraded 自愈回 enabled（04 篇 §3「degraded→recovered，
  health-check 端点驱动」）；disabled 终态语义不变（探活结果不迁移终态）；
- degraded 的运行时效应在领域模型：``ensure_usable_for_new_session`` 拒绑（新会话 409），
  存量 Run 不经该校验（跑完不中断）。

端点接线（api/agents.py health-check 成败两分支各一行调用）因本批 api/ 冻结面暂缓，
登记遗留（领域模型/迁移/本服务函数已就绪，接线=纯增量）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from services.agent.domain.model.agent import Agent

logger = logging.getLogger(__name__)


async def record_adapter_health_outcome(
    uow: Any,  # AsyncUnitOfWork（端点/任务侧注入；Protocol 免循环 import）
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    healthy: bool,
    degrade_threshold: int = 3,
) -> Agent | None:
    """探活结果记账（health-check 端点成败后调用；幂等：disabled 不受影响）。

    返回记账后的 Agent（不存在返回 None——调用方按 404 口径处理）。
    迁移语义与计数清零全部在聚合方法 ``Agent.record_adapter_health``（04 §1 不变式只写聚合）。
    """
    async with uow.for_tenant(tenant_id) as tx:
        agent = await tx.agents.get(agent_id)
        if agent is None:
            return None
        before = agent.status
        agent.record_adapter_health(healthy=healthy, degrade_threshold=degrade_threshold)
        await tx.agents.save_meta(agent)
        if agent.status is not before:
            logger.warning(
                "适配器状态迁移：agent=%s %s→%s（连续失败 %d 次，阈值 %d）",
                agent_id,
                before.value,
                agent.status.value,
                agent.adapter_failure_count,
                degrade_threshold,
            )
        return agent
