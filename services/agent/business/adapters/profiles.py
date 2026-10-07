"""Agent Profile 数据文件机制（docs/Agent/05 §5.2 字段权威 + 20 篇 §4）。

**自定义接入新 agent = 写一份 YAML，不写一行 Python**（05 篇 §5.1 设计原则）：具体工具
只是一份 profile 配置，自研代码只存在于形态适配器（http_service/cli_jsonl/acp）里。

- profile 目录：``services/agent/business/adapters/profiles/{service,cli}/*.yaml``（内置）；
  部署期可经 Settings.agent_adapter_profiles_root 追加覆盖（一切可变参数走 Settings）；
- 字段（05 篇 §5.2 v0.1）：profile/profile_version/tool/form/pinned/transport/event_map/
  permission_map/session_map/sandbox/capabilities——本批消费 tool/form/protocol/transport/
  event_map/capabilities/text_tail，permission_map/session_map 先按数据透传（审批桥接线
  随 B5 审批面批次）；未登记键拒绝（api/01 §3「未知字段一律拒绝」同构，防配置漂移）；
- 字段路径表达式（20 篇 §3.2）：``$.type``/``$.data.delta``——``$``=行 JSON 根、``.``=
  逐层键路径；缺字段→:data:`MISSING` 哨兵（缺字段容错，逐行跳过不断流）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_PROFILES_ROOT = Path(__file__).resolve().parent / "profiles"

# 合法枚举（05 篇 §3 形态学；本批只实装 F2/F3，F4 归 G1、F5 列下波）
_ALLOWED_FORMS = ("F2", "F3")
_ALLOWED_TOOLS = ("http-generic", "cli-generic")
# F3 协议模式（20 篇 §3.1）：openai-chat=企业 agent 通用模板；custom-rest=自画像 REST/SSE/WS
_ALLOWED_F3_PROTOCOLS = ("openai-chat", "custom-rest")
# F2 协议模式（20 篇 §3.2）：jsonl=行式 JSON 事件；text-tail=纯文本尾部提取（功能受限档）
_ALLOWED_F2_PROTOCOLS = ("jsonl", "text-tail")


class ProfileError(ValueError):
    """profile 数据不合法（YAML/形状/枚举；加载面吞掉记日志，取用面 fail-closed）。"""


class AuthSpec(BaseModel):
    """transport.auth（20 篇 §3 落地项 ③）：bearer=Authorization 头；apikey=自定义头名。"""

    model_config = ConfigDict(extra="forbid")

    type: str = "none"  # none | bearer | apikey
    api_key_env: str = ""  # 凭据环境变量名（值不进 profile 文件——脱敏门禁）
    header: str = "X-Api-Key"  # apikey 模式请求头名


class EndpointSpec(BaseModel):
    """transport.endpoints 单端点声明（custom-rest 四端点；events 另带 type=sse|ws）。"""

    model_config = ConfigDict(extra="forbid")

    method: str = "POST"
    path: str = ""
    type: str = "sse"  # events 专用：sse | ws


class TransportSpec(BaseModel):
    """transport：F3=base_url+endpoints；F2=cmd；公共=auth/health。"""

    model_config = ConfigDict(extra="forbid")

    # ── F3 ──
    base_url: str = ""  # 部署期可被 Settings.agent_http_generic_base_url 覆盖
    model: str = ""  # openai-chat 模式模型名（空=组合根 Settings.agent_http_generic_model 注入）
    auth: AuthSpec = Field(default_factory=AuthSpec)
    health_path: str = ""  # 探活 GET 路径（空=不可探活；openai-compatible 缺省 /models）
    session_body_field: str = ""  # openai-chat：对端会话 id 注入请求体字段（nanobot 口径）
    endpoints: dict[str, EndpointSpec] = Field(default_factory=dict)
    # ── F2 ──
    cmd: list[str] = Field(default_factory=list)  # 命令模板（{prompt} 占位符）
    prompt_mode: str = "arg"  # arg={prompt} 占位 | stdin=全文写标准输入
    prompt_template: str = ""  # L1 重放模板（{history}/{message} 字面替换；空=内置缺省）
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)


class EventRule(BaseModel):
    """单条事件归一规则：kind 匹配 → GenerationEvent kind + 增量/用量字段路径。"""

    model_config = ConfigDict(extra="forbid")

    match: str  # kind_path 解析值精确匹配
    emit: str  # text_delta | reasoning_delta | finish
    delta_path: str = ""  # 增量文本路径（text/reasoning）
    usage_path: str = ""  # finish 用量对象路径（dict → finish.usage）


class EventMapSpec(BaseModel):
    """event_map（05 篇 §4.2 表的机器可读版，F2 JSONL 消费）：kind 判别路径 + 规则表。"""

    model_config = ConfigDict(extra="forbid")

    kind_path: str = "$.type"
    rules: list[EventRule] = Field(default_factory=list)


class AgentProfile(BaseModel):
    """Agent Profile（05 篇 §5.2 v0.1 的本批消费子集；frozen=装载后不可变；未知键拒绝）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str  # profile id（唯一）
    profile_version: int = 1
    tool: str  # agent_tool 枚举键（http-generic | cli-generic）
    form: str  # F2 | F3
    protocol: str  # F3: openai-chat|custom-rest；F2: jsonl|text-tail
    pinned: dict[str, Any] = Field(default_factory=dict)  # 版本锚点（05 篇 §6 风险 1）
    transport: TransportSpec = Field(default_factory=TransportSpec)
    event_map: EventMapSpec = Field(default_factory=EventMapSpec)
    permission_map: dict[str, Any] = Field(default_factory=dict)  # 审批桥接线随 B5 批（数据先行）
    session_map: dict[str, Any] = Field(default_factory=dict)  # tool_session_field 等会话语义
    text_tail: dict[str, Any] = Field(default_factory=dict)  # F2 text-tail：{max_chars}
    capabilities: dict[str, bool] = Field(default_factory=dict)  # feed/approval/resume/artifacts
    sandbox: dict[str, Any] = Field(default_factory=dict)  # 07 §7.5 声明面（上架审核字段，本批不消费）

    @property
    def id(self) -> str:
        return self.profile

    @property
    def tail_max_chars(self) -> int:
        raw = self.text_tail.get("max_chars")
        return raw if isinstance(raw, int) and raw > 0 else 4000


