"""A3 扩展点分发器（02 §2 A3：循环只认八扩展点接口，内核不 import 任何具体能力）。

注册面（02 §4.1 注 1）：register_tool / register_gate / register_context_provider /
register_planning_strategy / register_event_sink / register_execution_backend /
register_reasoning_engine / register_memory_policy / register_agent_slot / register_model。
注册即校验（违反即拒注册，fail-fast）：

- meta 为 :class:`ExtensionMeta` 且 name 带「命名空间.名称」、version 合 semver（§4.1 纪律③）；
- 语义标注非空（§7.4 无语义标注不上架）；工具绑定必须声明 ``action_iri``（行动类对账键）；
- 版本握手（§4.1 注 4）：提供方 ``loop_versions`` 与内核 loop 契约版本主版本号不兼容即拒；
- 同一行动类重复绑定 / 同名重复注册一律拒绝。
"""

from __future__ import annotations

import re

from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.extensions import (
    AgentSlot,
    ContextProvider,
    EventSink,
    ExecutionBackend,
    MemoryPolicy,
    PlanningStrategy,
    PostGate,
    PreGate,
    ReasoningEngine,
    ToolPort,
)
from services.agent.domain.model.kernel_context import ExtensionMeta
from services.platform.ports.model_port import ModelPort

LOOP_CONTRACT_VERSION = "1.0.0"  # 内核 loop 契约版本（semver，§4.1 注 4 握手基准）

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _require_meta(extension: object, *, point: str) -> ExtensionMeta:
    meta = getattr(extension, "meta", None)
    if not isinstance(meta, ExtensionMeta):
        raise KernelContractError(f"{point} 扩展缺少合法 ExtensionMeta（§4.1 纪律③）")
    if "." not in meta.name:
        raise KernelContractError(f"{point} meta.name 须为「命名空间.名称」: {meta.name}")
    if not _SEMVER_RE.match(meta.version):
        raise KernelContractError(f"{point} meta.version 非 semver: {meta.version}")
    if not meta.semantic_annotation:
        raise KernelContractError(f"{point} 扩展无本体语义标注，禁止上架（§7.4）: {meta.name}")
    return meta


def _check_loop_versions(loop_versions: tuple[str, ...], *, extension_name: str) -> None:
    """版本握手：主版本号须与内核 LOOP_CONTRACT_VERSION 一致，不兼容拒注册（§4.1 注 4）。"""
    kernel_major = LOOP_CONTRACT_VERSION.split(".")[0]
    for version in loop_versions:
        if _SEMVER_RE.match(version) and version.split(".")[0] == kernel_major:
            return
    raise KernelContractError(
        f"{extension_name} loop_versions={loop_versions} 与内核契约 {LOOP_CONTRACT_VERSION} "
        "不兼容，拒注册（fail-fast，§4.1 注 4）"
    )


