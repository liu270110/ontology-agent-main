"""X16 工作流执行引擎：确定性 DAG 解释器（27 篇 §3 + 设计宪法 2——编排确定性，LLM 只在
agent 节点内）。

权威与纪律：
- 拓扑序执行已发布图（workflow_run=head 不可变版本快照；workflow_test=受理时固化的草稿
  快照，payload.workflow_graph——resume 续跑确定性依据，27 篇 §3 试运行断点）；
- 节点八类语义（27 篇 §3 v1）：start_end=直通；template=受限 ``${path}`` 模板渲染（零
  eval）；condition=确定性表达式（结构化 ``when`` 对象优先〔推荐口径，杜绝 eval〕，其次
  受限表达式字符串递归下降求值——仅比较/布尔组合，宪法 2 禁裸 LLM 语义分支）；parallel=
  分支组并行（同波就绪节点 asyncio.gather，事件 payload.parallel_id=最近 parallel 祖先）；
  approval=落审批锚点（H-0b approval_pending 键同源）后 run.wait_external() 暂停，
  WORKFLOW_NODE_FINISHED(status=waiting_approval) 标注，/resume 端点唤醒续跑（40 篇 R9
  复用既有审批队列，不新造审批通道）；agent=ChatAdapter 单轮（复用既有适配器端口）；
  tool=能力层工具端口；retrieval=kb 公开检索服务（chat_context 同源消费面，规则 3）；
- 失败语义：节点失败/超时 → FINISHED(failed) → run failed + task failed（v1 无自动重试，
  attempt 结构留好——节点级 attempt 计数随 payload 状态持久）；
- 断点（workflow_test 专属）：breakpoints 节点集**前置检查**命中 → STARTED 即
  FINISHED(waiting_approval) 暂停（协议不变量「STARTED 先于 FINISHED」不破，40 篇 §4.3 #2）；
- 事件：全部 WORKFLOW_NODE_* 经本执行器唯一生成器产出（STARTED 波首发、FINISHED 波尾
  按波序补发——并发节点的事件也保持 STARTED→FINISHED 单调，落库归 worker 消费侧统一
  按 seq 落 task_events 回放根+hub 双通道，_drain_orchestrator 同款纪律）；RUN_STARTED
  仅首拍发射（resume 重放不重发——同 run 协议不变量），task_type=workflow_run|
  workflow_test 透传（40 篇 §4.2，前端据此挂运行卡）；
- 取消：abort 端点聚合终态后，本执行器**波间检查** run 行状态（非 running 即静默收敛，
  对齐 tasks/cancel「取消清单化传播归执行编排」M3+ 口径的 v1 最小面）。

task.payload 契约（执行器写入/读取，runs.py 受理侧同源；JSONB 整体重赋值纪律）：
    workflow_id / kind(workflow_run|workflow_test) / version(head 版本号，test=None) /
    breakpoints([node_id]) / variables(入口入参) / workflow_graph(固化执行图) /
    triggered_by / origin_trace_id / workflow_state = {
        nodes: {nid: {status,attempt,started_at,ended_at,error,parallel_id,title}},
        outputs: {nid: 出参 dict}, vars: 入参变量, paused_node/paused_kind,
        params_override: {nid: 修参}, inactive_edges: [[src,tgt]...]（条件未取分支）,
        breakpoint_hit: [nid...]（已断点暂停过的节点，resume 后不重复命中）}}

跨模块边界：import agent.business（chat_events 零依赖叶子 + exec_events 值对象 + 适配器）
与 kb.business（search_service 公开检索面，规则 3 许可面）；被 agent.business.task_worker
经 Protocol+provider 注入消费（worker 零 workflows import——组合根装配边归 gateway.app）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from services.agent.business.adapters.base import ChatAdapter, ChatTurn
from services.agent.business.chat_events import ChatEvent, ChatEventName
from services.agent.business.exec_events import (
    WorkflowNodeFinishedPayload,
    WorkflowNodeStartedPayload,
    WorkflowNodeStatus,
    WorkflowNodeType,
)
from services.agent.domain.model.kernel_context import TenantContext
from services.agent.domain.model.task import RunStatus, TaskEvent, TaskStatus
from services.workflows.domain.model.graph import WfNodeKind, WorkflowEdge, WorkflowGraph, WorkflowNode

logger = logging.getLogger(__name__)

__all__ = [
    "KIND_WORKFLOW_RUN",
    "KIND_WORKFLOW_TEST",
    "STATE_KEY",
    "NodeExecutionError",
    "NodePorts",
    "WorkflowRunCommand",
    "WorkflowRunExecutor",
    "build_default_node_ports",
    "evaluate_condition",
    "render_template",
]

WORKFLOW_TASK_TYPES = ("workflow_run", "workflow_test")  # worker 分派键（task.type）
KIND_WORKFLOW_RUN = "workflow_run"
KIND_WORKFLOW_TEST = "workflow_test"
STATE_KEY = "workflow_state"  # task.payload 执行态键（executor 写 / runs.py 读）
PENDING_KEY = "approval_pending"  # H-0b 锚点键同源（approval_service.PENDING_KEY 复用值）
TICKETS_KEY = "approvals"  # H-0b 票仓键同源（poller resume 认领谓词依赖）
APPROVAL_ACTION_PREFIX = "workflow:approval"  # approval 节点 action_iri 前缀（审批中心呈现面）

_MAX_NODE_TIMEOUT_S = 3600.0


class NodeExecutionError(Exception):
    """节点执行失败（结构化：code/message 组成 FINISHED.error 与 run.error）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── 节点端口（agent/tool/retrieval 复用既有端口；测试注桩）────────────────────


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """节点执行产物：output 进下游输入与 payload.outputs；usage 归 FINISHED.usage。"""

    output: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)


