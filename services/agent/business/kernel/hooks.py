"""H-0a hooks 机制（评审 2026-09-28 §4 补查批次；07 研究 §7.4/§332 L3 通道 ``on(event)`` 承诺）。

首批 hook 矩阵（闭合枚举 :class:`HookName`，扩面须锚点评审——transform_tool_result
等评审 H-0a 行其余矩阵项为后续批次）：

- ``pre_tool_call``   唯一 block 点（:class:`Allow` | :class:`Block`），工具执行前求值；
- ``post_tool_call``  observer（工具实际执行且结果管线闭合后，只看不改）；
- ``on_kernel_event`` observer（kernel 锚点事件经 Emit 落账时同步广播，fire-and-forget）。

纪律（照抄 hermes，评审 H-0a 行；07 边界契约 §2-3「安全语义在内核、呈现外置」）：

1. **observer-only，唯一 block 点收敛 pre_tool_call**：post/on_kernel_event 恒 observer，
   异常一律吞掉留 WARNING、不影响结果；
2. **hook 无法改写 GateReport/判据/StepState**（状态主权 T2）：hook 签名只收
   ToolCall/TenantContext/ToolResult/KernelEvent 的防御性副本，从不接触门禁报告、
   B2 判据与步状态——防 07 §6.7-2 校验器私有化；
3. **pre hook 异常降级 Allow**：安全权威恒在门禁基线 B1（内核硬编码、只增不替），
   hook 不构成第二安全面——崩溃的 hook 不得借 fail-closed 劫持执行面；
4. **hook 回调禁止再调内核**（防循环依赖/递归风暴）：广播期间再触发的嵌套广播被
   守卫丢弃（事件仍正常落账，只是不再二次广播）；
5. **与扩展点注册的差异**：无 ExtensionMeta/语义标注/版本握手要求——observer 无
   契约版本（不参与 loop 契约兼容矩阵），事件名闭合枚举 fail-fast（未知即 ValueError）；
6. **空注册零开销**：调用点以 ``*_hooks`` 属性判空 fast-path（无 hook 的运行零分配）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.agent.domain.model.kernel_actions import ToolCall, ToolResult
from services.agent.domain.model.kernel_context import KernelEvent, TenantContext

logger = logging.getLogger(__name__)


class HookName(StrEnum):
    """hook 事件名（H-0a 首批闭合枚举）：未知事件名在注册期即 ValueError（fail-fast）。"""

    PRE_TOOL_CALL = "pre_tool_call"  # 唯一 block 点（Allow | Block）
    POST_TOOL_CALL = "post_tool_call"  # observer：工具调用闭合后只看不改
    ON_KERNEL_EVENT = "on_kernel_event"  # observer：内核锚点事件广播


@dataclass(frozen=True)
class Allow:
    """放行决定（无字段哨兵；热路径复用模块级 ``ALLOW``，零分配）。"""


ALLOW = Allow()


@dataclass(frozen=True)
class Block:
    """阻断决定（唯一 block 点 pre_tool_call 生效）：内核据此合成结构化拒绝 ToolResult。"""

    reason: str  # 拒绝原因（审计可读）
    structured_message: str | None = None  # 回喂 LLM 的结构化拒绝文案（缺省回退 reason）


HookDecision = Allow | Block

# hook 签名（类型面按 H-0a 批次定义；运行时对返回 Awaitable 的 async 实现同样兼容）
PreToolCallHook = Callable[[ToolCall, TenantContext], HookDecision]
PostToolCallHook = Callable[[ToolResult, TenantContext], Awaitable[None] | None]
OnKernelEventHook = Callable[[KernelEvent], Awaitable[None] | None]

_HOOK_FN = Callable[..., Any]


def _name_of(fn: _HOOK_FN) -> str:
    """hook 可读名（lambda/partial/可调用对象皆可，仅 WARNING 留痕用）。"""
    return getattr(fn, "__name__", None) or repr(fn)


class HookRegistry:
    """hook 注册表（内核私有，经 :class:`~services.agent.business.kernel.dispatcher.ExtensionDispatcher`
    的 ``register_hook`` 触达）：``register(event, fn)`` 同函数幂等；注册序即求值序。
    """

    def __init__(self) -> None:
        self._hooks: dict[HookName, list[_HOOK_FN]] = {name: [] for name in HookName}
        self._observer_tasks: set[asyncio.Task[None]] = set()  # 强持有 async observer（防 GC 回收在途任务）
        self._in_broadcast = False  # 嵌套广播守卫（纪律④：hook 回调禁再触发内核广播）

    # ── 注册面（同名同函数幂等）──────────────────────────────────────────
    def register(self, event: HookName, fn: _HOOK_FN) -> None:
        """同一函数对象重复注册为 no-op；不同函数按注册序共存（首 Block 生效）。"""
        if not callable(fn):
            raise TypeError(f"hook 必须可调用: {fn!r}")
        hooks = self._hooks[event]
        if fn not in hooks:
            hooks.append(fn)

    # ── 取用面（空注册零开销：调用点以这些属性判空 fast-path）──────────────
    @property
    def pre_tool_call_hooks(self) -> tuple[PreToolCallHook, ...]:
        return tuple(self._hooks[HookName.PRE_TOOL_CALL])

    @property
    def post_tool_call_hooks(self) -> tuple[PostToolCallHook, ...]:
        return tuple(self._hooks[HookName.POST_TOOL_CALL])

    @property
    def on_kernel_event_hooks(self) -> tuple[OnKernelEventHook, ...]:
        return tuple(self._hooks[HookName.ON_KERNEL_EVENT])

    # ── 求值/广播（内核调用点）───────────────────────────────────────────
    async def run_pre_tool_call(self, call: ToolCall, ctx: TenantContext) -> HookDecision:
        """pre_tool_call：按注册序求值，首个 Block 生效（唯一 block 点）。

        入参传防御性副本（纪律②：hook 改写不影响工具真实入参与参数哈希绑定）；
        hook 异常降级 Allow+WARNING——安全权威恒在门禁基线 B1（纪律③）。
        """
        if not self._hooks[HookName.PRE_TOOL_CALL]:
            return ALLOW  # 空注册 fast-path（零分配零调用）
        payload = call.model_copy(deep=True)
        for fn in self._hooks[HookName.PRE_TOOL_CALL]:
            try:
                decision = fn(payload, ctx)
                if inspect.isawaitable(decision):
                    decision = await decision
            except Exception as exc:  # observer 纪律③：异常不逃逸、不劫持执行面
                logger.warning(
                    "pre_tool_call hook %s 异常，降级 Allow（安全权威在门禁基线 B1）: %s", _name_of(fn), exc
                )
                continue
            if isinstance(decision, Block):
                return decision
        return ALLOW

    async def run_post_tool_call(self, result: ToolResult, ctx: TenantContext) -> None:
        """post_tool_call（observer）：只看不改；异常吞掉留 WARNING，不影响结果（纪律①）。"""
        if not self._hooks[HookName.POST_TOOL_CALL]:
            return  # 空注册 fast-path
        payload = result.model_copy(deep=True)
        for fn in self._hooks[HookName.POST_TOOL_CALL]:
            try:
                ret = fn(payload, ctx)
                if inspect.isawaitable(ret):
                    await ret
            except Exception as exc:  # observer 纪律①：异常不逃逸、结果不受影响
                logger.warning("post_tool_call hook %s 异常（observer 纪律，不影响结果）: %s", _name_of(fn), exc)

    def broadcast_kernel_event(self, event: KernelEvent) -> None:
        """on_kernel_event（observer）：Emit 落账时同步广播（fire-and-forget）。

        同步 observer 内联调用；异步 observer（返回 Awaitable）派发后台任务，异常在
        任务内 WARNING 留痕；嵌套广播被守卫丢弃（纪律④：hook 回调禁止再调内核——
        落账在 Emit 侧先行完成，被丢弃的只是二次广播，事件不丢）。
        """
        if self._in_broadcast:
            return  # 嵌套守卫：只挡广播不挡落账（落账先于本调用完成）
        hooks = self._hooks[HookName.ON_KERNEL_EVENT]
        if not hooks:
            return  # 空注册 fast-path
        payload = event.model_copy(deep=True)
        self._in_broadcast = True
        try:
            for fn in hooks:
                try:
                    ret = fn(payload)
                except Exception as exc:  # observer 纪律①：同步 observer 异常留痕不扩散
                    logger.warning("on_kernel_event hook %s 异常（observer 纪律）: %s", _name_of(fn), exc)
                    continue
                if inspect.isawaitable(ret):
                    self._schedule_observer(ret, fn)
        finally:
            self._in_broadcast = False

    def _schedule_observer(self, awaitable: Awaitable[None], fn: _HOOK_FN) -> None:
        """异步 observer 派发（fire-and-forget）：包一层守卫协程，异常 WARNING 不外泄。"""

        async def _guard() -> None:
            try:
                await awaitable
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # observer 纪律①：异步路径异常同样不外泄
                logger.warning("on_kernel_event hook %s 异常（observer 纪律）: %s", _name_of(fn), exc)

        try:
            task = asyncio.ensure_future(_guard())
        except RuntimeError:  # 无运行中事件循环（Emit 理论上仅在协程内触发，防御性兜底）
            logger.warning("无运行中事件循环，on_kernel_event hook %s 未派发（丢弃）", _name_of(fn))
            return
        self._observer_tasks.add(task)
        task.add_done_callback(self._observer_tasks.discard)

    async def drain_observers(self, *, timeout_s: float = 5.0) -> None:
        """等待在途 async observer 收尾（测试与关停辅助；fire-and-forget 无业务等待方）。"""
        if not self._observer_tasks:
            return
        pending = set(self._observer_tasks)
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout_s)
        except TimeoutError:
            logger.warning("on_kernel_event observer 排水超时（%d 项在途，留痕丢弃）", len(pending))
