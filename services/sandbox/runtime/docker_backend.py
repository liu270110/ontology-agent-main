"""docker 后端（v1 唯一实现；对齐 DSec Container 档）。

隔离参数组合 = 2026-09-27 本地 PoC 验证结论（docs/Sandbox/_poc）：
--read-only 根（符号链接逃逸天然免疫）+ workspace 卷 + --tmpfs size= 显式限制（tmpfs 页
不触发 cgroup OOM 的实测对策）+ --memory/--cpus/--pids-limit + cap-drop ALL +
no-new-privileges + internal 网（HTTP/DNS/IP 三路全拒已实测）。
快照 = docker get_archive 原始 tar 流（pack_diff v1 形态），唤醒 = 快照还原 + 容器重建
（销毁重建为唯一恢复语义，Sandbox §5.5）。
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
from collections.abc import Callable
from pathlib import Path

from docker.errors import APIError, NotFound
from docker.types import LogConfig

import docker
from services.platform.config import get_settings
from services.sandbox.runtime.types import Scenario, TrustLevel

from .backend import (
    ExecCommand,
    ExecLimitError,
    ExecResult,
    ProvisionSpec,
    SandboxHandle,
    SnapshotRef,
)

_OUTPUT_LIMIT = 64 * 1024  # 单命令输出截断（Sandbox §7.1）


def _remove_with_retry(fn: Callable[[], None], *, attempts: int = 4, delay: float = 1.5) -> None:
    """Docker 资源删除重试兜底：409（in use / already in progress）退避重试，
    末次仍失败则放弃（资源交由 Docker 生命周期回收）。"""
    for i in range(attempts):
        try:
            fn()
            return
        except NotFound:  # 已不存在 = 删除成功
            return
        except APIError:  # 409 竞态等：退避后重试
            if i == attempts - 1:
                return
            time.sleep(delay * (i + 1))


class DockerBackend:
    """单机 docker 后端（daemon 直连；T3 与 S4/S5 不支持——fail-closed 由 supports 裁定）。"""

    def __init__(
        self,
        *,
        client: docker.DockerClient | None = None,
        network_name: str = "oa-sandbox",
        snapshot_dir: Path | None = None,
        output_limit: int = _OUTPUT_LIMIT,
    ) -> None:
        self._client = client or docker.from_env()
        self._network_name = network_name
        self._snapshot_dir = Path(snapshot_dir) if snapshot_dir else Path(get_settings().sandbox_snapshot_dir)
        self._output_limit = output_limit

    # -- 协议实现 ----------------------------------------------------------

    def supports(self, trust_level: TrustLevel, scenario: Scenario) -> bool:
        # v1 仅 docker 后端：T0/T1/T2 常规可用；T3 与对抗性负载（S4/S5）不可达（Sandbox §3.2）。
        if trust_level is TrustLevel.T3_ADVERSARIAL or scenario in (Scenario.S4_EVAL, Scenario.S5_TRAINING):
            return False
        return True

    async def create(self, spec: ProvisionSpec) -> SandboxHandle:
        await asyncio.to_thread(self._ensure_network)
        name, volume = f"oa-sbx-{spec.instance_id}", f"oa-sbx-ws-{spec.instance_id}"
        await asyncio.to_thread(self._remove_stale_container, name)
        await asyncio.to_thread(self._remove_stale_volume, volume)
        await asyncio.to_thread(self._client.volumes.create, volume, labels={"oa.sandbox": spec.instance_id})
        await asyncio.to_thread(
            self._client.containers.run,
            spec.base_image,
            command=["sleep", "infinity"],  # 常驻待命；exec 是工作入口（就绪探针语义）
            name=name,
            detach=True,
            read_only=True,  # 根只读（PoC P5）
            network=self._network_name,  # internal：默认全拒（PoC P2）
            mem_limit=f"{spec.mem_limit_mb}m",
            nano_cpus=int(spec.cpu_limit * 1e9),
            pids_limit=spec.pids_limit,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            tmpfs={"/tmp": f"rw,size={spec.tmpfs_size_mb}m"},  # 显式 size（PoC 发现：tmpfs 不计 cgroup OOM）
            volumes={volume: {"bind": "/workspace", "mode": "rw"}},
            environment=dict(spec.env),
            labels={"oa.sandbox": spec.instance_id, "oa.trust": spec.trust_level.value},
            log_config=LogConfig(type=LogConfig.types.JSON, config={"max-size": "10m", "max-file": "1"}),
        )
        return SandboxHandle(instance_id=spec.instance_id, container_name=name, workspace_volume=volume)

    async def exec(self, handle: SandboxHandle, cmd: ExecCommand) -> ExecResult:
        container = await asyncio.to_thread(self._client.containers.get, handle.container_name)
        try:
            result = await asyncio.to_thread(container.exec_run, cmd=cmd.cmd, workdir=cmd.workdir, demux=True)
        except Exception as exc:  # docker-py 超时包装不统一，统一归执行失败语义
            raise ExecLimitError(f"exec 失败/超时：{exc}") from exc
        out, err = result.output
        out_b, err_b = out or b"", err or b""
        truncated = len(out_b) > self._output_limit or len(err_b) > self._output_limit
        return ExecResult(
            exit_code=result.exit_code,
            stdout_b=out_b[: self._output_limit],
            stderr_b=err_b[: self._output_limit],
            truncated=truncated,
        )

    async def snapshot(self, handle: SandboxHandle, source: str = "hibernate") -> SnapshotRef:
        """workspace 卷打包（pack_diff v1 = docker 原始 tar；可写容器层不入快照——状态权威在平台侧）。"""
        container = await asyncio.to_thread(self._client.containers.get, handle.container_name)

        def _pack() -> bytes:
            stream, _stat = container.get_archive("/workspace")
            buf = io.BytesIO()
            for chunk in stream:
                buf.write(chunk)
            return buf.getvalue()

        payload = await asyncio.to_thread(_pack)
        object_key = f"snapshots/{handle.instance_id}/{source}.tar"
        target = self._snapshot_dir / object_key
        await asyncio.to_thread(self._mkdir_write, target, payload)
        return SnapshotRef(instance_id=handle.instance_id, object_key=str(target), size_bytes=len(payload))

    async def restore(self, snapshot: SnapshotRef, spec: ProvisionSpec) -> SandboxHandle:
        """唤醒 = 快照还原 + 重建容器（唯一恢复语义，Sandbox §5.5；不重放命令）。

        只读根不接受 put_archive("/")——快照 tar（首层 workspace/）重组为相对路径成员后
        直接写入卷挂载点 /workspace；成员路径规范化拒绝 ".." 逃逸（Sandbox §7.1）。
        """
        handle = await self.create(spec)

        def _unpack() -> None:
            container = self._client.containers.get(handle.container_name)
            out = io.BytesIO()
            with (
                tarfile.open(fileobj=io.BytesIO(Path(snapshot.object_key).read_bytes())) as src,
                tarfile.open(fileobj=out, mode="w") as dst,
            ):
                for member in src.getmembers():
                    if member.isdir():
                        continue
                    rel = member.name.removeprefix("workspace/").lstrip("./")
                    if not rel or ".." in Path(rel).parts:
                        continue
                    f = src.extractfile(member)
                    if f is None:
                        continue
                    info = tarfile.TarInfo(name=rel)
                    info.size = member.size
                    info.mode = member.mode
                    dst.addfile(info, f)
            out.seek(0)
            container.put_archive("/workspace", out)

        await asyncio.to_thread(_unpack)
        return handle

    async def destroy(self, handle: SandboxHandle, *, remove_workspace: bool = True) -> None:
        await asyncio.to_thread(self._remove_stale_container, handle.container_name)
        if remove_workspace:
            await asyncio.to_thread(self._remove_stale_volume, handle.workspace_volume)

    # -- 内部 ----------------------------------------------------------

    @staticmethod
    def _mkdir_write(target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    def _ensure_network(self) -> None:
        try:
            self._client.networks.get(self._network_name)
        except NotFound:
            self._client.networks.create(self._network_name, internal=True, labels={"oa.sandbox": "network"})

    def _remove_stale_container(self, name: str) -> None:
        """池语义：同 id 重供给 = 销毁重建（v=True 连卷），不做原地复用——防残留泄漏（Sandbox §5.2）。"""
        _remove_with_retry(lambda: self._client.containers.get(name).remove(force=True, v=True))

    def _remove_stale_volume(self, volume: str) -> None:
        _remove_with_retry(lambda: self._client.volumes.get(volume).remove(force=True))
