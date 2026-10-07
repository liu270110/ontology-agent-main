# tests/agent/test_bridge_profiles.py
"""Agent Profile 数据文件机制单测（docs/Agent/05 §5.2 + 20 篇 §4）。

断言目标：
- 内置四 profile 装载：openai-compatible/nanobot-gateway（F3）+ pi-jsonl/text-tail（F2）；
- 校验面：未知键拒绝、form/tool/protocol 枚举非法拒绝（推理分级宪法：平台配置过确定性校验）；
- 字段路径表达式：嵌套 $/缺字段 MISSING 哨兵（容错不抛）；
- agent_tool 白名单扩展（20 篇 §3.3）：http-generic/cli-generic 注册贯通 + 未知值拒绝；
- 组合根装配（compose.build_generic_adapters）：Settings 缺省空=零装配；配置 profile id=装配；
  未配 base_url 装配成功（调用期 5002 fail-closed）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.agent.business.adapters.base import ChatTurn
from services.agent.business.adapters.compose import build_generic_adapters
from services.agent.business.adapters.profiles import (
    DEFAULT_PROFILES_ROOT,
    AgentProfile,
    ProfileError,
    is_missing,
    load_profile_file,
    resolve_json_path,
    scan_profiles,
)
from services.agent.domain.model.agent import ALLOWED_AGENT_TOOLS, Agent, AgentError
from services.agent.domain.model.kernel_context import TenantContext
from services.platform.ports.model_port import ModelUnavailableError

BUILTIN_IDS = {"openai-compatible", "nanobot-gateway", "pi-jsonl", "text-tail"}


# ── 内置 profile 目录扫描（20 篇 §4：写一份 YAML 即接入）─────────────────────


def test_内置四_profile_装载_形态与能力声明齐备() -> None:
    """profiles/service 两 F3 + profiles/cli 两 F2 装载；capabilities 四键声明（05 §5.2 字段）。"""
    profiles = scan_profiles(DEFAULT_PROFILES_ROOT)
    assert BUILTIN_IDS <= set(profiles)
    assert profiles["openai-compatible"].form == "F3" and profiles["openai-compatible"].protocol == "openai-chat"
    assert profiles["nanobot-gateway"].tool == "http-generic"
    assert profiles["pi-jsonl"].form == "F2" and profiles["pi-jsonl"].protocol == "jsonl"
    assert profiles["text-tail"].protocol == "text-tail"
    caps = profiles["openai-compatible"].capabilities
    assert set(caps) == {"feed", "approval", "resume", "artifacts"}


def test_profile_装载_未知键拒绝与枚举非法拒绝(tmp_path: Path) -> None:
    """未知键拒绝（api/01 §3 同构）；form/tool/protocol 枚举校验（形状失败→ProfileError）。"""
    bad_key = tmp_path / "bad_key.yaml"
    bad_key.write_text("profile: x\ntool: cli-generic\nform: F2\nprotocol: jsonl\nunknown_key: 1\n", encoding="utf-8")
    with pytest.raises(ProfileError):
        load_profile_file(bad_key)

    cases = [
        ("form", "profile: x\ntool: cli-generic\nform: F9\nprotocol: jsonl\n"),
        ("tool", "profile: x\ntool: mystery\nform: F2\nprotocol: jsonl\n"),
        ("protocol", "profile: x\ntool: cli-generic\nform: F2\nprotocol: grpc\n"),
        ("protocol-form", "profile: x\ntool: http-generic\nform: F3\nprotocol: jsonl\n"),  # F2 协议挂 F3
    ]
    for name, body in cases:
        path = tmp_path / f"bad_{name}.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ProfileError):
            load_profile_file(path)


def test_坏_YAML_跳过不炸_扫描面fail_soft(tmp_path: Path) -> None:
    """坏 YAML/非映射顶层 → 扫描面跳过并继续（fail-soft；取用面按 id 缺失 fail-closed）。"""
    (tmp_path / "broken.yaml").write_text("::: 不是 YAML ::: [", encoding="utf-8")
    (tmp_path / "list.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    assert scan_profiles(tmp_path) == {}


def test_字段路径表达式_嵌套与缺字段哨兵() -> None:
    """$.a.b 求值：命中/缺字段/中途非映射→MISSING 哨兵（is_missing 容错，不抛）。"""
    doc: dict[str, object] = {"a": {"b": "v"}}
    assert resolve_json_path(doc, "$.a.b") == "v"
    assert is_missing(resolve_json_path(doc, "$.a.x"))  # 缺字段
    assert is_missing(resolve_json_path(doc, "$.a.b.c"))  # 中途叶子再深入
    assert is_missing(resolve_json_path(doc, "no-prefix"))  # 非 $. 前缀
    assert is_missing(resolve_json_path("str", "$.a"))  # 根非映射


# ── agent_tool 白名单扩展（20 篇 §3.3 六值口径的 G2 份额）───────────────────


@pytest.mark.parametrize("tool", ["http-generic", "cli-generic"])
def test_agent_tool_白名单_增两通用形态值(tool: str) -> None:
    """http-generic/cli-generic 注册贯通（聚合校验通过）；acp 留 G1、未知值仍拒绝。"""
    agent = Agent.register(
        tenant_id=uuid.uuid4(), name=f"桥-{tool}", agent_tool=tool, adapter_id=uuid.uuid4()
    )
    assert agent.agent_tool == tool
    assert "acp" in ALLOWED_AGENT_TOOLS  # G1 已合入：四值并集终态（G1+G2 并集解，主会话裁决）
    with pytest.raises(AgentError):
        Agent.register(tenant_id=uuid.uuid4(), name="x", agent_tool="mystery", adapter_id=uuid.uuid4())


# ── 组合根装配（compose.build_generic_adapters）─────────────────────────────


class _Settings:
    """Settings 形状桩（compose 只读六个 agent_* 键）。"""

    def __init__(self, **kw: str) -> None:
        self.agent_http_generic_profile = ""
        self.agent_http_generic_base_url = ""
        self.agent_http_generic_api_key = ""
        self.agent_http_generic_model = ""
        self.agent_cli_generic_profile = ""
        self.agent_adapter_profiles_root = ""
        for k, v in kw.items():
            setattr(self, k, v)


def test_组合根_Settings缺省空_零装配() -> None:
    """六键全空 → 空字典（编排器 adapters 无新键，现行行为零变化——本批安全底线）。"""
    assert build_generic_adapters(_Settings(), session_factory=None) == {}


def test_组合根_配置profile_id_装配两通用适配器() -> None:
    """配置 profile id → http-generic/cli-generic 注册（键=command.adapter 路由键）。"""
    settings = _Settings(
        agent_http_generic_profile="openai-compatible",
        agent_http_generic_base_url="http://svc.test/v1",
        agent_http_generic_api_key="sk-x",
        agent_http_generic_model="m1",
        agent_cli_generic_profile="pi-jsonl",
    )
    adapters = build_generic_adapters(settings, session_factory=None)
    assert set(adapters) == {"http-generic", "cli-generic"}
    assert adapters["http-generic"].meta.name == "chat.http_generic"
    assert adapters["cli-generic"].meta.name == "chat.cli_generic"


async def test_组合根_未配base_url_装配成功_调用期5002() -> None:
    """未配 base_url 装配成功（组合根不探活），调用期 5002 fail-closed（宁拒不错载）。"""
    settings = _Settings(agent_http_generic_profile="openai-compatible")
    adapter = build_generic_adapters(settings, session_factory=None)["http-generic"]
    turn = ChatTurn(tenant_id=uuid.uuid4(), session_id=uuid.uuid4(), run_id=uuid.uuid4(), message="m")
    ctx = TenantContext(tenant_id=uuid.uuid4(), scopes=(), trace_id="t")
    with pytest.raises(ModelUnavailableError, match="base_url"):
        _ = [e async for e in adapter.stream_chat(turn, ctx)]


def test_组合根_profile_id不存在_跳过装配() -> None:
    """配置了不存在的 profile id → 跳过 + 告警（fail-soft；对话请求编排器报适配器未注册）。"""
    settings = _Settings(agent_http_generic_profile="no-such-profile")
    assert "http-generic" not in build_generic_adapters(settings, session_factory=None)


def test_AgentProfile_frozen_装载后不可变() -> None:
    """profile=frozen 值对象（装载后禁改——配置漂移防线，05 §5.2 profile_version 递增纪律）。"""
    profile = AgentProfile(profile="t", tool="cli-generic", form="F2", protocol="jsonl")
    with pytest.raises(ValidationError):
        profile.protocol = "text-tail"  # type: ignore[misc]
