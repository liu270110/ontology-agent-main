"""沙箱机制测试（integration）：把 2026-09-27 本地 PoC 的断言固化为回归（docs/Sandbox/_poc）。

覆盖 Sandbox §7.1 隔离矩阵的 docker 侧基线 + §5.5 快照重建唯一恢复语义 + fail-closed。
依赖 Docker（Docker Desktop/WSL2 或 Linux）；CI-S3 integration 档。命名前缀 oa-tst-，测后清理。
"""

from __future__ import annotations

import time
import uuid

import pytest
from docker.errors import APIError, NotFound

from services.sandbox.domain.model.sandbox import Scenario, TrustLevel
from services.sandbox.runtime import (
    BackendUnavailableError,
    DockerBackend,
    ExecCommand,
    assert_supported,
    spec_from_mapping,
)

pytestmark = pytest.mark.integration

_IMAGE = "docker.m.daocloud.io/library/python:3.12-slim"  # 已预拉


def _remove_with_retry(fn, *, attempts: int = 4, delay: float = 1.5) -> None:
    """与 services/sandbox/runtime/docker_backend.py 同款重试语义（不 import 其私有工具）：
    409（in use / already in progress）退避重试，末次仍失败则放弃（资源交由 Docker 生命周期回收）。"""
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


@pytest.fixture()
async def backend(tmp_path):
    b = DockerBackend(network_name="oa-tst-sbx-net", snapshot_dir=tmp_path)
    yield b
    # 清理：本测试创建的容器/卷以标签兜底（destroy 已在用例内调用，此处双保险）；
    # 删除套重试兜底：teardown 与用例内 destroy 并发删同一容器/卷时 Docker 报 409 竞态。
    # 网络不删：每用例删/建 internal 网存在竞态（全量回归实测偶发 error），留待 Docker 生命周期自然回收。
    for c in b._client.containers.list(all=True, filters={"label": "oa.sandbox"}):
        _remove_with_retry(lambda c=c: c.remove(force=True, v=True))
    for v in b._client.volumes.list(filters={"label": "oa.sandbox"}):
        _remove_with_retry(lambda v=v: v.remove(force=True))


def _spec(**kw):
    kw.setdefault("instance_id", uuid.uuid4().hex[:12])
    kw.setdefault("base_image", _IMAGE)
    kw.setdefault("mem_limit_mb", 256)
    kw.setdefault("cpu_limit", 0.5)
    return spec_from_mapping(kw)


async def test_isolation_readonly_root_and_workspace(backend):
    """只读根全拒、workspace 可写、/tmp tmpfs 可写（PoC P5a/b/d）。"""
    spec = _spec()
    handle = await backend.create(spec)
    try:
        r = await backend.exec(handle, ExecCommand(cmd=["sh", "-c", "echo x > /etc/oa-test 2>&1"]))
        assert r.exit_code != 0 and "Read-only" in r.stdout + r.stderr
        r = await backend.exec(handle, ExecCommand(cmd=["sh", "-c", "echo ok > /workspace/note; cat /workspace/note"]))
        assert r.exit_code == 0 and "ok" in r.stdout
        r = await backend.exec(handle, ExecCommand(cmd=["sh", "-c", "echo t > /tmp/t && echo tmp-ok"]))
        assert r.exit_code == 0 and "tmp-ok" in r.stdout
    finally:
        await backend.destroy(handle)


async def test_isolation_symlink_escape_blocked(backend):
    """符号链接逃逸：workspace 内链到 / 再写 /etc 被只读根阻断（PoC P5c）。"""
    spec = _spec()
    handle = await backend.create(spec)
    try:
        r = await backend.exec(
            handle,
            ExecCommand(cmd=["sh", "-c", "ln -s / /workspace/esc; echo p > /workspace/esc/etc/pwn 2>&1; echo rc=$?"]),
        )
        assert "Read-only" in r.stdout + r.stderr or "rc=1" in r.stdout or "rc=2" in r.stdout
        r = await backend.exec(handle, ExecCommand(cmd=["ls", "/etc/pwn"]))
        assert r.exit_code != 0
    finally:
        await backend.destroy(handle)


