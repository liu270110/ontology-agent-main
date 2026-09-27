"""L4 领域模型：Agent 聚合（Agent 服务设计 §2 三聚合根之一；DDL 权威=database/01 §3.2）。

不变式（Agent 服务设计 §2，落到本仓 ORM 形态）：
- 一条 Agent 只绑定一种适配器类型：``agent_tool`` 单值列（结构上成立），聚合注册时再断言合法枚举；
- 适配器绑定必须指向已注册适配器行（``adapter_id`` FK；聚合只持有 id，存在性由仓储/端点保证）；
- **disabled 不得被新会话引用**（:meth:`Agent.ensure_usable_for_new_session`，创建会话端点调用）；
- 同一租户内 ``name`` 唯一（DB 唯一约束 uk_agents_tenant_id_name 兜底，聚合断言非空与长度）；
- 改适配器类型（agent_tool）视为重建：聚合实例不提供该变更通道（PATCH DTO 不携带）。

config 形状校验（推理分级宪法：平台配置同样过确定性校验）：仅收窄四个已登记键——
``model``（模型别名）/ ``temperature``（0~2）/ ``tool_whitelist``（工具白名单 list[str]）/
``num_ctx``（上下文窗口，正整数）；未知键拒绝（api/01 §3「未知字段一律拒绝」同构）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# M3 落地顺序裁决（Agent 服务设计 §3.2：M3 仅 builtin+claude，pi/nanobot 随 M5，openclaw/hermes
# 走标准协议接入不自研适配器）；新增类型=此处扩枚举 + DTO pattern 同步，禁绕过聚合直写。
ALLOWED_AGENT_TOOLS: tuple[str, ...] = ("builtin", "claude")

_CONFIG_KEYS = frozenset({"model", "temperature", "tool_whitelist", "num_ctx"})


class AgentError(Exception):
    """领域错误（映射 api/01 已登记码：形状/枚举类→3001；禁用引用→HTTP 409）。"""


class AgentStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class AgentAdapterInfo(BaseModel):
    """适配器绑定行投影（值对象 frozen）：详情/健康检查的只读面（agent_adapters 行）。"""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    agent_tool: str
    version: str
    health_endpoint: str | None = None


def _validate_config(config: dict[str, Any]) -> None:
    """config 形状校验：未知键拒绝；已知键收窄（值不经采样）。"""
    if not isinstance(config, dict):
        raise AgentError("3001 PARAM_INVALID: config 须为对象")
    unknown = set(config) - _CONFIG_KEYS
    if unknown:
        raise AgentError(f"3001 PARAM_INVALID: config 含未登记键 {sorted(unknown)}（仅收 {_CONFIG_KEYS}）")
    model = config.get("model")
    if model is not None and (not isinstance(model, str) or not model or len(model) > 128):
        raise AgentError("3001 PARAM_INVALID: config.model 须为 1~128 字符的模型别名")
    temperature = config.get("temperature")
    if temperature is not None and (not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2):
        raise AgentError("3001 PARAM_INVALID: config.temperature 须在 [0, 2]")
    whitelist = config.get("tool_whitelist")
    if whitelist is not None:
        if not isinstance(whitelist, list) or not all(isinstance(t, str) and 0 < len(t) <= 128 for t in whitelist):
            raise AgentError("3001 PARAM_INVALID: config.tool_whitelist 须为非空字符串数组（单项 ≤128）")
    num_ctx = config.get("num_ctx")
    if num_ctx is not None and (not isinstance(num_ctx, int) or isinstance(num_ctx, bool) or num_ctx <= 0):
        raise AgentError("3001 PARAM_INVALID: config.num_ctx 须为正整数")


class Agent(BaseModel):
    """Agent 聚合根：一条平台内注册的 agent 工具实例配置（按 id 判等）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    name: str
    agent_tool: str
    adapter_id: uuid.UUID
    system_prompt: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    status: AgentStatus = AgentStatus.ENABLED
    created_at: datetime | None = None  # 仓储回填，聚合内不消费

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Agent) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # ── 工厂（注册用例：api/01 §5.1 POST /agents）──────────────────────────
    @classmethod
    def register(
        cls,
        *,
        tenant_id: uuid.UUID,
        name: str,
        agent_tool: str,
        adapter_id: uuid.UUID,
        system_prompt: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> Agent:
        """注册一条 Agent：合法枚举 + 名称纪律 + config 形状校验（violation→AgentError）。"""
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
            raise AgentError("3001 PARAM_INVALID: name 须为 1~128 字符")
        if agent_tool not in ALLOWED_AGENT_TOOLS:
            raise AgentError(
                f"3001 PARAM_INVALID: agent_tool={agent_tool} 非法（允许 {ALLOWED_AGENT_TOOLS}；"
                "M3 落地顺序裁决，Agent 服务设计 §3.2）"
            )
        _validate_config(config or {})
        return cls(
            tenant_id=tenant_id,
            name=name.strip(),
            agent_tool=agent_tool,
            adapter_id=adapter_id,
            system_prompt=system_prompt,
            config=dict(config or {}),
        )

    # ── 行为（业务不变式只写聚合方法，04 §1）───────────────────────────────
    def update(self, *, system_prompt: str | None = None, config: dict[str, Any] | None = None) -> None:
        """更新提示词/配置（api/01 §5.1 PATCH）：agent_tool 不在变更通道（改类型=重建）。"""
        if config is not None:
            _validate_config(config)
            self.config = dict(config)
        if system_prompt is not None:
            if len(system_prompt) > 65_536:
                raise AgentError("3001 PARAM_INVALID: system_prompt 超长（≤65536）")
            self.system_prompt = system_prompt

    def set_tools(self, tools: list[str]) -> None:
        """绑定/解绑工具白名单（api/01 §5.1 PUT /tools，覆盖式）：去重保序。"""
        if not isinstance(tools, list) or not all(isinstance(t, str) and 0 < len(t) <= 128 for t in tools):
            raise AgentError("3001 PARAM_INVALID: tools 须为非空字符串数组（单项 ≤128）")
        seen: dict[str, None] = {}
        for tool in tools:
            seen.setdefault(tool, None)
        self.config = {**self.config, "tool_whitelist": list(seen)}

    def disable(self) -> None:
        self.status = AgentStatus.DISABLED

    def enable(self) -> None:
        self.status = AgentStatus.ENABLED

    def ensure_usable_for_new_session(self) -> None:
        """不变式：disabled 不得被新会话引用（Agent 服务设计 §2；创建会话端点强制）。"""
        if self.status is AgentStatus.DISABLED:
            raise AgentError("AGENT_DISABLED: agent 已禁用，禁止创建新会话")
