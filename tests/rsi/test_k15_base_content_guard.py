# tests/rsi/test_k15_base_content_guard.py
"""K15 批 G-15 整内容比对第三道写冲突防线用例（方案依据=docs/Agent/13 §21；蓝本=
openviking policy_updater.py:259-267 base-content guard「before_content 与当前内容不匹配
即拒写该 item」）。

断言目标：
- K15-a 原文快照：submit(baseline_content) 时条目原文与 baseline_hash 同位双存（内存口径=
  阶段 A 条目 prompt 模板/config，KB 级）；缺省 None 向后兼容（旧提案两字段皆空）；
- K15-b 第三道直比：hash 通道假性通过（hash 一致但原文漂移——手工构造模拟 hash 实现错/
  快照构造缺陷/键序漂移残余面）时整内容直比命中，与既有 drift 同路拒绝审计（detail 增
  check=content_direct）+ BaselineDriftError + K13 落册 reason=baseline_drift + 状态不变；
- hash 通道兼容：baseline_content=None 走 K9 语义零变化（漂移拒 detail 形态与 K9 一致，
  直比通道不参与、不产出 content_direct 审计）；
- None 跳过：baseline_content=None 且 hash 通过 → 直比通道跳过，不虚拒，apply 落既有恒拒
  红线；
- 通道顺序=先 hash 后直比：hash 通道未通过时按 K9 原样拒（直比不抢跑）。
"""

from __future__ import annotations

import uuid

import pytest

from services.rsi.audit import ACTION_APPLY_BASELINE_DRIFT, InMemoryAuditTrail
from services.rsi.proposal import Proposal, ProposalStatus, TriggerTrack, entry_baseline_hash
from services.rsi.service import BaselineDriftError, RsiApplyForbiddenError, RsiService
from services.rsi.whitelist import ImprovementType

TENANT = uuid.uuid4()
TRACE = "trace-k15-test"
TARGET = "prompt_templates/extract_power@v3"
ENTRY_CONTENT_V1 = 'prompt_template: extract_power v1\nprefix: "输出必须引用证据编号"'
ENTRY_CONTENT_V2 = 'prompt_template: extract_power v1\nprefix: "输出必须引用证据编号（含表号）"'

VALID_ENVELOPE = {
    "patch": {"system_prefix_addition": "输出必须引用证据编号"},
    "expected_gain": "同型失败率 -10%",
    "risk_level": "low",
    "eval_plan": {"golden": "extract_power_v1", "threshold": -0.02},
}


def _service(entries: dict[str, str]) -> tuple[RsiService, InMemoryAuditTrail]:
    """服务 + 条目注册表假体（闭包取数口实时读 dict——快照后并发改写即模拟条目漂移）。"""
    trail = InMemoryAuditTrail()

    async def loader(target: str) -> str | None:
        return entries.get(target)

    return RsiService(audit_trail=trail, entry_loader=loader), trail


async def _submit(
    service: RsiService, *, baseline_content: str | None = None
) -> Proposal:
    return await service.submit(
        tenant_id=TENANT,
        type_str=ImprovementType.PROMPT_TEMPLATE.value,
        target=TARGET,
        envelope=dict(VALID_ENVELOPE),
        trigger=TriggerTrack.EXPERIENCE,
        source_trace_ids=(TRACE,),
        baseline_content=baseline_content,
    )


# ── K15-a 原文快照：同位双存 + 缺省 None 向后兼容 ─────────────────────────


async def test_K15a_快照原文与hash同位双存() -> None:
    service, _trail = _service({TARGET: ENTRY_CONTENT_V1})
    proposal = await _submit(service, baseline_content=ENTRY_CONTENT_V1)
    assert proposal.baseline_content == ENTRY_CONTENT_V1  # 原文直存（非再加工/非截断）
    assert proposal.baseline_hash == entry_baseline_hash(ENTRY_CONTENT_V1)  # hash 通道照旧


async def test_K15a_缺省None向后兼容_两字段皆空() -> None:
    service, _trail = _service({TARGET: ENTRY_CONTENT_V1})
    proposal = await _submit(service)  # 未传 baseline_content（K9 时代调用形态零改动）
    assert proposal.baseline_content is None
    assert proposal.baseline_hash is None


# ── K15-b 第三道直比：hash 一致但原文漂移 → 命中 ──────────────────────────