class NodePorts(Protocol):
    """三类有副作用节点的执行端口（组合根装配生产实现；集成测试注入桩）。"""

    async def agent_turn(
        self, node: WorkflowNode, message: str, ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        """agent 节点：ChatAdapter 单轮（27 篇 §3「Agent 绑插槽实例」）。"""
        ...

    async def invoke_tool(
        self, node: WorkflowNode, args: dict[str, Any], ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        """tool 节点：能力层工具调用（27 篇 §3「注册表选取」）。"""
        ...

    async def retrieve(self, node: WorkflowNode, query: str, ctx: TenantContext, *, run_id: uuid.UUID) -> NodeOutcome:
        """retrieval 节点：kb 检索（GraphRAG 三模式 v1=local 缺省）。"""
        ...


class _UnwiredPorts:
    """端口未装配的 fail-closed 实现（组合根未接线/直调形态：5004 结构化拒绝不静默）。"""

    async def agent_turn(
        self, node: WorkflowNode, message: str, ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        raise NodeExecutionError(5004, "agent 节点端口未装配（组合根未接线，fail-closed）")

    async def invoke_tool(
        self, node: WorkflowNode, args: dict[str, Any], ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        raise NodeExecutionError(5004, "tool 节点端口未装配（组合根未接线，fail-closed）")

    async def retrieve(self, node: WorkflowNode, query: str, ctx: TenantContext, *, run_id: uuid.UUID) -> NodeOutcome:
        raise NodeExecutionError(5004, "retrieval 节点端口未装配（组合根未接线，fail-closed）")


# ── 受限表达式求值（condition 节点；零 eval——宪法 2 确定性口径）────────────────

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<num>-?\d+(?:\.\d+)?)
      | (?P<str>"[^"]*"|'[^']*')
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)
      | (?P<op>==|!=|>=|<=|>|<|\(|\))
    )""",
    re.VERBOSE,
)

_CMP_OPS = {"==", "!=", ">=", "<=", ">", "<"}


def _tokenize(expression: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expression):
        match = _TOKEN_RE.match(expression, pos)
        if match is None:
            if expression[pos:].strip() == "":
                break
            raise NodeExecutionError(3001, f"condition 表达式含非法记号（位置 {pos}）: {expression[pos : pos + 8]!r}")
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return tokens


class _ConditionParser:
    """递归下降求值器：or → and → not → comparison → operand（标识符点路径取变量）。"""

    def __init__(self, tokens: list[tuple[str, str]], resolve: Any) -> None:
        self._tokens = tokens
        self._pos = 0
        self._resolve = resolve

    def parse(self) -> Any:
        value = self._or()
        if self._pos != len(self._tokens):
            raise NodeExecutionError(3001, f"condition 表达式有未消费记号: {self._tokens[self._pos :]}")
        return value

    def _peek_op(self, *ops: str) -> str | None:
        if self._pos < len(self._tokens):
            kind, text = self._tokens[self._pos]
            if kind == "op" and text in ops:
                return text
        return None

    def _or(self) -> Any:
        value = self._and()
        while self._pos < len(self._tokens) and self._tokens[self._pos] == ("name", "or"):
            self._pos += 1
            rhs = self._and()  # 急切求值（零副作用语法；短路会留未消费记号破坏解析位）
            value = bool(value) or bool(rhs)
        return value

    def _and(self) -> Any:
        value = self._not()
        while self._pos < len(self._tokens) and self._tokens[self._pos] == ("name", "and"):
            self._pos += 1
            rhs = self._not()
            value = bool(value) and bool(rhs)
        return value

    def _not(self) -> Any:
        if self._pos < len(self._tokens) and self._tokens[self._pos] == ("name", "not"):
            self._pos += 1
            return not bool(self._not())
        return self._comparison()

    def _comparison(self) -> Any:
        left = self._operand()
        op = self._peek_op(*_CMP_OPS)
        if op is None:
            return left
        self._pos += 1
        return _compare(op, left, self._operand())

    def _operand(self) -> Any:
        if self._pos >= len(self._tokens):
            raise NodeExecutionError(3001, "condition 表达式意外结束")
        kind, text = self._tokens[self._pos]
        if kind == "op" and text == "(":
            self._pos += 1
            value = self._or()
            if self._peek_op(")") is None:
                raise NodeExecutionError(3001, "condition 表达式括号不闭合")
            self._pos += 1
            return value
        if kind in ("num", "str"):
            self._pos += 1
            return text[1:-1] if kind == "str" else (float(text) if "." in text else int(text))
        if kind == "name":
            self._pos += 1
            if text == "true":
                return True
            if text == "false":
                return False
            if text == "null":
                return None
            return self._resolve(text)
        raise NodeExecutionError(3001, f"condition 表达式非法记号: {text!r}")


def _compare(op: str, left: Any, right: Any) -> bool:
    try:
        matched = {
            "==": left == right,
            "!=": left != right,
            ">": left > right,
            "<": left < right,
            ">=": left >= right,
            "<=": left <= right,
        }[op]
    except TypeError as exc:
        raise NodeExecutionError(3001, f"condition 比较类型不可比: {left!r} {op} {right!r}") from exc
    return bool(matched)


def _resolve_path(path: str, ctx: dict[str, Any]) -> Any:
    """点路径取变量：首段查 ctx（入口变量+nodes 出参表），逐段下钻；未知显性失败。"""
    parts = path.split(".")
    if parts[0] not in ctx:
        raise NodeExecutionError(3001, f"condition 引用未知变量: {path}")
    value: Any = ctx[parts[0]]
    for part in parts[1:]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise NodeExecutionError(3001, f"condition 变量路径不可求值: {path}")
    return value


def evaluate_condition(node: WorkflowNode, ctx: dict[str, Any]) -> bool:
    """条件节点确定性求值：结构化 ``when`` 对象优先（推荐口径，杜绝 eval），其次受限
    表达式字符串（递归下降，仅比较/布尔组合）。求值失败=节点失败（确定性逻辑错误必须
    显性失败，禁静默默认分支——宪法 2）。"""
    when = node.params.get("when")
    if isinstance(when, dict):
        return _eval_when(when, ctx)
    expression = node.params.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise NodeExecutionError(3001, f"condition 节点 {node.id} 缺确定性表达式（params.expression/when）")
    parser = _ConditionParser(_tokenize(expression), lambda path: _resolve_path(path, ctx))
    return bool(parser.parse())


def _eval_when(when: dict[str, Any], ctx: dict[str, Any]) -> bool:
    """结构化 when：{"all":[...]} / {"any":[...]} 可嵌套；叶子 {"path","op","value"}。"""
    if set(when.keys()) - {"all", "any"} or not when:
        raise NodeExecutionError(3001, f"condition when 结构非法（仅 all/any 键）: {sorted(when)}")
    if "all" in when:
        items = when["all"]
        if not isinstance(items, list):
            raise NodeExecutionError(3001, "condition when.all 须为数组")
        return all(_eval_when_clause(item, ctx) for item in items)
    items = when.get("any")
    if not isinstance(items, list):
        raise NodeExecutionError(3001, "condition when.any 须为数组")
    return any(_eval_when_clause(item, ctx) for item in items)


def _eval_when_clause(clause: Any, ctx: dict[str, Any]) -> bool:
    if isinstance(clause, dict) and (set(clause) & {"all", "any"}):
        return _eval_when(clause, ctx)
    if not isinstance(clause, dict) or not {"path", "op"} <= set(clause):
        raise NodeExecutionError(3001, f"condition when 叶子须为 {{path,op,value}}: {clause!r}")
    op = str(clause["op"])
    if op not in _CMP_OPS:
        raise NodeExecutionError(3001, f"condition when.op 非法: {op}")
    return _compare(op, _resolve_path(str(clause["path"]), ctx), clause.get("value"))


def render_template(template: str, ctx: dict[str, Any]) -> str:
    """模板渲染（template/agent prompt）：受限 ``${path}`` 占位符替换（零 eval）；
    未知变量显性失败（与 condition 同一求值面）。"""

    def _sub(match: re.Match[str]) -> str:
        return str(_resolve_path(match.group(1).strip(), ctx))

    return re.sub(r"\$\{([^}]+)\}", _sub, template)


# ── 执行命令与执行器 ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkflowRunCommand:
    """一次工作流 run 的执行命令（worker 认领/resume 后构造；worker 经本形状驱动）。"""

    tenant_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    task_type: str  # workflow_run | workflow_test（RUN_STARTED.task_type 透传）
    trace_id: str  # C2 贯穿（worker 回溯受理链 or 合成兜底，红队 C4 同款）


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _param_hash(workflow_id: uuid.UUID, node: WorkflowNode, payload: dict[str, Any]) -> str:
    """审批/断点锚点参数哈希（B5 参数哈希绑定同型：防批准错对象/换参重放）。"""
    material = json.dumps(
        {"workflow_id": str(workflow_id), "node_id": node.id, "params": node.params, "input": payload},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _timeout_s(node: WorkflowNode, default_s: float) -> float:
    """per-node 超时：params.timeout_s（秒，正值）优先，缺省回落统一配置层值；上限 3600。"""
    raw = node.params.get("timeout_s")
    value = float(raw) if isinstance(raw, (int, float)) and float(raw) > 0 else default_s
    return min(value, _MAX_NODE_TIMEOUT_S)


def _branch_takes(edge: WorkflowEdge, branch: bool) -> bool:
    """条件出边分支判定：是/true/yes/1 ↔ 否/false/no/0；无标注边=恒通行（缺省画法）。"""
    label = (edge.label or "").strip().lower()
    if not label:
        return True
    truthy, falsy = {"是", "true", "yes", "1"}, {"否", "false", "no", "0"}
    return label in truthy if branch else label in falsy


class WorkflowRunExecutor:
    """工作流 run 执行器：拓扑序解释执行 + WORKFLOW_NODE_* 事件流 + 终态回写。

    消费方=TaskRunWorker（Protocol 注入，provider 形态）：``execute_run`` 产出
    ChatEvent 流，worker 沿 _drain_orchestrator 同款纪律落 task_events（回放根）+hub
    双通道；run/task 行终态与审计行由本执行器自写（chat 结果汇的 workflow 同构面——
    无会话消息回写，workflow 任务 session_id=None）。
    """

    def __init__(
        self,
        *,
        uow: Any,  # AsyncUnitOfWork（组合根注入；鸭子类型防跨模块 import）
        ports: NodePorts | None = None,
        node_timeout_default_s: float = 60.0,
    ) -> None:
        self._uow = uow
        self._ports = ports or _UnwiredPorts()
        self._timeout_default_s = node_timeout_default_s

    async def execute_run(
        self,
        *,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID,
        run_id: uuid.UUID,
        task_type: str,
        trace_id: str,
    ) -> AsyncIterator[ChatEvent]:
        """执行一次认领（fresh 或 resume）：产出事件流并自写终态。

        签名取**基础元**（worker 零 workflows import 的 Protocol 消费面——组合根装配，
        agent.business 经 provider 注入仅见本方法形状）；内部组装 WorkflowRunCommand。
        """
        command = WorkflowRunCommand(
            tenant_id=tenant_id, task_id=task_id, run_id=run_id, task_type=task_type, trace_id=trace_id
        )
        started = time.monotonic()
        async with self._uow.for_tenant(command.tenant_id) as tx:
            task = await tx.tasks.get(command.task_id)
            if task is None:
                logger.warning("工作流任务缺失，执行放弃（task=%s）", command.task_id)
                return
            run = next((r for r in task.runs if r.id == command.run_id), None)
            if run is None or run.status is not RunStatus.RUNNING:
                logger.warning("工作流 Run 非运行态，执行放弃（run=%s）", command.run_id)
                return
            payload = dict(task.payload or {})
        workflow_id = uuid.UUID(str(payload["workflow_id"]))
        graph = WorkflowGraph.from_storage(payload.get("workflow_graph"))
        state = self._state_of(payload)
        state["run_id"] = command.run_id  # 端口调用归因（_ctx_run_id）；落库投影剔除
        resuming = bool(state.get("paused_node"))
        usage_total: dict[str, Any] = {}

        first_event: ChatEvent | None
        if resuming:  # resume 首拍：清暂停标记（断点节点回 pending 重跑；approval 批准即完成）
            first_event = await self._resume_paused_node(command, graph, state)
            await self._persist_state(command, state)
        else:
            first_event = self._run_started(command, payload)  # RUN_STARTED 仅首拍（同 run 协议不变量）
        # 崩溃恢复残留：running 行回 pending 重执行（attempt 结构保留，重试 Run 续跑同款）
        for row in (state.get("nodes") or {}).values():
            if row.get("status") == "running":
                row["status"] = "pending"
        if first_event is not None:  # 断点 resume 无首拍事件（节点回主循环正常调度）
            yield first_event

        failure: dict[str, Any] | None = None
        try:
            async for wave_event in self._drive(command, workflow_id, graph, state, usage_total):
                yield wave_event
        except NodeExecutionError as exc:
            failure = {"code": int(exc.code), "message": exc.message, "retryable": False}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 ——执行器兜底结构化（禁裸异常逃逸，02 §4.1 ④）
            logger.exception("工作流节点执行未预期异常（run=%s）", command.run_id)
            failure = {"code": 5999, "message": str(exc), "retryable": False}

        if failure is None and state.get("paused_node") is None:
            await self._finalize(command, failure=None, started=started, usage=usage_total)
            yield ChatEvent(
                name=ChatEventName.RUN_FINISHED,
                data={
                    "run_id": str(command.run_id),
                    "workflow_id": str(workflow_id),
                    "task_type": command.task_type,
                    "usage": usage_total,
                },
                run_id=command.run_id,
                trace_id=command.trace_id,
            )
        elif failure is not None:
            await self._finalize(command, failure=failure, started=started, usage=usage_total)
            yield ChatEvent(
                name=ChatEventName.RUN_ERROR,
                data={
                    "run_id": str(command.run_id),
                    "workflow_id": str(workflow_id),
                    "task_type": command.task_type,
                    "code": failure["code"],
                    "message": failure["message"],
                    "retryable": False,
                },
                run_id=command.run_id,
                trace_id=command.trace_id,
            )
        # 其余分支=暂停（approval/breakpoint）：无终态事件，/resume 唤醒后重新认领续跑

    # ── 主循环：波次调度（拓扑序 + 条件激活 + parallel 分组）──────────────────

    async def _drive(
        self,
        command: WorkflowRunCommand,
        workflow_id: uuid.UUID,
        graph: WorkflowGraph,
        state: dict[str, Any],
        usage_total: dict[str, Any],
    ) -> AsyncIterator[ChatEvent]:
        nodes = {n.id: n for n in graph.nodes}
        out_edges: dict[str, list[WorkflowEdge]] = {nid: [] for nid in nodes}
        in_edges: dict[str, list[WorkflowEdge]] = {nid: [] for nid in nodes}
        for edge in graph.edges:
            out_edges.setdefault(edge.source, []).append(edge)
            in_edges.setdefault(edge.target, []).append(edge)

        rows: dict[str, dict[str, Any]] = state.setdefault("nodes", {})
        statuses = {nid: str(rows.get(nid, {}).get("status", "pending")) for nid in nodes}
        inactive = {tuple(e) for e in state.get("inactive_edges") or ()}

        def active(edge: WorkflowEdge) -> bool:
            return (edge.source, edge.target) not in inactive

        # 账本初始化（resume 续跑同口径）：活跃入边计数 + 已终态源节点的边预结算
        remaining = {nid: sum(1 for e in in_edges[nid] if active(e)) for nid in nodes}
        succeeded_in = {nid: 0 for nid in nodes}
        for nid in rows:
            if statuses.get(nid) not in ("succeeded", "skipped"):
                continue
            credit = statuses.get(nid) == "succeeded"
            for edge in out_edges.get(nid, []):
                if active(edge):
                    remaining[edge.target] = max(0, remaining[edge.target] - 1)
                    if credit:
                        succeeded_in[edge.target] += 1
        parallel_of: dict[str, str | None] = {nid: rows.get(nid, {}).get("parallel_id") for nid in nodes}

        def inherit_parallel(nid: str) -> str | None:
            """parallel_id=最近 parallel 祖先（沿激活入边继承；dify 扁平组树 v1 单层）。"""
            for edge in in_edges[nid]:
                if not active(edge) or statuses.get(edge.source) != "succeeded":
                    continue
                if nodes[edge.source].kind is WfNodeKind.PARALLEL:
                    return edge.source
                return parallel_of.get(edge.source)
            return None

        while True:
            if not await self._run_alive(command):  # 波间取消检查（abort 已聚合终态→静默收敛）
                return
            ready = [
                nid
                for nid in nodes
                if statuses[nid] == "pending" and remaining[nid] == 0 and (succeeded_in[nid] > 0 or not in_edges[nid])
            ]
            skippable = [
                nid
                for nid in nodes
                if statuses[nid] == "pending"
                and remaining[nid] == 0
                and succeeded_in[nid] == 0
                and in_edges[nid]  # 全部入边未取/源跳过：跳过传播
            ]
            if not ready and not skippable:
                pending = [nid for nid in nodes if statuses[nid] == "pending"]
                if pending:  # DAG 无环由受理校验保证；防御性显性失败（禁静默挂起）
                    raise NodeExecutionError(4801, f"图调度死锁（存在不可达节点 {sorted(pending)[:8]}，环依赖）")
                return

            for nid in skippable:  # 跳过传播：STARTED→FINISHED(skipped)（协议不变量不破）
                statuses[nid] = "skipped"
                self._record(state, nid, "skipped", parallel_id=parallel_of.get(nid), title=nodes[nid].label)
                for edge in out_edges[nid]:
                    if active(edge):
                        remaining[edge.target] = max(0, remaining[edge.target] - 1)
            if skippable:
                for nid in skippable:
                    yield self._started_event(command, nodes[nid], parallel_id=parallel_of.get(nid))
                    yield self._finished_event(command, nodes[nid], WorkflowNodeStatus.SKIPPED, duration_ms=0)
                await self._persist_state(command, state)
                continue

            # 断点前置检查（workflow_test）：命中即暂停（已命中过的节点不重复断）
            breakpoint = next(
                (nid for nid in ready if self._is_breakpoint(command, state, nid)),
                None,
            )
            if breakpoint is not None:
                statuses[breakpoint] = "waiting_approval"
                bp_parallel = inherit_parallel(breakpoint)
                self._record(
                    state, breakpoint, "waiting_approval", parallel_id=bp_parallel, title=nodes[breakpoint].label
                )
                state.setdefault("breakpoint_hit", []).append(breakpoint)
                state["paused_node"] = breakpoint
                state["paused_kind"] = "breakpoint"
                await self._pause(command, workflow_id, nodes[breakpoint], state, kind="breakpoint")
                yield self._started_event(command, nodes[breakpoint], parallel_id=inherit_parallel(breakpoint))
                yield self._finished_event(command, nodes[breakpoint], WorkflowNodeStatus.WAITING_APPROVAL)
                return

            # 波内并行：就绪节点同波 gather（parallel=分支组并行，27 篇 §3）
            wave_parallel = {nid: inherit_parallel(nid) for nid in ready}
            for nid in ready:
                statuses[nid] = "running"
                self._record(
                    state, nid, "running", parallel_id=wave_parallel[nid], title=nodes[nid].label, started_at=_now_iso()
                )
            for nid in ready:
                yield self._started_event(command, nodes[nid], parallel_id=wave_parallel[nid])
            await self._persist_state(command, state)

            results = await asyncio.gather(
                *(self._execute_node(command, nodes[nid], in_edges[nid], active, state) for nid in ready),
                return_exceptions=False,
            )

            paused_node: str | None = None
            node_failed = False
            for nid, result in zip(ready, results, strict=True):
                if isinstance(result, _NodeWaiting):
                    statuses[nid] = "waiting_approval"
                    paused_node = nid
                    # 协议不变量：STARTED 必配 FINISHED（waiting_approval=暂停终态标注，40 篇 §4.3 #2）
                    yield self._finished_event(command, nodes[nid], WorkflowNodeStatus.WAITING_APPROVAL)
                elif isinstance(result, _NodeFailure):
                    statuses[nid] = "failed"
                    node_failed = True
                    self._record(
                        state,
                        nid,
                        "failed",
                        parallel_id=wave_parallel[nid],
                        title=nodes[nid].label,
                        error=dict(result.error),
                        duration_ms=result.duration_ms,
                    )
                    yield self._finished_event(
                        command,
                        nodes[nid],
                        WorkflowNodeStatus.FAILED,
                        duration_ms=result.duration_ms,
                        error=dict(result.error),
                    )
                else:
                    statuses[nid] = "succeeded"
                    state.setdefault("outputs", {})[nid] = dict(result.output)
                    for key, value in result.usage.items():
                        if isinstance(value, (int, float)):
                            usage_total[key] = usage_total.get(key, 0) + value
                    self._record(
                        state,
                        nid,
                        "succeeded",
                        parallel_id=wave_parallel[nid],
                        title=nodes[nid].label,
                        ended_at=_now_iso(),
                        duration_ms=result.duration_ms,
                        usage=dict(result.usage),
                    )
                    yield self._finished_event(
                        command,
                        nodes[nid],
                        WorkflowNodeStatus.SUCCEEDED,
                        duration_ms=result.duration_ms,
                        usage=dict(result.usage),
                    )
                    if nodes[nid].kind is not WfNodeKind.CONDITION:
                        # 直通出边结算（条件节点的分支裁决在波尾统一处理）
                        for edge in out_edges[nid]:
                            if active(edge):
                                remaining[edge.target] = max(0, remaining[edge.target] - 1)
                                succeeded_in[edge.target] += 1
                    if nodes[nid].kind is WfNodeKind.PARALLEL:
                        for edge in out_edges[nid]:  # 分支组标记：直接后继归属本 parallel 组
                            parallel_of[edge.target] = nid
            if paused_node is not None:
                state["paused_node"] = paused_node
                state["paused_kind"] = "approval"
                await self._persist_state(command, state)
                return  # 审批暂停：/resume 端点唤醒后重新认领续跑
            if node_failed:
                await self._persist_state(command, state)
                # 失败语义：节点失败 → run failed（v1 无自动重试）——显性上抛首个失败，
                # execute_run 捕获后 finalize run/task 终态 + RUN_ERROR（FINISHED(failed) 已发）
                first = next(
                    (r for r in results if isinstance(r, _NodeFailure)),
                    _NodeFailure({"code": 5999, "message": "节点失败"}, 0),
                )
                raise NodeExecutionError(int(first.error["code"]), str(first.error["message"]))

            # 条件分支裁决（条件节点成功后）：取分支边结算、弃分支边停用
            for nid in ready:
                if statuses[nid] != "succeeded" or nodes[nid].kind is not WfNodeKind.CONDITION:
                    continue
                branch = bool((state.get("outputs") or {}).get(nid, {}).get("branch"))
                for edge in out_edges[nid]:
                    if not active(edge):
                        continue
                    if _branch_takes(edge, branch):
                        remaining[edge.target] = max(0, remaining[edge.target] - 1)
                        succeeded_in[edge.target] += 1
                    else:
                        state.setdefault("inactive_edges", []).append([edge.source, edge.target])
                        inactive.add((edge.source, edge.target))
                        remaining[edge.target] = max(0, remaining[edge.target] - 1)
            await self._persist_state(command, state)

    # ── 单节点执行（超时钳取 + 端口分发；零写副作用——落账归波次边界）─────────

    async def _execute_node(
        self,
        command: WorkflowRunCommand,
        node: WorkflowNode,
        in_edges: list[WorkflowEdge],
        active: Any,
        state: dict[str, Any],
    ) -> Any:
        """执行一个节点，返回 _NodeSuccess/_NodeFailure/_NodeWaiting（事件由调度器补发）。"""
        started = time.monotonic()
        ctx = TenantContext(
            tenant_id=command.tenant_id, roles=("operator",), scopes=("workflow:run",), trace_id=command.trace_id
        )
        override = (state.get("params_override") or {}).get(node.id)
        params = {**node.params, **(override if isinstance(override, dict) else {})}
        node_input = self._node_input(node, in_edges, active, state)
        if node.kind is WfNodeKind.APPROVAL:
            # 审批节点：执行体=人——落锚点+run.wait_external 暂停（工单/票归 resume 端点）
            error = await self._approval_pause(command, node, params, node_input, state)
            return _NodeWaiting() if error is None else _NodeFailure(error, 0)
        try:
            outcome = await asyncio.wait_for(
                self._dispatch(node, params, node_input, ctx, state),
                timeout=_timeout_s(node, self._timeout_default_s),
            )
        except TimeoutError:
            return _NodeFailure(
                {"code": 5003, "message": f"节点执行超时（>{_timeout_s(node, self._timeout_default_s):.0f}s）"},
                int((time.monotonic() - started) * 1000),
            )
        except NodeExecutionError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return _NodeFailure({"code": int(exc.code), "message": exc.message}, elapsed)
        except Exception as exc:  # noqa: BLE001 ——端口未分类异常结构化（禁裸异常逃逸）
            logger.exception("工作流节点端口异常（node=%s kind=%s）", node.id, node.kind.value)
            return _NodeFailure({"code": 5999, "message": str(exc)}, int((time.monotonic() - started) * 1000))
        return _NodeSuccess(dict(outcome.output), dict(outcome.usage), int((time.monotonic() - started) * 1000))

    async def _dispatch(
        self,
        node: WorkflowNode,
        params: dict[str, Any],
        node_input: dict[str, Any],
        ctx: TenantContext,
        state: dict[str, Any],
    ) -> NodeOutcome:
        """七类节点分发（approval 上移暂停路径；确定性节点本地求值，副作用节点走端口）。"""
        kind = node.kind
        ctx_vars = self._condition_ctx(state)
        if kind is WfNodeKind.START_END:
            return NodeOutcome(output=dict(node_input))
        if kind is WfNodeKind.TEMPLATE:
            template = params.get("template")
            if not isinstance(template, str) or not template:
                return NodeOutcome(output=dict(node_input))  # 无模板=直通（纯映射语义 v1）
            return NodeOutcome(output={"output": render_template(template, ctx_vars)})
        if kind is WfNodeKind.CONDITION:
            branch = evaluate_condition(node, ctx_vars)
            return NodeOutcome(output={"branch": branch, "expression": params.get("expression")})
        if kind is WfNodeKind.PARALLEL:
            return NodeOutcome(output=dict(node_input))  # 分支组标记节点：直通（组并行归调度器波次）
        if kind is WfNodeKind.AGENT:
            prompt = params.get("prompt")
            message = (
                render_template(str(prompt), ctx_vars)
                if isinstance(prompt, str) and prompt
                else json.dumps(node_input, ensure_ascii=False)
            )
            return await self._ports.agent_turn(node, message, ctx, run_id=_ctx_run_id(state))
        if kind is WfNodeKind.TOOL:
            args = params.get("args") if isinstance(params.get("args"), dict) else dict(node_input)
            return await self._ports.invoke_tool(node, args, ctx, run_id=_ctx_run_id(state))
        if kind is WfNodeKind.RETRIEVAL:
            query = params.get("query")
            query_text = (
                render_template(str(query), ctx_vars)
                if isinstance(query, str) and query
                else json.dumps(node_input, ensure_ascii=False)
            )
            return await self._ports.retrieve(node, query_text, ctx, run_id=_ctx_run_id(state))
        raise NodeExecutionError(4802, f"未知节点类型: {kind}")

    # ── 审批/断点暂停落账（approval/breakpoint 共用；H-0b 锚点键同源）─────────

    async def _approval_pause(
        self,
        command: WorkflowRunCommand,
        node: WorkflowNode,
        params: dict[str, Any],
        node_input: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """审批节点执行=落锚点+run.wait_external（同事务）；返回 None=暂停成立。"""
        workflow_id = str(state.get("workflow_id") or "")
        async with self._uow.for_tenant(command.tenant_id) as tx:
            task = await tx.tasks.get(command.task_id)
            if task is None:
                return {"code": 5004, "message": "任务不存在（审批暂停落账失败）"}
            run = next((r for r in task.runs if r.id == command.run_id), None)
            if run is None or run.status is not RunStatus.RUNNING:
                return {"code": 4102, "message": "Run 非运行态（审批暂停落账失败）"}
            payload = dict(task.payload or {})
            payload[STATE_KEY] = {**self._public_state(state), "workflow_id": workflow_id}
            payload[PENDING_KEY] = {
                "run_id": str(command.run_id),
                "node_id": node.id,
                "kind": "approval",
                "action_iri": f"{APPROVAL_ACTION_PREFIX}:{workflow_id}:{node.id}",
                "param_hash": _param_hash(uuid.UUID(workflow_id), node, node_input) if workflow_id else "",
                "waiting_since": _now_iso(),
                "trace_id": command.trace_id,
            }
            run.wait_external()  # running→waiting_tool（合法长等：不入孤儿回收面，task_poller 口径）
            task.payload = payload
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                command.task_id,
                TaskEvent(
                    task_id=command.task_id,
                    event_type="run.approval_pending",
                    data={"run_id": str(command.run_id), "node_id": node.id, "kind": "approval"},
                ),
            )
        return None

    async def _pause(
        self,
        command: WorkflowRunCommand,
        workflow_id: uuid.UUID,
        node: WorkflowNode,
        state: dict[str, Any],
        *,
        kind: str,
    ) -> None:
        """断点暂停落账：锚点（param_hash 绑定修参核验）+run.wait_external（同事务）。"""
        node_input = self._node_input(node, [], lambda _e: True, state)
        async with self._uow.for_tenant(command.tenant_id) as tx:
            task = await tx.tasks.get(command.task_id)
            if task is None:
                return
            run = next((r for r in task.runs if r.id == command.run_id), None)
            if run is None or run.status is not RunStatus.RUNNING:
                return
            payload = dict(task.payload or {})
            payload[STATE_KEY] = {**self._public_state(state), "workflow_id": str(workflow_id)}
            payload[PENDING_KEY] = {
                "run_id": str(command.run_id),
                "node_id": node.id,
                "kind": kind,
                "action_iri": f"{APPROVAL_ACTION_PREFIX}:{workflow_id}:{node.id}",
                "param_hash": _param_hash(workflow_id, node, node_input),
                "waiting_since": _now_iso(),
                "trace_id": command.trace_id,
            }
            run.wait_external()
            task.payload = payload
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                command.task_id,
                TaskEvent(
                    task_id=command.task_id,
                    event_type="run.breakpoint_paused",
                    data={"run_id": str(command.run_id), "node_id": node.id, "kind": kind},
                ),
            )

    async def _resume_paused_node(
        self, command: WorkflowRunCommand, graph: WorkflowGraph, state: dict[str, Any]
    ) -> ChatEvent | None:
        """resume 续跑首拍：清暂停标记；approval 节点=批准即完成（FINISHED succeeded 事件
        交生成器）；breakpoint 节点回 pending 交主循环正常调度（修参经 params_override 已
        由 resume 端点合入）。锚点/票的消费归 resume 端点与 worker（职责分界）。"""
        paused = str(state.get("paused_node") or "")
        kind = str(state.get("paused_kind") or "")
        state["paused_node"] = None
        state["paused_kind"] = None
        node = next((n for n in graph.nodes if n.id == paused), None)
        if node is None:
            return None
        if kind != "approval":
            rows = state.setdefault("nodes", {})
            row = rows.get(paused)
            if isinstance(row, dict):
                row["status"] = "pending"  # 断点节点重跑（27 篇 §3 time-travel「从暂停节点继续」）
            return None
        prior_parallel = (state.get("nodes") or {}).get(paused, {}).get("parallel_id")
        self._record(state, paused, "succeeded", parallel_id=prior_parallel, title=node.label, ended_at=_now_iso())
        return self._finished_event(command, node, WorkflowNodeStatus.SUCCEEDED, duration_ms=0)

    # ── 终态回写（chat 结果汇的 workflow 同构面；无会话消息回写）─────────────

    async def _finalize(
        self, command: WorkflowRunCommand, *, failure: dict[str, Any] | None, started: float, usage: dict[str, Any]
    ) -> None:
        cost_ms = int((time.monotonic() - started) * 1000)
        async with self._uow.for_tenant(command.tenant_id) as tx:
            task = await tx.tasks.get(command.task_id)
            if task is None:
                return
            run = next((r for r in task.runs if r.id == command.run_id), None)
            if run is not None and run.status is RunStatus.RUNNING:
                if failure is None:
                    run.complete(dict(usage))
                else:
                    run.fail(dict(failure))
            if task.status is TaskStatus.RUNNING:
                if failure is None:
                    task.succeed()
                else:
                    task.fail()
                    task.error = str(failure.get("message") or "")
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                command.task_id,
                TaskEvent(
                    task_id=command.task_id,
                    event_type="run.finished" if failure is None else "run.error",
                    data={
                        "run_id": str(command.run_id),
                        "status": "completed" if failure is None else "failed",
                        "usage": usage,
                        "cost_ms": cost_ms,
                        "code": failure["code"] if failure else None,
                        "message": failure["message"] if failure else None,
                        "task_type": command.task_type,
                    },
                ),
            )

    # ── 状态与事件小件 ────────────────────────────────────────────────────

    def _run_started(self, command: WorkflowRunCommand, payload: dict[str, Any]) -> ChatEvent:
        return ChatEvent(
            name=ChatEventName.RUN_STARTED,
            data={
                "run_id": str(command.run_id),
                "session_id": None,
                "task_id": str(command.task_id),
                "agent_id": None,
                "task_type": command.task_type,
                "workflow_id": str(payload.get("workflow_id") or ""),
                "workflow_kind": str(payload.get("kind") or command.task_type),
                "version": payload.get("version"),
            },
            run_id=command.run_id,
            trace_id=command.trace_id,
        )

    def _started_event(self, command: WorkflowRunCommand, node: WorkflowNode, *, parallel_id: str | None) -> ChatEvent:
        payload = WorkflowNodeStartedPayload(
            workflow_run_id=str(command.run_id),
            node_id=node.id,
            node_type=WorkflowNodeType(node.kind.value),
            title=node.label,
            attempt=1,  # v1 无自动重试（attempt 结构留好：节点行 attempt 随状态持久）
            parallel_id=parallel_id,
            parent_parallel_id=None,  # dify 扁平组树：嵌套组 v1 不发树（40 篇 §4.2）
            started_at=_now_iso(),
            trace_id=command.trace_id,
        )
        return ChatEvent(
            name=ChatEventName.WORKFLOW_NODE_STARTED,
            data=payload.model_dump(mode="json"),
            run_id=command.run_id,
            trace_id=command.trace_id,
        )

    def _finished_event(
        self,
        command: WorkflowRunCommand,
        node: WorkflowNode,
        status: WorkflowNodeStatus,
        *,
        duration_ms: int = 0,
        error: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> ChatEvent:
        payload = WorkflowNodeFinishedPayload(
            workflow_run_id=str(command.run_id),
            node_id=node.id,
            attempt=1,
            status=status,
            duration_ms=duration_ms,
            error=error,
            usage=usage,
            trace_id=command.trace_id,
        )
        return ChatEvent(
            name=ChatEventName.WORKFLOW_NODE_FINISHED,
            data=payload.model_dump(mode="json"),
            run_id=command.run_id,
            trace_id=command.trace_id,
        )

    async def _persist_state(self, command: WorkflowRunCommand, state: dict[str, Any]) -> None:
        """执行态落账（JSONB 整体重赋值；resume 续跑依据）。失败留痕不阻断（审计不阻塞
        主流程，02 §3 ⑥）；波次边界单写者，并发节点协程零交叉写。"""
        try:
            async with self._uow.for_tenant(command.tenant_id) as tx:
                task = await tx.tasks.get(command.task_id)
                if task is None:
                    return
                payload = dict(task.payload or {})
                payload[STATE_KEY] = self._public_state(state)
                task.payload = payload
                await tx.tasks.save(task)
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow_state 落账失败（run=%s）: %s", command.run_id, exc)

    async def _run_alive(self, command: WorkflowRunCommand) -> bool:
        """波间取消检查：run 行非 running（abort 已终态化）即 False。查询失败按存活（可用性优先）。"""
        try:
            async with self._uow.for_tenant(command.tenant_id) as tx:
                task = await tx.tasks.get(command.task_id)
                run = next((r for r in (task.runs if task else []) if r.id == command.run_id), None)
                return run is not None and run.status is RunStatus.RUNNING
        except Exception:  # noqa: BLE001
            return True

    def _is_breakpoint(self, command: WorkflowRunCommand, state: dict[str, Any], node_id: str) -> bool:
        """断点判定：workflow_test 且节点在 breakpoints 集且本 run 未命中过（防 resume 死循环）。"""
        if command.task_type != KIND_WORKFLOW_TEST:
            return False
        return node_id in set(state.get("breakpoints") or ()) and node_id not in set(state.get("breakpoint_hit") or ())

    @staticmethod
    def _node_input(
        node: WorkflowNode, in_edges: list[WorkflowEdge], active: Any, state: dict[str, Any]
    ) -> dict[str, Any]:
        """节点输入=激活入边源节点出参合并（边序覆盖）；空入边=入口变量投影。"""
        outputs = state.get("outputs") or {}
        merged: dict[str, Any] = {}
        for edge in in_edges:
            if active(edge) and isinstance(outputs.get(edge.source), dict):
                merged.update(outputs[edge.source])
        return merged or dict(state.get("vars") or {})

    @staticmethod
    def _condition_ctx(state: dict[str, Any]) -> dict[str, Any]:
        """表达式/模板求值上下文：入口变量 + nodes 出参表（点路径解析面）。"""
        ctx = dict(state.get("vars") or {})
        ctx.setdefault("nodes", dict(state.get("outputs") or {}))
        return ctx

    @staticmethod
    def _state_of(payload: dict[str, Any]) -> dict[str, Any]:
        state_row = payload.get(STATE_KEY)
        base: dict[str, Any] = {
            "nodes": {},
            "outputs": {},
            "vars": payload.get("variables") or {},
            "breakpoints": payload.get("breakpoints") or [],
            "breakpoint_hit": [],
            "inactive_edges": [],
            "paused_node": None,
            "paused_kind": None,
            "params_override": {},
            "workflow_id": payload.get("workflow_id"),
        }
        if isinstance(state_row, dict):
            base.update({k: v for k, v in state_row.items() if k in base})
        for key in ("nodes", "outputs", "params_override"):
            if not isinstance(base.get(key), dict):
                base[key] = {}
        for key in ("breakpoint_hit", "inactive_edges"):
            if not isinstance(base.get(key), list):
                base[key] = []
        return base

    @staticmethod
    def _public_state(state: dict[str, Any]) -> dict[str, Any]:
        """落库投影：剔除调度期临时键（run_id 等不落 payload 的运行时上下文）。"""
        return {k: v for k, v in state.items() if k != "run_id"}

    @staticmethod
    def _record(
        state: dict[str, Any],
        node_id: str,
        status: str,
        *,
        attempt: int = 1,
        started_at: str | None = None,
        ended_at: str | None = None,
        parallel_id: str | None = None,
        title: str | None = None,
        error: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """节点行状态推进（payload.workflow_state.nodes；attempt 结构留好）。"""
        nodes = state.setdefault("nodes", {})
        row = nodes.get(node_id) if isinstance(nodes.get(node_id), dict) else {}
        row.update({"status": status, "attempt": attempt, "parallel_id": parallel_id, "title": title})
        if started_at is not None:
            row["started_at"] = started_at
        if ended_at is not None:
            row["ended_at"] = ended_at
        if error is not None:
            row["error"] = error
        if duration_ms is not None:
            row["duration_ms"] = duration_ms
        if usage:
            row["usage"] = usage
        nodes[node_id] = row


@dataclass(frozen=True, slots=True)
class _NodeSuccess:
    """波内节点执行结果：成功（output 进下游输入；usage 归 run 汇总）。"""

    output: dict[str, Any]
    usage: dict[str, Any]
    duration_ms: int


@dataclass(frozen=True, slots=True)
class _NodeFailure:
    """波内节点执行结果：失败（error 进 FINISHED.error 与 run.error）。"""

    error: dict[str, Any]
    duration_ms: int


@dataclass(frozen=True, slots=True)
class _NodeWaiting:
    """波内节点执行结果：审批暂停（锚点已落、run 已 waiting_tool）。"""


def _ctx_run_id(state: dict[str, Any]) -> uuid.UUID:
    """端口调用的 run 归属标识（审计/子 run 血统用）。"""
    raw = state.get("run_id")
    if isinstance(raw, uuid.UUID):
        return raw
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return uuid.uuid4()


# ── 生产端口装配（组合根调用；kb 检索经公开服务面——规则 3 许可面）──────────────


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class _AgentTurnPort:
    """agent 生产端口：ChatAdapter 单轮（27 篇 §3「继承群聊成员参数」v1=params.system_prompt）。"""

    def __init__(self, adapter: ChatAdapter) -> None:
        self._adapter = adapter

    async def agent_turn(
        self, node: WorkflowNode, message: str, ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        turn = ChatTurn(
            tenant_id=ctx.tenant_id,
            session_id=run_id,  # 占位标识（工作流无会话语义，不触发 L1 回写；_ChatSubRunRunner 同款）
            run_id=run_id,
            message=message,
            history=(),
            context_text="",
            system_prompt=_opt_str(node.params.get("system_prompt")),
        )
        parts: list[str] = []
        usage: dict[str, Any] = {}
        async for generated in self._adapter.stream_chat(turn, ctx):
            if generated.kind == "text_delta":
                parts.append(generated.delta)
            elif generated.kind == "finish":
                usage = dict(generated.usage)
        return NodeOutcome(output={"answer": "".join(parts)}, usage=usage)

    async def invoke_tool(
        self, node: WorkflowNode, args: dict[str, Any], ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        raise NodeExecutionError(5004, "tool 节点端口未装配（_AgentTurnPort 仅承载 agent）")

    async def retrieve(self, node: WorkflowNode, query: str, ctx: TenantContext, *, run_id: uuid.UUID) -> NodeOutcome:
        raise NodeExecutionError(5004, "retrieval 节点端口未装配（_AgentTurnPort 仅承载 agent）")


class _KbRetrievalPort:
    """retrieval 生产端口：KnowledgeSearchService 公开检索面（chat_context 同源消费）。"""

    def __init__(self, search_service: Any) -> None:
        self._search = search_service

    async def retrieve(self, node: WorkflowNode, query: str, ctx: TenantContext, *, run_id: uuid.UUID) -> NodeOutcome:
        result = await self._search.search(tenant_id=ctx.tenant_id, query=query, mode="local")
        return NodeOutcome(
            output={
                "chunks": [c.model_dump(mode="json") for c in getattr(result, "citations", [])],
                "degraded": bool(getattr(result, "degraded", False)),
            },
            usage={},
        )

    async def agent_turn(
        self, node: WorkflowNode, message: str, ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        raise NodeExecutionError(5004, "agent 节点端口未装配（_KbRetrievalPort 仅承载检索）")

    async def invoke_tool(
        self, node: WorkflowNode, args: dict[str, Any], ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        raise NodeExecutionError(5004, "tool 节点端口未装配（_KbRetrievalPort 仅承载检索）")


class _CompositePorts:
    """按节点 kind 分发的端口集合（agent/tool/retrieval 各归各端口；未装配 fail-closed）。"""

    def __init__(
        self,
        *,
        agent: NodePorts | None = None,
        tool: NodePorts | None = None,
        retrieval: NodePorts | None = None,
    ) -> None:
        self._agent = agent
        self._tool = tool
        self._retrieval = retrieval

    async def agent_turn(
        self, node: WorkflowNode, message: str, ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        if self._agent is None:
            raise NodeExecutionError(5004, "agent 节点端口未装配（组合根未接线，fail-closed）")
        return await self._agent.agent_turn(node, message, ctx, run_id=run_id)

    async def invoke_tool(
        self, node: WorkflowNode, args: dict[str, Any], ctx: TenantContext, *, run_id: uuid.UUID
    ) -> NodeOutcome:
        if self._tool is None:
            raise NodeExecutionError(5004, "tool 节点端口未装配（组合根未接线，fail-closed）")
        return await self._tool.invoke_tool(node, args, ctx, run_id=run_id)

    async def retrieve(self, node: WorkflowNode, query: str, ctx: TenantContext, *, run_id: uuid.UUID) -> NodeOutcome:
        if self._retrieval is None:
            raise NodeExecutionError(5004, "retrieval 节点端口未装配（组合根未接线，fail-closed）")
        return await self._retrieval.retrieve(node, query, ctx, run_id=run_id)


def build_default_node_ports(
    *,
    model_port: Any | None,
    session_factory: Any | None,
    settings: Any | None = None,
) -> NodePorts:
    """生产端口装配（gateway 组合根调用）：agent=builtin ChatAdapter 单轮；
    retrieval=kb 公开检索服务；tool=v1 未接线（fail-closed 5004——工具调用通道随
    MCP 桥批次接线）。model_port 未配置 → agent 端口同样 fail-closed。"""
    agent = None
    if model_port is not None:
        from services.agent.business.adapters.builtin import BuiltinAdapter

        agent = _AgentTurnPort(BuiltinAdapter(model_port))
    retrieval = None
    if session_factory is not None:
        from services.kb.business.search_service import KnowledgeSearchService

        retrieval = _KbRetrievalPort(
            KnowledgeSearchService(
                session_factory,
                ollama_base_url=(settings.ollama_base_url if settings is not None else ""),
            )
        )
    return _CompositePorts(agent=agent, tool=None, retrieval=retrieval)
