"""L2 网关 · mcp 管理域 DTO（api/01 §5.7 行 + ★ 预登记行）。

形状权威=frontend/src/features/mcp/api.ts（McpServerRow/McpToolRow/DiscoveredTool/
DiscoverResult 逐字段）+ mocks/platform-handlers.ts §5.7 响应形状。铁律：extra="forbid"、
snake_case、只数据无行为（iam api/schemas/admin.py 同款）。

已知形状差异（后端做不到/有意收敛的 mock 形状，tests/mcp/test_management_api.py 头注同步）：
- mock `ok()` 信封 {code,message,data} → live 裸 DTO/裸 {items} 体（api/01 §3.1 反例裁决，
  admin 域同款；错误体走统一四字段 {code,message,detail,trace_id}）；
- id：mock 前缀串 'mcp-crm-prod'/'mt-1' → live UUID 串；tool_id 为内容寻址短 id
  'mt-<hash10>'（发现态 'nd-<hash10>'）——mock 顺序号在两次探测间不稳定，live 以
  server 名+远端工具名 salt 使刷新重探 id 不变；
- transport：mock 词表 'streamable http'（带空格）→ 入参两种拼写均收（'streamable_http'
  同义），出参恒 'streamable http'（内部词表=McpTargetConfig 'streamable_http'）；
- 注册响应 tools 含未纳管行（adopted=false）：mock 仅回纳管行，但 mock 种子行 mt-6
  （adopted:false）证明前端已渲染未纳管行——live 对账远端真集，全量落缓存；
- discovered_count：mock = max(工具行数, 固定样例长度) → live = 最近一次探测远端工具
  全集数（honest 计数）；
- url_masked：mock 种子两行一明一掩（crm-prod 全显/legacy-erp 半掩）→ live 统一主机
  掩码（保前半段+'****'，端口保留；endpoint_masked 先例同式）；
- 协议/版本：mock 固定 '2025-06-18'/'v2.4.1' → live 取 initialize 握手 protocolVersion/
  serverInfo.version（探测前为空串）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TransportOut = Literal["streamable http", "stdio"]  # 出参恒 mock 词表（内部词表 streamable_http 投影）


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 入参


class DiscoverIn(_In):
    """POST /mcp/discover（frontend api.ts discoverServer body 逐字段）。"""

    name: str = Field(min_length=1, max_length=32)
    transport: str = Field(min_length=1, max_length=16)
    url: str | None = Field(default=None, max_length=512)
    command: str | None = Field(default=None, max_length=512)
    auth: str = Field(default="none", max_length=64)
    token: str | None = Field(default=None, max_length=2048)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class ServerCreateIn(_In):
    """POST /mcp/servers（frontend api.ts registerServer body 逐字段；adopt_tool_ids=
    discover 返回的 nd- 短 id 勾选集，空=全部不纳管——mock filter 同口径）。"""

    name: str = Field(min_length=1, max_length=32)
    desc: str = Field(default="", max_length=512)
    transport: str = Field(min_length=1, max_length=16)
    url: str | None = Field(default=None, max_length=512)
    command: str | None = Field(default=None, max_length=512)
    auth: str = Field(default="none", max_length=64)
    token: str | None = Field(default=None, max_length=2048)
    adopt_tool_ids: list[str] = Field(default_factory=list, max_length=256)


# ---------------------------------------------------------------- 出参


class ProbeToolOut(BaseModel):
    """发现态工具行（mock DiscoveredTool 逐字段）。"""

    model_config = ConfigDict(extra="forbid")
    tool_id: str
    name: str
    desc: str
    write: bool
    read_only: bool


class DiscoverOut(BaseModel):
    """POST /mcp/discover 响应（mock DISCOVER_REPLY 逐字段；不落库）。"""

    model_config = ConfigDict(extra="forbid")
    ok: bool = True
    latency_ms: int
    protocol: str
    server_version: str
    tools: list[ProbeToolOut]


class McpToolRow(BaseModel):
    """工具缓存行（mock McpTool 逐字段）。"""

    model_config = ConfigDict(extra="forbid")
    tool_id: str
    name: str
    desc: str
    write: bool
    read_only: bool
    adopted: bool
    enabled: bool


class ProbeWindowOut(BaseModel):
    """probes_24h 元素（mock {ok: boolean}）。"""

    model_config = ConfigDict(extra="forbid")
    ok: bool


class McpServerRow(BaseModel):
    """Server 行（mock McpServer 逐字段；tools 内嵌全量缓存行）。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    desc: str
    transport: TransportOut  # 恒 'streamable http' | 'stdio'
    url_masked: str
    command: str | None = None
    auth: str
    token_masked: str
    protocol: str
    server_version: str
    status: str  # healthy | unknown | failing
    latency_ms: int
    consecutive_failures: int
    last_probe: str | None = None
    probes_24h: list[ProbeWindowOut]
    adopted_count: int
    discovered_count: int
    added_by: str
    added_at: str
    tools: list[McpToolRow]


class ServerListOut(BaseModel):
    """GET /mcp/servers 响应（mock ok({items}) 的 live 裸体）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[McpServerRow]


class ServerCreatedOut(McpServerRow):
    """POST /mcp/servers 201 响应（=行 + token_sentinel，mock 同形）。"""

    token_sentinel: bool = False


class ServerToolsListOut(BaseModel):
    """GET /mcp/servers/{id}/tools 响应（mock ok({items}) 的 live 裸体）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[McpToolRow]


class RefreshOut(BaseModel):
    """POST /mcp/servers/{id}/refresh 响应（mock 逐字段：latency_ms/tools/discovered_count）。"""

    model_config = ConfigDict(extra="forbid")
    latency_ms: int
    tools: list[McpToolRow]
    discovered_count: int


class ToolEnabledOut(BaseModel):
    """POST /mcp/tools/{tool_id}/enable|disable 响应（mock setMcpToolEnabled 逐字段）。"""

    model_config = ConfigDict(extra="forbid")
    tool_id: str
    enabled: bool