async def test_K15b_hash假性通过_整内容直比命中() -> None:
    entries = {TARGET: ENTRY_CONTENT_V1}
    service, trail = _service(entries)
    proposal = await _submit(service, baseline_content=ENTRY_CONTENT_V1)
    entries[TARGET] = ENTRY_CONTENT_V2  # 并发写发生在快照后
    # 手工构造「hash 一致但原文漂移」残余面（模拟 hash 实现错/快照构造缺陷/键序漂移）：
    # 快照 hash 被以当前新内容构造 → hash 通道假性通过，唯有第三道整内容直比可命中
    proposal.baseline_hash = entry_baseline_hash(ENTRY_CONTENT_V2)
    with pytest.raises(BaselineDriftError) as exc_info:
        await service.apply(proposal.id, operator="agent://rsi")
    assert "changed during refinement" in str(exc_info.value)
    assert not isinstance(exc_info.value, RsiApplyForbiddenError)
    drifts = [r for r in trail.entries if r.action == ACTION_APPLY_BASELINE_DRIFT]
    assert len(drifts) == 1 and drifts[0].outcome == "rejected"
    assert drifts[0].detail["check"] == "content_direct"  # 直比通道标记（与 hash 通道可区分）
    assert drifts[0].detail["target"] == TARGET
    assert drifts[0].detail["entry_present"] is True
    assert service.pool[proposal.id].status is ProposalStatus.DRAFT  # 候选状态不变
    # 与既有 drift 同路：K13 落册 reason=baseline_drift（同因去重前先入册一条）
    ledger = service.list_rejections(TARGET)
    assert len(ledger) == 1
    assert ledger[0].proposal_id == proposal.id and ledger[0].reason == "baseline_drift"


async def test_K15b_通道顺序_先hash后直比_hash未过时按K9原样拒() -> None:
    entries = {TARGET: ENTRY_CONTENT_V1}
    service, trail = _service(entries)
    proposal = await _submit(service, baseline_content=ENTRY_CONTENT_V1)
    entries[TARGET] = ENTRY_CONTENT_V2  # 内容已漂移，但快照 hash 保持旧文复算口径
    with pytest.raises(BaselineDriftError):
        await service.apply(proposal.id)
    drifts = [r for r in trail.entries if r.action == ACTION_APPLY_BASELINE_DRIFT]
    assert len(drifts) == 1
    assert "check" not in drifts[0].detail  # hash 通道先命中，detail 形态与 K9 完全一致
    assert drifts[0].detail["baseline_hash"] == entry_baseline_hash(ENTRY_CONTENT_V1)


# ── hash 通道兼容：baseline_content=None 走 K9 语义零变化 ─────────────────


async def test_K15b_None走K9_hash通道_语义零变化() -> None:
    entries = {TARGET: ENTRY_CONTENT_V1}
    service, trail = _service(entries)
    proposal = await _submit(service)  # baseline_content=None（无原文快照）
    proposal.baseline_hash = entry_baseline_hash(ENTRY_CONTENT_V1)  # 手工补 K9 形态快照
    entries[TARGET] = ENTRY_CONTENT_V2  # 条目漂移 → hash 通道拒
    with pytest.raises(BaselineDriftError):
        await service.apply(proposal.id)
    drifts = [r for r in trail.entries if r.action == ACTION_APPLY_BASELINE_DRIFT]
    assert len(drifts) == 1 and drifts[0].outcome == "rejected"
    assert "check" not in drifts[0].detail  # 直比通道未参与：K9 audit detail 形态零变化
    assert drifts[0].detail["entry_present"] is True
    assert drifts[0].detail["baseline_hash"] == entry_baseline_hash(ENTRY_CONTENT_V1)


async def test_K15b_None直比通道跳过_不虚拒落既有恒拒红线() -> None:
    service, trail = _service({TARGET: ENTRY_CONTENT_V1})  # 内容未变，hash 通道通过
    proposal = await _submit(service)
    proposal.baseline_hash = entry_baseline_hash(ENTRY_CONTENT_V1)  # K9 形态：有 hash 无原文
    with pytest.raises(RsiApplyForbiddenError):  # 无原文可比 → 直比跳过 → apply 既有恒拒红线
        await service.apply(proposal.id)
    assert not [r for r in trail.entries if r.action == ACTION_APPLY_BASELINE_DRIFT]
