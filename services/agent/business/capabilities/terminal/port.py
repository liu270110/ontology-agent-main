"""terminal 能力的沙箱会话端口（结构化镜像 services/sandbox/runtime 执行面，禁直 import）。

import-linter 契约「kb.retrieval 与 sandbox.runtime 模块私有」把 services.agent 列为
sandbox.runtime 的禁用源（pyproject 本期禁改、不加豁免边）——能力层不得直 import 沙箱运行时。
故本包按鸭子类型镜像 docker_backend 的执行入口字段（``ExecCommand.cmd/timeout_seconds/workdir``、
``ExecResult.exit_code/stdout_b/stderr_b/truncated``），组合根（唯一可同时触达双方的位置）用
:class:`SandboxSessionExecutor` 把 ``(DockerBackend, SandboxHandle)`` 适配成本端口，再注入
:func:`build_terminal_bindings`。B4 出口控制（internal 网+cap-drop+只读根）硬编码在后端：
本端口零配置面、工具零绕过面——拿不到会话端口就没有任何执行路径（fail-closed 的结构性保证）。

Windows 宿主 / 容器执行语义差异（报告口径，落码处就近标注）：

- 命令一律在容器内 ``/bin/sh -c``（POSIX）执行：无盘符/cmd/PowerShell 语义，路径均为容器侧
  POSIX 路径（工作区根=/workspace）；Windows 形态入参（``C:\\..``、反斜杠）在 guards 层按
  越界拒绝，不做宿主侧翻译。
- 宿主 asyncio 无法中断 to_thread 里的 docker-py exec 流：命令级超时由工具层
  ``asyncio.wait_for`` 强制收口；被收口的命令在线程里自然耗尽后由 docker 侧回收，工具侧
  立即返回结构化超时（宿主不等待、更不落宿主执行）。
- 产出经 demux 二进制分流后按 **bytes 截断再 decode(errors="replace")**：容器 locale 与
  Windows 宿主控制台编码差异不影响回传形状。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

DEFAULT_WORKDIR = "/workspace"  # 容器工作区根（docker_backend 卷挂载点，Sandbox §4）


@dataclass(frozen=True)
class TerminalExecSpec:
    """一次命令执行规格（字段与 sandbox ``ExecCommand`` 镜像，组合根可直传真实后端）。"""

    cmd: list[str]
    timeout_seconds: int = 60
    workdir: str = DEFAULT_WORKDIR


@dataclass(frozen=True)
class TerminalExecOutcome:
    """一次命令执行产出（字段与 sandbox ``ExecResult`` 镜像；bytes 原样回传，截断在工具层复核）。"""

    exit_code: int
    stdout_b: bytes
    stderr_b: bytes
    truncated: bool = False  # 后端侧截断标记（docker_backend 64KB 同源）；工具层二次收口


class SandboxPortError(Exception):
    """会话端口失约（装配缺 exec 入口/后端产出形状不可读）——工具层一律转 5003 结构化失败。"""


@runtime_checkable
class TerminalSandboxSession(Protocol):
    """沙箱会话端口：terminal 工具唯一的执行通道（无此端口 = 无执行路径）。"""

    async def exec(self, cmd: TerminalExecSpec) -> TerminalExecOutcome: ...


class SandboxSessionExecutor:
    """(backend, handle) → 会话端口适配器（组合根装配帮手；零沙箱 import，鸭子类型读产出）。

    组合根用法（接线示例，gateway/daemon lifespan）::

        from services.sandbox.runtime import DockerBackend, spec_from_mapping
        backend = DockerBackend(snapshot_dir=settings.sandbox_snapshot_dir)
        handle = await backend.create(spec_from_mapping({"instance_id": run_id.hex, ...}))
        session = SandboxSessionExecutor(backend, handle)
        for tool in build_terminal_bindings(session):
            dispatcher.register_tool(tool)
    """

    def __init__(self, backend: Any, handle: Any) -> None:
        if backend is None or not callable(getattr(backend, "exec", None)):
            raise SandboxPortError("沙箱后端不可用：缺少 exec 执行入口（fail-closed 拒绝装配）")
        self._backend = backend
        self._handle = handle

    async def exec(self, cmd: TerminalExecSpec) -> TerminalExecOutcome:
        result = await self._backend.exec(self._handle, cmd)
        try:
            return TerminalExecOutcome(
                exit_code=int(result.exit_code),
                stdout_b=bytes(result.stdout_b),
                stderr_b=bytes(result.stderr_b),
                truncated=bool(getattr(result, "truncated", False)),
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise SandboxPortError(f"沙箱后端产出形状不可读: {type(result).__name__}") from exc
