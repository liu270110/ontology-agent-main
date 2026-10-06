# tests/rsi/test_k13_rejection_feedback.py
"""K13 批 G-13 落选回喂半环用例（方案依据=docs/Agent/13 §19 方案 A；蓝本=reef
train/cordis_backend/backend.py:1055-1057 rejected_proposals 有界入册+回喂，
docs/研究整理/12/22-reef.md §4.2）。

断言目标：
- K13-a 落册：evaluate() 门禁 fail→REJECTED 分支与 apply() BaselineDrift 分支均落
  key=target 有界登记册（内容={proposal_id,type,reason,at}，最旧→最新；maxlen 淘汰
  旧条；0=关闭落册零残留）；REJECTED 吸收终态语义不变；
- K13-b 提交回显：submit() 受理成功回填 rejection_feedback（同 target 最近 5 条，
  有界截断；无历史=空元组；不含本次新提案）；
- K13-c 查询面：list_rejections(target) 只读快照（副本语义，未知 target=空列表）；
- D2 缺省通道：构造参数缺省读 Settings.rsi_rejection_ledger_maxlen。
"""

from __future__ import annotations

import uuid

import pytest

from services.platform.config import get_settings
from services.rsi.audit import InMemoryAuditTrail
from services.rsi.proposal import Proposal, ProposalStatus, RejectionRecord, TriggerTrack
from services.rsi.service import (
    REJECTION_FEEDBACK_MAX,
    REJECTION_LEDGER_MAXLEN,
    BaselineDriftError,
    RsiApplyForbiddenError,
    RsiService,
)
from services.rsi.whitelist import ImprovementType

TENANT = uuid.uuid4()
TRACE = "trace-k13-test"
TARGET = "prompt_templates/extract_power@v3"
TARGET_B = "prompt_templates/summarize_table@v1"
ENTRY_CONTENT_V1 = 'prompt_template: extract_power v1\nprefix: "输出必须引用证据编号"'
ENTRY_CONTENT_V2 = 'prompt_template: extract_power v1\nprefix: "输出必须引用证据编号（含表号）"'

VALID_ENVELOPE = {
    "patch": {"system_prefix_addition": "输出必须引用证据编号"},
    "expected_gain": "同型失败率 -10%",
    "risk_level": "low",
    "eval_plan": {"golden": "extract_power_v1", "threshold": -0.02},
}


def _envelope_missing(*drop: str) -> dict:
    """缺键信封（0 级门禁 fail 的确定性造法，reason 含「统一信封缺键」）。"""
    env = dict(VALID_ENVELOPE)
    for key in drop:
        env.pop(key, None)
    return env


def _service(
    *, entries: dict[str, str] | None = None, rejection_ledger_maxlen: int | None = None
) -> RsiService:
    """服务假体（闭包取数口同 K9 测试口径；maxlen 缺省走 Settings D2 通道）。"""
    trail = InMemoryAuditTrail()

    async def loader(target: str) -> str | None:
        return None if entries is None else entries.get(target)

    kw: dict = {"audit_trail": trail, "entry_loader": loader if entries is not None else None}
    if rejection_ledger_maxlen is not None:
        kw["rejection_ledger_maxlen"] = rejection_ledger_maxlen
    return RsiService(**kw)


async def _submit(
    service: RsiService,
    *,
    target: str = TARGET,
    envelope: dict | None = None,
    baseline_content: str | None = None,
) -> Proposal:
    kw: dict = {}
    if baseline_content is not None:
        kw["baseline_content"] = baseline_content
    return await service.submit(
        tenant_id=TENANT,
        type_str=ImprovementType.PROMPT_TEMPLATE.value,
        target=target,
        envelope=envelope if envelope is not None else dict(VALID_ENVELOPE),
        trigger=TriggerTrack.EXPERIENCE,
        source_trace_ids=(TRACE,),
        **kw,
    )


async def _submit_and_fail(
    service: RsiService, *, target: str = TARGET, envelope: dict | None = None
) -> Proposal:
    """确定性落选：缺 eval_plan 键 → 0 级门禁 fail → REJECTED 终态。"""
    proposal = await _submit(service, target=target, envelope=envelope or _envelope_missing("eval_plan"))
    evaluated = await service.evaluate(proposal.id)
    assert evaluated.status is ProposalStatus.REJECTED  # 终态语义不变（K13 只记账不迁移）
    return evaluated


# ── K13-a 门禁 fail 落册 ─────────────────────────────────────────────────


async def test_门禁fail落册_内容完整() -> None:
    service = _service()
    rejected = await _submit_and_fail(service)

    ledger = service.list_rejections(TARGET)
    assert len(ledger) == 1
    record = ledger[0]
    assert isinstance(record, RejectionRecord)
    assert record.proposal_id == rejected.id
    assert record.type == ImprovementType.PROMPT_TEMPLATE.value
    assert record.reason.startswith("fail:")  # gates verdict 摘要
    assert "统一信封缺键" in record.reason
    assert record.at.tzinfo is not None  # UTC 带时区时刻


