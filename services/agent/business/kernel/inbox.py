"""运行中输入面三通道收件箱（M4.5-A，docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1.1）。

每 Run 一个 :class:`KernelInbox` 实例，三队列语义对齐 DSH inbox：

- **followup**（Run 结束后由编排器取走 → 成为下一条用户消息的候选）——步边界 drain
  **不取**，步中不生效；
- **steer**（步边界唤醒——影响后续步的组装上下文）；
- **inject**（注入不唤醒——并入上下文但不打断步序）。

纪律：
- 容量上限 ``Settings.kernel_inbox_max_per_run``（三队列合计待处理条目；缺省 8，
  ge=1 le=32），超限抛 :class:`KernelError`（4203 INBOX_CAPACITY，结构化拒绝）；
- 每笔 submit 落 ledger 事件 ``kernel.inbox_spliced``（payload：kind/source/seq/text
  ——用户指令属审计必需内容，非工具正文，不违 transport-only）；注册窗口内的 submit
  由 :meth:`attach_auditor` 补记（事件先落库后生效，与「先落库后推送」同序）；
- K26-a 三态+去重（durable ingress，docs/Agent/13 §32；上游 openclaw §9
  claim/complete/release）：条目 ``status`` 迁移 pending→claimed→completed（frozen
  值对象经 model_copy 派生新实例表达迁移）；submit 同 ``dedupe_key``
  （=sha256("source|text")，12 篇 A 批登记口径）命中即幂等回执原 seq——不双份、
  不占容量、不重复落审计（回执即原受理回执，与 C2 EXTERNAL_WRITE 幂等回执同型；
  先查重后查容量：重投不因箱满被拒）；completed 命中同回执原 seq 不留新条目
  （内容已消费，复注即双份）；
- 非线程安全：归属单事件循环（lite 单进程部署；多副本 inbox 分发随 D4 族，§5 不做）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from services.agent.business.kernel.errors import KernelContractError, KernelError
from services.platform.config import get_settings
from services.platform.errors import ErrorCode

InboxKind = Literal["followup", "steer", "inject"]
InboxStatus = Literal["pending", "claimed", "completed"]

# 步边界取件的通道面（followup 留存，§1.1）
_STEERABLE_KINDS: tuple[str, ...] = ("steer", "inject")


def _dedupe_key(source: str, text: str) -> str:
    """重投去重键（12 篇 A 批登记口径：source+sha256(text)；不含 kind——同文跨通道同为重投）。"""
    return hashlib.sha256(f"{source}|{text}".encode()).hexdigest()


class InboxItem(BaseModel):
    """收件箱条目（值对象 frozen）：seq 全箱单调递增，审计对账键。

    K26-a durable ingress 三态（docs/Agent/13 §32）：``status`` 随两段式消费迁移
    pending→claimed→completed；frozen 语义下迁移=``model_copy`` 派生新实例替换旧位。
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    kind: InboxKind
    text: str
    source: str  # 提交方标识（API 路径=提交主体 user_id；测试/内部路径自报）
    status: InboxStatus = "pending"
    dedupe_key: str = ""  # 重投幂等对账键（submit 处自动计算）
    received_at: datetime  # 提交时刻（UTC）


