"""subagent 能力安全护栏（docs/Agent/06 路线 #4；研究整理/08 C4：深度与并发上限是子 agent 工具面的共有裁决）。

四道护栏（全部 deny-by-default，拒绝消息面向模型给可执行指引）：

- **递归深度上限**（默认 2 层，防无限派生）：派生深度经 :data:`DERIVATION_DEPTH`
  ContextVar 随「内核 spawn_sub 调用任务」传播——能力层只把内核 AgentSlot 调用装进
  asyncio 任务（子 Run 本体仍由内核派生，禁绕内核直起任务），任务边界 copy_context
  下派，子 Run 的执行子树（kernel.run → execution → 工具 invoke）读到的即自身深度。
  根 Run 深度 0；允许派生当且仅当 当前深度 < max_depth（2 = 最多两层子代理）。
- **并发子代理上限**（默认 4）：按能力实例在途子代理计数（组合根装配粒度=租户部署
  v1 单例）；批量 spawn 整批 all-or-nothing——容量不足整批拒绝、零部分派生。
- **派生白名单**：可派生 agent 类型注册表；空白名单=全拒（与 web 出口白名单同语义：
  安全边界而非功能开关，部署方按租户显式下发）。
- **预算继承**：子代理预算 = min(申请额, ⌊父剩余 × share⌋)（A4 分账的上限份额，参数
  可调）；父剩余由注入探针解析（组合根接线，缺省探针=None=不限）。本层份额只是前置
  收敛，内核 AgentSlot 的 A4 断言（≤父剩余，超限拒派生）仍是最后防线。
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from services.platform.errors import ErrorCode

# ── 护栏缺省值（组合根可注入覆盖）─────────────────────────────────────────────
MAX_DERIVATION_DEPTH_DEFAULT = 2  # 递归深度上限：最多两层子代理（根 Run=深度 0）
MAX_CONCURRENT_SUBAGENTS_DEFAULT = 4  # 并发在途子代理上限（能力实例计数）
BUDGET_SHARE_DEFAULT = 0.5  # 子代理预算份额：⌊父剩余 × share⌋ 为分账上限
MAX_BATCH_SIZE = 16  # 单次 spawn 批量子任务数上限（防单调用失控扇出）
DEFAULT_CHILD_TIMEOUT_MS = 600_000  # 子 Run 时长建议上限 10min（02 §4.2 同款缺省）
DEFAULT_WAIT_TIMEOUT_MS = 30_000  # wait 缺省等待上限

# 派生深度上下文（本模块唯一全局态；随 asyncio 任务边界传播，见模块 docstring）
DERIVATION_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("subagent_derivation_depth", default=0)


class SubagentGuardError(Exception):
    """subagent 能力结构化错误：登记错误码 + 面向模型的消息（禁裸异常语义，fs 同款）。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── 护栏 1：递归深度 ─────────────────────────────────────────────────────────


def current_depth() -> int:
    """当前派生深度（根 Run 调用=0；子 Run 执行子树内=父派生时 +1）。"""
    return DERIVATION_DEPTH.get()


def child_context(depth: int) -> contextvars.Context:
    """构造子代理执行上下文：深度 = 当前 + 1（随内核 spawn_sub 调用任务下派）。

    机制：copy 当前 Context 后在其中设深度，再以该 Context 启动内核派生任务——
    子 Run 的整个执行子树（含其工具调用）读到的即自身深度；父侧上下文不受污染。
    """
    ctx = contextvars.copy_context()
    ctx.run(DERIVATION_DEPTH.set, depth + 1)
    return ctx


def check_depth(max_depth: int) -> int:
    """深度护栏：当前深度已达上限即结构化拒绝；否则返回当前深度。"""
    depth = current_depth()
    if depth >= max_depth:
        raise SubagentGuardError(
            ErrorCode.SCOPE_INSUFFICIENT,
            f"递归深度超限：当前派生深度 {depth} 已达上限 {max_depth}（默认 2 层），"
            "拒绝再派生（防无限派生）；请在本层汇总结果后返回",
        )
    return depth


# ── 护栏 2：并发上限 ─────────────────────────────────────────────────────────


def check_concurrency(in_flight: int, requested: int, max_concurrent: int) -> None:
    """并发护栏（批量 all-or-nothing）：在途 + 本次批次 > 上限即整批拒绝。"""
    projected = in_flight + requested
    if projected > max_concurrent:
        raise SubagentGuardError(
            ErrorCode.TOOL_BUSY,
            f"并发子代理超限：在途 {in_flight} + 本次 {requested} > 上限 {max_concurrent}，"
            "整批拒绝（零部分派生）；请先 subagent_wait 回收在途成员或 subagent_interrupt 取消",
        )


# ── 护栏 3：派生白名单 ───────────────────────────────────────────────────────


class DerivationWhitelist:
    """可派生 agent 类型注册表（deny-by-default：空白名单=全拒）。"""

    __slots__ = ("_types",)

    def __init__(self, allowed: Iterable[str] = ()) -> None:
        types = frozenset(allowed)
        if any(not isinstance(t, str) or not t.strip() for t in types):
            raise ValueError("派生白名单类型须为非空字符串")
        self._types = types

    def allows(self, agent_type: str) -> bool:
        return agent_type in self._types

    @property
    def types(self) -> frozenset[str]:
        return self._types


