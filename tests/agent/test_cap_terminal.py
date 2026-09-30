# tests/agent/test_cap_terminal.py
"""terminal 能力测试（docs/Agent/06 路线 #2；研究依据=研究整理/08 C2 Shell 与代码执行）。

覆盖：正例（echo/真实容器内命令）、超时收口、64KB 截断、无网断言（容器内出网被拒的
既定语义复验，PoC P2）、沙箱不可用 fail-closed（禁宿主直跑红线 AST 站岗）、命令审计行
（sha256 摘要+退出码+时长，无命令原文）、B5 审批拒绝、cwd/timeout 护栏、ToolPort 绑定形状。

两段纪律：假会话端口单测恒跑；真容器用例标 ``integration``（注册 marker）并在 Docker
不可达环境自动 skip。AAA + 中文命名。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.agent.business.capabilities.terminal import (
    OUTPUT_LIMIT,
    TERMINAL_ACTION_IRI,
    TERMINAL_INPUT_SCHEMA,
    TERMINAL_LOGGER_NAME,
    AuditSink,
    SandboxSessionExecutor,
    TerminalExecOutcome,
    TerminalTool,
    TerminalToolError,
    build_terminal_bindings,
    clamped_timeout,
)
from services.agent.business.capabilities.terminal.tool import _TRUNCATION_MARK
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall
from services.platform.errors import ErrorCode
from tests.agent.conftest import make_ctx, make_tool_dispatcher

# ── 桩与构造器 ───────────────────────────────────────────────────────────


class FakeSession:
    """会话端口桩：登记 spec，按配置回产出/异常/延时（不落任何宿主执行）。"""

    def __init__(
        self,
        *,
        outcome: TerminalExecOutcome | None = None,
        exc: Exception | None = None,
        sleep_s: float = 0.0,
    ) -> None:
        self.outcome = outcome or TerminalExecOutcome(exit_code=0, stdout_b=b"", stderr_b=b"")
        self.exc = exc
        self.sleep_s = sleep_s
        self.specs: list = []

    async def exec(self, spec):
        self.specs.append(spec)
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        if self.exc is not None:
            raise self.exc
        return self.outcome


class _ShapelessBackend:
    """形状失约后端桩：exec 返回缺字段对象（模拟异构后端产出失约）。"""

    async def exec(self, handle, cmd):  # noqa: ANN001 —— 测试桩最小签名
        return "not-an-exec-result"


def _call(params: dict, mode: ExecutionMode = ExecutionMode.CODE) -> ToolCall:
    return ToolCall(
        action_iri=TERMINAL_ACTION_IRI,
        execution_mode=mode,
        parameters=params,
        param_hash=canonical_param_hash(params),
    )


def _ticket(params: dict) -> ApprovalTicket:
    return ApprovalTicket(param_hash=canonical_param_hash(params))


def _invoke(session: FakeSession, params: dict, *, timeout_ms: int = 30_000, sink: AuditSink | None = None):
    tool = build_terminal_bindings(session, audit_sink=sink)[0]
    return tool.invoke(_call(params), make_ctx(), approval=_ticket(params), timeout_ms=timeout_ms)


# ── 假会话端口单测（恒跑）──────────────────────────────────────────────────


async def test_正常命令回传退出码输出与审计行():
    """Arrange-Act：echo 命令经会话端口执行；Assert：输出/规格/审计行三面核对。"""
    records: list[dict] = []
    session = FakeSession(outcome=TerminalExecOutcome(exit_code=0, stdout_b=b"oa-terminal-ok\n", stderr_b=b""))
    params = {"command": "echo oa-terminal-ok"}

    result = await _invoke(session, params, sink=records.append)

    assert result.ok is True
    assert result.output["exit_code"] == 0
    assert "oa-terminal-ok" in result.output["stdout"]
    assert result.output["truncated"] is False
    assert result.output["workdir"] == "/workspace"
    # 执行规格：容器内 sh -c（POSIX 语义）+ 缺省 60s 超时
    assert session.specs[0].cmd == ["sh", "-c", "echo oa-terminal-ok"]
    assert session.specs[0].timeout_seconds == 60
    # 审计行：sha256 摘要 + 退出码 + 时长；无命令原文（红线）
    record = records[0]
    assert record["outcome"] == "ok"
    assert record["exit_code"] == 0
    assert record["cmd_sha256"] == hashlib.sha256(b"echo oa-terminal-ok").hexdigest()
    assert isinstance(record["duration_ms"], int) and record["duration_ms"] >= 0
    assert "command" not in record and "oa-terminal-ok" not in json.dumps(record, ensure_ascii=False)


async def test_非零退出码如实回传不占用错误码():
    """命令完成执行（exit 3）=ok；exit_code/stderr 如实回传（错误即反馈），error_code 不虚占。"""
    session = FakeSession(outcome=TerminalExecOutcome(exit_code=3, stdout_b=b"", stderr_b=b"boom"))

    result = await _invoke(session, {"command": "exit 3"})

    assert result.ok is True
    assert result.error_code is None
    assert result.output["exit_code"] == 3
    assert "boom" in result.output["stderr"]


async def test_超时命令结构化失败_5001():
    """命令超时收口：5001 结构化失败（web/chat「超时=5001」先例），审计落 timeout。"""
    records: list[dict] = []
    session = FakeSession(sleep_s=5.0)
    params = {"command": "sleep 30", "timeout": 1}

    started = time.monotonic()
    result = await _invoke(session, params, sink=records.append)

    assert time.monotonic() - started < 4  # 收口立即返回，不等 5s 桩
    assert result.ok is False
    assert result.error_code == int(ErrorCode.LLM_TIMEOUT)
    assert "超时" in (result.error_message or "")
    assert records[0]["outcome"] == "timeout"
    assert records[0]["exit_code"] is None


async def test_输出超64KB截断并带标记():
    """stdout 超 64KB：字节硬顶 + 截断标记 + truncated 标记；后端侧截断位同样如实透传。"""
    big = b"x" * (OUTPUT_LIMIT + 100)
    session = FakeSession(outcome=TerminalExecOutcome(exit_code=0, stdout_b=big, stderr_b=b"y" * 10))

    result = await _invoke(session, {"command": "cat big.txt"})

    assert result.output["truncated"] is True
    assert result.output["stdout"].endswith(_TRUNCATION_MARK)
    assert len(result.output["stdout"].encode()) <= OUTPUT_LIMIT + len(_TRUNCATION_MARK.encode())
    assert "截断" not in result.output["stderr"]  # stderr 未超限不标记
    flagged = FakeSession(outcome=TerminalExecOutcome(exit_code=0, stdout_b=b"ab", stderr_b=b"", truncated=True))
    result2 = await _invoke(flagged, {"command": "echo ab"})
    assert result2.output["truncated"] is True


async def test_沙箱不可用_fail_closed结构化拒绝():
    """后端不可达 → 5003 结构化拒绝（已登记码），消息明示禁宿主直跑；零兜底执行。"""
    records: list[dict] = []
    session = FakeSession(exc=ConnectionError("docker daemon unreachable"))

    result = await _invoke(session, {"command": "echo sensitive-secret"}, sink=records.append)

    assert result.ok is False
    assert result.error_code == int(ErrorCode.MCP_TARGET_UNAVAILABLE)
    assert "fail-closed" in (result.error_message or "") and "禁宿主直跑" in (result.error_message or "")
    # 拒绝同样全量落审计（摘要口径）；失败仅经会话端口发生，无宿主兜底执行（AST 红线站岗）
    assert len(session.specs) == 1  # 唯一执行尝试走会话端口
    assert records[0]["outcome"] == "sandbox_unavailable"
    assert records[0]["cmd_sha256"] == hashlib.sha256(b"echo sensitive-secret").hexdigest()


async def test_端口失约_产出形状不可读转5003():
    """SandboxSessionExecutor 读不到后端产出字段 → SandboxPortError → 5003（fail-closed 同款）。"""
    session = SandboxSessionExecutor(_ShapelessBackend(), handle=object())
    params = {"command": "echo x"}
    tool = build_terminal_bindings(session)[0]

    result = await tool.invoke(_call(params), make_ctx(), approval=_ticket(params))

    assert result.ok is False
    assert result.error_code == int(ErrorCode.MCP_TARGET_UNAVAILABLE)


async def test_审批缺失与参数哈希不符_2001拒绝():
    """terminal 天然 code 级：无回执/回执哈希与调用不符一律 2001，未触达沙箱。"""
    records: list[dict] = []
    session = FakeSession()
    tool = build_terminal_bindings(session, audit_sink=records.append)[0]
    params = {"command": "echo x"}

    missing = await tool.invoke(_call(params), make_ctx(), approval=None)
    stale = await tool.invoke(_call(params), make_ctx(), approval=ApprovalTicket(param_hash="0" * 64))

    for result in (missing, stale):
        assert result.ok is False
        assert result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert session.specs == []  # 实现不得自查自放：无有效回执零执行


async def test_参数越界结构化拒绝_3001_且不触达沙箱():
    """cwd 绝对路径/穿越/盘符、空命令/非字符串/NUL → 3001 + 零执行。"""
    session = FakeSession()
    bad_params = [
        {"command": "echo x", "cwd": "../etc"},
        {"command": "echo x", "cwd": "/etc"},
        {"command": "echo x", "cwd": r"C:\windows"},
        {"command": ""},
        {"command": "   "},
        {"command": None},
        {"command": 123},
        {"command": "x\x00y"},
    ]

    for params in bad_params:
        result = await _invoke(session, params)

        assert result.ok is False, f"应拒绝: {params}"
        assert result.error_code == int(ErrorCode.PARAM_INVALID), f"错误码应为 3001: {params}"
    assert session.specs == []


async def test_超时上限收口_钳位不拒绝():
    """timeout 收口：缺省 60s、硬顶 600s、下限 1s；非整数钳回缺省（上限收口语义）。"""
    assert clamped_timeout(None) == 60
    assert clamped_timeout("abc") == 60
    assert clamped_timeout(0) == 1
    assert clamped_timeout(-5) == 1
    assert clamped_timeout(99999) == 600
    session = FakeSession()

    await _invoke(session, {"command": "echo x", "timeout": 99999})

    assert session.specs[0].timeout_seconds == 600


async def test_缺省日志汇_结构化审计行无命令原文(caplog):
    """缺省 sink=标准日志：行内带 sha256 摘要与退出码；命令原文/输出永不落（红线）。"""
    secret = "SECRET-CONTENT-xyz"
    session = FakeSession(outcome=TerminalExecOutcome(exit_code=0, stdout_b=b"ok", stderr_b=b""))

    with caplog.at_level(logging.INFO, logger=TERMINAL_LOGGER_NAME):
        await _invoke(session, {"command": f"echo {secret}"})

    digest = hashlib.sha256(f"echo {secret}".encode()).hexdigest()
    assert digest in caplog.text
    assert secret not in caplog.text  # 命令原文不落审计


def test_绑定工厂_空会话fail_closed与ToolPort形状():
    """build 期 fail-closed：无会话端口拒绝出厂；出厂工具可经 dispatcher 以行动类绑定（ToolPort 形状）。"""
    with pytest.raises(TerminalToolError):
        build_terminal_bindings(None)  # type: ignore[argtype]
    with pytest.raises(TerminalToolError):
        build_terminal_bindings(object())  # type: ignore[argtype]

    bindings = build_terminal_bindings(FakeSession())
    assert len(bindings) == 1
    tool = bindings[0]
    assert tool.meta.semantic_annotation["action_iri"] == TERMINAL_ACTION_IRI
    assert TERMINAL_INPUT_SCHEMA["required"] == ["command"]
    assert set(TERMINAL_INPUT_SCHEMA["properties"]) == {"command", "cwd", "timeout"}
    dispatcher: ExtensionDispatcher = make_tool_dispatcher(tool)
    assert dispatcher.tool_for(TERMINAL_ACTION_IRI) is tool


def test_terminal包零宿主执行面_AST红线():
    """红线站岗：terminal 包零 subprocess/pty/进程创建 import，零 services.sandbox 直 import
    （import-linter「sandbox.runtime 模块私有」契约镜像）；执行只能经沙箱会话端口。"""
    package = Path(__file__).resolve().parents[2] / "services/agent/business/capabilities/terminal"
    violations: list[str] = []
    for source in sorted(package.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in ("subprocess", "pty"):
                        violations.append(f"{source.name}: import {alias.name}")
                    if alias.name == "services.sandbox" or alias.name.startswith("services.sandbox."):
                        violations.append(f"{source.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.split(".")[0] in ("subprocess", "pty"):
                    violations.append(f"{source.name}: from {module} import …")
                if module == "services.sandbox" or module.startswith("services.sandbox."):
                    violations.append(f"{source.name}: from {module} import …")
            elif isinstance(node, ast.Attribute) and node.attr in ("system", "popen"):
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    violations.append(f"{source.name}: os.{node.attr}")
    assert violations == [], "terminal 包宿主执行面红线违例：\n" + "\n".join(violations)


# ── 真容器集成用例（integration marker；Docker 不可达自动 skip）──────────────

_IMAGE = "docker.m.daocloud.io/library/python:3.12-slim"  # 已预拉（同 tests/integration 沙箱批）
_TERM_NET = "oa-tst-term-net"


def _docker_reachable() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:  # noqa: BLE001 —— 探活失败=不可达（skip 纪律）
        return False
    return True


@dataclass
class _RealStack:
    """真容器栈句柄：工具 + 审计记录 + 后端/容器（销毁后 fail-closed 用例需直触 handle）。"""

    tool: TerminalTool
    records: list
    backend: object
    handle: object


@pytest.fixture()
async def real_tool(tmp_path: Path):
    """真 DockerBackend + 容器 → SandboxSessionExecutor → 绑定工具；测后销毁+标签兜底清理。"""
    if not _docker_reachable():
        pytest.skip("Docker 不可达：真容器 terminal 用例跳过（skip 纪律）")
    from services.sandbox.runtime import DockerBackend, spec_from_mapping

    backend = DockerBackend(network_name=_TERM_NET, snapshot_dir=tmp_path)
    spec = spec_from_mapping(
        {"instance_id": uuid.uuid4().hex[:12], "base_image": _IMAGE, "mem_limit_mb": 256, "cpu_limit": 0.5}
    )
    handle = await backend.create(spec)
    records: list[dict] = []
    tool = TerminalTool(SandboxSessionExecutor(backend, handle), audit_sink=records.append)
    try:
        yield _RealStack(tool=tool, records=records, backend=backend, handle=handle)
    finally:
        await backend.destroy(handle)
        for c in backend._client.containers.list(all=True, filters={"label": "oa.sandbox"}):
            c.remove(force=True, v=True)
        for v in backend._client.volumes.list(filters={"label": "oa.sandbox"}):
            v.remove(force=True)


@pytest.mark.integration
async def test_真实容器echo执行_退出码与输出回传(real_tool):
    """正例：命令在容器内 sh -c 执行，stdout/exit_code 经端口如实回传。"""
    stack = real_tool
    params = {"command": "echo oa-terminal-ok"}

    result = await stack.tool.invoke(_call(params), make_ctx(), approval=_ticket(params))

    assert result.ok is True
    assert result.output["exit_code"] == 0
    assert "oa-terminal-ok" in result.output["stdout"]
    assert stack.records[-1]["outcome"] == "ok"


@pytest.mark.integration
async def test_真实容器内出网被拒_无网语义复验(real_tool):
    """B4 无网既定语义复验（PoC P2）：internal 网 DNS 与 IP 直连全拒，命令非零退出。"""
    stack = real_tool
    denied_cmds = (
        "python -c \"import socket; socket.getaddrinfo('example.com', 80)\"",
        "python -c \"import socket; socket.create_connection(('223.5.5.5', 53), timeout=3)\"",
    )

    for cmd in denied_cmds:
        params = {"command": cmd, "timeout": 30}
        result = await stack.tool.invoke(_call(params), make_ctx(), approval=_ticket(params))

        assert result.ok is True, f"工具应完成执行: {cmd}"
        assert result.output["exit_code"] != 0, f"容器内出网应被拒: {cmd}"


@pytest.mark.integration
async def test_真实容器内超时收口_结构化5001(real_tool):
    """超时收口在真后端同样成立：立即返回结构化 5001，不等待 30s。"""
    stack = real_tool
    params = {"command": "sleep 30", "timeout": 2}

    started = time.monotonic()
    result = await stack.tool.invoke(_call(params), make_ctx(), approval=_ticket(params))

    assert result.ok is False
    assert result.error_code == int(ErrorCode.LLM_TIMEOUT)
    assert time.monotonic() - started < 15
    assert stack.records[-1]["outcome"] == "timeout"


@pytest.mark.integration
async def test_真实容器cwd工作区禁闭(real_tool):
    """cwd 禁闭：工作区内相对目录执行可达；穿越字形在容器触达前结构化拒绝。"""
    stack = real_tool
    write = {"command": "mkdir -p out && echo v1 > out/f.txt"}
    inside = {"command": "pwd && cat f.txt", "cwd": "out"}
    escape = {"command": "echo x", "cwd": "../../etc"}

    r_write = await stack.tool.invoke(_call(write), make_ctx(), approval=_ticket(write))
    r_inside = await stack.tool.invoke(_call(inside), make_ctx(), approval=_ticket(inside))
    r_escape = await stack.tool.invoke(_call(escape), make_ctx(), approval=_ticket(escape))

    assert r_write.ok is True and r_write.output["exit_code"] == 0
    assert r_inside.ok is True
    assert "/workspace/out" in r_inside.output["stdout"] and "v1" in r_inside.output["stdout"]
    assert r_escape.ok is False and r_escape.error_code == int(ErrorCode.PARAM_INVALID)


@pytest.mark.integration
async def test_真实容器输出截断_64KB硬顶(real_tool):
    """容器产出 70KB：工具层 64KB 字节硬顶 + 截断标记如实回传。"""
    stack = real_tool
    params = {"command": "python -c \"print('x' * 70000)\"", "timeout": 60}

    result = await stack.tool.invoke(_call(params), make_ctx(), approval=_ticket(params))

    assert result.ok is True
    assert result.output["truncated"] is True
    assert result.output["stdout"].endswith(_TRUNCATION_MARK)
    assert len(result.output["stdout"].encode()) <= OUTPUT_LIMIT + len(_TRUNCATION_MARK.encode()) + 1


@pytest.mark.integration
async def test_真实容器销毁后执行_fail_closed拒绝(real_tool):
    """容器销毁（沙箱不可达语义）→ 5003 结构化拒绝 + 落审计，绝不落宿主执行（红线）。"""
    stack = real_tool
    await stack.backend.destroy(stack.handle)  # 模拟沙箱实例消失
    params = {"command": "echo after-destroy"}

    result = await stack.tool.invoke(_call(params), make_ctx(), approval=_ticket(params))

    assert result.ok is False
    assert result.error_code == int(ErrorCode.MCP_TARGET_UNAVAILABLE)
    assert "禁宿主直跑" in (result.error_message or "")
    assert stack.records[-1]["outcome"] == "sandbox_unavailable"