class ExtensionDispatcher:
    """扩展点注册表 + 类型化取用面：内核其余部分只经本类触达能力（依赖倒置，锚点 §3.4）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolPort] = {}
        self._pre_gates: list[PreGate] = []
        self._post_gates: list[PostGate] = []
        self._context_providers: list[ContextProvider] = []
        self._planning_strategy: PlanningStrategy | None = None
        self._reasoning_engines: dict[str, ReasoningEngine] = {}
        self._memory_policies: dict[str, MemoryPolicy] = {}
        self._event_sinks: list[EventSink] = []
        self._execution_backends: dict[str, ExecutionBackend] = {}
        self._agent_slots: dict[str, AgentSlot] = {}
        self._model: ModelPort | None = None

    # ── 注册面 ────────────────────────────────────────────────────────────
    def register_tool(self, tool: ToolPort, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)) -> None:
        """绑定行动类 → 工具实现（tools.bindings）；同一行动类仅一个绑定。"""
        meta = _require_meta(tool, point="ToolPort")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        action_iri = meta.semantic_annotation.get("action_iri")
        if not isinstance(action_iri, str) or not action_iri:
            raise KernelContractError(f"ToolPort {meta.name} 语义标注缺 action_iri（行动类对账键）")
        if action_iri in self._tools:
            raise KernelContractError(f"行动类重复绑定: {action_iri}")
        self._tools[action_iri] = tool

    def register_pre_gate(self, gate: PreGate, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)) -> None:
        """注册增量前置门禁（gates.pre，L2 包 gate 默认禁用待复核）。"""
        meta = _require_meta(gate, point="PreGate")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        self._pre_gates.append(gate)

    def register_post_gate(self, gate: PostGate, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)) -> None:
        meta = _require_meta(gate, point="PostGate")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        self._post_gates.append(gate)

    def register_context_provider(
        self, provider: ContextProvider, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)
    ) -> None:
        meta = _require_meta(provider, point="ContextProvider")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        self._context_providers.append(provider)

    def register_planning_strategy(
        self, strategy: PlanningStrategy, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)
    ) -> None:
        """规划策略唯一（策略可下放可配，但一次运行一个路由决策）。"""
        meta = _require_meta(strategy, point="PlanningStrategy")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        if self._planning_strategy is not None:
            raise KernelContractError("PlanningStrategy 已注册，禁重复（一次运行一个规划策略）")
        self._planning_strategy = strategy

    def register_reasoning_engine(
        self, engine: ReasoningEngine, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)
    ) -> None:
        meta = _require_meta(engine, point="ReasoningEngine")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        self._reasoning_engines[meta.name] = engine

    def register_memory_policy(
        self, policy: MemoryPolicy, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)
    ) -> None:
        meta = _require_meta(policy, point="MemoryPolicy")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        self._memory_policies[meta.name] = policy

    def register_event_sink(
        self, sink: EventSink, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)
    ) -> None:
        """外部通知事件汇（Outbox/审计 sink 为内核必选，不经此注册、不可卸载）。"""
        meta = _require_meta(sink, point="EventSink")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        self._event_sinks.append(sink)

    def register_execution_backend(
        self, backend: ExecutionBackend, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)
    ) -> None:
        meta = _require_meta(backend, point="ExecutionBackend")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        self._execution_backends[meta.name] = backend

    def register_agent_slot(
        self, slot: AgentSlot, *, loop_versions: tuple[str, ...] = (LOOP_CONTRACT_VERSION,)
    ) -> None:
        """agent.slots（02 §4.2）：与八扩展点同一注册表，内核内置、不可被能力层替换。"""
        meta = _require_meta(slot, point="AgentSlot")
        _check_loop_versions(loop_versions, extension_name=meta.name)
        self._agent_slots[meta.name] = slot

    def register_model(self, model: ModelPort) -> None:
        """L7 模型渠道（ModelPort，锚点 §3.4 倒置口）：预算与 fallback 策略归内核 A4。"""
        if getattr(model, "complete_structured", None) is None:
            raise KernelContractError("ModelPort 实现缺少 complete_structured（platform 端口契约）")
        self._model = model

    # ── 取用面（内核只认 Protocol）────────────────────────────────────────
    @property
    def model(self) -> ModelPort | None:
        return self._model

    @property
    def planning_strategy(self) -> PlanningStrategy | None:
        return self._planning_strategy

    @property
    def context_providers(self) -> list[ContextProvider]:
        return list(self._context_providers)

    @property
    def pre_gates(self) -> list[PreGate]:
        return list(self._pre_gates)

    @property
    def post_gates(self) -> list[PostGate]:
        return list(self._post_gates)

    @property
    def event_sinks(self) -> list[EventSink]:
        return list(self._event_sinks)

    def tool_for(self, action_iri: str) -> ToolPort | None:
        """行动类 IRI → 工具绑定（无绑定=行动类枚举非法，B1 基线拒绝）。"""
        return self._tools.get(action_iri)

    def execution_backend(self, name: str | None = None) -> ExecutionBackend | None:
        if name is not None:
            return self._execution_backends.get(name)
        if len(self._execution_backends) == 1:
            return next(iter(self._execution_backends.values()))
        return None

    def agent_slot(self, name: str | None = None) -> AgentSlot | None:
        if name is not None:
            return self._agent_slots.get(name)
        if len(self._agent_slots) == 1:
            return next(iter(self._agent_slots.values()))
        return None

    def reasoning_engine(self, name: str | None = None) -> ReasoningEngine | None:
        if name is not None:
            return self._reasoning_engines.get(name)
        if len(self._reasoning_engines) == 1:
            return next(iter(self._reasoning_engines.values()))
        return None

    def memory_policy(self, name: str | None = None) -> MemoryPolicy | None:
        if name is not None:
            return self._memory_policies.get(name)
        if len(self._memory_policies) == 1:
            return next(iter(self._memory_policies.values()))
        return None