def _validate_profile(data: dict[str, Any], source: Path) -> AgentProfile:
    """形状+枚举校验（推理分级宪法：平台配置同样过确定性校验；未知键拒绝）。"""
    if not isinstance(data, dict):
        raise ProfileError(f"{source}: profile 文件顶层须为映射")
    try:
        profile = AgentProfile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(f"{source}: {exc.error_count()} 处字段不合法（{exc.errors()[0]['msg']}）") from exc
    if profile.form not in _ALLOWED_FORMS:
        raise ProfileError(f"{source}: form={profile.form} 非法（允许 {_ALLOWED_FORMS}）")
    if profile.tool not in _ALLOWED_TOOLS:
        raise ProfileError(f"{source}: tool={profile.tool} 非法（允许 {_ALLOWED_TOOLS}）")
    allowed_protocols = _ALLOWED_F3_PROTOCOLS if profile.form == "F3" else _ALLOWED_F2_PROTOCOLS
    if profile.protocol not in allowed_protocols:
        raise ProfileError(
            f"{source}: protocol={profile.protocol} 对 form={profile.form} 非法（允许 {allowed_protocols}）"
        )
    return profile


def load_profile_file(path: Path) -> AgentProfile:
    """装载单个 profile YAML（解析/形状任一失败 → ProfileError，由扫描面吞并记日志）。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"{path}: YAML 解析失败（{exc}）") from exc
    return _validate_profile(data if isinstance(data, dict) else {}, path)


def scan_profiles(root: Path | str | None = None) -> dict[str, AgentProfile]:
    """递归扫描 profile 目录（*.yaml；同 id 后者覆盖并记日志——确定性字典序装载）。"""
    base = Path(root) if root else DEFAULT_PROFILES_ROOT
    profiles: dict[str, AgentProfile] = {}
    if not base.is_dir():
        logger.warning("adapter profile 目录不存在（以空注册表继续）: %s", base)
        return profiles
    for path in sorted(base.rglob("*.yaml")):
        try:
            profile = load_profile_file(path)
        except ProfileError as exc:
            logger.warning("adapter profile 装载失败（跳过）: %s", exc)
            continue
        if profile.profile in profiles:
            logger.warning("adapter profile id 重复（后者覆盖）: %s (%s)", profile.profile, path)
        profiles[profile.profile] = profile
    return profiles


_MISSING = object()


def resolve_json_path(doc: Any, path: str) -> Any:
    """字段路径表达式求值（``$.a.b``）：缺字段/中途非映射返回 :data:`MISSING`（容错不抛）。"""
    if not path.startswith("$."):
        return _MISSING
    current = doc
    for part in path[2:].split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def is_missing(value: Any) -> bool:
    return value is _MISSING