async def test_落册顺序最旧到最新_target间隔离() -> None:
    service = _service()
    first = await _submit_and_fail(service, envelope=_envelope_missing("eval_plan"))
    second = await _submit_and_fail(service, envelope=_envelope_missing("expected_gain"))
    other = await _submit_and_fail(service, target=TARGET_B)

    ledger = service.list_rejections(TARGET)
    assert [r.proposal_id for r in ledger] == [first.id, second.id]  # 最旧→最新
    assert ledger[0].reason != ledger[1].reason  # reason 随各自 verdict
    assert len(service.list_rejections(TARGET_B)) == 1  # key=target 互不串册
    assert service.list_rejections(TARGET_B)[0].proposal_id == other.id


async def test_BaselineDrift也落册_reason_baseline_drift() -> None:
    entries = {TARGET: ENTRY_CONTENT_V1}
    service = _service(entries=entries)
    proposal = await _submit(service, baseline_content=ENTRY_CONTENT_V1)

    entries[TARGET] = ENTRY_CONTENT_V2  # 基线漂移
    with pytest.raises(BaselineDriftError):
        await service.apply(proposal.id)

    ledger = service.list_rejections(TARGET)
    assert len(ledger) == 1
    assert ledger[0].proposal_id == proposal.id
    assert ledger[0].reason == "baseline_drift"
    assert proposal.status is ProposalStatus.DRAFT  # K9 语义不变：漂移拒不动状态

    # 对照：基线一致 → 走 apply 恒拒红线，不落册
    fresh = await _submit(service, baseline_content=ENTRY_CONTENT_V2)
    with pytest.raises(RsiApplyForbiddenError):
        await service.apply(fresh.id)
    assert len(service.list_rejections(TARGET)) == 1  # 仍只有漂移那一条


# ── K13-b 提交回显 ───────────────────────────────────────────────────────


async def test_提交回显_有历史截断最近N条() -> None:
    service = _service(rejection_ledger_maxlen=REJECTION_LEDGER_MAXLEN)
    rejected_ids = [(await _submit_and_fail(service)).id for _ in range(7)]

    newcomer = await _submit(service)  # 第 8 个提案：受理成功即回显

    feedback = newcomer.rejection_feedback
    assert len(feedback) == REJECTION_FEEDBACK_MAX == 5  # 有界截断
    assert [r.proposal_id for r in feedback] == rejected_ids[-5:]  # 最近 5 条，最旧→最新
    assert newcomer.id not in {r.proposal_id for r in feedback}  # 不含本次新提案
    assert all(r.reason.startswith("fail:") for r in feedback)


async def test_提交回显_无历史为空() -> None:
    service = _service()
    proposal = await _submit(service)
    assert proposal.rejection_feedback == ()
    assert service.list_rejections(TARGET) == []


# ── K13-c 查询面 ─────────────────────────────────────────────────────────


async def test_查询面快照副本语义_未知target空列表() -> None:
    service = _service()
    rejected = await _submit_and_fail(service)

    snapshot = service.list_rejections(TARGET)
    snapshot.clear()  # 改副本不得穿透登记册
    assert len(service.list_rejections(TARGET)) == 1
    assert service.list_rejections(TARGET)[0].proposal_id == rejected.id
    assert service.list_rejections("memory_policies/no_such@v9") == []


# ── 有界容量与关闭语义 ───────────────────────────────────────────────────


async def test_有界maxlen淘汰旧条() -> None:
    service = _service(rejection_ledger_maxlen=2)
    first = await _submit_and_fail(service)
    second = await _submit_and_fail(service)
    third = await _submit_and_fail(service)

    ledger = service.list_rejections(TARGET)
    assert [r.proposal_id for r in ledger] == [second.id, third.id]  # 最旧者淘汰
    assert first.id not in {r.proposal_id for r in ledger}
    newcomer = await _submit(service)
    assert [r.proposal_id for r in newcomer.rejection_feedback] == [second.id, third.id]


async def test_maxlen0关闭落册_状态机不受影响() -> None:
    service = _service(rejection_ledger_maxlen=0)
    rejected = await _submit_and_fail(service)  # 门禁照常拒（0=只关落册，不关门禁）

    assert rejected.status is ProposalStatus.REJECTED
    assert service.list_rejections(TARGET) == []
    newcomer = await _submit(service)
    assert newcomer.rejection_feedback == ()


async def test_构造缺省读Settings_D2通道() -> None:
    assert RsiService()._rejection_maxlen == get_settings().rsi_rejection_ledger_maxlen
    assert REJECTION_LEDGER_MAXLEN == get_settings().rsi_rejection_ledger_maxlen == 25


async def test_同提案同因重复落选不重复占册() -> None:
    """ocr 2026-10-06 评审：apply 对漂移提案反复重试，同 proposal_id+同 reason 只落册一次。"""
    service = _service()
    proposal = await _submit(service)  # 受理成功的提案（未落选）

    service._record_rejection(proposal, reason="baseline_drift")
    service._record_rejection(proposal, reason="baseline_drift")  # 同因重复 → 去重
    service._record_rejection(proposal, reason="gate:another")  # 异因仍落

    records = service.list_rejections(TARGET)
    drifts = [r for r in records if r.reason == "baseline_drift"]
    assert len(drifts) == 1 and len(records) == 2
    assert all(r.proposal_id == proposal.id for r in records)
