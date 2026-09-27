"""ask_user 答复通道（问询板）：问询登记—答复送达—关联 id 贯穿（docs/Agent/06 路线 #6）。

问询-答复时序（question_id = 工具调用 call_id 字符串，与内核账本 tool_call 登记同键）：

1. 工具发起：``open_question`` 登记未决问询（并发上限内），随后挂起等待；
2. 问询下发：ChatStream TOOL_CALL_ARGS 载荷携带 question_id + 问题文本 + 选项（前端可见性）；
3. 答复到达：用户答复经既有 messages 受理进入会话后，消息路径适配器按
   :func:`parse_answer_message` 约定解析出 question_id，调 ``submit_answer`` 送达；
4. 收口三态：``answered``（结构化结果携带答复文本）/ ``timeout``（超时默认拒绝，
   B5 同纪律）/ ``cancelled``（运行取消即作废，迟答复不误配）。

InMemory 形态为 v1 会话内实现（单事件循环）；跨进程形态（PG 轮询/Redis）随 chat
接线批次按同一 :class:`AskUserBoard` Protocol 替换。答复文本一律不可信外部输入
（B3）：本通道只搬运，信任级由内核标界（agent_attested）。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from services.agent.business.capabilities.ask_user.guards import DEFAULT_MAX_PENDING, AskUserToolError
from services.platform.errors import ErrorCode

# 会话消息路径的答复约定（复用既有 messages 受理；chat 接线批次在消息入口调用）：
# 「/answer <question_id> 答复正文」——非该前缀的消息返回 None（普通对话消息，不投板）
_ANSWER_MESSAGE_RE = re.compile(r"^/answer\s+(\S+)\s+(.+)$", re.DOTALL)

# 时钟注入口（测试确定性纪律，standards：禁用例直睡真钟）；缺省单调钟
Clock = Callable[[], float]


class AskResolution(StrEnum):
    """问询收口三态（AnswerResolution.status 与结构化结果 resolved_as 字段取值）。"""

    ANSWERED = "answered"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class QuestionRecord(BaseModel):
    """未决问询登记行（值对象 frozen）：question_id 为问询-答复贯穿审计的关联键。"""

    model_config = ConfigDict(frozen=True)

    question_id: str
    question: str
    options: tuple[str, ...] | None = None
    context: str | None = None
    tenant_id: UUID | None = None  # C3 归属（发起时从 TenantContext 注入）
    trace_id: str | None = None  # C2 贯穿（审计对账）
    run_id: UUID | None = None  # 组合根装配注入（审计归因）
    session_id: UUID | None = None  # 同上
    opened_at: float = 0.0  # clock() 发起时刻（耗时核算）


class AnswerResolution(BaseModel):
    """问询收口结果（值对象 frozen）：答复文本为不可信外部输入（B3 内核标界）。"""

    model_config = ConfigDict(frozen=True)

    question_id: str
    status: AskResolution
    answer_text: str | None = None  # 仅 status=answered 非空
    source: str | None = None  # 答复来源标注（如 "user_message"）


def parse_answer_message(text: str) -> tuple[str, str] | None:
    """从会话消息文本解析问询答复（消息路径适配器入口约定）。

    命中「/answer <question_id> 正文」返回 (question_id, 答复正文)；其余消息返回
    None（普通对话消息不投板，既有 messages 受理路径不动）。答复正文保留原文
    （含换行），仅去指令前缀。
    """
    if not isinstance(text, str):
        return None
    match = _ANSWER_MESSAGE_RE.match(text.strip())
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


class _PendingEntry:
    """未决问询的等待项：future 完成即收口（shield 包裹，超时/取消不误伤内层）。"""

    __slots__ = ("future", "record")

    def __init__(self, record: QuestionRecord) -> None:
        self.record = record
        self.future: asyncio.Future[AnswerResolution] = asyncio.get_running_loop().create_future()


@runtime_checkable
class AskUserBoard(Protocol):
    """问询板端口：工具（发起/等待）与消息路径适配器（送达）的会合面。"""

    def open_question(self, record: QuestionRecord) -> None:
        """登记未决问询；超出并发上限抛 AskUserToolError（4103）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def wait_answer(self, question_id: str, *, timeout_s: float) -> AnswerResolution:
        """挂起等待答复；超时默认拒绝（B5 同纪律）；取消传播前作废问询。"""
        ...  # pragma: no cover — Protocol 方法无实现

    def submit_answer(self, question_id: str, answer_text: str, *, source: str = "user_message") -> bool:
        """送达答复：命中未决问询返回 True；未命中（已决/不存在）返回 False（迟答复留痕）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    def discard(self, question_id: str, *, reason: str) -> bool:
        """作废未决问询（取消路径）：返回是否命中未决项。"""
        ...  # pragma: no cover — Protocol 方法无实现


class InMemoryAskUserBoard:
    """问询板 v1 内存实现（单事件循环，归属一次会话装配）：并发上限 + 三态收口。

    - ``open_question``：未决数达 ``max_pending`` 即结构化拒绝（4103 TOOL_BUSY，
      默认 1——防连环问询卡死，route #6 裁决形态）；
    - ``wait_answer``：shield 等待内层 future——超时（TimeoutError）与外层取消
      （CancelledError）都不误伤内层，收口后再作废，迟到的 submit_answer 返回
      False（误配防线，审计可查 answer_mismatch）；
    - 答复正文只搬运不改写（B3：信任级归内核标界）。
    """

    def __init__(self, *, max_pending: int = DEFAULT_MAX_PENDING, clock: Clock = time.monotonic) -> None:
        if max_pending < 1:
            raise AskUserToolError(ErrorCode.PARAM_INVALID, f"max_pending 须 ≥ 1，得到 {max_pending}")
        self._max_pending = max_pending
        self._clock = clock
        self._pending: dict[str, _PendingEntry] = {}

    @property
    def pending_ids(self) -> tuple[str, ...]:
        """当前未决问询 id（护栏自检与测试观察口）。"""
        return tuple(self._pending)

    def open_question(self, record: QuestionRecord) -> None:
        if record.question_id in self._pending:
            raise AskUserToolError(ErrorCode.PARAM_INVALID, f"问询重复登记: {record.question_id}")
        if len(self._pending) >= self._max_pending:
            raise AskUserToolError(
                ErrorCode.TOOL_BUSY,
                f"已有 {len(self._pending)} 个未决问询（并发上限 {self._max_pending}），"
                "拒绝连环问询：请先等当前问询收口再发起新问询",
            )
        stamped = record.model_copy(update={"opened_at": self._clock()})
        self._pending[record.question_id] = _PendingEntry(stamped)

    async def wait_answer(self, question_id: str, *, timeout_s: float) -> AnswerResolution:
        entry = self._pending.get(question_id)
        if entry is None:
            raise AskUserToolError(ErrorCode.PARAM_INVALID, f"问询未登记或已收口: {question_id}")
        try:
            resolution = await asyncio.wait_for(asyncio.shield(entry.future), timeout=timeout_s)
            self._pending.pop(question_id, None)  # answered 收口：摘除未决项，并发位即释放
            return resolution
        except TimeoutError:
            self._settle(question_id, AnswerResolution(question_id=question_id, status=AskResolution.TIMEOUT))
            return AnswerResolution(question_id=question_id, status=AskResolution.TIMEOUT)
        except asyncio.CancelledError:
            # 取消传播纪律（§2.4）：先作废未决项（迟答复不误配）再向内核清单重抛
            self._settle(question_id, AnswerResolution(question_id=question_id, status=AskResolution.CANCELLED))
            raise

    def submit_answer(self, question_id: str, answer_text: str, *, source: str = "user_message") -> bool:
        entry = self._pending.get(question_id)
        if entry is None or entry.future.done():
            return False  # 迟到/不存在的答复：不误配、由消息路径留痕（answer_mismatch）
        if not isinstance(answer_text, str) or not answer_text.strip():
            raise AskUserToolError(ErrorCode.PARAM_INVALID, "答复正文为空：请携带 /answer <question_id> <正文>")
        entry.future.set_result(
            AnswerResolution(
                question_id=question_id, status=AskResolution.ANSWERED, answer_text=answer_text, source=source
            )
        )
        return True

    def discard(self, question_id: str, *, reason: str) -> bool:
        entry = self._pending.pop(question_id, None)
        if entry is None or entry.future.done():
            return False
        entry.future.set_result(AnswerResolution(question_id=question_id, status=AskResolution.CANCELLED))
        return True

    def _settle(self, question_id: str, resolution: AnswerResolution) -> None:
        """终态化并摘除未决项：future 已被外层收口语义占用时幂等跳过。"""
        entry = self._pending.pop(question_id, None)
        if entry is not None and not entry.future.done():
            entry.future.set_result(resolution)