async def test_network_egress_denied_by_default(backend):
    """internal 网默认全拒：HTTP 域名解析失败、IP 直连不可达（PoC P2a/b/c）。"""
    spec = _spec()
    handle = await backend.create(spec)
    try:
        # DNS：域名解析应失败（PoC P2b：internal 网内嵌 DNS 不转发外网，SERVFAIL）
        r = await backend.exec(
            handle, ExecCommand(cmd=["python", "-c", "import socket; socket.getaddrinfo('example.com', 80)"])
        )
        assert r.exit_code != 0
        # IP 直连：无外网路由（PoC P2c）
        r = await backend.exec(
            handle,
            ExecCommand(cmd=["python", "-c", "import socket; socket.create_connection(('223.5.5.5', 53), timeout=3)"]),
        )
        assert r.exit_code != 0
    finally:
        await backend.destroy(handle)


async def test_memory_limit_oom_isolated(backend):
    """256m 容器吃 600MB 匿名内存被 OOM 杀（PoC P4：exit 137 / exec 非零），不伤宿主。"""
    spec = _spec(mem_limit_mb=256)
    handle = await backend.create(spec)
    try:
        r = await backend.exec(
            handle,
            ExecCommand(
                cmd=["python", "-c", "a=bytearray(600_000_000); print('survived-unexpectedly')"],
                timeout_seconds=60,
            ),
        )
        assert r.exit_code != 0
        assert "survived-unexpectedly" not in r.stdout
    finally:
        await backend.destroy(handle)


async def test_snapshot_destroy_restore_consistent(backend):
    """快照→销毁→重建还原：workspace 内容一致（Sandbox §5.5 唯一恢复语义；PoC P3）。"""
    spec = _spec()
    handle = await backend.create(spec)
    r = await backend.exec(
        handle,
        ExecCommand(
            cmd=[
                "sh",
                "-c",
                "mkdir -p /workspace/artifacts; echo report-v1 > /workspace/artifacts/r.md; echo n1 > /workspace/n.txt",
            ]
        ),
    )
    assert r.exit_code == 0
    snap = await backend.snapshot(handle, source="hibernate")
    await backend.destroy(handle)
    # 销毁后容器确已不存在
    with pytest.raises(NotFound):
        backend._client.containers.get(handle.container_name)
    handle2 = await backend.restore(snap, spec)
    try:
        r = await backend.exec(handle2, ExecCommand(cmd=["cat", "/workspace/artifacts/r.md", "/workspace/n.txt"]))
        assert "report-v1" in r.stdout and "n1" in r.stdout
    finally:
        await backend.destroy(handle2)


async def test_fail_closed_for_t3_and_adversarial(backend):
    """fail-closed：T3 与 S4/S5 在 docker 后端拒绝供给，禁止静默降级（Sandbox §3.2）。"""
    for kw in ({"trust_level": "T3"}, {"scenario": "S4"}, {"scenario": "S5"}):
        spec = _spec(**kw)
        assert not backend.supports(spec.trust_level, spec.scenario)
        with pytest.raises(BackendUnavailableError):
            assert_supported(backend, spec)


async def test_reprovision_is_rebuild_not_reuse(backend):
    """同 id 重供给 = 销毁重建：旧 workspace 内容不得残留（防池化泄漏，Sandbox §5.2）。"""
    spec = _spec()
    handle = await backend.create(spec)
    await backend.exec(handle, ExecCommand(cmd=["sh", "-c", "echo leak > /workspace/marker"]))
    await backend.destroy(handle)
    handle2 = await backend.create(spec)
    try:
        r = await backend.exec(handle2, ExecCommand(cmd=["cat", "/workspace/marker"]))
        assert r.exit_code != 0  # 旧卷随销毁移除，marker 不可见
    finally:
        await backend.destroy(handle2)


def test_trust_and_scenario_enums_stable():
    """枚举值与 database/01 §3.10 CHECK 约定一致性（防漂移）。"""
    assert {e.value for e in Scenario} == {"S0", "S1", "S2", "S3", "S4", "S5"}
    assert {e.value for e in TrustLevel} == {"T0", "T1", "T2", "T3"}
