"""技术能力端口 · CapabilityProvider（锚点 01 §3.4 倒置例外二；07 篇 §1 按序消化定稿形状）。

依赖倒置：本模块（顶层 MCP 出口 services/mcp）要对外暴露 L3/L5 能力，但禁止依赖上层实现——
故此处只定义协议与数据形状（纯 stdlib、零框架依赖），实现在 L3/L5（或组合根装配的适配器），
启动时经 CapabilityRegistry 注册（registry 归 services/mcp/registry.py，07 篇 §1 序列图）。
网关/出口只认协议不认实现；远程发现改造（拆分开关）只换 Registry 实现，本协议零改动（07 §1）。

版本握手（07 §1 按序消化）：provider 声明 ``provider_version``（semver）与
``supported_loop_versions``（兼容的内核 loop 契约版本区间，如 ["1.x"]）；注册时校验与
内核 loop 契约版本的兼容区间，不兼容拒注册（fail-fast，``IncompatibleProviderError``）。

红线（设计宪法/研究整理 05 §6）：descriptor 的 ``annotations``（readOnlyHint 等 UI 提示）
属不可信元数据，仅作展示——授权一律走平台 PDP（platform.security.authorize 的 scope 精确匹配），
annotations 永不进入授权代码路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

# 内核 loop 契约版本（07 §1 版本握手基准；Agent/02 §4.1 内核侧同规则）
KERNEL_LOOP_VERSION = "1.x"


@dataclass(frozen=True, slots=True)
class CallContext:
    """一次能力调用的调用方上下文（07 §1：tenant_id / trace_id / 调用方身份贯穿）。

    scopes 为调用方被授予的平台 scope 集（deny-by-default，PDP 第 3 步由出口统一判定）；
    ``extra`` 携带通道级透传（如 MCP session id），语义标注/工具元数据禁止混入授权信息。
    """

    tenant_id: UUID
    trace_id: str
    subject_id: UUID | None = None
    scopes: tuple[str, ...] = ()
    caller_type: str = "external"  # external | agent | cli（mcp_invocations.caller_type 口径）
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """能力清单条目（07 §1：name / description / input_schema / scope_required / 语义标注）。

    ``name`` = ``{namespace}.{local}``（如 ``ontology.validate``）；namespace 段即 provider 命名空间。
    ``annotations`` 仅为 MCP ToolAnnotations 形状的 UI 提示（不可信，不进授权路径）；
    ``semantic`` 为 ``_meta.x-ontology`` 语义标注（docs/api/03 §3 逐工具契约）。
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    required_scopes: tuple[str, ...] = ()
    annotations: dict[str, Any] = field(default_factory=dict)  # UI 提示（不可信元数据）
    semantic: dict[str, Any] = field(default_factory=dict)  # _meta.x-ontology 语义标注
    tags: tuple[str, ...] = ()
    external: bool = False  # 外部 MCP server 来的工具默认不可信（注册表按 source 治理）


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """能力调用结果（ok=value；失败用 error 形状，code 取 02 §7 已登记平台错误码）。"""

    value: dict[str, Any] | None = None
    code: int | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.code is None

    @classmethod
    def success(cls, value: dict[str, Any]) -> CapabilityResult:
        return cls(value=value)

    @classmethod
    def failure(cls, code: int, message: str) -> CapabilityResult:
        return cls(code=code, message=message)


class CapabilityError(Exception):
    """能力执行期错误（provider 侧抛出；code 必须取自 02 §7 已登记码，禁新编）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class IncompatibleProviderError(Exception):
    """版本握手失败：provider 兼容区间与内核 loop 契约版本无交集（fail-fast 拒注册，07 §1）。"""


@runtime_checkable
class CapabilityProvider(Protocol):
    """能力供给方协议（07 §1 定稿形状；L3/L5 实现方与测试 Fake 以此结构化满足）。"""

    provider_version: str
    supported_loop_versions: list[str]

    def namespace(self) -> str:
        """能力命名空间（如 "ontology" / "knowledge" / "memory"；平台保留段见 registry）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    def capabilities(self) -> list[CapabilityDescriptor]:
        """声明能力清单（含语义标注与所需 scope）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        """执行能力调用；ctx 携带 tenant_id / trace_id / 调用方身份（07 §1）。"""
        ...  # pragma: no cover — Protocol 方法无实现


def loop_versions_compatible(supported: list[str]) -> bool:
    """版本握手判定（M4.1 子集：精确/主版本段匹配 KERNEL_LOOP_VERSION；区间语法随内核契约演进）。"""
    return KERNEL_LOOP_VERSION in supported
