"""ORSI 贡献者注册/探测 API DTO（architecture/09 §14.1/§14.3；Pydantic v2，批次 A）。

铁律：extra="forbid"、snake_case、只数据无行为（02 篇 §6）；信封全按 api/01 §3.1
{data, meta}（PageMeta/EmptyMeta）。值域 Literal 与 domain 枚举、SHACL sh:in 同词汇表
（rsi:ContributorShape/ContributionShape，rsi.ttl v0.1）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.platform.schemas import EmptyMeta, PageMeta
from services.rsi.domain.contributor import RsiContributor

# 值域（domain/contributor.py 枚举 + rsi:ContributorShape sh:in 同词汇表）
TierValues = Literal["solo", "team", "enterprise"]
TransportValues = Literal["streamable_http", "stdio"]  # MCP 篇 VALID_TRANSPORTS 同词汇表


class ContributorRegisterIn(BaseModel):
    """注册请求（POST /api/v1/rsi/contributors；09 §14.1 body 含 id/显示名/治理档位）。"""

    model_config = ConfigDict(extra="forbid")

    contributor_id: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]{0,63}$",
        description="全局唯一 slug（投递目录/claims 绑定公共命名面）",
    )
    display_name: str = Field(min_length=1, max_length=256)
    governance_tier: TierValues = "team"
    trust_score: float | None = Field(default=None, ge=0, le=1)  # 缺省 1.0（领域层 DEFAULT_TRUST_SCORE）


class ContributorOut(BaseModel):
    """贡献者登记行投影（含投递目录记录与最近探测档案；软删行不外露）。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    contributor_id: str
    display_name: str
    governance_tier: str
    trust_score: float
    delivery_dir: str
    probe_profile: dict[str, Any] | None  # 最近一次探测握手能力面档案（None=未探测）
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, contributor: RsiContributor) -> ContributorOut:
        """领域聚合 → DTO（单一收敛点，OrsiCapabilityOut.from_domain 同款）。"""
        return cls(
            id=contributor.id,
            contributor_id=contributor.contributor_id,
            display_name=contributor.display_name,
            governance_tier=contributor.governance_tier.value,
            trust_score=float(contributor.trust_score),
            delivery_dir=contributor.delivery_dir,
            probe_profile=contributor.probe_profile,
            created_at=contributor.created_at,
            updated_at=contributor.updated_at,
        )


class ContributorPageOut(BaseModel):
    """列表响应信封 {data: [...], meta: PageMeta}（api/01 §3.1 be2 口径）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[ContributorOut]
    meta: PageMeta


class ContributorItemOut(BaseModel):
    """单条响应信封 {data: {...}, meta: {}}（EmptyMeta 序列化恒 {}）。"""

    model_config = ConfigDict(extra="forbid")

    data: ContributorOut
    meta: EmptyMeta


class McpEndpointIn(BaseModel):
    """MCP server 端点连接参数（§14.3 步 1 探测握手输入；McpTargetConfig 字段子集）。"""

    model_config = ConfigDict(extra="forbid")

    transport: TransportValues
    url: str | None = None  # streamable_http 必填
    command: str | None = None  # stdio 必填
    args: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=10.0, gt=0, le=60)


class ProbeRequestIn(BaseModel):
    """探测握手请求（POST /api/v1/rsi/contributors/{id}/probe；§14.3 步 1）。

    三输入互斥恰一：endpoint（真实 MCP server）/ stub（桩档案内联）/ stub_path（桩文件）。
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: McpEndpointIn | None = None
    stub: dict[str, Any] | None = None
    stub_path: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ProbeRequestIn:
        sources = [self.endpoint is not None, self.stub is not None, self.stub_path is not None]
        if sum(sources) != 1:
            raise ValueError("探测输入互斥恰一：endpoint / stub / stub_path 三选一")
        if self.endpoint is not None:
            if self.endpoint.transport == "streamable_http" and not self.endpoint.url:
                raise ValueError("streamable_http 端点必须提供 url")
            if self.endpoint.transport == "stdio" and not self.endpoint.command:
                raise ValueError("stdio 端点必须提供 command")
        return self


class MatrixCellOut(BaseModel):
    """兼容性矩阵单格（八扩展点一行一结论；§14.3 步 2 三色）。"""

    model_config = ConfigDict(extra="forbid")

    extension_point: str
    channel: str
    color: Literal["green", "yellow", "red"]
    rule: str
    adapt_subtype: str | None
    assemblable: bool


class ProbeOut(BaseModel):
    """探测握手响应：能力面档案 + 兼容性矩阵（§14.3 步 1~2 产出）。"""

    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any]  # ContributorProfile.to_dict()（档案 schema_version 口径见 probe.py）
    matrix: list[MatrixCellOut]
    meta: EmptyMeta
