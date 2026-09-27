"""取消完整性：取消清单化传播（02 §2.4，2026-09-26 痛点优化裁决，状态主权归内核 T2/T4）。

固定清单顺序（逐项 5s 超时兜底，卡死即强制并登记残留交回收任务补扫——取消不能被取消卡死）：

1. 子 Run 级联取消（递归向全部子 Run 下发取消，各自走同一清单）；
2. 在途工具调用中止（不可中止的只读调用放行收尾；未闭合 tool_call 以取消错误闭合，
   04 篇 task 不变式：每个 tool_call 必须闭合才进终态）；
3. exclusive 租约 / Pooled 实例强制释放（不等持有方优雅释放）；
4. 工作区标记 cancelled。

纪律：``cancelled`` 为终态但资源释放先行——清单执行完毕才落终态，落态即代表零残留
（验收用例：取消后租约表零残留 + 「工具调用卡住取消、5s 被强制」分支）。
插件不得自判「已取消」，取消动作经本协调器（内核）执行并全程留审计与 trace（C2）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from services.agent.business.kernel.ledger import KernelLedger
from services.agent.domain.model.kernel_actions import ToolResult
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

ITEM_TIMEOUT_S = 5.0  # 取消动作自身超时兜底（02 §2.4 硬性 5s）


class CancellationReport(BaseModel):
    """取消清单执行报告（值对象 frozen）：completed/forced 逐项可追溯（C2）。"""

    model_config = ConfigDict(frozen=True)

    completed: tuple[str, ...] = ()
    forced: tuple[str, ...] = ()  # 5s 超时被强制的清单项（残留已登记账本）

    @property
    def finished(self) -> bool:
        """清单是否全部完成（True ⇒ 可落 cancelled 终态且零残留承诺成立）。"""
        return not self.forced


async def _await_tool_task(task: asyncio.Task[ToolResult]) -> None:
    """等待工具任务收尾（结果不再消费；异常/取消由清单项统一转义）。"""
    await task


class CancellationCoordinator:
    """取消协调器：内核持有，插件只注册释放钩子、不自判取消。"""

    def __init__(self, ledger: KernelLedger) -> None:
        self._ledger = ledger
        self._child_run_hooks: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        self._tool_tasks: dict[UUID, asyncio.Task[ToolResult]] = {}
        self._lease_hooks: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        self._workspace_hook: Callable[[], Awaitable[None]] | None = None

    # ── 登记（内核/执行后端在资源创建时挂入）──────────────────────────────
    def register_child_run(self, name: str, cancel_hook: Callable[[], Awaitable[None]]) -> None:
        self._child_run_hooks.append((name, cancel_hook))

    def unregister_child_run(self, name: str) -> None:
        """正常终态子 Run 摘除钩子（取消清单只兜底仍在途的子 Run）。"""
        self._child_run_hooks = [(n, h) for n, h in self._child_run_hooks if n != name]

    def track_tool_task(self, call_id: UUID, task: asyncio.Task[ToolResult]) -> None:
        self._tool_tasks[call_id] = task

    def untrack_tool_task(self, call_id: UUID) -> None:
        self._tool_tasks.pop(call_id, None)

    def tracked_call_ids(self) -> tuple[UUID, ...]:
        return tuple(self._tool_tasks)

    def register_lease(self, name: str, release_hook: Callable[[], Awaitable[None]]) -> None:
        self._lease_hooks.append((name, release_hook))

    def unregister_lease(self, name: str) -> None:
        """正常路径释放后摘除钩子（取消清单只兜底仍未释放的租约）。"""
        self._lease_hooks = [(n, h) for n, h in self._lease_hooks if n != name]

    def register_workspace(self, mark_hook: Callable[[], Awaitable[None]]) -> None:
        self._workspace_hook = mark_hook

    # ── 清单执行 ─────────────────────────────────────────────────────────
    async def _run_item(
        self,
        item: str,
        action: Callable[[], Awaitable[None]],
        *,
        reason: str,
        abort_is_success: bool = False,
    ) -> bool:
        """执行单个清单项（5s 兜底）。返回 True=完成；False=被强制（残留已登记账本）。"""
        try:
            async with asyncio.timeout(ITEM_TIMEOUT_S):
                await action()
            return True
        except TimeoutError:
            self._ledger.record_residual(f"{item}:{reason}")
            return False
        except asyncio.CancelledError:
            # 工具任务被清单取消并快速收尾=中止成功；钩子被再取消打断=按卡死强制继续
            if abort_is_success:
                return True
            self._ledger.record_residual(f"{item}:{reason}")
            return False
        except Exception as exc:  # 清单必须走完：结构化转义留痕（standards/01 §2.6 允许转义）
            self._ledger.record_residual(f"{item}:{reason}:{type(exc).__name__}")
            logger.warning("取消清单项 %s 异常转义: %s", item, exc)
            return False

    async def execute(self, *, reason: str) -> CancellationReport:
        """按固定清单顺序传播取消；逐项 5s 强制；未竟项登记账本残留（补扫依据）。"""
        completed: list[str] = []
        forced: list[str] = []

        for name, hook in self._child_run_hooks:  # 步骤 1：子 Run 级联取消
            item = f"child_runs/{name}"
            if await self._run_item(item, hook, reason=reason):
                completed.append(item)
            else:
                forced.append(item)

        for call_id, task in list(self._tool_tasks.items()):  # 步骤 2：在途工具调用中止
            item = f"in_flight_tool_calls/{call_id}"
            if task.done():
                self.untrack_tool_task(call_id)
                completed.append(item)
                continue
            task.cancel()
            if await self._run_item(item, lambda t=task: _await_tool_task(t), reason=reason, abort_is_success=True):
                completed.append(item)
            else:
                forced.append(item)
            # 未闭合调用以取消错误闭合（04 篇不变式；不可中止只读调用放行收尾后同样闭合）
            if call_id in self._ledger.open_call_ids():
                self._ledger.close_tool_call(call_id, error_code=int(ErrorCode.SESSION_CLOSED), cancelled=True)

        for name, hook in self._lease_hooks:  # 步骤 3：租约强制释放
            item = f"exclusive_leases/{name}"
            if await self._run_item(item, hook, reason=reason):
                completed.append(item)
            else:
                forced.append(item)

        if self._workspace_hook is not None:  # 步骤 4：工作区标记 cancelled
            if await self._run_item("workspace", self._workspace_hook, reason=reason):
                completed.append("workspace")
            else:
                forced.append("workspace")

        return CancellationReport(completed=tuple(completed), forced=tuple(forced))
