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
- 非线程安全：归属单事件循环（lite 单进程部署；多副本 inbox 分发随 D4 族，§5 不做）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from services.agent.business.kernel.errors import KernelContractError, KernelError
from services.platform.config import get_settings
from services.platform.errors import ErrorCode

InboxKind = Literal["followup", "steer", "inject"]

# 步边界 drain 的取件通道（followup 留存，§1.1）
_STEERABLE_KINDS: tuple[str, ...] = ("steer", "inject")


class InboxItem(BaseModel):
    """收件箱条目（值对象 frozen）：seq 全箱单调递增，审计对账键。"""

    model_config = ConfigDict(frozen=True)

    seq: int
    kind: InboxKind
    text: str
    source: str  # 提交方标识（API 路径=提交主体 user_id；测试/内部路径自报）


class KernelInbox:
    """单 Run 收件箱：submit 即达（同进程 API→内核），步边界 drain、终态后 take。"""

    def __init__(self, *, max_per_run: int | None = None) -> None:
        # D2/F-4 纪律：显式注入优先（测试与组合根直传通道），未注入读配置层唯一事实源
        self._max_per_run = max_per_run if max_per_run is not None else get_settings().kernel_inbox_max_per_run
        self._followup: list[InboxItem] = []
        self._steerable: list[InboxItem] = []
        self._next_seq = 1
        self._auditor: Callable[[str, dict[str, Any]], None] | None = None
        self._spliced_log: list[dict[str, Any]] = []  # 全量 splice 留痕（attach 前的补记源）

    # ── 提交面（API/编排器同进程直调）─────────────────────────────────────
    def submit(self, kind: str, text: str, *, source: str) -> int:
        """入箱一笔（返回全箱单调 seq）；容量超限 4203 结构化拒绝、空文本契约拒绝。"""
        if kind not in ("followup", "steer", "inject"):
            raise KernelContractError(f"inbox kind 非法: {kind!r}（三通道=followup/steer/inject）")
        if not isinstance(text, str) or not text.strip():
            raise KernelContractError("inbox 文本为空（拒绝空提交）")
        if self.pending_count >= self._max_per_run:
            raise KernelError(
                ErrorCode.INBOX_CAPACITY,
                f"INBOX_CAPACITY: 收件箱容量超限（pending={self.pending_count} ≥ max_per_run={self._max_per_run}）",
            )
        item = InboxItem(seq=self._next_seq, kind=kind, text=text, source=source)  # type: ignore[arg-type]
        self._next_seq += 1
        if kind == "followup":
            self._followup.append(item)
        else:
            self._steerable.append(item)
        self._audit("kernel.inbox_spliced", item)
        return item.seq

    # ── 消费面（内核 loop / 编排器）───────────────────────────────────────
    def drain_steerable(self) -> tuple[InboxItem, ...]:
        """步边界取件：steer+inject 全取（保持提交序），followup 留存步中不生效。"""
        drained = tuple(self._steerable)
        self._steerable.clear()
        return drained

    def take_followups(self) -> tuple[InboxItem, ...]:
        """终态后取件（编排器调用）：followup 全取 → 下一条用户消息候选。"""
        taken = tuple(self._followup)
        self._followup.clear()
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
