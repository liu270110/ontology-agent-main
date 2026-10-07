"""L4 领域模型：Agent 聚合（Agent 服务设计 §2 三聚合根之一；DDL 权威=database/01 §3.2）。

不变式（Agent 服务设计 §2，落到本仓 ORM 形态）：
- 一条 Agent 只绑定一种适配器类型：``agent_tool`` 单值列（结构上成立），聚合注册时再断言合法枚举；
- 适配器绑定必须指向已注册适配器行（``adapter_id`` FK；聚合只持有 id，存在性由仓储/端点保证）；
- **disabled/degraded 不得被新会话引用**（:meth:`Agent.ensure_usable_for_new_session`，创建会话端点调用；
  degraded=探活连续失败降级，04 §10 裁决，存量 Run 跑完不中断）；
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
# 20 篇 §2/§3.3（2026-10-07）：Bridge 通用形态扩枚举——acp（F4 标准协议，G1 批）；
# http-generic/cli-generic/a2a 随 G2/下波追加（注册面 profile 缺失=422，见 agents.py）。
ALLOWED_AGENT_TOOLS: tuple[str, ...] = ("builtin", "claude", "acp")

# G1（20 篇 §2）：acp_profile=ACP 画像名（adapters/profiles/acp/<名>.yaml；注册面校验 profile
# 在位否则 422，值域收窄同其余键——api/01「未知字段一律拒绝」同构）。
_CONFIG_KEYS = frozenset({"model", "temperature", "tool_whitelist", "num_ctx", "acp_profile"})


class AgentError(Exception):
    """领域错误（映射 api/01 已登记码：形状/枚举类→3001；禁用引用→HTTP 409）。"""


class AgentStatus(StrEnum):
    """三态（04 篇 §10 degraded 裁决，2026-09-26 补；H-0c ③ 代码化 2026-09-29）：

    enabled⇄degraded 双向（探活失败 N 次→degraded，成功自愈回 enabled）；
    disabled 终态语义不变（仅 enable() 可离终态）；degraded=新会话拒绑、存量跑完。
    """

    ENABLED = "enabled"
    DEGRADED = "degraded"
    DISABLED = "disabled"


# 状态机：enabled⇄degraded 双向；disabled 仅 enable() 出口（终态语义不变）
_VALID_AGENT_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.ENABLED: frozenset({AgentStatus.DISABLED, AgentStatus.DEGRADED}),
    AgentStatus.DEGRADED: frozenset({AgentStatus.ENABLED, AgentStatus.DISABLED}),
    AgentStatus.DISABLED: frozenset({AgentStatus.ENABLED}),
}


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
    acp_profile = config.get("acp_profile")
    if acp_profile is not None and (
        not isinstance(acp_profile, str) or not acp_profile.strip() or len(acp_profile) > 128
    ):
        raise AgentError("3001 PARAM_INVALID: config.acp_profile 须为 1~128 字符的画像名")


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
    # 适配器探活连续失败计数（H-0c ③；DDL=agents.adapter_failure_count，2026-09-29 迁移）：
    # 失败 +1、成功清零；≥阈值（Settings.agent_degrade_threshold）→ degrade()
    adapter_failure_count: int = 0
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
        self._transition(AgentStatus.DISABLED)

    def enable(self) -> None:
        self._transition(AgentStatus.ENABLED)

    def degrade(self) -> None:
        """适配器降级（enabled→degraded，04 §10 裁决）：新会话拒绑、存量 Run 跑完不中断。"""
        self._transition(AgentStatus.DEGRADED)

    def record_adapter_health(self, *, healthy: bool, degrade_threshold: int = 3) -> None:
        """health-check 结果记账（H-0c ③，探活端点驱动）：

        - 失败：``adapter_failure_count`` +1；连续失败 ≥ 阈值且当前 enabled → degrade()；
        - 成功：计数清零；degraded 自愈回 enabled（04 篇 §3「degraded→recovered，
          health-check 端点驱动」）；disabled 不受探活结果影响（终态语义不变）。
        """
        if healthy:
            self.adapter_failure_count = 0
            if self.status is AgentStatus.DEGRADED:
                self.enable()  # 自愈（degraded→enabled）
            return
        self.adapter_failure_count += 1
        if self.adapter_failure_count >= degrade_threshold and self.status is AgentStatus.ENABLED:
            self.degrade()

    def ensure_usable_for_new_session(self) -> None:
        """不变式：disabled/degraded 不得被新会话引用（Agent 服务设计 §2 + 04 §10 degraded
        裁决；创建会话端点强制，AgentError→409）。存量 Run 不经此校验（跑完不中断）。
        """
        if self.status is AgentStatus.DISABLED:
            raise AgentError("AGENT_DISABLED: agent 已禁用，禁止创建新会话")
        if self.status is AgentStatus.DEGRADED:
            raise AgentError("AGENT_DEGRADED: 适配器探活连续失败已降级，禁止创建新会话（存量 Run 不受影响）")

    def _transition(self, to: AgentStatus) -> None:
        if to not in _VALID_AGENT_TRANSITIONS[self.status]:
            raise AgentError(f"非法状态迁移 {self.status} → {to}（agent 状态机：enabled⇄degraded，disabled 终态）")
        self.status = to
