# tests/rsi/test_k9_concurrency.py
"""K9 批 G-9 并发漂移防线用例（方案依据=docs/Agent/13 §15；蓝本=prime-agent
refine.rs:388-400「entry changed during refinement planning」即拒 + planner.rs:368-379
baseline 逐项比对）。

断言目标：
- K9-a expect CAS：transition(expect) 命中照常迁移 / 漂移拒 StaleProposalError（状态不变）/
  None 向后兼容既有调用方；异常风格对齐（StaleProposalError ⊂ ProposalError）；
- K9-b baseline 防线：submit(baseline_content) 建快照；apply 路径当前条目内容 hash≠快照即拒
  （BaselineDriftError，错误信息含 changed during refinement 语义 + 拒绝审计 + 状态不变）；
  条目消失亦属漂移；baseline_hash=None 旧提案跳过；无取数口跳过；红线（apply 恒拒）不因
  基线一致而松动。
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from services.rsi.audit import ACTION_APPLY_BASELINE_DRIFT, InMemoryAuditTrail
from services.rsi.proposal import (
    BASELINE_HASH_VERSION,
    Proposal,
    ProposalError,
    ProposalStatus,
    StaleProposalError,
    TriggerTrack,
    entry_baseline_hash,
)
from services.rsi.service import BaselineDriftError, RsiApplyForbiddenError, RsiService
from services.rsi.whitelist import ImprovementType

TENANT = uuid.uuid4()
TRACE = "trace-k9-test"
TARGET = "prompt_templates/extract_power@v3"
ENTRY_CONTENT_V1 = 'prompt_template: extract_power v1\nprefix: "输出必须引用证据编号"'
ENTRY_CONTENT_V2 = 'prompt_template: extract_power v1\nprefix: "输出必须引用证据编号（含表号）"'

VALID_ENVELOPE = {
    "patch": {"system_prefix_addition": "输出必须引用证据编号"},
    "expected_gain": "同型失败率 -10%",
    "risk_level": "low",
    "eval_plan": {"golden": "extract_power_v1", "threshold": -0.02},
}


def _proposal(**kw: object) -> Proposal:
    params: dict = {
        "tenant_id": TENANT,
        "type": ImprovementType.PROMPT_TEMPLATE,
        "target": TARGET,
        "trigger": TriggerTrack.EXPERIENCE,
        "envelope": dict(VALID_ENVELOPE),
    }
    params.update(kw)
    return Proposal(**params)  # type: ignore[arg-type]


def _service(entries: dict[str, str] | None = None) -> tuple[RsiService, InMemoryAuditTrail]:
    """服务 + 可选条目注册表假体（闭包取数口：target → 当前内容）。"""
    trail = InMemoryAuditTrail()

    async def loader(target: str) -> str | None:
        return None if entries is None else entries.get(target)

    return RsiService(audit_trail=trail, entry_loader=loader if entries is not None else None), trail


# ── K9-a expect CAS ──────────────────────────────────────────────────────


def test_expect命中_照常迁移() -> None:
    proposal = _proposal()  # draft
    proposal.transition(ProposalStatus.EVALUATED, expect=ProposalStatus.DRAFT)  # 声明与实际一致
    assert proposal.status is ProposalStatus.EVALUATED


def test_expect漂移即拒_StaleProposalError_状态不变() -> None:
    proposal = _proposal()
    proposal.transition(ProposalStatus.EVALUATED)
    # 调用方仍以为在 draft（读旧），声明 expect=DRAFT 与实际 evaluated 不符 → 拒
    with pytest.raises(StaleProposalError) as exc_info:
        proposal.transition(ProposalStatus.REJECTED, expect=ProposalStatus.DRAFT)
    assert "changed during refinement" in str(exc_info.value)
    assert proposal.status is ProposalStatus.EVALUATED  # 状态不被并发写覆盖


def test_expect漂移优先于迁移合法性断言() -> None:
    proposal = _proposal()
    proposal.transition(ProposalStatus.EVALUATED)
    # expect 不符时先报漂移（而非 evaluated→approved 的非法迁移错），引导调用方重读
    with pytest.raises(StaleProposalError):
        proposal.transition(ProposalStatus.APPROVED, expect=ProposalStatus.DRAFT)


def test_expect_None向后兼容_既有调用零改动() -> None:
    proposal = _proposal()
    proposal.transition(ProposalStatus.EVALUATED)  # 不传 expect = 旧调用形态
    assert proposal.status is ProposalStatus.EVALUATED
    # 显式 None 同义；且不做漂移校验（状态机合法性断言仍生效：draft→approved 非法）
    illegal = _proposal()
    with pytest.raises(ProposalError) as exc_info:
        illegal.transition(ProposalStatus.APPROVED, expect=None)
    assert "非法状态迁移" in str(exc_info.value)
    assert not isinstance(exc_info.value, StaleProposalError)


def test_StaleProposalError对齐模块异常风格() -> None:
    assert issubclass(StaleProposalError, ProposalError)  # 调用方按 ProposalError 兜底可捕获


# ── K9-b baseline 基线快照 + apply 路径漂移拒 ─────────────────────────────


def test_entry_baseline_hash_口径与确定性() -> None:
    h1, h1b = entry_baseline_hash(ENTRY_CONTENT_V1), entry_baseline_hash(ENTRY_CONTENT_V1)
    assert h1 == h1b  # 同内容同哈希
    assert entry_baseline_hash(ENTRY_CONTENT_V2) != h1  # 内容变则哈希变
    # 口径版本参与哈希（升级即换版本号，新旧快照空间隔离）
    assert h1 == hashlib.sha256(f"{BASELINE_HASH_VERSION}\x1f{ENTRY_CONTENT_V1}".encode()).hexdigest()


async def test_submit建快照_落baseline_hash() -> None:
    service, _trail = _service(entries={TARGET: ENTRY_CONTENT_V1})
    proposal = await service.submit(
        tenant_id=TENANT,
        type_str=ImprovementType.PROMPT_TEMPLATE.value,
        target=TARGET,
        envelope=dict(VALID_ENVELOPE),
        trigger=TriggerTrack.EXPERIENCE,
        source_trace_ids=(TRACE,),
        baseline_content=ENTRY_CONTENT_V1,
    )
    assert proposal.baseline_hash == entry_baseline_hash(ENTRY_CONTENT_V1)


async def test_baseline漂移即拒_apply路径() -> None:
    service, trail = _service(entries={TARGET: ENTRY_CONTENT_V2})  # 条目已被并发改写
    proposal = await service.submit(
        tenant_id=TENANT,
        type_str=ImprovementType.PROMPT_TEMPLATE.value,
        target=TARGET,
        envelope=dict(VALID_ENVELOPE),
        trigger=TriggerTrack.EXPERIENCE,
        source_trace_ids=(TRACE,),
        baseline_content=ENTRY_CONTENT_V1,  # 提案基于旧内容
    )
    with pytest.raises(BaselineDriftError) as exc_info:
        await service.apply(proposal.id, operator="agent://rsi")
    assert "changed during refinement" in str(exc_info.value)
    assert not isinstance(exc_info.value, RsiApplyForbiddenError)
    drifts = [r for r in trail.entries if r.action == ACTION_APPLY_BASELINE_DRIFT]
    assert len(drifts) == 1 and drifts[0].outcome == "rejected"
    assert drifts[0].detail["target"] == TARGET
    assert service.pool[proposal.id].status is ProposalStatus.DRAFT  # 状态不变


async def test_条目消失视为漂移() -> None:
    service, _trail = _service(entries={})  # 目标条目已被删除
    proposal = await service.submit(
        tenant_id=TENANT,
        type_str=ImprovementType.PROMPT_TEMPLATE.value,
        target=TARGET,
        envelope=dict(VALID_ENVELOPE),
        trigger=TriggerTrack.EXPERIENCE,
        source_trace_ids=(TRACE,),
        baseline_content=ENTRY_CONTENT_V1,
    )
    with pytest.raises(BaselineDriftError):
        await service.apply(proposal.id)


async def test_baseline一致_不触发漂移_apply仍恒拒() -> None:
    service, trail = _service(entries={TARGET: ENTRY_CONTENT_V1})  # 内容未变
    proposal = await service.submit(
        tenant_id=TENANT,
        type_str=ImprovementType.PROMPT_TEMPLATE.value,
        target=TARGET,
        envelope=dict(VALID_ENVELOPE),
        trigger=TriggerTrack.EXPERIENCE,
        source_trace_ids=(TRACE,),
        baseline_content=ENTRY_CONTENT_V1,
    )
    with pytest.raises(RsiApplyForbiddenError):  # 基线一致 → 走既有恒拒红线，不虚报漂移
        await service.apply(proposal.id)
    assert not [r for r in trail.entries if r.action == ACTION_APPLY_BASELINE_DRIFT]


async def test_baseline_None旧提案跳过校验() -> None:
    service, trail = _service(entries={TARGET: ENTRY_CONTENT_V2})  # 条目已变，但旧提案无快照
    proposal = await service.submit(
        tenant_id=TENANT,
        type_str=ImprovementType.PROMPT_TEMPLATE.value,
        target=TARGET,
        envelope=dict(VALID_ENVELOPE),
        trigger=TriggerTrack.EXPERIENCE,
        source_trace_ids=(TRACE,),
    )  # 未传 baseline_content
    assert proposal.baseline_hash is None
    with pytest.raises(RsiApplyForbiddenError):  # 跳过基线校验 → 既有恒拒红线
        await service.apply(proposal.id)
    assert not [r for r in trail.entries if r.action == ACTION_APPLY_BASELINE_DRIFT]


async def test_无取数口_基线校验跳过不虚拒() -> None:
    service, _trail = _service(entries=None)  # 未注入 entry_loader（阶段 A 缺省形态）
    proposal = await service.submit(
        tenant_id=TENANT,
        type_str=ImprovementType.PROMPT_TEMPLATE.value,
        target=TARGET,
        envelope=dict(VALID_ENVELOPE),
        trigger=TriggerTrack.EXPERIENCE,
        source_trace_ids=(TRACE,),
        baseline_content=ENTRY_CONTENT_V1,
    )
    with pytest.raises(RsiApplyForbiddenError):  # 无法取当前内容 ≠ 已漂移：不虚拒
        await service.apply(proposal.id)


async def test_baseline校验_候选不存在仍报ProposalError() -> None:
    service, _trail = _service(entries={TARGET: ENTRY_CONTENT_V1})
    with pytest.raises(ProposalError):
        await service.apply(uuid.uuid4())  # _require 先于基线校验
