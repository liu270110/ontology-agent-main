"""进程内运行注册表（M4.5-A，docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1.1）。

``run_id → {inbox, control_probe}``：编排器 spawn 内核 Run 时注册、终态注销（显式
remove 防泄漏——Run 对象随注册表条目存活，弱引用不足以救「忘注销」，纪律靠
ChatOrchestrator._execute_turn 的 finally 注销面）；API 侧提交即达（lite 单进程部署
成立；多副本 inbox 分发/estop 广播随 D4 租约族扩展，§5 明确不做）。

control_probe 签名 = ``Callable[[], str | None]``（同步、无副作用）：返回 None=放行，
返回字符串=紧急停止激活原因（EStopStore.probe 闭包，内存镜像读）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from services.agent.business.kernel.inbox import KernelInbox


class RunHandle:
    """注册表条目（可变容器）：inbox 必备、control_probe 可缺（未接 estop 的直跑形态）。"""

    __slots__ = ("inbox", "control_probe")

    def __init__(self, inbox: KernelInbox, control_probe: Callable[[], str | None] | None) -> None:
        self.inbox = inbox
        self.control_probe = control_probe


class RunRegistry:
    """进程内 run_id → RunHandle 注册表（非线程安全，归属单事件循环）。"""

    def __init__(self) -> None:
        self._runs: dict[UUID, RunHandle] = {}

    def register(
        self, run_id: UUID, *, inbox: KernelInbox, control_probe: Callable[[], str | None] | None = None
    ) -> RunHandle:
        """spawn 注册（同 run_id 重复注册视为组合根违例，拒绝——防条目互踩泄漏）。"""
        if run_id in self._runs:
            raise RuntimeError(f"run 注册表重复注册: {run_id}（终态未注销，组合根纪律违例）")
        handle = RunHandle(inbox, control_probe)
        self._runs[run_id] = handle
        return handle

    def get(self, run_id: UUID) -> RunHandle | None:
        """查询（None=Run 不在本进程：终态已注销/他副本执行 → API 侧 4105 RUN_NOT_LOCAL）。"""
        return self._runs.get(run_id)

    def unregister(self, run_id: UUID) -> RunHandle | None:
        """终态注销（显式 remove 防泄漏）；未注册的注销幂等返回 None。"""
        return self._runs.pop(run_id, None)

    def __len__(self) -> int:
        return len(self._runs)

    def snapshot(self) -> dict[UUID, Any]:
        """调试/观测快照（浅拷贝；keys=在册 run_id）。"""
        return dict(self._runs)
