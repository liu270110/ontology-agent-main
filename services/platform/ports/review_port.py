"""技术能力端口 · CandidateReviewPort（候选非成品宪法第 3 条的跨模块注入面）。

依赖倒置（同 ModelPort 模式）：kb 抽取流水线必须把 LLM 候选双写进 review_tickets（08 §4
候选统一入口），而 review.data 模块私有（pyproject 契约六：gateway/kb 双侧禁入且无豁免项），
pyproject 本期冻结——故 kb 侧只依赖本 Protocol（纯 Python、零框架依赖），实现由
services/review/business/candidates.py 提供，绑定只发生在组合根（L2 网关 lifespan）。

信封契约（standards/01 §5.3）：``payload`` = 统一信封
{envelope_version, candidate_type, template_ref, trace_id, payload, confidence, review}；
不变式：candidate_id 与 review 状态由系统回填流转，LLM 只产 payload 与 confidence。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol


class CandidateReviewPort(Protocol):
    """候选审核登记端口（review_tickets 写路径唯一入口）。"""

    async def submit_candidate(
        self,
        *,
        tenant_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        payload: dict[str, Any],
        status: str = "pending_review",
        submitter_id: uuid.UUID | None = None,
        sla_deadline: datetime | None = None,
    ) -> uuid.UUID:
        """登记一条候选进审核队列，返回单据 id。

        幂等契约：同 (tenant_id, target_type, target_id) 已有 open 单（draft/pending_review，
        uk_review_one_open）时原样返回既有 id，不覆盖原信封——断点续跑重放安全。
        """
        ...  # pragma: no cover — Protocol 方法无实现

    async def attach_gate_result(
        self, *, tenant_id: uuid.UUID, target_id: uuid.UUID, gate_result: dict[str, Any]
    ) -> None:
        """把门禁结论（SHACL gate_result 等）合并进该候选 open 单的信封（payload 浅合并键）。"""
        ...  # pragma: no cover — Protocol 方法无实现