def check_whitelist(whitelist: DerivationWhitelist, agent_type: str) -> None:
    """白名单护栏：未注册的 agent 类型一律拒绝派生。"""
    if not whitelist.allows(agent_type):
        registered = ", ".join(sorted(whitelist.types)) or "（空——尚未注册任何可派生类型）"
        raise SubagentGuardError(
            ErrorCode.SCOPE_INSUFFICIENT,
            f"目标不在派生白名单：agent_type={agent_type!r} 未注册，拒绝派生；可派生类型注册表: {registered}",
        )


# ── 护栏 4：预算继承（A4 上限份额，参数可调）──────────────────────────────────


class BudgetSharePolicy:
    """预算继承策略：子代理预算上限 = ⌊父剩余 × share⌋，且不超过申请额。

    share ∈ (0, 1]（缺省 0.5）；父剩余=None（不限/未知）时不设份额上限，仅受申请额
    约束——内核 AgentSlot 的 A4 分账断言仍兜底（超父剩余拒派生）。
    """

    __slots__ = ("_share",)

    def __init__(self, share: float = BUDGET_SHARE_DEFAULT) -> None:
        if not 0 < share <= 1:
            raise ValueError(f"budget_share 须在 (0, 1]，得到 {share}")
        self._share = share

    @property
    def share(self) -> float:
        return self._share

    def allocate(self, requested: int, parent_remaining: int | None) -> int:
        """分配子预算：min(申请额, ⌊父剩余 × share⌋)；份额塌缩到 ≤0 即结构化拒绝。"""
        if requested <= 0:
            raise SubagentGuardError(
                ErrorCode.PARAM_INVALID, f"context_budget 须为正整数（token 计），得到 {requested}"
            )
        cap = requested if parent_remaining is None else min(requested, int(parent_remaining * self._share))
        if cap <= 0:
            raise SubagentGuardError(
                ErrorCode.RETRY_BUDGET_EXHAUSTED,
                f"预算继承不足：父剩余 {parent_remaining} × 份额 {self._share:.2f} 的上限份额 ≤0，"
                "拒绝派生（A4 不新增总额）；请缩减批次或先收敛父预算",
            )
        return cap


# ── spawn 参数形状（模型申报面；内核 B1 参数域收敛后的纵深防御）─────────────────


class SpawnTaskSpec(BaseModel):
    """单个子任务申报（frozen）：类型/目标/申请预算/回传契约/时长上限。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_type: str
    objective: str
    context_budget: int
    artifact_schema: dict[str, Any]
    timeout_ms: int = DEFAULT_CHILD_TIMEOUT_MS


def parse_spawn_tasks(parameters: Mapping[str, Any]) -> list[SpawnTaskSpec]:
    """解析并校验 spawn 批量参数（1~MAX_BATCH 项；形状违例结构化拒绝，零派生）。"""
    raw = parameters.get("tasks")
    if not isinstance(raw, list) or not raw:
        raise SubagentGuardError(
            ErrorCode.PARAM_INVALID,
            f"tasks 须为非空数组（子任务批次，1~{MAX_BATCH_SIZE} 项），得到 {type(raw).__name__}",
        )
    if len(raw) > MAX_BATCH_SIZE:
        raise SubagentGuardError(
            ErrorCode.PARAM_INVALID,
            f"tasks 批量 {len(raw)} 项超过单次上限 {MAX_BATCH_SIZE}；请拆分多次 spawn（并发仍受在途上限约束）",
        )
    specs: list[SpawnTaskSpec] = []
    for i, item in enumerate(raw):
        try:
            spec = SpawnTaskSpec.model_validate(item)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            loc = ".".join(str(p) for p in first.get("loc", ()))
            raise SubagentGuardError(
                ErrorCode.PARAM_INVALID,
                f"tasks[{i}] 形状非法（{loc or '未知字段'}: {first.get('msg', '校验失败')}）；"
                "须为 {agent_type, objective, context_budget, artifact_schema[, timeout_ms]}",
            ) from exc
        if not spec.agent_type.strip():
            raise SubagentGuardError(ErrorCode.PARAM_INVALID, f"tasks[{i}].agent_type 为空：请给出白名单内的目标类型")
        if not spec.objective.strip():
            raise SubagentGuardError(
                ErrorCode.PARAM_INVALID,
                f"tasks[{i}].objective 为空：子 Run 需要独立目标（窗口隔离，不共享父历史）",
            )
        if spec.context_budget <= 0:
            raise SubagentGuardError(
                ErrorCode.PARAM_INVALID,
                f"tasks[{i}].context_budget 须为正整数（token 计），得到 {spec.context_budget}",
            )
        if spec.artifact_schema.get("type") != "object":
            raise SubagentGuardError(
                ErrorCode.PARAM_INVALID,
                f"tasks[{i}].artifact_schema 须为 {{'type': 'object', ...}} JSON Schema"
                "（02 §4.2 回传契约：结构化 Artifact，禁自由文本）",
            )
        if spec.timeout_ms <= 0:
            raise SubagentGuardError(
                ErrorCode.PARAM_INVALID,
                f"tasks[{i}].timeout_ms 须为正整数（毫秒），得到 {spec.timeout_ms}",
            )
        specs.append(spec)
    return specs
