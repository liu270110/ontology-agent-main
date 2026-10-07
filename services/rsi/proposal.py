"""RSI 候选改进项（architecture/09 §7 数据模型要点 + §13.5 生命周期的阶段 A 骨架承载）。

- 状态机（09 §7）：``draft → evaluated → approved → rolled_out → reverted``，任一门禁/
  审批拒绝进终态 ``rejected``；终态不可逆。v1 用代码校验承载同语义（§13.5：PG CHECK 枚举
  + 代码校验，本体化随 M2 任务本体交付，不阻塞程序性 RSI 最小链）；
- 统一信封（09 §2）：``{patch|content, expected_gain, risk_level, eval_plan}``——LLM 起草、
  结构化字段过 0 级静态检查（gates.py）；
- 持久化边界（本批明确不做）：PG ``rsi_proposals`` 表 DDL 详设回填 database/01 属报告欠账
  （09 §7 登记待办），阶段 A 骨架以进程内候选池承载，禁写库。

K9 批（2026-10-05，docs/Agent/13 §15，G-9 并发漂移防线）追加：``transition(expect)`` 乐观
并发 CAS（StaleProposalError）与 ``baseline_hash`` 基线快照字段（创建时条目内容 sha256，
apply 路径漂移即拒）——蓝本=prime-agent refine.rs:388-400 + planner.rs:368-379。

K13 批（2026-10-05，docs/Agent/13 §19，G-13 落选回喂半环方案 A）追加：``RejectionRecord``
落选登记册条目（{proposal_id,type,reason,at}）与 ``Proposal.rejection_feedback`` 提交回显
字段（submit() 受理成功时服务侧回填同 target 最近 rejected 上下文）——蓝本=reef cordis
backend.py:1055-1057 rejected_proposals 有界入册+回喂。

K15 批（2026-10-07，docs/Agent/13 §21，G-15 整内容比对第三道写冲突防线）追加：
``Proposal.baseline_content`` 原文快照字段（submit() 受理时与 ``baseline_hash`` 同位双存
条目原文；内存口径=阶段 A 条目为 prompt 模板/config，KB 级量级直存可接受）——apply 前
hash 通道通过后整内容直比，兜住 hash 实现错/快照构造缺陷/hash 键序漂移等假性通过残余面，
蓝本=openviking policy_updater.py:259-267 base-content guard。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from services.rsi.whitelist import ImprovementType


class TriggerTrack(StrEnum):
    """三轨触发（09 §2 双轨 + §13.4 缺口轨；GAP 枚举位随 B9 G0 批启用——新增轨=框架变更）。

    - EXPERIENCE / METRIC：经 TriggerRegistry 注册分派（triggers.py）；
    - GAP：缺口检测器产出（GapCollector 滑窗聚类达标后 submit 入池）——**不经触发注册面**：
      缺口检测器=本体聚合巡检而非 handler 注册制，triggers.register 对 GAP 显式拒绝（§13.4）。
    """

    EXPERIENCE = "experience"  # 经验轨：任务终态事件复盘（事件触发）
    METRIC = "metric"  # 指标轨：08 §7 三类基准退化（周期触发）
    GAP = "gap"  # 缺口轨：结构信号（本体自检，零 LLM；09 §13.4，B9 G0 批启用）


class ProposalStatus(StrEnum):
    """候选状态机（09 §7 权威表）。"""

    DRAFT = "draft"
    EVALUATED = "evaluated"
    APPROVED = "approved"
    ROLLED_OUT = "rolled_out"
    REVERTED = "reverted"
    REJECTED = "rejected"


# 状态机（09 §7）：单向至终态，终态不可逆；approved 可被审批拒绝回落 rejected
_VALID_TRANSITIONS: dict[ProposalStatus, set[ProposalStatus]] = {
    ProposalStatus.DRAFT: {ProposalStatus.EVALUATED, ProposalStatus.REJECTED},
    ProposalStatus.EVALUATED: {ProposalStatus.APPROVED, ProposalStatus.REJECTED},
    ProposalStatus.APPROVED: {ProposalStatus.ROLLED_OUT, ProposalStatus.REJECTED},
    ProposalStatus.ROLLED_OUT: {ProposalStatus.REVERTED},
    ProposalStatus.REVERTED: set(),
    ProposalStatus.REJECTED: set(),
}

# 统一信封必备键（09 §2 产出信封；0 级静态检查的 schema 半边）
ENVELOPE_REQUIRED_KEYS: tuple[str, ...] = ("patch", "expected_gain", "risk_level", "eval_plan")

# 基线快照哈希口径版本（K9-b，G-9；参与哈希输入——升级口径即换版本号，新旧快照空间隔离，
# orsi capability_fingerprint / gap 场景指纹同款纪律）
BASELINE_HASH_VERSION = "rsi_proposal_baseline_v1"


def entry_baseline_hash(content: str) -> str:
    """目标能力条目内容的基线快照哈希（K9-b；口径 v1，本 docstring 即唯一权威）::

        canonical = BASELINE_HASH_VERSION + "\\x1f" + content
        entry_baseline_hash = sha256(canonical.encode("utf-8")).hexdigest()

    创建 Proposal 时对目标条目内容取此快照（service.submit ``baseline_content`` 口），
    apply/审批通过路径执行前对当前内容复算比对，不一致即拒（防「基于过期基线的进化」，
    prime-agent planner.rs:368-379 逐项 baseline 比对蓝本）。分隔符 ``\\x1f`` 与 gap.py
    场景指纹同款，防版本号与内容拼接歧义。
    """
    canonical = f"{BASELINE_HASH_VERSION}\x1f{content}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProposalError(Exception):
    """候选聚合错误（非法状态迁移/未找到等；阶段 A 无独立错误码段——不触 02 §7 码表）。"""


class StaleProposalError(ProposalError):
    """乐观并发校验失败（K9-a，G-9）：调用方声明的 expect 状态与候选实际状态不一致即拒。

    语义对齐 prime-agent refine.rs:388-400「entry changed during refinement planning」——
    规划/审批期间的并发写已使调用方持有的读旧，继续推进即覆盖他人变更，故先拒后重读
    （方案依据=docs/Agent/13 §15）。
    """


@dataclass(frozen=True, slots=True)
class RejectionRecord:
    """落选登记册条目（K13-a，G-13；蓝本=reef cordis backend.py:1055-1057 rejected 回喂，
    docs/研究整理/12/22-reef.md §4.2）。

    - proposal_id：落选候选 id；type：改进类型（ImprovementType.value）；
    - reason：落选因由（门禁 fail=gates verdict 摘要 / baseline_drift=K9-b 基线漂移）；
    - at：落册时刻（UTC）。REJECTED 均为吸收终态（09 §7 状态机不变），本条目只是
      结构化回喂记账（key=target 有界 deque 承载于 service，只读面出口）。
    """

    proposal_id: uuid.UUID
    type: str
    reason: str
    at: datetime


@dataclass(slots=True)
class Proposal:
    """候选改进项（09 §7 表要点；PG 行的阶段 A 进程内承载形）。"""

    tenant_id: uuid.UUID
    type: ImprovementType
    target: str  # 载体标识 + 基线版本号（如 prompt_templates/extract_power@v3）
    trigger: TriggerTrack  # 来源轨（experience/metric/gap 三轨，09 §2 + §13.4）
    envelope: dict[str, Any]  # 统一信封（patch|content、expected_gain、risk_level、eval_plan）
    source_trace_ids: tuple[str, ...] = ()  # 证据链：来源轨迹（09 §7 逐环可回链的起点）
    # K9-b 基线快照：创建时目标条目内容 sha256（entry_baseline_hash 口径）；None=旧提案无快照，apply 跳过基线校验
    baseline_hash: str | None = None
    # K15-a 原文快照（docs/Agent/13 §21，G-15）：受理时条目原文与 baseline_hash 同位双存，
    # apply 路径 hash 通道通过后整内容直比（第三道写冲突防线）。内存口径：阶段 A 条目=
    # prompt 模板/config（KB 级），进程内候选池直存原文可接受；None=旧提案/未存原文（K9
    # hash 通道语义不变）；rsi_proposals DDL 欠账清偿时同列落库。repr=False（K23 P2①）：
    # 原文快照不进 repr——日志/异常消息间接泄漏面收口（hash 摘要照常可见，非原文）。
    baseline_content: str | None = field(default=None, repr=False)
    # K13-b 提交回显：submit() 受理成功时由服务侧回填同 target 最近 rejected 上下文
    # （有界截断，最旧→最新；构造时恒空——落选回喂的提交端可见面）
    rejection_feedback: tuple[RejectionRecord, ...] = ()
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: ProposalStatus = ProposalStatus.DRAFT
    eval_report: dict[str, Any] | None = None  # 三级门禁结论（evaluate 时回填）
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def transition(self, to: ProposalStatus, *, expect: ProposalStatus | None = None) -> None:
        """状态迁移（09 §7 迁移合法性在此断言；迁移留痕由服务层落审计）。

        expect（K9-a 乐观并发/CAS，可选）：调用方声明的「我看到的当前状态」；非 None 且与
        实际状态不符 → 先抛 StaleProposalError（后于该断言的迁移合法性不再判定），调用方
        应重读后重试；``None`` = 不做该校验（既有调用方零改动，向后兼容）。
        """
        if expect is not None and self.status is not expect:
            raise StaleProposalError(
                f"候选状态已漂移：期望 {expect.value}，实际 {self.status.value}"
                f"（entry changed during refinement planning 语义，K9-a/G-9）"
            )
        if to not in _VALID_TRANSITIONS[self.status]:
            raise ProposalError(f"非法状态迁移 {self.status.value} → {to.value}（09 §7 状态机）")
        self.status = to