class KernelInbox:
    """单 Run 收件箱：submit 即达（同进程 API→内核），段边界 claim/complete 两段式、终态后 take。"""

    def __init__(self, *, max_per_run: int | None = None) -> None:
        # D2/F-4 纪律：显式注入优先（测试与组合根直传通道），未注入读配置层唯一事实源
        self._max_per_run = max_per_run if max_per_run is not None else get_settings().kernel_inbox_max_per_run
        self._followup: list[InboxItem] = []
        self._steerable: list[InboxItem] = []
        self._next_seq = 1
        self._auditor: Callable[[str, dict[str, Any]], None] | None = None
        self._spliced_log: list[dict[str, Any]] = []  # 全量 splice 留痕（attach 前的补记源）
        # K26-a completed 留痕（dedupe_key→seq；最小侵入：只留幂等对账所需，不保完整条目）
        self._completed: dict[str, int] = {}

    # ── 提交面（API/编排器同进程直调）─────────────────────────────────────
    def submit(self, kind: str, text: str, *, source: str) -> int:
        """入箱一笔（返回全箱单调 seq）。

        K26-a 幂等：同 dedupe_key 命中（pending/claimed 活动面或 completed 留痕）即回执
        原 seq——不新增条目、不占容量、不重复落审计；先查重后查容量（重投不因箱满被拒）。
        容量超限 4203 结构化拒绝、空文本契约拒绝。
        """
        if kind not in ("followup", "steer", "inject"):
            raise KernelContractError(f"inbox kind 非法: {kind!r}（三通道=followup/steer/inject）")
        if not isinstance(text, str) or not text.strip():
            raise KernelContractError("inbox 文本为空（拒绝空提交）")
        key = _dedupe_key(source, text)
        hit = self._dedupe_hit(key)
        if hit is not None:
            return hit
        if self.pending_count >= self._max_per_run:
            raise KernelError(
                ErrorCode.INBOX_CAPACITY,
                f"INBOX_CAPACITY: 收件箱容量超限（pending={self.pending_count} ≥ max_per_run={self._max_per_run}）",
            )
        item = InboxItem(  # type: ignore[arg-type]
            seq=self._next_seq, kind=kind, text=text, source=source, dedupe_key=key, received_at=datetime.now(UTC)
        )
        self._next_seq += 1
        if kind == "followup":
            self._followup.append(item)
        else:
            self._steerable.append(item)
        self._audit("kernel.inbox_spliced", item)
        return item.seq

    def _dedupe_hit(self, key: str) -> int | None:
        """同键命中回执：pending/claimed 活动面优先，其次 completed 留痕（K26-a）。"""
        for item in (*self._followup, *self._steerable):
            if item.dedupe_key == key:
                return item.seq
        return self._completed.get(key)

    # ── 消费面（内核 loop / 编排器）───────────────────────────────────────
    def claim_steerable(self) -> tuple[InboxItem, ...]:
        """段边界取件（K26-b 两段式第一段）：pending→claimed 标记后返回（保持提交序），
        **不清除列表**——claimed 项再 claim 不重复返回；followup 留存步中不生效。
        complete 前崩溃/中断窗口内 claimed 项留箱（容量仍占位），release_stale 可重投。
        """
        claimed: list[InboxItem] = []
        marked: list[InboxItem] = []
        for item in self._steerable:
            if item.status == "pending":
                item = item.model_copy(update={"status": "claimed"})
                claimed.append(item)
            marked.append(item)
        self._steerable = marked
        return tuple(claimed)

    def complete(self, seq: int) -> bool:
        """两段式第二段（K26-b）：claimed→completed——从活动列表清除，dedupe_key 进
        completed 留痕（重投同文回执原 seq 不复注）。loop 于 kernel.inbox_drained 落账后
        调用（注入可见即消费闭环）；seq 未命中（已 completed/未知）幂等返回 False。"""
        for i, item in enumerate(self._steerable):
            if item.seq == seq:
                self._completed.setdefault(item.dedupe_key, item.seq)
                del self._steerable[i]
                return True
        return False

    def release_stale(self) -> tuple[InboxItem, ...]:
        """对账口（K26-c）：claimed→pending 重投——claim 后未 complete 的项（中断窗口）
        回 pending 可再 claim；返回重投项（观测/测试用；fresh 箱调用=空转幂等）。
        挂点=编排器 fresh inbox 起点（chat_orchestrator；恢复链可持旧 inbox 引用时对
        其实调，跨 Run 遗弃语义登记 M5+）。"""
        released: list[InboxItem] = []
        reset: list[InboxItem] = []
        for item in self._steerable:
            if item.status == "claimed":
                item = item.model_copy(update={"status": "pending"})
                released.append(item)
            reset.append(item)
        self._steerable = reset
        return tuple(released)

    def take_followups(self) -> tuple[InboxItem, ...]:
        """终态后取件（编排器调用）：followup 全取 → 下一条用户消息候选。

        K26-b 取舍：保持清除式——followup 无 claim 窗口（终态后一次性取走即出 Run
        生命周期，无「注入前崩溃」的恢复需求；跨 Run 持久化属 M5+）；取走即视为消费
        完成，dedupe_key 进 completed 留痕防终态窗口重投双份。
        """
        taken = tuple(self._followup)
        self._followup.clear()
        for item in taken:
            self._completed.setdefault(item.dedupe_key, item.seq)
        return taken

    @property
    def pending_count(self) -> int:
        """待处理条目数（三队列合计；容量口径）。"""
        return len(self._followup) + len(self._steerable)

    # ── 审计挂钩（内核 loop 在 run() 起点挂账本发射口，C2）──────────────────
    def attach_auditor(self, auditor: Callable[[str, dict[str, Any]], None] | None) -> None:
        """挂/摘 splice 审计发射口（签名=``(event_type, payload)``）。

        挂上即补记注册窗口内的既有 submit（事件先落库后生效：文本进组装前，
        kernel.inbox_spliced 必已在账本）。
        """
        self._auditor = auditor
        if auditor is not None:
            for entry in self._spliced_log:
                auditor("kernel.inbox_spliced", dict(entry))

    def _audit(self, event_type: str, item: InboxItem) -> None:
        payload = {"kind": item.kind, "source": item.source, "seq": item.seq, "text": item.text}
        self._spliced_log.append(dict(payload))
        if self._auditor is not None:
            self._auditor(event_type, dict(payload))
