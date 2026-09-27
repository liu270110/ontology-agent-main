"""A2A Agent Card 发布（docs/api/04 §2/§3：发现契约 + skills 映射规则）。

- 发布地址 ``/.well-known/agent-card.json``（A2A v1.0 惯例，无需鉴权的发现入口）；
- 字段表按 api/04 §2 卡片字段表：name/description/protocolVersion/url/capabilities/
  skills[]/provider/authentication/defaultInputModes/defaultOutputModes；
- skills[] 来源（api/04 §3 映射规则）：id=行动类本地名 slug、name=label、
  description=definition（含触发事件类说明）、tags=业务域。**与本体行动类同源的自动
  同步随 changeset publish 流水线接入（M5+，报告欠账）**——本批 skills 为装配参数，
  独立入口缺省给一条平台委托技能占位（如实标注占位语义，不冒充本体行动类）；
- 签名卡（v1.0 头号特性）待办沿用 api/04 §7，本批不声明。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = "1.0"

DEFAULT_INPUT_MODES = ("text/plain", "application/json")
DEFAULT_OUTPUT_MODES = ("text/plain", "application/json")


class AgentSkill(BaseModel):
    """技能条目（api/04 §2/§3）：由本体行动类映射，字段名对齐 A2A v1.0。"""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)


class AgentCapabilities(BaseModel):
    """能力开关（api/04 §2）：本批 SSE 流式/push 通知未启用，如实声明 false。"""

    model_config = ConfigDict(frozen=True)

    streaming: bool = False
    push_notifications: bool = Field(default=False, alias="pushNotifications")
    state_transition_history: bool = Field(default=True, alias="stateTransitionHistory")


class AgentProvider(BaseModel):
    """提供方（api/04 §2）：企业/部门。"""

    model_config = ConfigDict(frozen=True)

    organization: str
    url: str | None = None


class AgentAuthentication(BaseModel):
    """认证方案声明（api/04 §5）：本批 api_key 起步；oauth2 随 M5+ 通道替换。"""

    model_config = ConfigDict(frozen=True)

    schemes: list[str] = Field(default_factory=lambda: ["api_key"])


class AgentCard(BaseModel):
    """Agent Card（api/04 §2 字段表；wire 字段=camelCase，model_dump(by_alias=True)）。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    name: str
    description: str
    protocol_version: str = Field(default=PROTOCOL_VERSION, alias="protocolVersion")
    url: str  # 任务端点（/a2a）
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    skills: list[AgentSkill] = Field(default_factory=list)
    provider: AgentProvider
    authentication: AgentAuthentication = Field(default_factory=AgentAuthentication)
    default_input_modes: list[str] = Field(default_factory=lambda: list(DEFAULT_INPUT_MODES), alias="defaultInputModes")
    default_output_modes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_OUTPUT_MODES), alias="defaultOutputModes"
    )

    def to_wire(self) -> dict[str, Any]:
        """发现响应载荷（camelCase wire 形状）。"""
        return self.model_dump(by_alias=True)


def build_agent_card(
    *,
    base_url: str,
    skills: list[AgentSkill] | None = None,
    name: str = "ontology-agent",
    description: str = "以本体为语义基座的智能体平台：知识检索、本体推理、记忆与业务行动闭环",
    provider_organization: str = "ontology-agent",
    authentication_schemes: list[str] | None = None,
) -> AgentCard:
    """Card 工厂（api/04 §2 完整字段示例形态）：url=任务端点 /a2a。"""
    return AgentCard(
        name=name,
        description=description,
        url=f"{base_url.rstrip('/')}/a2a",
        skills=skills or [],
        provider=AgentProvider(organization=provider_organization, url=base_url.rstrip("/")),
        authentication=AgentAuthentication(schemes=authentication_schemes or ["api_key"]),
    )
